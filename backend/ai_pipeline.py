import base64
import io
import json
import logging
import math
import os
import re
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from groq import Groq, GroqError
from pydantic import BaseModel, Field, field_validator

from .database import (
    IncompleteNewItem, InventoryWrite, ProductChange, UnknownInventoryItem,
    customer_details, execute_analytics_query, get_existing_items, get_khata_ledger, log_khata_query, read_inventory,
    update_inventory_batch, update_khata,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
api_key = os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key, timeout=30, max_retries=1) if api_key else None
logger = logging.getLogger("voxmetrics")


# Public replies use two-letter routing codes. A dialect without its own
# ISO 639-1 code keeps its wording/script independently of its voice route.
GTTS_LANG_MAP = {
    **{code: code for code in "hi en ta te kn ml mr gu bn pa ur ne".split()},
    "or": "bn", "as": "bn", "bh": "hi", "bgc": "hi", "raj": "hi",
    "mai": "hi", "kok": "mr", "sd": "ur", "ks": "ur", "sa": "hi",
}

LANGUAGE_ROUTES = {
    "bho": "hi", "bgc": "hi", "raj": "hi", "mai": "hi", "kok": "mr",
    "doi": "hi", "brx": "hi", "mni": "hi", "sat": "hi",
    "hinglish": "hi", "tanglish": "ta", "kanglish": "kn",
}


class Command(BaseModel):
    transcript: str = Field(max_length=4000)


class CommandItem(BaseModel):
    action: Optional[Literal["ADD", "SELL", "UPDATE_PRICE", "CREATE_ITEM"]] = None
    item_name: Optional[str] = None
    quantity: Optional[float] = Field(default=None, strict=True)
    new_price: Optional[float] = Field(default=None, strict=True)
    price_words: Optional[str] = None
    unit: Optional[str] = None
    price_per_unit: Optional[float] = Field(default=None, strict=True)
    quantity_words: Optional[str] = None
    source_clause: Optional[str] = None
    source_name: Optional[str] = None
    action_words: Optional[str] = None

    @field_validator("quantity", "new_price", "price_per_unit", mode="before")
    @classmethod
    def numeric_value(cls, value):
        return value if type(value) in (int, float) else None


class KhataEntry(BaseModel):
    mode: Optional[str] = None
    customer_name: Optional[str] = None
    source_name: Optional[str] = None
    last4_digits: Optional[str] = None
    amount: Optional[float] = None
    amount_words: Optional[str] = None
    last4_words: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def numeric_amount(cls, value):
        return value if type(value) in (int, float) else None

    @field_validator("last4_digits", mode="before")
    @classmethod
    def identifier_string(cls, value):
        return value if isinstance(value, str) else None


class Decision(BaseModel):
    detected_lang: str = "hi"
    intent: Literal["INVENTORY_UPDATE", "ADD_INVENTORY", "SELL_ITEM", "ANALYTICS_QUERY", "KHATA_UPDATE", "KHATA_QUERY", "CUSTOMER_SAVE", "CUSTOMER_LOOKUP", "INCOMPLETE_INPUT", "NOISE_ERROR"]
    khata: Optional[KhataEntry] = None
    customer_name: Optional[str] = None
    last4_digits: Optional[str] = None
    details: Optional[str] = None
    source_name: Optional[str] = None
    last4_words: Optional[str] = None
    items: list[CommandItem] = Field(default_factory=list)
    metric: Optional[str] = None
    sql_query: str = ""
    reply_local: str = ""

    @field_validator("detected_lang", mode="before")
    @classmethod
    def normalize_language(cls, value):
        code = value.strip().lower() if isinstance(value, str) else ""
        if code in GTTS_LANG_MAP and len(code) == 2:
            return code
        return LANGUAGE_ROUTES.get(code, "hi")


ITEM_ALIASES = {
    "ಸಕ್ಕರೆ": "sugar", "sakkare": "sugar", "ಮೊಸರು": "curd", "mosaru": "curd",
    "ಹಾಲು": "milk", "haalu": "milk", "ಅಕ್ಕಿ": "rice", "akki": "rice",
    "ಎಣ್ಣೆ": "oil", "enne": "oil", "ಉಪ್ಪು": "salt", "ಬೇಳೆ": "lentils",
    "bele": "lentils", "ಚಹಾ": "tea", "chaha": "tea",
    "mirch": "chilli", "lal mirch": "chilli", "haldi": "turmeric", "atta": "flour",
    "besan": "gram_flour", "sabun": "soap",
    "मिर्च": "chilli", "लाल मिर्च": "chilli", "हल्दी": "turmeric", "आटा": "flour",
    "बेसन": "gram_flour", "साबुन": "soap",
    "khand": "sugar", "panchasara": "sugar", "chini": "sugar", "perugu": "curd",
    "paalu": "milk", "biyyam": "rice", "bhat": "rice", "ennai": "oil",
    "meeth": "salt", "noon": "salt", "paruppu": "lentils", "theneer": "tea", "chaya": "tea",
    "curd": "curd", "mattha": "curd", "dahi": "curd", "thayir": "curd", "doi": "curd",
    "sugar": "sugar", "cheeni": "sugar", "sakkar": "sugar", "sakkarai": "sugar",
    "milk": "milk", "doodh": "milk", "dudh": "milk", "paal": "milk",
    "rice": "rice", "chawal": "rice", "arisi": "rice", "biyam": "rice",
    "tea": "tea", "chai": "tea", "tea patti": "tea", "chai patti": "tea",
    "lentils": "lentils", "dal": "lentils", "daal": "lentils", "pappu": "lentils",
    "oil": "oil", "tel": "oil", "nune": "oil",
    "salt": "salt", "namak": "salt", "uppu": "salt",
}

ACTION_VERBS = {
    "hi": {
        "ADD": "add karo|daal do|badha do|aaya hai|stock mein jodo|jama karo|जोड़ो|जोड़ें|डाल दो|बढ़ा दो|आया है",
        "SELL": "bik gaya|bik gayi|bik gaye|becha|kam karo|nikal gaya|grahak ko diya|बिक गया|बिक गई|बेचा|कम करो|ग्राहक को दिया",
    },
    "kn": {
        "ADD": "ಸೇರಿಸಿ|serisi|ಹಾಕಿ|haaki|ಸೇರಿಸು|serisu|ಬಂದಿದೆ|bandide",
        "SELL": "ಮಾರಾಟವಾಯಿತು|maratavayitu|ಮಾರಿದೆ|maride|ಕಡಿಮೆ ಮಾಡಿ|kadime madi|ಹೋಯಿತು|hoyitu",
    },
    "ta": {
        "ADD": "சேர்க்கவும்|serkkavum|சேர்த்துக்கொள்ளுங்கள்|serthukkonga|போடு|podu",
        "SELL": "விற்றுவிட்டது|vitruvittathu|குறைக்கவும்|kuraikkavum",
    },
    "te": {
        "ADD": "కలపండి|kalapandi|వేయండి|veyandi|యాడ్ చేయండి",
        "SELL": "అమ్ముడైంది|ammudaindi|తగ్గించండి|tagginchandi",
    },
    "mr": {
        "ADD": "टाका|taka|वाढवा|vadhva|ऍड करा",
        "SELL": "विकले|vikle|कमी करा|kami kara",
    },
    "bn": {
        "ADD": "যোগ করুন|jog korun|দিন|din|অ্যাড করুন",
        "SELL": "বিক্রি হয়েছে|bikri hoyeche|কমান|koman",
    },
    "en": {
        "ADD": "add|restock|received",
        "SELL": "sell|sold|deduct|subtract|remove|reduce|customer bought|customer purchased|customer took|to a customer|to the customer",
    },
}


def _verb_pattern(action: str) -> re.Pattern:
    phrases = [phrase for verbs in ACTION_VERBS.values() for phrase in verbs[action].split("|")]
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, phrases)) + r")(?!\w)", re.I)


ADD_VERB = _verb_pattern("ADD")
SELL_VERB = _verb_pattern("SELL")

_COUNTS = {
    "one": 1, "ek": 1, "एक": 1, "two": 2, "do": 2, "दो": 2,
    "three": 3, "teen": 3, "तीन": 3, "four": 4, "char": 4, "चार": 4,
    "five": 5, "paanch": 5, "पाँच": 5, "six": 6, "chhe": 6, "छह": 6,
    "seven": 7, "saat": 7, "सात": 7, "eight": 8, "aath": 8, "आठ": 8,
    "nine": 9, "nau": 9, "नौ": 9, "ten": 10, "das": 10, "दस": 10,
}
_FRACTIONS = {
    "pao": .25, "pav": .25, "पाव": .25, "aadha": .5, "half": .5, "आधा": .5,
    "sawa": 1.25, "सवा": 1.25, "dedh": 1.5, "डेढ़": 1.5, "dhai": 2.5, "ढाई": 2.5,
}
# Factor and applicable inventory unit. An empty unit means the item's base unit.
_MEASURES = {
    **dict.fromkeys("pipa pipas पीपा पीपे tin tins टिन dabba dabbas dabbe डब्बा डिब्बा".split(), (10, "L")),
    **dict.fromkeys("katta kattas katte कट्टा कट्टे bori boris बोरी bora boras बोरा".split(), (50, "sack")),
    **dict.fromkeys("darjan dozen dozens दर्जन".split(), (12, "count")),
    **dict.fromkeys("g gm gram grams ग्राम".split(), (.001, "kg")),
    **dict.fromkeys("ml milliliter milliliters millilitre millilitres मिलीलीटर".split(), (.001, "L")),
    **dict.fromkeys("kg kilo kilos किलो".split(), (1, "kg")),
    **dict.fromkeys("l liter liters litre litres लीटर".split(), (1, "L")),
}
_SACK_ITEMS = {"rice", "sugar", "lentils", "flour", "salt"}
_MEASURE_PATTERN = "|".join(sorted(_MEASURES, key=len, reverse=True))
_QUANTITY_PHRASE = re.compile(
    r"(?<![\w.,+-])(?:(?P<count>[+-]?(?:\d+(?:,\d{2,3})*(?:\.\d+)?|\.\d+)|"
    + "|".join(_COUNTS) + r")\s*)?(?:(?P<fraction>" + "|".join(_FRACTIONS)
    + r")(?:\s+(?:a\s+)?(?P<fraction_unit>" + _MEASURE_PATTERN + r"))?|(?P<measure>"
    + _MEASURE_PATTERN + r"))(?!\w)", re.I,
)


def normalize_kirana_quantity(raw_text, item_name, quantity, *, base_unit=None):
    """Derive the amount from a single spoken measure, independent of model scaling."""
    matches = [m for m in _QUANTITY_PHRASE.finditer(raw_text.casefold()) if m["count"] or m["fraction"]]
    if not matches:
        return quantity
    if len(matches) != 1:
        return None
    match = matches[0]
    count = match["count"]
    fraction = match["fraction"]
    number = _COUNTS.get(count, count) if count else 1
    factor, unit = _MEASURES.get(match["measure"] or match["fraction_unit"], (1, base_unit))
    canonical = ITEM_ALIASES.get(item_name, item_name)
    if not base_unit:
        if canonical in _SACK_ITEMS:
            base_unit = "kg"
        elif canonical in {"oil", "milk"}:
            base_unit = "L"
    if unit == "sack":
        if canonical not in _SACK_ITEMS:
            return None
        unit = "kg"
    elif unit == "count":
        unit = base_unit if base_unit in {"pieces", "packets"} else "pieces"
    elif factor == 10 and not base_unit and canonical not in {"oil", "milk"}:
        return None
    if unit and base_unit and unit != base_unit:
        return None
    return float(Decimal(str(number).replace(",", "")) * Decimal(str(_FRACTIONS.get(fraction, 1)))
                 * Decimal(str(factor)))


def _normalize_stock_item(item, transcript, single_item, inventory_units):
    """Bind one measurement to this item and return the evidence for its confirmation."""
    words = (item.quantity_words or "").strip().casefold()
    clause = (item.source_clause or (transcript if single_item else words)).casefold()
    if not clause or clause not in transcript.casefold():
        return ""
    matches = [m for m in _QUANTITY_PHRASE.finditer(clause) if m["count"] or m["fraction"]]
    if words and words in clause and matches:
        # Shared-verb clauses can contain another item's container alongside
        # this item's ordinary quantity. Only use the cited measurement.
        matches = [m for m in matches if words == m.group() or words in m.group().split()
                   or m.group() in words]
    if len(matches) > 1:
        item.quantity = None
        return ""
    if not matches:
        return ""
    match = matches[0]
    raw_name = (item.item_name or "").casefold()
    name = ITEM_ALIASES.get(raw_name, raw_name.replace(" ", "_"))
    factor, unit = _MEASURES.get(match["measure"] or match["fraction_unit"], (1, None))
    base_unit = inventory_units.get(raw_name) or inventory_units.get(name) or item.unit
    if not base_unit:
        if name in _SACK_ITEMS:
            base_unit = "kg"
        elif name in {"milk", "oil"}:
            base_unit = "L"
        elif unit == "count":
            base_unit = "pieces"
        elif unit in {"kg", "L"} and factor != 10:
            base_unit = unit
    item.quantity = normalize_kirana_quantity(match.group(), name, item.quantity, base_unit=base_unit)
    item.unit = base_unit
    return match.group() if factor != 1 or match["fraction"] else ""

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
  "detected_lang": <two-letter language routing code>,
  "intent": "INVENTORY_UPDATE" | "ANALYTICS_QUERY" | "KHATA_UPDATE" | "KHATA_QUERY" | "CUSTOMER_SAVE" | "CUSTOMER_LOOKUP" | "INCOMPLETE_INPUT" | "NOISE_ERROR",
  "khata": {"mode": "UDHAAR" | "JAMA", "customer_name": <account name>, "source_name": <verbatim name>, "last4_digits": <4-digit string>, "amount": <rupees>, "amount_words": <verbatim number words if spoken>, "last4_words": <verbatim digit words if spoken>} | null,
  "details": <verbatim note to save, or null>,
  "source_name": <verbatim name for customer details or null>,
  "last4_words": <verbatim spoken digits or null>,
  "customer_name": <customer name for customer details or ledger query, or null>,
  "last4_digits": <four-digit string for customer details, optional filter for KHATA_QUERY, or null>,
  "items": [{"action": "ADD" | "SELL" | "UPDATE_PRICE" | "CREATE_ITEM", "item_name": <canonical product name or null>, "quantity": <stock amount or null>, "new_price": <explicit updated price or null>, "unit": "kg" | "L" | "packets" | "pieces" | null, "price_per_unit": <explicit selling price for creation or null>, "source_name": <verbatim product words>, "action_words": <verbatim affirmative action phrase or null>, "source_clause": <verbatim clause governing this item or null>, "quantity_words": <verbatim spoken quantity words or null>, "price_words": <verbatim spoken price words or null>}],
  "metric": "lowest_stock" | "highest_price" | "lowest_price" | "highest_profit" | "top_selling" | "all_inventory" | null,
  "sql_query": "<SQL string or empty>",
  "reply_local": "<conversational sentence in detected_lang>"
}

Strict Rules:
0. CUSTOMER_SAVE appends delivery, address or personal notes. CUSTOMER_LOOKUP reads those
   notes and the outstanding balance. Use top-level customer_name (common Latin spelling),
   last4_digits (four-digit string including leading zeros), source_name and last4_words.
   Require BOTH name and digits even for lookup. Copy only the actual note from the transcript
   into details for a save. "Naya customer Rahul 7890 add karo, Gali number 4 mein rehta hai"
   saves "Gali number 4 mein rehta hai". "Rahul 7890 ki detail nikalo" is a lookup.
   Missing details or identifiers require clarification; never infer digits from the address.
   A customer taking goods on credit increases their outstanding balance (KHATA_UPDATE,
   UDHAAR). A payment against that balance decreases it (JAMA). Interpret these meanings
   in any supported dialect. Copy the name verbatim into source_name. For customer_name
   use the same name in common Latin spelling for both updates and queries (रमेश → Ramesh).
   Never substitute another person. Keep a Latin-script name as spoken.
   Require an explicit customer name, last four phone digits (preserve leading zeros),
   and separate positive rupee amount. Never infer one from another or from prior data.
   For missing details, mixed ledger and inventory writes, negations or hypotheticals,
   ask for clarification without writing. Balance questions are read-only KHATA_QUERY;
   place any requested filters at the top level. For ledger intents use empty items and SQL.
1. Canonical Item Names (map regional terms):
   Resolve native scripts, romanizations, inflections and dialect synonyms by meaning.
   Examples of canonical grocery names appear below. They are not exhaustive.
   Match existing_items first. For other new products use the English singular in lowercase,
   separating words with underscores. Never substitute a similar-sounding existing product.
   Only CREATE_ITEM can introduce a product. Use it for an affirmative request to stock
   a new product with its explicit quantity, unit and selling price per unit. For example,
   "5 kilo haldi 80 rupaye mein add karo" creates turmeric, 5 kg at 80 per kg.
   Never infer a unit price from total spend. Copy the creation clause into source_clause.
   Include source_name with the exact product words from the transcript when translating
   a regional product name or converting its plural form to singular.
   Never create from a sale, price change, query, negation or hypothetical.
   For an existing product use ADD for ordinary restocking; if an explicit add also
   specifies its selling price, use CREATE_ITEM to restock and apply that price.
   If a new product lacks quantity or selling price, return INCOMPLETE_INPUT and request both.

2. Unit Conversions:
   For ADD, SELL and CREATE_ITEM return quantity and unit in the item's inventory base
   unit. Copy the entire original quantity expression (count AND measure) into
   quantity_words for each item separately. For liquids, a pipa/tin/dabba is 10 L.
   For rice, sugar, lentils, flour/atta and salt, a katta/bori/bora is 50 kg.
   A darjan/dozen is 12 count units. A pao/pav is 0.25 (teen pao = 0.75);
   aadha/half = 0.5, sawa = 1.25, dedh = 1.5 and dhai = 2.5.
   Convert grams to kg and ml to L (500 of either is 0.5 in its base unit).
   Apply each factor once. Keep quantities separate from prices. Ask for clarification
   when the spoken measure cannot be converted to the item's database unit.

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
     the missing quantity in reply_local. For unknown SELL or UPDATE_PRICE items ask for the item.
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

SYSTEM_PROMPT += """

Interpret commands in all Scheduled languages: Assamese, Bengali, Bodo, Dogri,
Gujarati, Hindi, Kannada, Kashmiri, Konkani, Maithili, Malayalam, Manipuri (Meitei),
Marathi, Nepali, Odia, Punjabi, Sanskrit, Santali, Sindhi, Tamil, Telugu and Urdu.
Also handle English, Bhojpuri, Haryanvi, Rajasthani, Hinglish, Tanglish, Kanglish
and other dialects or mixtures. Classify ADD, SELL, CREATE_ITEM, UPDATE_PRICE and
ANALYTICS_QUERY by the meaning of each clause, including verbs absent from examples.
Copy the affirmative verb phrase into action_words, never a bare quantity or noun.
Use the transcript's language, register, mix and script for reply_local even if its
voice uses a different language. Return its ISO 639-1 code when available
(hi en ta te kn ml mr gu bn pa or as ur ne sd ks sa). For languages without one,
use mr for Konkani and hi for Maithili, Bodo, Dogri, Manipuri, Santali and unlisted
dialects without a code. Use hi/ta/kn for Hinglish/Tanglish/Kanglish respectively.
Grocery aliases:
""" + json.dumps(ITEM_ALIASES, ensure_ascii=False) + """
Some examples of action verbs:
""" + json.dumps(ACTION_VERBS, ensure_ascii=False, indent=2) + """
Detect the language of the command even when it is romanized. Both "2 ಕೆಜಿ ಸಕ್ಕರೆ ಸೇರಿಸಿ"
and "2 Keji sakkare serisi" are Kannada (kn), ADD sugar, quantity 2. ADD verbs increase
stock and never increment units_sold. Never classify an item as SELL unless its own
clause explicitly describes a sale, handoff to a customer, or stock deduction. Interpret din
as an add verb in Bengali and taka in Marathi only when they function as commands. A noun and
quantity alone are insufficient. Copy the full verbatim clause into source_clause for
each stock action, including a shared verb when it governs several items. Interpret verbs
in context: negated or hypothetical actions and questions about sales are not writes."""

SILENCE_HALLUCINATIONS = {
    "", "you", "thank you", "thank you so much", "thank you very much",
    "thanks", "thanks for watching", "thank you for watching",
    "bye", "bye bye", "goodbye", "subscribe", "please subscribe",
    "watching", "the end",
}

CHAT_MODELS = [
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


NEW_ITEM_PROMPTS = {
    "kn": "ಹೊಸ ವಸ್ತುವನ್ನು ಸೇರಿಸಲು ಪ್ರಮಾಣ, ಘಟಕ ಮತ್ತು ಮಾರಾಟದ ಬೆಲೆಯನ್ನು ತಿಳಿಸಿ.",
    "hi": "Naya item add karne ke liye quantity, unit aur selling price batayein.",
    "en": "To add the product, specify its quantity, unit and selling price per unit.",
    "ta": "Pudhiya porulin alavu, alagu matrum virpanai vilaiyai sollungal.",
    "mr": "Navin vastuche praman, ekak ani vikri kimmat sanga.",
    "bn": "Notun ponner poriman, ekok o bikrir daam bolun.",
    "te": "Kotta vastuvu parimanam, unit mariyu ammaka dhara cheppandi.",
}


def _cites_action(item: CommandItem, clause: str) -> bool:
    words = (item.action_words or "").strip().casefold()
    return bool(words and any(c.isalpha() for c in words)
                and words != (item.source_name or item.item_name or "").casefold()
                and re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", clause.casefold()))


def _creation_stated(item: CommandItem, transcript: str, converted_phrase: str = "") -> bool:
    """Check that the request to add and its amounts occur in the cited clause."""
    clause = (item.source_clause or transcript).strip().casefold()
    if not clause or clause not in transcript.casefold():
        return False
    if not _cites_action(item, clause) and not ADD_VERB.search(clause) and not re.search(r"\b(add|stock|restock|create|register|include|introduce|received|jod\w*|daal\w*|shamil|serkk\w*|cherch\w*|jog|ghala)\b"
                     r"|जोड़|जोड|डाल|शामिल|ऐड|एड|घाला|சேர்|చేర్చ|జోడించ|যোগ", clause):
        return False
    raw_name = (item.item_name or "").strip().lower().replace("_", " ")
    canonical = ITEM_ALIASES.get(raw_name, raw_name.replace(" ", "_"))
    names = [canonical.replace("_", " "), raw_name]
    names.extend(alias for alias, name in ITEM_ALIASES.items() if name == canonical)
    if item.source_name:
        names.append(item.source_name.strip().casefold())
    if not any(n and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", clause) for n in names):
        return False
    if SELL_VERB.search(clause) or re.search(r"\b(no|not|never|don['’]t|sell|sold|bik|bech\w*|mat|nahi|if|how much|in stock)\b|मत\s|नहीं|बिक|बेच", clause):
        return False
    # Consume distinct mentions so one number cannot supply both the price and quantity.
    if converted_phrase:
        clause = clause.replace(converted_phrase, "", 1)
    numbers = [Decimal(m.replace(",", "")) for m in re.findall(
        r"[+-]?(?:\d+(?:,\d{2,3})*(?:\.\d+)?|\.\d+)", clause.replace("−", "-")
    )]
    for value, words, is_quantity in [(item.price_per_unit, item.price_words, False),
                                      (item.quantity, item.quantity_words, True)]:
        if value is None or (is_quantity and converted_phrase):
            continue
        amount = Decimal(str(value))
        if amount in numbers:
            numbers.remove(amount)
        elif words and words.casefold() in clause and not any(c.isdigit() for c in words):
            clause = clause.replace(words.casefold(), "", 1)
        elif (is_quantity and amount * 1000 in numbers
              and re.search(r"\b(grams?|g|gm|ml|millilit\w*)\b|ग्राम|मिलीलीटर", clause)):
            numbers.remove(amount * 1000)
        else:
            return False
    return True


def _checked_sale(item: CommandItem, transcript: str, single: bool, lang: str) -> Optional[str]:
    """Resolve a proposed sale against the verb governing this product."""
    raw_name = (item.item_name or "").casefold()
    canonical = ITEM_ALIASES.get(raw_name, raw_name)
    names = [canonical.replace("_", " "), item.source_name or ""]
    names.extend(alias for alias, name in ITEM_ALIASES.items() if name == canonical)
    def mentions_item(text):
        return any(n and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I) for n in names)

    clause = transcript if single else item.source_clause
    if not clause:
        clauses = re.split(r"[,;]|\b(?:and|aur|mattu|ಮತ್ತು)\b", transcript, flags=re.I)
        matches = [part for part in clauses if mentions_item(part)]
        clause = matches[0] if len(matches) == 1 else ""
    if not clause or (not single and not mentions_item(clause)):
        return None
    if clause.casefold() not in transcript.casefold():
        return None
    if re.search(r"\b(not|never|don['’]t|if|mat|nahi|beda|illa|how much|how many)\b|ಬೇಡ|ಇಲ್ಲ|नहीं|मत\s", transcript, re.I):
        return None
    add = any(m.group().casefold() not in {"din", "taka"} or
              (m.group().casefold(), lang) in {("din", "bn"), ("taka", "mr")}
              for m in ADD_VERB.finditer(clause))
    sell = bool(SELL_VERB.search(clause))
    if sell and not add:
        return "SELL"
    if add and not sell:
        return "ADD"
    if not add and not sell and mentions_item(clause) and _cites_action(item, clause):
        return "SELL"
    return None


_DIGIT_WORDS = {
    word: str(digit)
    for language in (
        "zero one two three four five six seven eight nine",
        "shunya ek do teen chaar paanch chhe saat aath nau",
        "शून्य एक दो तीन चार पाँच छह सात आठ नौ",
        "शून्य एक दोन तीन चार पाच सहा सात आठ नऊ",
        "শূন্য এক দুই তিন চার পাঁচ ছয় সাত আট নয়",
        "பூஜ்ஜியம் ஒன்று இரண்டு மூன்று நான்கு ஐந்து ஆறு ஏழு எட்டு ஒன்பது",
        "సున్నా ఒకటి రెండు మూడు నాలుగు ఐదు ఆరు ఏడు ఎనిమిది తొమ్మిది",
        "ಸೊನ್ನೆ ಒಂದು ಎರಡು ಮೂರು ನಾಲ್ಕು ಐದು ಆರು ಏಳು ಎಂಟು ಒಂಬತ್ತು",
        "പൂജ്യം ഒന്ന് രണ്ട് മൂന്ന് നാല് അഞ്ച് ആറ് ഏഴ് എട്ട് ഒമ്പത്",
        "શૂન્ય એક બે ત્રણ ચાર પાંચ છ સાત આઠ નવ",
        "ਸਿਫ਼ਰ ਇੱਕ ਦੋ ਤਿੰਨ ਚਾਰ ਪੰਜ ਛੇ ਸੱਤ ਅੱਠ ਨੌਂ",
        "صفر ایک دو تین چار پانچ چھ سات آٹھ نو",
    )
    for digit, word in enumerate(language.split())
}


def _execute_khata(decision: Decision, transcript: str, shop_id: int) -> ExecutionResult:
    def clarify(message):
        decision.intent = "INCOMPLETE_INPUT"
        return ExecutionResult(grounded_reply=message)

    if decision.items:
        return clarify("Please give the inventory command and the ledger command separately.")
    if decision.intent == "KHATA_QUERY":
        name = " ".join((decision.customer_name or "").casefold().split())
        digits = decision.last4_digits
        if digits is not None and not re.fullmatch("[0-9]{4}", digits):
            return clarify("Please give the four digits of the customer account.")
        rows = [row for row in get_khata_ledger(shop_id)
                if (not name or row["customer_name"].casefold() == name)
                and (digits is None or row["last4_digits"] == digits)]
        if not rows:
            return ExecutionResult(grounded_reply="No matching customer account was found.")
        if len(rows) == 1:
            row = rows[0]
            reply = f"{row['customer_name']} - {row['last4_digits']} has a pending balance of ₹{row['balance']:.2f}."
        elif name or digits:
            return clarify("Several accounts match. Please give the customer's name and last four digits.")
        else:
            total = sum(Decimal(str(row["balance"])) for row in rows)
            reply = f"The total pending balance across {len(rows)} accounts is ₹{total:.2f}."
        log_khata_query(reply, transcript, shop_id)
        return ExecutionResult(grounded_reply=reply)

    entry = decision.khata or KhataEntry()
    name = (entry.customer_name or "").strip()
    digits = entry.last4_digits or ""
    amount = entry.amount
    details = None
    is_customer = decision.intent in {"CUSTOMER_SAVE", "CUSTOMER_LOOKUP"}
    if is_customer:
        name = (decision.customer_name or "").strip()
        digits = decision.last4_digits or ""
        if not name or not re.fullmatch("[0-9]{4}", digits):
            return clarify("Please give the customer's name and last four digits.")
        entry = KhataEntry(source_name=decision.source_name, last4_words=decision.last4_words)
        if decision.intent == "CUSTOMER_SAVE":
            details = (decision.details or "").strip()
            if not details or details.casefold() not in transcript.casefold():
                return clarify("What customer details should I save?")
    elif (not name or not re.fullmatch("[0-9]{4}", digits) or entry.mode not in {"UDHAAR", "JAMA"}
            or amount is None or not math.isfinite(amount) or amount <= 0 or round(amount, 2) != amount):
        return clarify("Please give the customer's name, last four digits, credit or payment, and a positive rupee amount.")

    # Reserve the name and identifier before looking for a separate amount.
    identity_text = transcript.casefold()
    if details:
        identity_text = identity_text.replace(details.casefold(), " ", 1)
    text = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in identity_text)
    spoken_name = (entry.source_name or name).strip().casefold()
    if not spoken_name or (spoken_name.isascii() and spoken_name != name.casefold()):
        return clarify("Please repeat the customer's name.")
    name_match = re.search(r"(?<!\w)" + re.escape(spoken_name) + r"(?!\w)", text)
    if not name_match:
        return clarify("Please repeat the customer's name.")
    text = text[:name_match.start()] + " " + text[name_match.end():]
    identifier = re.search(r"(?<![\d.₹])" + r"\s*".join(digits) + r"(?!\d|[.,]\d)", text)
    if not identifier and entry.last4_words:
        words = entry.last4_words.strip().casefold()
        if "".join(_DIGIT_WORDS.get(word, "?") for word in words.split()) == digits:
            identifier = re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", text)
    if not identifier:
        return clarify("Please repeat the customer's four digits.")
    text = text[:identifier.start()] + " " + text[identifier.end():]
    if is_customer:
        row = customer_details(shop_id, name, digits, details=details, raw_command=transcript)
        if not row:
            return ExecutionResult(grounded_reply="No matching customer account was found.")
        if details is not None:
            return ExecutionResult(grounded_reply=f"Details saved for {name} - {digits}.")
        notes = row["notes"] or "No notes saved."
        return ExecutionResult(grounded_reply=f"{name} - {digits}: {notes} Pending balance: ₹{row['balance']:.2f}.")
    numbers = re.findall(r"[+-]?(?:\d+(?:,\d{2,3})*(?:\.\d+)?|\.\d+)", text.replace("−", "-"))
    stated = any(Decimal(n.replace(",", "")) == Decimal(str(amount)) for n in numbers)
    words = (entry.amount_words or "").strip().casefold()
    if not stated and not (not numbers and words and any(c.isalpha() for c in words)
                           and re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", text)):
        return clarify("Please specify the rupee amount separately from the customer's four digits.")
    row = update_khata(name, digits, amount, entry.mode, raw_command=transcript, shop_id=shop_id)
    action = "Credit added" if entry.mode == "UDHAAR" else "Payment recorded"
    return ExecutionResult(grounded_reply=(
        f"{action} for {row['customer_name']} - {digits}: ₹{amount:.2f}. "
        f"Pending balance: ₹{row['balance']:.2f}."
    ))


def execute_decision(decision: Decision, transcript: Optional[str] = None,
                     existing_items: Optional[list[str]] = None,
                     inventory_units: Optional[dict[str, str]] = None, *, shop_id=1) -> ExecutionResult:
    if decision.intent in {"KHATA_UPDATE", "KHATA_QUERY", "CUSTOMER_SAVE", "CUSTOMER_LOOKUP"}:
        return _execute_khata(decision, transcript or "", shop_id)
    sql = decision.sql_query.strip().removesuffix(";").strip()
    is_update = decision.intent in {"INVENTORY_UPDATE", "ADD_INVENTORY", "SELL_ITEM"}
    if is_update:
        existing = {name.casefold(): name for name in (get_existing_items(shop_id) if existing_items is None else existing_items)}
        conversions = []
        for item in decision.items:
            phrase = ""
            if transcript and item.action != "UPDATE_PRICE":
                phrase = _normalize_stock_item(item, transcript, len(decision.items) == 1, inventory_units or {})
                if phrase and item.quantity is None:
                    decision.intent = "INCOMPLETE_INPUT"
                    return ExecutionResult(grounded_reply=(
                        f"Please specify the amount of {item.item_name} in {item.unit or 'inventory units'}."
                    ))
            conversions.append(phrase)
            if item.action == "CREATE_ITEM" and (
                item.quantity is None or not math.isfinite(item.quantity) or item.quantity <= 0
                or (item.price_per_unit is not None and
                    (not math.isfinite(item.price_per_unit) or item.price_per_unit <= 0))
                or (transcript is not None and not _creation_stated(item, transcript, phrase))
            ):
                decision.intent = "INCOMPLETE_INPUT"
                return ExecutionResult(grounded_reply=NEW_ITEM_PROMPTS.get(decision.detected_lang, NEW_ITEM_PROMPTS["en"]))
            if item.action == "UPDATE_PRICE" and (
                item.new_price is None or not math.isfinite(item.new_price) or item.new_price <= 0
                or (transcript is not None and not _price_stated(item, transcript))
            ):
                decision.intent = "INCOMPLETE_INPUT"
                prompts = {
                    "kn": "ಹೊಸ ಮಾರಾಟದ ಬೆಲೆಯನ್ನು ರೂಪಾಯಿಗಳಲ್ಲಿ ತಿಳಿಸಿ",
                    "hi": "Naya bikri ka daam rupaye mein batayein",
                    "ta": "Pudhiya virpanai vilaiyai roobaayil sollungal",
                    "mr": "Navin vikri kimmat rupayant sanga",
                    "bn": "Notun bikrir daam takay bolun",
                    "te": "Kotta ammaka dhara rupayallo cheppandi",
                    "en": "Please specify the exact new selling price in rupees",
                }
                return ExecutionResult(grounded_reply=prompts.get(decision.detected_lang, prompts["en"]) + ".")
        invalid = [
            item for item in decision.items
            if item.action != "UPDATE_PRICE"
            if item.quantity is None or not math.isfinite(item.quantity) or item.quantity <= 0
        ]
        if not decision.items or invalid:
            decision.intent = "INCOMPLETE_INPUT"
            decision.sql_query = ""
            prompts = {
                "kn": "ದಯವಿಟ್ಟು ಸರಿಯಾದ ಪ್ರಮಾಣವನ್ನು ತಿಳಿಸಿ",
                "hi": "Kripya sahi quantity batayein",
                "ta": "Sariyana alavai sollungal",
                "mr": "Krupaya yogya praman sanga",
                "bn": "Shothik poriman bolun",
                "te": "Dayachesi saraina parimanam cheppandi",
                "en": "Please specify a positive quantity",
            }
            names = ", ".join(item.item_name.capitalize() for item in invalid if item.item_name)
            prompt = prompts.get(decision.detected_lang, prompts["en"])
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
                raw_name = " ".join((item.item_name or "").lower().split())
                canonical = ITEM_ALIASES.get(raw_name, raw_name.replace(" ", "_"))
                name = existing.get(raw_name, existing.get(canonical, canonical))
                if name.casefold() not in existing and not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name):
                    raise UnknownInventoryItem(item.item_name or "item")
                action = item.action or legacy_action
                if action == "SELL" and transcript is not None:
                    action = _checked_sale(item, transcript, len(decision.items) == 1, decision.detected_lang)
                    if action == "ADD" and decision.intent == "SELL_ITEM":
                        decision.intent = "INVENTORY_UPDATE"
                if action is None:
                    decision.intent = "INCOMPLETE_INPUT"
                    prompts = {
                        "kn": "ಸೇರಿಸಬೇಕೇ ಅಥವಾ ಮಾರಾಟ ಮಾಡಬೇಕೇ",
                        "hi": "Jodna hai ya bechna hai",
                        "ta": "Serkkava allathu virkava",
                        "mr": "Jodayche ki vikayche",
                        "bn": "Jog korbo naki bikri korbo",
                        "te": "Cherchala leka ammala",
                        "en": "Add or sell",
                    }
                    return ExecutionResult(grounded_reply=f"{prompts.get(decision.detected_lang, prompts['en'])} ({name.capitalize()})?")
                value = item.new_price if action == "UPDATE_PRICE" else item.quantity
                items.append(InventoryWrite(name, value, action, item.unit, item.price_per_unit))
            changes = update_inventory_batch(items, raw_command=transcript or "", shop_id=shop_id)
        except IncompleteNewItem:
            decision.intent = "INCOMPLETE_INPUT"
            return ExecutionResult(grounded_reply=NEW_ITEM_PROMPTS.get(decision.detected_lang, NEW_ITEM_PROMPTS["en"]))
        except UnknownInventoryItem as exc:
            decision.intent = "INCOMPLETE_INPUT"
            if decision.detected_lang == "kn":
                return ExecutionResult(grounded_reply=f"ದಾಸ್ತಾನಿನಲ್ಲಿ '{exc}' ಲಭ್ಯವಿಲ್ಲ. ವಸ್ತುವಿನ ಹೆಸರು ತಿಳಿಸಿ.")
            reply = (
                f"Item '{exc}' inventory mein uplabdh nahi hai. Kripya sahi grocery item batayein."
                if decision.detected_lang == "hi"
                else f"Item '{exc}' is not in the inventory. Please specify an inventory item."
            )
            return ExecutionResult(grounded_reply=reply)

        introductions = {
            "kn": "ದಾಸ್ತಾನು ನವೀಕರಿಸಲಾಗಿದೆ",
            "hi": "Stock update ho gaya",
            "ta": "Irupu pudhuppikkappattadhu",
            "mr": "Satha adyatan zala",
            "bn": "Mojut update hoyeche",
            "te": "Nilva navikarinchabadindi",
            "en": "Stock updated",
        }
        price_labels = {
            "kn": "ಬೆಲೆ",
            "hi": "ka daam", "ta": "vilai", "mr": "chi kimmat", "bn": "er daam",
            "te": "dhara", "en": "price",
        }
        parts = []
        for change, write, phrase in zip(changes, items, conversions):
            name = change.item_name.replace("_", " ")
            before, after, unit = change.before, change.after, change.unit
            action = write.action
            total = format(Decimal(str(after)).normalize(), "f")
            detail = f" ({phrase}), total stock {total} {unit}" if phrase else ""
            if isinstance(change, ProductChange):
                amount = format((Decimal(str(after)) - Decimal(str(before))).normalize(), "f")
                price = format(Decimal(str(change.price)).normalize(), "f")
                new_label = {"kn": "ಹೊಸ ವಸ್ತು", "hi": "Naya item", "ta": "Pudhiya porul", "mr": "Navin vastu",
                             "bn": "Notun ponno", "te": "Kotta vastuvu", "en": "New item"}
                label = new_label.get(decision.detected_lang, new_label["en"]) + " " if change.created else ""
                if change.created and len(changes) == 1 and decision.detected_lang == "hi":
                    return ExecutionResult(grounded_reply=f"{label}{name.capitalize()} add ho gaya: {amount} {unit} @ ₹{price}/{unit}{detail}.")
                parts.append(f"{label}{name.capitalize()} (+{amount} {unit} @ ₹{price}/{unit}){detail}")
                continue
            if action == "UPDATE_PRICE":
                old = format(Decimal(str(before)).normalize(), "f")
                new = format(Decimal(str(after)).normalize(), "f")
                per_unit = "packet" if unit == "packets" else unit
                parts.append(f"{name.capitalize()} {price_labels.get(decision.detected_lang, price_labels['en'])} (₹{old} → ₹{new}/{per_unit})")
                continue
            delta = Decimal(str(after)) - Decimal(str(before))
            amount = format(delta.normalize(), "+f") if delta else "0"
            parts.append(f"{name.capitalize()} ({amount} {unit}){detail}")
        if any(write.action == "UPDATE_PRICE" for write in items):
            introductions = {
                "kn": "ನವೀಕರಿಸಲಾಗಿದೆ",
                "hi": "Update ho gaya", "ta": "Pudhuppikkappattadhu", "mr": "Badal kela",
                "bn": "Bodol hoyeche", "te": "Mārpu chēsāmu", "en": "Updated",
            }
        return ExecutionResult(grounded_reply=f"{introductions.get(decision.detected_lang, introductions['en'])}: {', '.join(parts)}.")

    # ---------------------------------------------------------
    # Layer 4: Deterministic Grounding (ANALYTICS_QUERY)
    # ---------------------------------------------------------
    if not re.match(r"^SELECT\s", sql, re.I):
        # Fall back to metric map if LLM SQL was malformed
        sql = METRIC_SQL_MAP.get(decision.metric, "SELECT item_name AS label, price AS value FROM inventory ORDER BY value DESC LIMIT 3")

    chart = execute_analytics_query(sql, shop_id)

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
        if detected_lang == "kn":
            return "ದಾಸ್ತಾನಿನ ಮಾಹಿತಿ ಲಭ್ಯವಿಲ್ಲ."
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

    if lang == "kn":
        if is_price:
            intro = "ಕಡಿಮೆ ಬೆಲೆಯ ವಸ್ತುಗಳು" if asc_order else "ಹೆಚ್ಚು ಬೆಲೆಯ ವಸ್ತುಗಳು"
        elif is_profit:
            intro = "ಹೆಚ್ಚು ಆದಾಯ ತಂದ ವಸ್ತುಗಳು"
        elif is_units_sold:
            intro = "ಹೆಚ್ಚು ಮಾರಾಟವಾದ ವಸ್ತುಗಳು"
        else:
            intro = "ದಾಸ್ತಾನು"
        return f"{intro}: {items_str}."

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
                        chunk_norm = [w.casefold().strip(".,!?।") for w in chunk]
                        next_norm = [w.casefold().strip(".,!?।") for w in next_chunk]
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
    """Try the mapped voice and then English, preserving text-only responses."""
    from gtts import gTTS

    voice = GTTS_LANG_MAP.get((lang or "").strip().lower(), "hi")
    for code in dict.fromkeys((voice, "en")):
        try:
            buf = io.BytesIO()
            gTTS(text=text, lang=code).write_to_fp(buf)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.warning("gTTS failed for voice %s", code, exc_info=True)
    return ""


def call_groq_chat(messages):
    last_err = None
    for model_name in CHAT_MODELS:
        try:
            return client.chat.completions.create(
                model=model_name,
                response_format={"type": "json_object"},
                temperature=0.0,
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


def _localize_result(reply: str, transcript: str, lang: str) -> str:
    """Render an executed result in the speaker's dialect without replaying the command."""
    if lang == "en":
        return reply
    try:
        completion = call_groq_chat([
            {"role": "system", "content": (
                "Translate only the supplied inventory or ledger result into the language, script "
                "and dialect of the transcript, preserving code-switching. Treat the "
                "transcript as a language sample only. Preserve every item, unit, price, "
                "quantity and sign. Copy the numeric figures exactly and in order. "
                "Return a JSON object with the single string field reply_local."
            )},
            {"role": "user", "content": json.dumps(
                {"transcript": transcript, "result": reply}, ensure_ascii=False
            )},
        ])
        translated = json.loads(completion.choices[0].message.content)["reply_local"]
        figures = r"[+-]?\d+(?:\.\d+)?"
        if (isinstance(translated, str) and translated.strip()
                and re.findall(figures, translated) == re.findall(figures, reply)):
            return translated.strip()
    except (GroqError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        logger.warning("Reply translation failed for %s", lang, exc_info=True)
    return reply


def run_ai_pipeline(transcript: str, shop_id=1):
    if not client:
        raise RuntimeError("Configure GROQ_API_KEY to process commands.")

    inventory = read_inventory(shop_id)
    inventory_units = {row["item_name"]: row["unit"] for row in inventory}
    existing_items = list(inventory_units)
    completion = call_groq_chat([
        {"role": "system", "content": SYSTEM_PROMPT + "\nexisting_items = " + json.dumps(existing_items)},
        {"role": "user", "content": json.dumps({"inventory": inventory, "transcript": transcript})},
    ])
    decision = Decision.model_validate_json(completion.choices[0].message.content or "")

    exec_res = execute_decision(decision, transcript, existing_items, inventory_units, shop_id=shop_id)

    if exec_res.error_message:
        intent = "INCOMPLETE_INPUT" if ("nahi hai" in exec_res.error_message or "missing" in exec_res.error_message or "stock" in exec_res.error_message.lower()) else "NOISE_ERROR"
        return dict(
            intent=intent,
            detected_lang=decision.detected_lang,
            reply_local=deduplicate_sentences(_localize_result(
                exec_res.error_message, transcript, decision.detected_lang
            )),
            chart_data=None,
            inventory=read_inventory(shop_id),
        )

    reply = exec_res.grounded_reply or decision.reply_local
    if exec_res.grounded_reply and reply != decision.reply_local:
        reply = _localize_result(reply, transcript, decision.detected_lang)
    reply = deduplicate_sentences(reply)
    chart_clean = [{"label": r["label"], "value": r["value"]} for r in exec_res.chart] if exec_res.chart else None

    return dict(
        intent=decision.intent,
        detected_lang=decision.detected_lang,
        reply_local=reply,
        chart_data=chart_clean,
        inventory=read_inventory(shop_id),
    )


def process_voice_command(audio_bytes: bytes, filename: str, content_type: str, shop_id=1) -> dict:
    """Transcribe audio with Groq Whisper (confidence gate + deduplication),
    run the AI pipeline, and generate gTTS audio."""
    if not client:
        raise RuntimeError("Configure GROQ_API_KEY to process voice commands.")

    # ---------------------------------------------------------
    # Layer 1: Whisper Confidence Gate & Deduplication
    # ---------------------------------------------------------
    transcription = client.audio.transcriptions.create(
        model="whisper-large-v3",
        file=(filename, audio_bytes, content_type),
        response_format="verbose_json",
        temperature=0.0,
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
            inventory=read_inventory(shop_id),
            audio_base64=generate_tts_base64(msg, "hi"),
        )

    result = run_ai_pipeline(transcript, shop_id)
    result["reply_local"] = deduplicate_sentences(result["reply_local"])
    audio_b64 = generate_tts_base64(result["reply_local"], result["detected_lang"])
    return {**result, "transcript": transcript, "audio_base64": audio_b64}
