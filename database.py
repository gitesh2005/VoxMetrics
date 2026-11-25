import sqlite3
from contextlib import closing
from pathlib import Path


DB_PATH = Path(__file__).with_name("voxmetrics.db")
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
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT UNIQUE,
                quantity REAL,
                unit TEXT,
                price REAL,
                units_sold REAL
            )
        """)
        if connection.execute("SELECT 1 FROM inventory LIMIT 1").fetchone() is None:
            connection.executemany("""
                INSERT INTO inventory (item_name, quantity, unit, price, units_sold)
                VALUES (?, ?, ?, ?, ?)
            """, SEED_INVENTORY)


def read_inventory():
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT id, item_name, quantity, unit, price, units_sold
            FROM inventory
            ORDER BY id
        """)
        return [dict(row) for row in rows]
