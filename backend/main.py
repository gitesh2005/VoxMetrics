import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from groq import GroqError
from pydantic import ValidationError

from .ai_pipeline import (
    Command,
    deduplicate_sentences,
    generate_tts_base64,
    process_voice_command,
    run_ai_pipeline,
)
from .database import initialize_database, read_inventory


logger = logging.getLogger("voxmetrics")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/inventory")
def get_inventory():
    return read_inventory()


@app.post("/api/process-command")
def process_command(command: Command):
    try:
        result = run_ai_pipeline(command.transcript)
        result["reply_local"] = deduplicate_sentences(result["reply_local"])
        audio_b64 = generate_tts_base64(result["reply_local"], result["detected_lang"])
        return {**result, "audio_base64": audio_b64}
    except RuntimeError as exc:
        status, message = 503, str(exc)
    except (GroqError, ValidationError):
        status, message = 502, "Could not interpret the command. Please try again."
    except (ValueError, sqlite3.DatabaseError):
        status, message = 400, "Could not apply the command. Check the item, quantity and available stock."
    return JSONResponse(status_code=status, content=dict(
        intent="NOISE_ERROR", detected_lang="en", reply_local=message,
        chart_data=None, inventory=read_inventory(), audio_base64="",
    ))


@app.post("/api/voice-command")
async def voice_command(audio: UploadFile = File(...)):
    """Accept an uploaded audio file, transcribe it with Groq Whisper,
    run the AI pipeline on the transcript, generate TTS, and return
    the full response payload."""
    try:
        audio_bytes = await audio.read()
        filename = audio.filename or "recording.wav"
        content_type = audio.content_type or ("audio/wav" if filename.endswith(".wav") else "audio/webm")
        return process_voice_command(audio_bytes, filename, content_type)
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=str(exc), chart_data=None, inventory=read_inventory(),
            audio_base64="",
        ))
    except (GroqError, ValidationError):
        logger.exception("Voice pipeline error")
        msg = "Could not interpret the voice command. Please try again."
        return JSONResponse(status_code=502, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, inventory=read_inventory(),
            audio_base64="",
        ))
    except (ValueError, sqlite3.DatabaseError):
        logger.exception("Voice pipeline DB error")
        msg = "Could not apply the command. Check the item, quantity and available stock."
        return JSONResponse(status_code=400, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, inventory=read_inventory(),
            audio_base64="",
        ))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
