import base64
import io
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from groq import Groq, GroqError
from pydantic import BaseModel, Field, ValidationError

import database
from database import initialize_database, read_inventory

load_dotenv(Path(__file__).with_name(".env"))
api_key = os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key, timeout=30, max_retries=1) if api_key else None
logger = logging.getLogger("voxmetrics")


class Command(BaseModel):
    transcript: str = Field(max_length=4000)


class Decision(BaseModel):
    detected_lang: Literal["hi", "ta", "mr", "bn", "te", "en"] = "hi"
    intent: Literal["ADD_INVENTORY", "SELL_ITEM", "ANALYTICS_QUERY", "INCOMPLETE_INPUT", "NOISE_ERROR"]
    item_name: Optional[str] = None
    quantity: Optional[float] = None
    metric: Optional[str] = None
    sql_query: str = ""
    reply_local: str = ""


ALLOWED_ITEMS = {"curd", "sugar", "milk", "rice", "tea", "lentils", "oil", "salt"}
ITEM_ALIASES = {
    "curd": "curd", "mattha": "curd", "dahi": "curd", "thayir": "curd", "doi": "curd",
    "sugar": "sugar", "cheeni": "sugar", "sakkar": "sugar", "sakkarai": "sugar",
    "milk": "milk", "doodh": "milk", "dudh": "milk", "paal": "milk",
    "rice": "rice", "chawal": "rice", "arisi": "rice", "biyam": "rice",
    "tea": "tea", "chai": "tea", "tea patti": "tea", "chai patti": "tea",
    "lentils": "lentils", "dal": "lentils", "daal": "lentils", "pappu": "lentils",
    "oil": "oil", "tel": "oil", "nune": "oil",
    "salt": "salt", "namak": "salt", "uppu": "salt",
}

METRIC_SQL_MAP = {
    "highest_price": "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3",
    "lowest_price": "SELECT item_name AS label, price AS value FROM inventory ORDER BY value ASC LIMIT 3",
    "highest_profit": "SELECT item_name AS label, (price * units_sold) AS value FROM inventory ORDER BY value DESC LIMIT 3",
    "top_selling": "SELECT item_name AS label, units_sold AS value FROM inventory ORDER BY value DESC LIMIT 3",
    "lowest_stock": "SELECT item_name AS label, quantity AS value FROM inventory ORDER BY value ASC LIMIT 3",
    "all_inventory": "SELECT item_name AS label, quantity AS value FROM inventory ORDER BY value ASC",
}

SYSTEM_PROMPT = """You are an AI assistant for an Indian Kirana grocery store inventory system.
Analyze the user transcript and return ONLY a single JSON object with these exact keys:
{
  "detected_lang": "hi" | "ta" | "mr" | "bn" | "te" | "en",
  "intent": "ADD_INVENTORY" | "SELL_ITEM" | "ANALYTICS_QUERY" | "INCOMPLETE_INPUT" | "NOISE_ERROR",
  "item_name": "curd" | "sugar" | "milk" | "rice" | "tea" | "lentils" | "oil" | "salt" | null,
  "quantity": <float number or null>,
  "metric": "lowest_stock" | "highest_price" | "lowest_price" | "highest_profit" | "top_selling" | "all_inventory" | null,
  "sql_query": "<SQL string or empty>",
  "reply_local": "<conversational sentence in detected_lang>"
}

Strict Rules:
1. Canonical Item Names (map regional terms):
   - mattha / dahi / thayir / doi -> curd (packets)
   - cheeni / sakkarai / sakkar -> sugar (kg)
   - doodh / paal -> milk (L)
   - chawal / arisi -> rice (kg)
   - chai / tea patti -> tea (packets)
   - dal / lentils -> lentils (kg)
   - tel / oil -> oil (L)
   - namak / salt -> salt (kg)
   For any other item, set item_name = null.

2. Unit Conversions:
   Convert grams to kg (500 gram = 0.5) and ml to L (500 ml = 0.5). Never invent amounts.

3. Strict Quantity Rule for Stock Updates:
   If intent is ADD_INVENTORY or SELL_ITEM:
   - Extract the explicit numeric quantity mentioned in the transcript.
   - If quantity is null, missing, or <= 0: YOU MUST SET intent = "INCOMPLETE_INPUT", sql_query = "", and ask for the quantity in reply_local. NEVER GUESS 1.

4. Stock Update SQL:
   - ADD_INVENTORY: UPDATE inventory SET quantity = quantity + <qty> WHERE item_name = '<item>'
   - SELL_ITEM: UPDATE inventory SET quantity = quantity - <qty>, units_sold = units_sold + <qty> WHERE item_name = '<item>'
   Set metric = null for stock updates.

5. Analytics Query Metrics & SQL:
   - "mehnga" / "most expensive" / "costly" / "highest price" / "rate" -> metric = "highest_price",
     sql_query = "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "sasta" / "cheapest" / "lowest price" -> metric = "lowest_price",
     sql_query = "SELECT item_name AS label, price AS value FROM inventory ORDER BY value ASC LIMIT 3;"
   - "profit" / "kamai" / "revenue" -> metric = "highest_profit",
     sql_query = "SELECT item_name AS label, (price * units_sold) AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "sabse zyada bika" / "best selling" / "top sold" -> metric = "top_selling",
     sql_query = "SELECT item_name AS label, units_sold AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "kam bacha hai" / "low stock" / "stock khatam" -> metric = "lowest_stock",
     sql_query = "SELECT item_name AS label, quantity AS value FROM inventory ORDER BY value ASC LIMIT 3;"
   Set item_name = null and quantity = null for analytics queries.

6. Incomplete / Noise:
   - If audio/transcript is noise or unintelligible: intent = "NOISE_ERROR", sql_query = "".
   - If quantity or item is missing on an update: intent = "INCOMPLETE_INPUT", sql_query = "".

7. Conciseness & Non-Repetition:
   Write at most 1-2 short, direct sentences in reply_local. Never repeat the same item, number, or phrase twice."""


class ExecutionResult:
    def __init__(self, chart=None, grounded_reply=None, error_message=None):
        self.chart = chart
        self.grounded_reply = grounded_reply
        self.error_message = error_message


def execute_decision(decision: Decision) -> ExecutionResult:
    sql = decision.sql_query.strip().removesuffix(";").strip()
    if re.search(r"\b(DROP|DELETE|ALTER|INSERT|TRUNCATE)\b", decision.sql_query, re.I):
        return ExecutionResult(error_message="Forbidden SQL command.")

    if decision.intent in {"INCOMPLETE_INPUT", "NOISE_ERROR"}:
        return ExecutionResult(grounded_reply=decision.reply_local)

    # ---------------------------------------------------------
    # Layer 3: Python Whitelist & Deterministic Grounding (UPDATE)
    # ---------------------------------------------------------
    if decision.intent in {"ADD_INVENTORY", "SELL_ITEM"}:
        raw_item = (decision.item_name or "").lower().strip()
        canonical_item = ITEM_ALIASES.get(raw_item)

        # Item whitelist validation
        if not canonical_item or canonical_item not in ALLOWED_ITEMS:
            not_found = (
                f"Item '{decision.item_name or 'item'}' inventory mein uplabdh nahi hai. Kripya sahi grocery item batayein."
                if decision.detected_lang == "hi"
                else f"Item '{decision.item_name or 'item'}' is not in the inventory."
            )
            return ExecutionResult(error_message=not_found)

        # Quantity validation
        if decision.quantity is None or decision.quantity <= 0:
            qty_missing = (
                f"{canonical_item.capitalize()} ki quantity batayein, kitna amount {'jodna' if decision.intent == 'ADD_INVENTORY' else 'bechna'} hai?"
                if decision.detected_lang == "hi"
                else f"Please specify a valid quantity for {canonical_item.capitalize()}."
            )
            return ExecutionResult(error_message=qty_missing)

        qty = float(decision.quantity)

        with closing(sqlite3.connect(database.DB_PATH)) as db, db:
            # 1. Read exact before_qty and unit directly from SQLite
            row = db.execute(
                "SELECT quantity, unit FROM inventory WHERE item_name = ?", (canonical_item,)
            ).fetchone()
            if not row:
                return ExecutionResult(error_message=f"Item '{canonical_item}' not found in database.")

            before_qty, unit = float(row[0]), str(row[1])

            # Check stock availability for sales
            if decision.intent == "SELL_ITEM":
                if before_qty < qty:
                    insufficient = (
                        f"Paryapt stock nahi hai. {canonical_item.capitalize()} ka sirf {_fmt_value(before_qty)} {unit} bacha hai."
                        if decision.detected_lang == "hi"
                        else f"Insufficient stock. Only {_fmt_value(before_qty)} {unit} of {canonical_item.capitalize()} available."
                    )
                    return ExecutionResult(error_message=insufficient)

                db.execute(
                    "UPDATE inventory SET quantity = quantity - ?, units_sold = units_sold + ? WHERE item_name = ?",
                    (qty, qty, canonical_item),
                )
            else:
                db.execute(
                    "UPDATE inventory SET quantity = quantity + ? WHERE item_name = ?",
                    (qty, canonical_item),
                )

            # 2. Read exact after_qty directly from SQLite
            after_row = db.execute(
                "SELECT quantity FROM inventory WHERE item_name = ?", (canonical_item,)
            ).fetchone()
            after_qty = float(after_row[0])

        # 3. Construct deterministic grounded fact string
        b_str, a_str, q_str = _fmt_value(before_qty), _fmt_value(after_qty), _fmt_value(qty)
        item_cap = canonical_item.capitalize()
        is_add = decision.intent == "ADD_INVENTORY"

        if decision.detected_lang == "hi":
            if is_add:
                grounded = f"{item_cap} stock update hua: {b_str} {unit} -> {a_str} {unit} (+{q_str} {unit} joda gaya)."
            else:
                grounded = f"{item_cap} bik gaya: {b_str} {unit} -> {a_str} {unit} ({q_str} {unit} kam hua)."
        elif decision.detected_lang == "ta":
            grounded = f"{item_cap} irupu matram: {b_str} {unit} -> {a_str} {unit}."
        else:
            action = "added" if is_add else "sold"
            sign = "+" if is_add else "-"
            grounded = f"Updated {item_cap}: {b_str} {unit} -> {a_str} {unit} ({sign}{q_str} {unit} {action})."

        return ExecutionResult(grounded_reply=grounded)

    # ---------------------------------------------------------
    # Layer 4: Deterministic Grounding (ANALYTICS_QUERY)
    # ---------------------------------------------------------
    with closing(sqlite3.connect(database.DB_PATH)) as db, db:
        if not re.match(r"^SELECT\s", sql, re.I):
            # Fall back to metric map if LLM SQL was malformed
            sql = METRIC_SQL_MAP.get(decision.metric, "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3")

        columns = {"item_name", "quantity", "unit", "price", "units_sold", ""}
        reads_inventory = False

        def authorize(action, table, column, schema, source):
            nonlocal reads_inventory
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and table == "inventory" and column in columns:
                reads_inventory = True
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and column in {"sum", "avg", "min", "max", "count", "round"}:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        db.set_authorizer(authorize)
        db.set_progress_handler(lambda: 1, 100_000)
        cursor = db.execute(sql)
        if not reads_inventory or len(cursor.description) != 2:
            raise ValueError("Expected label and value")
        raw_rows = cursor.fetchall()

        # Fetch units for every returned item_name so reply_local can include them
        db.set_authorizer(None)
        unit_map = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT item_name, unit FROM inventory"
            ).fetchall()
        }

        chart = []
        for label, value in raw_rows:
            if not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError("Expected a numeric chart value")
            chart.append({
                "label": str(label),
                "value": value,
                "unit": unit_map.get(str(label), ""),
            })

    # Generate deterministic, verified reply using ONLY exact database numbers
    analytics_reply = format_analytics_reply(
        sql_query=sql,
        chart=chart,
        detected_lang=decision.detected_lang,
        llm_intro=decision.reply_local,
        metric=decision.metric,
    )
    return ExecutionResult(chart=chart, grounded_reply=analytics_reply)


LANG_MAP = {"hi": "hi", "ta": "ta", "mr": "mr", "bn": "bn", "te": "te", "en": "en"}


# ---------------------------------------------------------------------------
# Analytics reply builder
# Converts executed SQL rows → a fully spoken sentence so reply_local is
# never left as a bare intro like "Here are the items:".
# ---------------------------------------------------------------------------

def _fmt_value(value: float) -> str:
    """Return a clean number string: integer when whole, else 2 decimal places."""
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def _item_phrase(label: str, value: float, unit: str) -> str:
    """Format one item as 'Tea (180 packets)' or 'Oil (₹130)'."""
    v = _fmt_value(value)
    if unit:
        return f"{label.capitalize()} ({v} {unit})"
    return f"{label.capitalize()} ({v})"


def _item_phrase_rupees(label: str, value: float) -> str:
    return f"{label.capitalize()} (₹{_fmt_value(value)})"


def format_analytics_reply(
    sql_query: str,
    chart: list[dict],
    detected_lang: str,
    llm_intro: str = "",
    metric: Optional[str] = None,
) -> str:
    """
    Build a complete spoken reply from actual DB rows.

    Strategy:
      1. Infer the query type from metric or SQL text.
      2. Format each row with real value + unit.
      3. Compose a 1–2 sentence reply in detected_lang with exact numbers.
    """
    if not chart:
        return "Inventory mein data uplabdh nahi hai." if detected_lang == "hi" else "No data available in inventory."

    sql_lower = sql_query.lower()

    # Detect metric from parameter or SQL
    is_price = metric in {"highest_price", "lowest_price"} or ("price" in sql_lower and "units_sold" not in sql_lower)
    is_profit = metric == "highest_profit" or ("price" in sql_lower and "units_sold" in sql_lower)
    is_units_sold = metric == "top_selling" or ("units_sold" in sql_lower and "price" not in sql_lower)
    is_quantity = metric in {"lowest_stock", "all_inventory"} or ("quantity" in sql_lower and "price" not in sql_lower)

    asc_order = "asc" in sql_lower or metric in {"lowest_stock", "lowest_price"}

    # Build item list string
    if is_price or is_profit:
        parts = [_item_phrase_rupees(r["label"], r["value"]) for r in chart]
    else:
        parts = [_item_phrase(r["label"], r["value"], r.get("unit", "")) for r in chart]

    items_str = ", ".join(parts)
    lang = detected_lang

    # --- Hindi / Hinglish ---
    if lang == "hi":
        if is_price and not asc_order:
            return f"Sabse mehenga item: {items_str}."
        if is_price and asc_order:
            return f"Sabse sasta item: {items_str}."
        if is_profit:
            return f"Sabse zyada kamai in items se hui: {items_str}."
        if is_units_sold:
            return f"Sabse zyada bika hua item: {items_str}."
        if is_quantity and asc_order:
            return f"Sabse kam stock in items ka hai: {items_str}."
        if is_quantity and not asc_order:
            return f"Sabse zyada stock in items ka hai: {items_str}."

    # --- Tamil ---
    if lang == "ta":
        if is_price and not asc_order:
            return f"Miga vilai ulla item: {items_str}."
        if is_price and asc_order:
            return f"Miga kuraiya vilai ulla item: {items_str}."
        if is_profit:
            return f"Atiga labam thantha item: {items_str}."
        if is_units_sold:
            return f"Atiga vikkiram aana item: {items_str}."
        if is_quantity:
            intro = "Kuraiya irupu" if asc_order else "Atiga irupu"
            return f"{intro}: {items_str}."

    # --- English (default for en / mr / bn / te) ---
    if is_price and not asc_order:
        return f"Most expensive items: {items_str}."
    if is_price and asc_order:
        return f"Cheapest items: {items_str}."
    if is_profit:
        return f"Highest profit items: {items_str}."
    if is_units_sold:
        return f"Best-selling items: {items_str}."
    if is_quantity and asc_order:
        return f"Lowest stock items: {items_str}."
    if is_quantity and not asc_order:
        return f"Highest stock items: {items_str}."

    # Generic fallback: strip trailing colon from LLM intro and append rows
    intro = (llm_intro or "Items").rstrip(": \n")
    return f"{intro}: {items_str}."


def deduplicate_sentences(text: str) -> str:
    """Removes duplicate consecutive or repeated sentences split on . / । / ! / ?
    while preserving order and punctuation."""
    if not text:
        return ""
    tokens = re.split(r"([.।!?]+)", text)
    seen = set()
    result_parts = []
    i = 0
    while i < len(tokens):
        sentence = tokens[i].strip()
        punct = tokens[i + 1].strip() if i + 1 < len(tokens) else "."
        i += 2
        if not sentence:
            continue
        norm = re.sub(r"[^\w\s]", "", sentence.lower()).strip()
        if not norm:
            continue
        if norm not in seen:
            seen.add(norm)
            result_parts.append(f"{sentence}{punct}")
    if not result_parts:
        return text.strip()
    return " ".join(result_parts).strip()


def clean_transcript(text: str) -> str:
    """Collapses repeated consecutive words or phrases caused by trailing silence
    or Whisper repetition loops (e.g. 'add karo add karo' -> 'add karo')."""
    if not text:
        return ""
    cleaned = text.strip()
    # Iterate passes to catch multi-level or nested consecutive repetitions
    for _ in range(5):
        words = cleaned.split()
        if not words:
            break
        n = len(words)
        modified = False
        # Test phrase lengths from up to 8 words down to 1 word
        for k in range(min(8, n // 2), 0, -1):
            i = 0
            new_words = []
            while i < len(words):
                chunk = words[i : i + k]
                if len(chunk) == k:
                    repeats = 1
                    while i + (repeats + 1) * k <= len(words):
                        next_chunk = words[i + repeats * k : i + (repeats + 1) * k]
                        chunk_norm = [re.sub(r"[^\w]", "", w.lower()) for w in chunk]
                        next_norm = [re.sub(r"[^\w]", "", w.lower()) for w in next_chunk]
                        if chunk_norm == next_norm and any(chunk_norm):
                            repeats += 1
                        else:
                            break
                    if repeats > 1:
                        new_words.extend(chunk)
                        i += repeats * k
                        modified = True
                        continue
                new_words.append(words[i])
                i += 1
            words = new_words
            if modified:
                break
        cleaned = " ".join(words)
        if not modified:
            break
    return re.sub(r"\s+", " ", cleaned).strip()


def generate_tts_base64(text: str, lang: str) -> str:
    """Generate an MP3 of *text* via gTTS and return its base64 string.

    Falls back through lang → 'hi' → 'en' if the requested language code
    is rejected, and returns an empty string if TTS fails entirely so that
    the calling endpoint never crashes.
    """
    from gtts import gTTS

    fallback_chain = []
    mapped = LANG_MAP.get(lang, lang)
    if mapped:
        fallback_chain.append(mapped)
    if "hi" not in fallback_chain:
        fallback_chain.append("hi")
    if "en" not in fallback_chain:
        fallback_chain.append("en")

    for try_lang in fallback_chain:
        try:
            tts = gTTS(text=text, lang=try_lang)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("ascii")
        except Exception:
            logger.warning("gTTS failed for lang=%s, trying next fallback", try_lang)
    return ""


CHAT_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]


def call_groq_chat(messages):
    last_err = None
    for model_name in CHAT_MODELS:
        try:
            return client.chat.completions.create(
                model=model_name,
                response_format={"type": "json_object"},
                temperature=0.0,
                frequency_penalty=0.6,
                presence_penalty=0.3,
                messages=messages,
            )
        except GroqError as e:
            err_str = str(e).lower()
            if hasattr(e, "status_code") and e.status_code == 404:
                logger.warning("Model %s returned 404, falling back", model_name)
                last_err = e
                continue
            if "model_not_found" in err_str or "does not exist" in err_str:
                logger.warning("Model %s not found, falling back", model_name)
                last_err = e
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("No Groq chat model available.")


def run_ai_pipeline(transcript: str):
    if not client:
        raise RuntimeError("Configure GROQ_API_KEY to process commands.")

    completion = call_groq_chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"inventory": read_inventory(), "transcript": transcript})},
    ])
    decision = Decision.model_validate_json(completion.choices[0].message.content or "")

    # Layer 2 Guardrail: enforce quantity requirement on stock updates
    if decision.intent in {"ADD_INVENTORY", "SELL_ITEM"}:
        if decision.quantity is None or decision.quantity <= 0:
            decision.intent = "INCOMPLETE_INPUT"
            decision.sql_query = ""
            canonical = ITEM_ALIASES.get((decision.item_name or "").lower().strip())
            disp = (canonical or decision.item_name or "item").capitalize()
            action_word = "jodna (add)" if decision.intent == "ADD_INVENTORY" else "bechna (sell)"
            if decision.detected_lang == "hi":
                decision.reply_local = f"{disp} ki quantity missing hai. Kitna amount {action_word} karna hai?"
            elif decision.detected_lang == "ta":
                decision.reply_local = f"{disp} alavu enna?"
            else:
                decision.reply_local = f"Please specify the quantity to update for {disp}."

    exec_res = execute_decision(decision)

    if exec_res.error_message:
        intent = "INCOMPLETE_INPUT" if ("nahi hai" in exec_res.error_message or "missing" in exec_res.error_message or "stock" in exec_res.error_message.lower()) else "NOISE_ERROR"
        return dict(
            intent=intent,
            detected_lang=decision.detected_lang,
            reply_local=deduplicate_sentences(exec_res.error_message),
            chart_data=None,
            inventory=read_inventory(),
        )

    reply = exec_res.grounded_reply or decision.reply_local
    reply = deduplicate_sentences(reply)
    chart_clean = [{"label": r["label"], "value": r["value"]} for r in exec_res.chart] if exec_res.chart else None

    return dict(
        intent=decision.intent,
        detected_lang=decision.detected_lang,
        reply_local=reply,
        chart_data=chart_clean,
        inventory=read_inventory(),
    )


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
    if not client:
        return JSONResponse(status_code=503, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local="Configure GROQ_API_KEY to process voice commands.",
            chart_data=None, inventory=read_inventory(), audio_base64="",
        ))
    try:
        audio_bytes = await audio.read()
        filename = audio.filename or "recording.wav"
        content_type = audio.content_type or ("audio/wav" if filename.endswith(".wav") else "audio/webm")

        # ---------------------------------------------------------
        # Layer 1: Whisper Confidence Gate & Deduplication
        # ---------------------------------------------------------
        whisper_kwargs = dict(
            model="whisper-large-v3",
            file=(filename, audio_bytes, content_type),
            response_format="verbose_json",
            temperature=0.0,
            prompt=(
                "Hindi, Hinglish, and English Kirana store voice command: "
                "konsa item sabse mehnga hai, sabse sasta saman, "
                "10 packet mattha add karo, 5 kilo cheeni, 2 liter doodh bik gaya, "
                "chawal, dal, tel, namak, chai patti."
            ),
        )

        try:
            transcription = client.audio.transcriptions.create(
                condition_on_previous_text=False,
                **whisper_kwargs,
            )
        except (TypeError, GroqError):
            transcription = client.audio.transcriptions.create(
                **whisper_kwargs,
            )

        if isinstance(transcription, dict):
            raw_transcript = transcription.get("text", "").strip()
            segments = transcription.get("segments", []) or []
        else:
            raw_transcript = getattr(transcription, "text", "").strip()
            segments = getattr(transcription, "segments", []) or []

        # Deduplicate repeated loops in Whisper transcript
        transcript = clean_transcript(raw_transcript)

        no_speech_probs = []
        for s in segments:
            p = s.get("no_speech_prob") if isinstance(s, dict) else getattr(s, "no_speech_prob", None)
            if p is not None:
                no_speech_probs.append(float(p))

        clean_text = re.sub(r"[^\w\s]", "", transcript.lower()).strip()
        silence_hallucinations = {
            "", "you", "thank you", "thank you so much", "thank you very much",
            "thanks", "thanks for watching", "thank you for watching",
            "bye", "bye bye", "goodbye", "subscribe", "please subscribe",
            "watching", "the end",
        }

        # Check no_speech_prob > 0.65
        has_high_no_speech = False
        if no_speech_probs:
            avg_no_speech = sum(no_speech_probs) / len(no_speech_probs)
            if avg_no_speech > 0.65 or (len(no_speech_probs) == 1 and no_speech_probs[0] > 0.65):
                has_high_no_speech = True

        if not transcript or len(transcript) < 2 or clean_text in silence_hallucinations or has_high_no_speech:
            msg = "Awaaz clear nahi aayi, kripya mic ke paas bolkar dubara try karein."
            return dict(
                transcript=transcript if transcript else "",
                intent="NOISE_ERROR",
                detected_lang="hi",
                reply_local=msg,
                chart_data=None,
                inventory=read_inventory(),
                audio_base64=generate_tts_base64(msg, "hi"),
            )

        result = run_ai_pipeline(transcript)
        result["reply_local"] = deduplicate_sentences(result["reply_local"])
        audio_b64 = generate_tts_base64(result["reply_local"], result["detected_lang"])
        return {**result, "transcript": transcript, "audio_base64": audio_b64}

    except (GroqError, ValidationError) as exc:
        logger.exception("Voice pipeline error")
        msg = "Could not interpret the voice command. Please try again."
        return JSONResponse(status_code=502, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, inventory=read_inventory(),
            audio_base64="",
        ))
    except (ValueError, sqlite3.DatabaseError) as exc:
        logger.exception("Voice pipeline DB error")
        msg = "Could not apply the command. Check the item, quantity and available stock."
        return JSONResponse(status_code=400, content=dict(
            transcript="", intent="NOISE_ERROR", detected_lang="en",
            reply_local=msg, chart_data=None, inventory=read_inventory(),
            audio_base64="",
        ))


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True))
