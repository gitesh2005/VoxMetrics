import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NamedTuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "voxmetrics.db"

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


def initialize_database():
    """Ensure the data directory exists, create the inventory table, and seed initial rows if empty."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT UNIQUE,
                quantity REAL,
                unit TEXT,
                price_per_unit REAL,
                price REAL GENERATED ALWAYS AS (price_per_unit) VIRTUAL,
                cost_price REAL,
                units_sold REAL
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(inventory)")}
        if "price_per_unit" not in columns:
            connection.execute("ALTER TABLE inventory RENAME COLUMN price TO price_per_unit")
            columns.remove("price")
        if "price" not in columns:
            connection.execute("ALTER TABLE inventory ADD COLUMN price REAL GENERATED ALWAYS AS (price_per_unit) VIRTUAL")
        if "cost_price" not in columns:
            connection.execute("ALTER TABLE inventory ADD COLUMN cost_price REAL")
        if connection.execute("SELECT 1 FROM inventory LIMIT 1").fetchone() is None:
            connection.executemany("""
                INSERT INTO inventory (item_name, quantity, unit, price_per_unit, units_sold)
                VALUES (?, ?, ?, ?, ?)
            """, SEED_INVENTORY)


def read_inventory():
    """Return all inventory rows ordered by id."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT id, item_name, quantity, unit, price, price_per_unit, units_sold
            FROM inventory
            ORDER BY id
        """)
        return [dict(row) for row in rows]


class UnknownInventoryItem(ValueError):
    pass


class StockChange(NamedTuple):
    item_name: str
    before: float
    after: float
    unit: str


def update_inventory_batch(items: list[tuple[str, float, str]]) -> list[StockChange]:
    """Validate a command and record each stock or selling-price change under one write lock."""
    if not items or any(not math.isfinite(value) or value <= 0 for _, value, _ in items):
        raise ValueError("Expected a positive finite amount for every item")
    if any(action not in {"ADD", "SELL", "UPDATE_PRICE"} for _, _, action in items):
        raise ValueError("Unknown inventory action")

    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        stock = {name for (name,) in db.execute("SELECT item_name FROM inventory")}
        for name, _, _ in items:
            if name not in stock:
                raise UnknownInventoryItem(name)

        changes = []
        for name, value, action in items:
            before_qty, before_price, unit = db.execute(
                "SELECT quantity, price_per_unit, unit FROM inventory WHERE item_name = ?", (name,)
            ).fetchone()
            if action == "SELL":
                db.execute(
                    "UPDATE inventory SET quantity = MAX(0, quantity - ?), units_sold = units_sold + ? WHERE item_name = ?",
                    (value, value, name),
                )
            elif action == "ADD":
                db.execute("UPDATE inventory SET quantity = quantity + ? WHERE item_name = ?", (value, name))
            else:
                db.execute("""
                    UPDATE inventory
                    SET price_per_unit = :price,
                        cost_price = CASE WHEN cost_price > :price THEN
                            CASE WHEN price_per_unit > cost_price THEN (cost_price / price_per_unit) * :price
                                 ELSE :price END
                            ELSE cost_price END
                    WHERE item_name = :name
                """, {"price": value, "name": name})
            after_qty, after_price, sold = db.execute(
                "SELECT quantity, price_per_unit, units_sold FROM inventory WHERE item_name = ?", (name,)
            ).fetchone()
            before, after = (before_price, after_price) if action == "UPDATE_PRICE" else (before_qty, after_qty)
            if not math.isfinite(after) or not math.isfinite(sold):
                raise ValueError("Inventory total exceeds the supported range")
            changes.append(StockChange(name, before, after, unit))
        return changes


def update_inventory_stock(item_name: str, qty: float, is_sale: bool):
    """Apply one stock update and return (before, after, unit)."""
    try:
        change, = update_inventory_batch([(item_name, qty, "SELL" if is_sale else "ADD")])
    except UnknownInventoryItem:
        return None, None, None
    return change.before, change.after, change.unit


def execute_analytics_query(sql: str) -> list[dict]:
    """Execute a read-only authorized SELECT query and return chart rows with units."""
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        columns = {"item_name", "quantity", "unit", "price", "price_per_unit", "units_sold", ""}
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
