import base64
import io
import json
import logging
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from groq import Groq, GroqError
from pydantic import BaseModel, Field, field_validator

from .database import UnknownInventoryItem, execute_analytics_query, read_inventory, update_inventory_batch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
api_key = os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key, timeout=30, max_retries=1) if api_key else None
logger = logging.getLogger("voxmetrics")


class Command(BaseModel):
    transcript: str = Field(max_length=4000)


class CommandItem(BaseModel):
    action: Optional[Literal["ADD", "SELL", "UPDATE_PRICE"]] = None
    item_name: Optional[str] = None
    quantity: Optional[float] = Field(default=None, strict=True)
    new_price: Optional[float] = Field(default=None, strict=True)
    price_words: Optional[str] = None

    @field_validator("quantity", "new_price", mode="before")
    @classmethod
    def numeric_value(cls, value):
        return value if type(value) in (int, float) else None


class Decision(BaseModel):
    detected_lang: Literal["hi", "ta", "mr", "bn", "te", "en"] = "hi"
    intent: Literal["INVENTORY_UPDATE", "ADD_INVENTORY", "SELL_ITEM", "ANALYTICS_QUERY", "INCOMPLETE_INPUT", "NOISE_ERROR"]
    items: list[CommandItem] = Field(default_factory=list)
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
  "intent": "INVENTORY_UPDATE" | "ANALYTICS_QUERY" | "INCOMPLETE_INPUT" | "NOISE_ERROR",
  "items": [{"action": "ADD" | "SELL" | "UPDATE_PRICE", "item_name": "curd" | "sugar" | "milk" | "rice" | "tea" | "lentils" | "oil" | "salt" | null, "quantity": <stock amount or null>, "new_price": <explicit new selling price or null>, "price_words": <verbatim number words if spoken as words, else null>}],
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
   Use intent INVENTORY_UPDATE for single-item, multi-item, and mixed stock updates:
   - Include one entry per mentioned item in spoken order, even for a single item.
     Handle comma-separated clauses, conjunctions, shared verbs, and mixed-language names.
     Assign ADD to each item being stocked and SELL to each item being sold or deducted,
     according to the verb governing that item. Preserve repeated items as separate actions.
     For "10 packet dahi add karo aur 2 kilo cheeni bik gayi", return items
     [{"action":"ADD","item_name":"curd","quantity":10},{"action":"SELL","item_name":"sugar","quantity":2}].
   - Each item's quantity must come from the transcript. Never use an inventory value, guess 1,
     borrow another item's quantity, or silently omit an incomplete or unknown item.
   - If items is empty or ANY ADD/SELL quantity is missing or <= 0, return INCOMPLETE_INPUT and ask for
     the missing quantity in reply_local. If any item is unknown, ask for the item.
   - If an item's action is unclear, return INCOMPLETE_INPUT and ask whether to add or sell it.

4. For stock updates, set sql_query = "" and metric = null.
   For a request to set an item's selling price, use INVENTORY_UPDATE with action UPDATE_PRICE,
   quantity null, and new_price in rupees per inventory unit. Recognize price, rate, daam, bhav,
   keemat, rupaye and regional equivalents. "cheeni ka price 50 rupaye kar do" sets sugar to 50,
   "doodh ka rate 68 kar do" sets milk to 68, and "set rice price to 65" sets rice to 65.
   If the new price is given in words, copy just those number words into price_words.
   Never infer a price from inventory, a stock quantity, another item, a budget or a prior price.
   If the requested new price is absent, ambiguous, or not positive, return INCOMPLETE_INPUT
   and ask for the exact selling price. Keep all clauses together, including stock changes.
   Questions about existing prices or prices matching a condition are read-only ANALYTICS_QUERY.
   For "doodh ka rate kya hai", select the price for milk.

5. Analytics Query Metrics & SQL:
   - "mehnga" / "most expensive" / "costly" / "highest price" -> metric = "highest_price",
     sql_query = "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "sasta" / "cheapest" / "lowest price" -> metric = "lowest_price",
     sql_query = "SELECT item_name AS label, price AS value FROM inventory ORDER BY value ASC LIMIT 3;"
   - "profit" / "kamai" / "revenue" -> metric = "highest_profit",
     sql_query = "SELECT item_name AS label, (price * units_sold) AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "sabse zyada bika" / "best selling" / "top sold" -> metric = "top_selling",
     sql_query = "SELECT item_name AS label, units_sold AS value FROM inventory ORDER BY value DESC LIMIT 3;"
   - "kam bacha hai" / "low stock" / "stock khatam" -> metric = "lowest_stock",
     sql_query = "SELECT item_name AS label, quantity AS value FROM inventory ORDER BY value ASC LIMIT 3;"
   Set items = [] for analytics queries.

6. Incomplete / Noise:
   - If audio/transcript is noise or unintelligible: intent = "NOISE_ERROR", sql_query = "".
   - If the item or the value required for its action is missing: intent = "INCOMPLETE_INPUT", sql_query = "".

7. Conciseness & Non-Repetition:
   Write at most 1-2 short, direct sentences in reply_local. Never repeat the same item, number, or phrase twice."""

LANG_MAP = {"hi": "hi", "ta": "ta", "mr": "mr", "bn": "bn", "te": "te", "en": "en"}

SILENCE_HALLUCINATIONS = {
    "", "you", "thank you", "thank you so much", "thank you very much",
    "thanks", "thanks for watching", "thank you for watching",
    "bye", "bye bye", "goodbye", "subscribe", "please subscribe",
    "watching", "the end",
}

CHAT_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]


class ExecutionResult:
    def __init__(self, chart=None, grounded_reply=None, error_message=None):
        self.chart = chart
        self.grounded_reply = grounded_reply
        self.error_message = error_message


def _price_stated(item: CommandItem, transcript: str) -> bool:
    numbers = re.findall(r"[+-]?(?:\d+(?:,\d{2,3})*(?:\.\d+)?|\.\d+)", transcript.replace("−", "-"))
    if any(Decimal(number.replace(",", "")) == Decimal(str(item.new_price)) for number in numbers):
        return True
    words = (item.price_words or "").strip().casefold()
    return bool(words and not any(c.isdigit() for c in words)
                and re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", transcript.casefold()))


def execute_decision(decision: Decision, transcript: Optional[str] = None) -> ExecutionResult:
    sql = decision.sql_query.strip().removesuffix(";").strip()
    is_update = decision.intent in {"INVENTORY_UPDATE", "ADD_INVENTORY", "SELL_ITEM"}
    if is_update:
        for item in decision.items:
            if item.action == "UPDATE_PRICE" and (
                item.new_price is None or not math.isfinite(item.new_price) or item.new_price <= 0
                or (transcript is not None and not _price_stated(item, transcript))
            ):
                decision.intent = "INCOMPLETE_INPUT"
                prompts = {
                    "hi": "Naya bikri ka daam rupaye mein batayein",
                    "ta": "Pudhiya virpanai vilaiyai roobaayil sollungal",
                    "mr": "Navin vikri kimmat rupayant sanga",
                    "bn": "Notun bikrir daam takay bolun",
                    "te": "Kotta ammaka dhara rupayallo cheppandi",
                    "en": "Please specify the exact new selling price in rupees",
                }
                return ExecutionResult(grounded_reply=prompts[decision.detected_lang] + ".")
        invalid = [
            item for item in decision.items
            if item.action != "UPDATE_PRICE"
            if item.quantity is None or not math.isfinite(item.quantity) or item.quantity <= 0
        ]
        if not decision.items or invalid:
            decision.intent = "INCOMPLETE_INPUT"
            decision.sql_query = ""
            prompts = {
                "hi": "Kripya sahi quantity batayein",
                "ta": "Sariyana alavai sollungal",
                "mr": "Krupaya yogya praman sanga",
                "bn": "Shothik poriman bolun",
                "te": "Dayachesi saraina parimanam cheppandi",
                "en": "Please specify a positive quantity",
            }
            names = ", ".join(item.item_name.capitalize() for item in invalid if item.item_name)
            prompt = prompts[decision.detected_lang]
            return ExecutionResult(grounded_reply=f"{prompt} ({names})." if names else f"{prompt}.")

    if re.search(r"\b(DROP|DELETE|ALTER|INSERT|TRUNCATE)\b", decision.sql_query, re.I):
        return ExecutionResult(error_message="Forbidden SQL command.")

    if decision.intent in {"INCOMPLETE_INPUT", "NOISE_ERROR"}:
        return ExecutionResult(grounded_reply=decision.reply_local)

    # ---------------------------------------------------------
    # Layer 3: Python Whitelist & Deterministic Grounding (UPDATE)
    # ---------------------------------------------------------
    if is_update:
        try:
            items = []
            legacy_action = {"ADD_INVENTORY": "ADD", "SELL_ITEM": "SELL"}.get(decision.intent)
            for item in decision.items:
                name = ITEM_ALIASES.get((item.item_name or "").strip().lower())
                if name not in ALLOWED_ITEMS:
                    raise UnknownInventoryItem(item.item_name or "item")
                action = item.action or legacy_action
                if action is None:
                    decision.intent = "INCOMPLETE_INPUT"
                    prompts = {
                        "hi": "Jodna hai ya bechna hai",
                        "ta": "Serkkava allathu virkava",
                        "mr": "Jodayche ki vikayche",
                        "bn": "Jog korbo naki bikri korbo",
                        "te": "Cherchala leka ammala",
                        "en": "Add or sell",
                    }
                    return ExecutionResult(grounded_reply=f"{prompts[decision.detected_lang]} ({name.capitalize()})?")
                items.append((name, item.new_price if action == "UPDATE_PRICE" else item.quantity, action))
            changes = update_inventory_batch(items)
        except UnknownInventoryItem as exc:
            decision.intent = "INCOMPLETE_INPUT"
            reply = (
                f"Item '{exc}' inventory mein uplabdh nahi hai. Kripya sahi grocery item batayein."
                if decision.detected_lang == "hi"
                else f"Item '{exc}' is not in the inventory. Please specify an inventory item."
            )
            return ExecutionResult(grounded_reply=reply)

        introductions = {
            "hi": "Stock update ho gaya",
            "ta": "Irupu pudhuppikkappattadhu",
            "mr": "Satha adyatan zala",
            "bn": "Mojut update hoyeche",
            "te": "Nilva navikarinchabadindi",
            "en": "Stock updated",
        }
        price_labels = {
            "hi": "ka daam", "ta": "vilai", "mr": "chi kimmat", "bn": "er daam",
            "te": "dhara", "en": "price",
        }
        parts = []
        for (name, before, after, unit), (_, _, action) in zip(changes, items):
            if action == "UPDATE_PRICE":
                old = format(Decimal(str(before)).normalize(), "f")
                new = format(Decimal(str(after)).normalize(), "f")
                per_unit = "packet" if unit == "packets" else unit
                parts.append(f"{name.capitalize()} {price_labels[decision.detected_lang]} (₹{old} → ₹{new}/{per_unit})")
                continue
            delta = Decimal(str(after)) - Decimal(str(before))
            amount = format(delta.normalize(), "+f") if delta else "0"
            parts.append(f"{name.capitalize()} ({amount} {unit})")
        if any(action == "UPDATE_PRICE" for _, _, action in items):
            introductions = {
                "hi": "Update ho gaya", "ta": "Pudhuppikkappattadhu", "mr": "Badal kela",
                "bn": "Bodol hoyeche", "te": "Mārpu chēsāmu", "en": "Updated",
            }
        return ExecutionResult(grounded_reply=f"{introductions[decision.detected_lang]}: {', '.join(parts)}.")

    # ---------------------------------------------------------
    # Layer 4: Deterministic Grounding (ANALYTICS_QUERY)
    # ---------------------------------------------------------
    if not re.match(r"^SELECT\s", sql, re.I):
        # Fall back to metric map if LLM SQL was malformed
        sql = METRIC_SQL_MAP.get(decision.metric, "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3")

    chart = execute_analytics_query(sql)

    # Generate deterministic, verified reply using ONLY exact database numbers
    analytics_reply = format_analytics_reply(
        sql_query=sql,
        chart=chart,
        detected_lang=decision.detected_lang,
        llm_intro=decision.reply_local,
        metric=decision.metric,
    )
    return ExecutionResult(chart=chart, grounded_reply=analytics_reply)


# ---------------------------------------------------------------------------
# Analytics reply builder
# Converts executed SQL rows -> a fully spoken sentence so reply_local is
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
      3. Compose a 1-2 sentence reply in detected_lang with exact numbers.
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
    tokens = re.split(r"((?:(?<!\d)\.|\.(?!\d)|[।!?])+)", text)
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

    Falls back through lang -> 'hi' -> 'en' if the requested language code
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

    exec_res = execute_decision(decision, transcript)

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


def process_voice_command(audio_bytes: bytes, filename: str, content_type: str) -> dict:
    """Transcribe audio with Groq Whisper (confidence gate + deduplication),
    run the AI pipeline, and generate gTTS audio."""
    if not client:
        raise RuntimeError("Configure GROQ_API_KEY to process voice commands.")

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

    # Check no_speech_prob > 0.65
    has_high_no_speech = False
    if no_speech_probs:
        avg_no_speech = sum(no_speech_probs) / len(no_speech_probs)
        if avg_no_speech > 0.65 or (len(no_speech_probs) == 1 and no_speech_probs[0] > 0.65):
            has_high_no_speech = True

    if not transcript or len(transcript) < 2 or clean_text in SILENCE_HALLUCINATIONS or has_high_no_speech:
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
