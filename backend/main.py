import logging
import secrets
import time
from threading import Lock
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from groq import GroqError
from pydantic import BaseModel, Field, ValidationError

from .ai_pipeline import (
    Command,
    deduplicate_sentences,
    generate_tts_base64,
    process_voice_command,
    run_ai_pipeline,
)
from .database import authenticate_shop, find_shop, register_shop, get_khata_ledger, get_recent_logs, init_db, read_inventory


logger = logging.getLogger("voxmetrics")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="VoxMetrics", version="2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


class PhoneInput(BaseModel):
    phone: str = Field(pattern=r"^[0-9]{10}$", min_length=10, max_length=10)


class LoginInput(PhoneInput):
    password: str = Field(min_length=1, max_length=256)


class Registration(LoginInput):
    otp: str = Field(pattern=r"^[0-9]{4}$")
    shop_name: str = Field(min_length=1, max_length=100)


otps = {}
sessions = {}
auth_lock = Lock()
PUBLIC_AUTH = {"/api/auth/send-otp", "/api/auth/login", "/api/auth/register"}


@app.middleware("http")
async def require_shop(request: Request, call_next):
    path = request.url.path.rstrip("/")
    if path.startswith("/api/") and path not in PUBLIC_AUTH and request.method != "OPTIONS":
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        with auth_lock:
            session = sessions.get(token) if scheme.lower() == "bearer" else None
            if session and session[2] <= time.monotonic():
                sessions.pop(token, None)
                session = None
        if not session:
            return JSONResponse({"detail": "Sign in to unlock the counter."}, status_code=401)
        request.state.shop_id, request.state.shop_name = session[:2]
        request.state.token = token
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def start_session(shop_id, shop_name):
    token = secrets.token_urlsafe(32)
    with auth_lock:
        sessions[token] = (shop_id, shop_name, time.monotonic() + 12 * 3600)
    return {"token": token, "shop_name": shop_name}


@app.post("/api/auth/send-otp")
def send_otp(body: PhoneInput):
    code = f"{secrets.randbelow(10000):04d}"
    with auth_lock:
        otps[body.phone] = (code, time.monotonic() + 300)
    return {"status": "ok", "otp_demo": code, "is_registered": find_shop(body.phone) is not None}


@app.post("/api/auth/register")
def register(body: Registration):
    name = body.shop_name.strip()
    if not name or not body.password.strip():
        raise HTTPException(400, "Enter a shop name and password.")
    with auth_lock:
        challenge = otps.get(body.phone)
        if not challenge or challenge[1] <= time.monotonic() or not secrets.compare_digest(challenge[0], body.otp):
            raise HTTPException(400, "Incorrect or expired OTP.")
        otps.pop(body.phone)
    try:
        shop_id = register_shop(body.phone, name, body.password)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This phone is already registered. Use Owner Login.") from None
    return start_session(shop_id, name)


@app.post("/api/auth/login")
def login(body: LoginInput):
    shop = authenticate_shop(body.phone, body.password)
    if not shop:
        raise HTTPException(401, "Incorrect phone or password.")
    return start_session(shop["id"], shop["shop_name"])


@app.post("/api/auth/logout")
def logout(request: Request):
    with auth_lock:
        sessions.pop(request.state.token, None)
    return {"status": "ok"}


def ledger_state(shop_id):
    return {"khata": get_khata_ledger(shop_id), "logs": get_recent_logs(shop_id=shop_id)}


@app.get("/api/inventory")
def get_inventory(request: Request, response: Response = None):
    shop_id = request.state.shop_id
    try:
        rows = read_inventory(shop_id)
    except sqlite3.Error:
        logger.exception("Could not read dashboard inventory")
        rows = []
        if response is not None:
            response.status_code = 503
    return {"inventory": rows, "shop_name": request.state.shop_name, **ledger_state(shop_id)}


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    if not hasattr(request.state, "shop_id"):
        return JSONResponse({"detail": "Check the phone number and required sign-in fields."}, status_code=422)
    return JSONResponse(status_code=422, content={
        "detail": jsonable_encoder(exc.errors()), **get_inventory(request),
        "intent": "INCOMPLETE_INPUT", "reply_local": "Please provide a text command or an audio recording.",
    })


@app.post("/api/process-command")
def process_command(command: Command, request: Request):
    try:
        result = run_ai_pipeline(command.transcript, request.state.shop_id)
        result["reply_local"] = deduplicate_sentences(result["reply_local"])
        audio_b64 = generate_tts_base64(result["reply_local"], result["detected_lang"])
        return {**result, "audio_base64": audio_b64, **ledger_state(request.state.shop_id)}
    except RuntimeError as exc:
        status, message = 503, str(exc)
    except (GroqError, ValidationError):
        status, message = 502, "Could not interpret the command. Please try again."
    except (ValueError, sqlite3.DatabaseError):
        status, message = 400, "Could not apply the command. Check the item, quantity and available stock."
    return JSONResponse(status_code=status, content=dict(
        intent="NOISE_ERROR", detected_lang="en", reply_local=message,
        chart_data=None, audio_base64="", **get_inventory(request),
    ))


@app.post("/api/voice-command")
async def voice_command(request: Request, audio: UploadFile = File(...)):
    """Accept an uploaded audio file, transcribe it with Groq Whisper,
    run the AI pipeline on the transcript, generate TTS, and return
    the full response payload."""
    try:
        audio_bytes = await audio.read()
        filename = audio.filename or "recording.wav"
        content_type = audio.content_type or ("audio/wav" if filename.endswith(".wav") else "audio/webm")
        return {**process_voice_command(audio_bytes, filename, content_type, request.state.shop_id), **ledger_state(request.state.shop_id)}
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=str(exc), chart_data=None, **get_inventory(request),
            audio_base64="",
        ))
    except (GroqError, ValidationError):
        logger.exception("Voice pipeline error")
        msg = "Could not interpret the voice command. Please try again."
        return JSONResponse(status_code=502, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, **get_inventory(request),
            audio_base64="",
        ))
    except (ValueError, sqlite3.DatabaseError):
        logger.exception("Voice pipeline DB error")
        msg = "Could not apply the command. Check the item, quantity and available stock."
        return JSONResponse(status_code=400, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, **get_inventory(request),
            audio_base64="",
        ))


@app.get("/")
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/khata")
def serve_khata():
    return FileResponse(FRONTEND_DIR / "khata.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
