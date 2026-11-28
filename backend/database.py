import hashlib
import hmac
import secrets
import logging
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import NamedTuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "voxmetrics.db"
logger = logging.getLogger("voxmetrics")

SEED_INVENTORY = (
    ("curd", 60, "packets", 20, 25),
    ("sugar", 100, "kg", 45, 60),
    ("milk", 60, "L", 22, 50),
    ("rice", 80, "kg", 55, 40),
    ("tea", 30, "packets", 180, 15),
    ("lentils", 50, "kg", 90, 30),
    ("oil", 25, "L", 130, 20),
    ("salt", 70, "kg", 18, 55),
)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(hashlib.sha256().name, password.encode(), bytes.fromhex(salt), 310_000)
    return f"{salt}:{digest.hex()}"


def find_shop(phone):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM shops WHERE phone=?", (phone,)).fetchone()


def authenticate_shop(phone, password):
    shop = find_shop(phone)
    if shop and hmac.compare_digest(shop["password_hash"], password_hash(password, shop["password_hash"].split(":")[0])):
        return shop
    return None


def seed_new_shop(shop_id, db=None):
    if db is None:
        with closing(sqlite3.connect(DB_PATH)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            seed_new_shop(shop_id, conn)
        return
    if not db.execute("SELECT 1 FROM inventory WHERE shop_id=? LIMIT 1", (shop_id,)).fetchone():
        db.executemany("""
            INSERT INTO inventory(shop_id,item_name,quantity,unit,price_per_unit,units_sold)
            VALUES (?,?,?,?,?,?)
        """, [(shop_id, *row) for row in SEED_INVENTORY])
    if not db.execute("SELECT 1 FROM activity_logs WHERE shop_id=? LIMIT 1", (shop_id,)).fetchone():
        _log_activity(db, "WELCOME", "Welcome to VoxMetrics. Your shop ledger is ready.", "", shop_id)


def register_shop(phone, shop_name, password):
    digest = password_hash(password)
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        shop_id = db.execute("INSERT INTO shops(phone,shop_name,password_hash) VALUES (?,?,?)",
                             (phone, shop_name, digest)).lastrowid
        seed_new_shop(shop_id, db)
        return shop_id


def init_db():
    """Move existing single-store rows into the demo shop in one transaction."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE NOT NULL, shop_name TEXT,
            password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        if not db.execute("SELECT 1 FROM shops WHERE id=1").fetchone():
            db.execute("INSERT INTO shops(id,phone,shop_name,password_hash) VALUES (1,?,?,?)",
                       ("9876543210", "Gupta Kirana Store", password_hash("1234")))

        inventory_cols = {r[1] for r in db.execute("PRAGMA table_xinfo(inventory)")}
        migrate_inventory = inventory_cols and "shop_id" not in inventory_cols
        if migrate_inventory:
            db.execute("ALTER TABLE inventory RENAME TO inventory_v1")
        db.execute("""CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL DEFAULT 1,
            item_name TEXT, quantity REAL, unit TEXT, price_per_unit REAL,
            price REAL GENERATED ALWAYS AS (price_per_unit) VIRTUAL, cost_price REAL,
            units_sold REAL, UNIQUE(shop_id,item_name))""")
        if migrate_inventory:
            price = "price_per_unit" if "price_per_unit" in inventory_cols else "price"
            cost = "cost_price" if "cost_price" in inventory_cols else "NULL"
            db.execute(f"""INSERT INTO inventory(id,item_name,quantity,unit,price_per_unit,cost_price,units_sold)
                           SELECT id,item_name,quantity,unit,{price},{cost},units_sold FROM inventory_v1""")
            db.execute("DROP TABLE inventory_v1")

        ledger_cols = {r[1] for r in db.execute("PRAGMA table_info(khata_ledger)")}
        migrate_ledger = ledger_cols and "shop_id" not in ledger_cols
        if migrate_ledger:
            db.execute("ALTER TABLE khata_ledger RENAME TO khata_v1")
        db.execute("""CREATE TABLE IF NOT EXISTS khata_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL DEFAULT 1,
            customer_key TEXT, customer_name TEXT, last4_digits TEXT, balance REAL DEFAULT 0,
            notes TEXT DEFAULT '', updated_at TEXT, UNIQUE(shop_id,customer_key))""")
        if migrate_ledger:
            fields = "id,customer_key,customer_name,last4_digits,balance,updated_at"
            if "notes" in ledger_cols:
                fields += ",notes"
            db.execute(f"INSERT INTO khata_ledger({fields}) SELECT {fields} FROM khata_v1")
            db.execute("DROP TABLE khata_v1")
        db.execute("""CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL DEFAULT 1,
            action_type TEXT, summary TEXT, raw_command TEXT, created_at TEXT)""")
        if "shop_id" not in {r[1] for r in db.execute("PRAGMA table_info(activity_logs)")}:
            db.execute("ALTER TABLE activity_logs ADD COLUMN shop_id INTEGER NOT NULL DEFAULT 1")
        seed_new_shop(1, db)
        if not db.execute("SELECT 1 FROM khata_ledger WHERE shop_id=1 LIMIT 1").fetchone():
            db.executemany("""
                INSERT INTO khata_ledger(shop_id,customer_key,customer_name,last4_digits,balance,updated_at)
                VALUES (1,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """, [(f"{n.casefold()}:{d}", n, d, b) for n, d, b in
                  [("Ramesh", "4521", 450), ("Suresh", "8812", 1200), ("Meena", "3094", 280)]])


initialize_database = init_db


def _log_activity(db, action, summary, raw_command, shop_id):
    db.execute("""
        INSERT INTO activity_logs(shop_id, action_type, summary, raw_command, created_at)
        VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    """, (shop_id, action, summary, raw_command))


def get_khata_ledger(shop_id=1) -> list[dict]:
    try:
        with closing(sqlite3.connect(DB_PATH)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM khata_ledger WHERE shop_id=? ORDER BY customer_key", (shop_id,))]
    except sqlite3.Error:
        logger.exception("Could not read khata ledger")
        return []


def get_recent_logs(limit=15, *, shop_id=1) -> list[dict]:
    try:
        with closing(sqlite3.connect(DB_PATH)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM activity_logs WHERE shop_id=? ORDER BY id DESC LIMIT ?", (shop_id, max(0, limit))
            )]
    except sqlite3.Error:
        logger.exception("Could not read activity log")
        return []


def log_khata_query(summary, raw_command, shop_id=1):
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        _log_activity(db, "KHATA_QUERY", summary, raw_command, shop_id)


def update_khata(customer_name, last4_digits, amount, mode, *, raw_command="", shop_id=1):
    name = " ".join(unicodedata.normalize("NFC", customer_name or "").split())
    if (not name or not isinstance(last4_digits, str) or not re.fullmatch("[0-9]{4}", last4_digits)
            or type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0
            or round(amount, 2) != amount or mode not in {"UDHAAR", "JAMA"}):
        raise ValueError("Specify a customer name, four digits, mode and positive rupee amount in paise precision.")
    key = f"{name.casefold()}:{last4_digits}"
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        delta = amount if mode == "UDHAAR" else -amount
        db.execute("""
            INSERT INTO khata_ledger(shop_id, customer_key, customer_name, last4_digits, balance, updated_at)
            VALUES (?, ?, ?, ?, MAX(0, ?), strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(shop_id,customer_key) DO UPDATE SET
                balance = MAX(0, ROUND(balance + ?, 2)), updated_at = excluded.updated_at
        """, (shop_id, key, name, last4_digits, delta, delta))
        row = dict(db.execute("SELECT * FROM khata_ledger WHERE shop_id=? AND customer_key = ?", (shop_id, key)).fetchone())
        if not math.isfinite(row["balance"]):
            raise ValueError("Balance exceeds the supported range")
        _log_activity(db, mode, f"{row['customer_name']} - {last4_digits} · ₹{amount:.2f} · balance ₹{row['balance']:.2f}", raw_command, shop_id)
        return row


def customer_details(shop_id, customer_name, digits, *, details=None, raw_command=""):
    name = " ".join(unicodedata.normalize("NFC", customer_name).split())
    if not name or not re.fullmatch("[0-9]{4}", digits):
        raise ValueError("Customer name and four digits required")
    key = f"{name.casefold()}:{digits}"
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        if details is not None:
            if not details.strip():
                raise ValueError("Customer details required")
            db.execute("""
                INSERT INTO khata_ledger(shop_id,customer_key,customer_name,last4_digits,notes,updated_at)
                VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(shop_id,customer_key) DO UPDATE SET updated_at=excluded.updated_at,
                    notes=CASE WHEN COALESCE(notes,'')='' THEN excluded.notes
                               ELSE notes || char(10) || excluded.notes END
            """, (shop_id, key, name, digits, details.strip()))
        row = db.execute("SELECT * FROM khata_ledger WHERE shop_id=? AND customer_key=?", (shop_id, key)).fetchone()
        if row:
            action = "CUSTOMER_LOOKUP" if details is None else "CUSTOMER_SAVE"
            _log_activity(db, action, f"{name} - {digits}", raw_command, shop_id)
        return dict(row) if row else None


def read_inventory(shop_id=1):
    """Return all inventory rows ordered by id."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT id, item_name, quantity, unit, price, price_per_unit, units_sold
            FROM inventory WHERE shop_id=?
            ORDER BY id
        """, (shop_id,))
        return [dict(row) for row in rows]


class UnknownInventoryItem(ValueError):
    pass


class IncompleteNewItem(ValueError):
    pass


def get_existing_items(shop_id=1) -> list[str]:
    with closing(sqlite3.connect(DB_PATH)) as db:
        return [name for (name,) in db.execute("SELECT DISTINCT item_name FROM inventory WHERE shop_id=? ORDER BY item_name", (shop_id,))]


class InventoryWrite(NamedTuple):
    name: str
    value: float
    action: str
    unit: str | None = None
    price: float | None = None


class StockChange(NamedTuple):
    item_name: str
    before: float
    after: float
    unit: str


class ProductChange(NamedTuple):
    item_name: str
    before: float
    after: float
    unit: str
    price: float
    created: bool


def update_inventory_batch(items: list[InventoryWrite | tuple[str, float, str]], *, raw_command="", shop_id=1) -> list[StockChange | ProductChange]:
    """Validate a command and record each stock or selling-price change under one write lock."""
    writes = [InventoryWrite(*item) for item in items]
    if not writes or any(not math.isfinite(w.value) or w.value <= 0 for w in writes):
        raise ValueError("Expected a positive finite amount for every item")
    if any(w.action not in {"ADD", "SELL", "UPDATE_PRICE", "CREATE_ITEM"} for w in writes):
        raise ValueError("Unknown inventory action")

    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        stock = dict(db.execute("SELECT item_name, unit FROM inventory WHERE shop_id=?", (shop_id,)))
        for w in writes:
            if w.action == "CREATE_ITEM":
                if w.price is not None and (not math.isfinite(w.price) or w.price <= 0):
                    raise IncompleteNewItem(w.name)
                if w.name not in stock and (w.price is None or w.unit not in {"kg", "L", "packets", "pieces"}):
                    raise IncompleteNewItem(w.name)
                if w.name in stock and w.unit is not None and w.unit != stock[w.name]:
                    raise IncompleteNewItem(w.name)
                stock.setdefault(w.name, w.unit)
            elif w.name not in stock and w.action == "ADD":
                raise IncompleteNewItem(w.name)
            elif w.name not in stock:
                raise UnknownInventoryItem(w.name)

        changes = []
        for name, value, action, requested_unit, price in writes:
            row = db.execute(
                "SELECT quantity, price_per_unit, unit FROM inventory WHERE shop_id=? AND item_name = ?", (shop_id, name)
            ).fetchone()
            created = row is None
            if created:
                if action != "CREATE_ITEM":
                    raise UnknownInventoryItem(name)
                before_qty, before_price, unit = 0, None, requested_unit
                db.execute("""
                    INSERT INTO inventory (shop_id, item_name, quantity, unit, price_per_unit, cost_price, units_sold)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                """, (shop_id, name, value, unit, price, round(price * 0.85, 2)))
            else:
                before_qty, before_price, unit = row
            if action == "SELL":
                db.execute(
                    "UPDATE inventory SET quantity = MAX(0, quantity - ?), units_sold = units_sold + ? WHERE shop_id=? AND item_name = ?",
                    (value, value, shop_id, name),
                )
            elif action == "ADD" or (action == "CREATE_ITEM" and not created):
                db.execute("UPDATE inventory SET quantity = quantity + ? WHERE shop_id=? AND item_name = ?", (value, shop_id, name))
            if action == "UPDATE_PRICE" or (action == "CREATE_ITEM" and not created and price is not None):
                db.execute("""
                    UPDATE inventory
                    SET price_per_unit = :price,
                        cost_price = CASE WHEN cost_price > :price THEN
                            CASE WHEN price_per_unit > cost_price THEN (cost_price / price_per_unit) * :price
                                 ELSE :price END
                            ELSE cost_price END
                    WHERE shop_id = :shop_id AND item_name = :name
                """, {"price": value if action == "UPDATE_PRICE" else price, "name": name, "shop_id": shop_id})
            after_qty, after_price, sold = db.execute(
                "SELECT quantity, price_per_unit, units_sold FROM inventory WHERE shop_id=? AND item_name = ?", (shop_id, name)
            ).fetchone()
            before, after = (before_price, after_price) if action == "UPDATE_PRICE" else (before_qty, after_qty)
            if not math.isfinite(after) or not math.isfinite(sold):
                raise ValueError("Inventory total exceeds the supported range")
            if action == "CREATE_ITEM":
                changes.append(ProductChange(name, before, after, unit, after_price, created))
            else:
                changes.append(StockChange(name, before, after, unit))
            summary = f"{name} · {before} → {after} {unit}"
            if action == "UPDATE_PRICE":
                summary = f"{name} · ₹{before} → ₹{after}/{unit}"
            elif action == "CREATE_ITEM":
                summary += f" · ₹{after_price}/{unit}"
            _log_activity(db, action, summary, raw_command, shop_id)
        return changes


def update_inventory_stock(item_name: str, qty: float, is_sale: bool, shop_id=1):
    """Apply one stock update and return (before, after, unit)."""
    try:
        change, = update_inventory_batch([(item_name, qty, "SELL" if is_sale else "ADD")], shop_id=shop_id)
    except (UnknownInventoryItem, IncompleteNewItem):
        return None, None, None
    return change.before, change.after, change.unit


def execute_analytics_query(sql: str, shop_id=1) -> list[dict]:
    """Execute a read-only authorized SELECT query and return chart rows with units."""
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("""CREATE TEMP TABLE inventory AS
                      SELECT item_name,quantity,unit,price,price_per_unit,units_sold
                      FROM main.inventory WHERE shop_id=?""", (shop_id,))
        columns = {"item_name", "quantity", "unit", "price", "price_per_unit", "units_sold", ""}
        reads_inventory = False

        def authorize(action, table, column, schema, source):
            nonlocal reads_inventory
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and schema == "temp" and table == "inventory" and column in columns:
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

        db.set_authorizer(None)
        unit_map = {
            row[0]: row[1]
            for row in db.execute("SELECT item_name, unit FROM inventory").fetchall()
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
        return chart
