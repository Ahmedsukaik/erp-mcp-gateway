"""SQLite storage for the stand-in ERP. Money is stored in integer minor units (pence/cents)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    stock INTEGER NOT NULL CHECK (stock >= 0),
    reorder_level INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'open',
    reference TEXT,
    notes TEXT,
    delivery_date TEXT,
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS order_lines (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_price INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid',
    due_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_CUSTOMERS = [
    ("C001", "Northwind Traders Ltd", "orders@northwind.example"),
    ("C002", "Harbour Cafe Group", "purchasing@harbourcafe.example"),
    ("C003", "Leeds Office Supplies", "buyer@leedsoffice.example"),
]
SEED_PRODUCTS = [
    # sku, name, unit_price (pence), stock, reorder_level
    ("WID-100", "Steel Widget 100mm", 450, 120, 40),
    ("WID-200", "Steel Widget 200mm", 790, 35, 40),
    ("GAD-010", "Control Gadget", 2599, 12, 10),
    ("BOX-050", "Shipping Box (pack of 50)", 1800, 300, 50),
]


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | Path, seed: bool = True) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        if seed and conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            conn.execute("BEGIN")
            conn.executemany("INSERT INTO customers VALUES (?,?,?)", SEED_CUSTOMERS)
            conn.executemany("INSERT INTO products VALUES (?,?,?,?,?)", SEED_PRODUCTS)
            # One historic order with an unpaid invoice, so read tools have data on day one.
            conn.execute(
                "INSERT INTO orders (customer_id, status, reference, total) VALUES ('C002','shipped','PO-7001', 9000)"
            )
            conn.execute("INSERT INTO order_lines VALUES (1,'BOX-050',5,1800)")
            conn.execute(
                "INSERT INTO invoices (order_id, amount, status, due_date) VALUES (1, 9000, 'unpaid', date('now','-5 day'))"
            )
            conn.execute("COMMIT")
