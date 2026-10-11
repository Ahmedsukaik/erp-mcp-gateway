"""A small REST API that stands in for a customer's custom ERP.

It exists so the gateway has a real HTTP system to talk to. In a client project
this file is replaced by the client's own ERP API; nothing in `gateway/` imports it.
"""
from __future__ import annotations

import hmac
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .db import connect, init_db

ALLOWED_TRANSITIONS = {
    "open": {"confirmed", "cancelled"},
    "confirmed": {"shipped", "cancelled"},
    "shipped": set(),
    "cancelled": set(),
}


class OrderLineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(gt=0, le=100_000)


class OrderIn(BaseModel):
    customer_id: str
    lines: list[OrderLineIn] = Field(min_length=1, max_length=100)
    reference: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=500)
    delivery_date: date | None = None


class OrderPatch(BaseModel):
    status: Literal["confirmed", "shipped", "cancelled"] | None = None
    delivery_date: date | None = None
    notes: str | None = Field(default=None, max_length=500)


class InvoiceIn(BaseModel):
    order_id: int


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))


def _order_dict(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    lines = conn.execute(
        "SELECT sku, qty, unit_price FROM order_lines WHERE order_id = ?", (order_id,)
    ).fetchall()
    out = {k: row[k] for k in row.keys() if k != "idempotency_key"}
    out["lines"] = [dict(line) for line in lines]
    return out


def create_app(db_path: str | Path, read_key: str, write_key: str) -> FastAPI:
    init_db(db_path)
    app = FastAPI(title="Demo ERP", version="1.0")

    def require_read(x_api_key: str = Header(default="")) -> str:
        if _same(x_api_key, read_key) or _same(x_api_key, write_key):
            return x_api_key
        raise HTTPException(401, "invalid API key")

    def require_write(x_api_key: str = Header(default="")) -> str:
        if _same(x_api_key, write_key):
            return x_api_key
        raise HTTPException(403, "this API key cannot write")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/customers", dependencies=[Depends(require_read)])
    def customers(q: str = Query(default="", max_length=100)) -> list[dict]:
        like = f"%{q.strip().lower()}%"
        with connect(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM customers WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR lower(id) = ? ORDER BY id LIMIT 20",
                (like, like, q.strip().lower()),
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/customers/{customer_id}", dependencies=[Depends(require_read)])
    def customer(customer_id: str) -> dict:
        with connect(db_path) as conn:
            row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"customer {customer_id} not found")
        return dict(row)

    @app.get("/products", dependencies=[Depends(require_read)])
    def products(q: str = Query(default="", max_length=100), low_stock: bool = False) -> list[dict]:
        like = f"%{q.strip().lower()}%"
        sql = "SELECT * FROM products WHERE (lower(name) LIKE ? OR lower(sku) LIKE ?)"
        if low_stock:
            sql += " AND stock <= reorder_level"
        with connect(db_path) as conn:
            rows = conn.execute(sql + " ORDER BY sku LIMIT 50", (like, like)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/products/{sku}", dependencies=[Depends(require_read)])
    def product(sku: str) -> dict:
        with connect(db_path) as conn:
            row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku.upper(),)).fetchone()
        if row is None:
            raise HTTPException(404, f"product {sku} not found")
        return dict(row)

    @app.get("/orders", dependencies=[Depends(require_read)])
    def orders(customer_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT id FROM orders WHERE 1=1", []
        if customer_id:
            sql += " AND customer_id = ?"
            args.append(customer_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        with connect(db_path) as conn:
            ids = [r["id"] for r in conn.execute(sql + " ORDER BY id DESC LIMIT 50", args)]
            return [_order_dict(conn, i) for i in ids]

    @app.get("/orders/{order_id}", dependencies=[Depends(require_read)])
    def order(order_id: int) -> dict:
        with connect(db_path) as conn:
            found = _order_dict(conn, order_id)
        if found is None:
            raise HTTPException(404, f"order {order_id} not found")
        return found

    @app.post("/orders", status_code=201, dependencies=[Depends(require_write)])
    def create_order(body: OrderIn, idempotency_key: str | None = Header(default=None)) -> dict:
        with connect(db_path) as conn:
            # BEGIN IMMEDIATE takes the write lock up front, so the stock check and the
            # stock decrement cannot interleave with another order.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    prior = conn.execute(
                        "SELECT id FROM orders WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                    if prior:
                        conn.execute("ROLLBACK")
                        return _order_dict(conn, prior["id"])
                if conn.execute("SELECT 1 FROM customers WHERE id = ?", (body.customer_id,)).fetchone() is None:
                    raise HTTPException(404, f"customer {body.customer_id} not found")
                wanted: dict[str, int] = {}
                for line in body.lines:
                    wanted[line.sku.upper()] = wanted.get(line.sku.upper(), 0) + line.qty
                total, priced = 0, []
                for sku, qty in wanted.items():
                    prod = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
                    if prod is None:
                        raise HTTPException(404, f"product {sku} not found")
                    if prod["stock"] < qty:
                        raise HTTPException(
                            409, f"insufficient stock for {sku}: requested {qty}, available {prod['stock']}"
                        )
                    priced.append((sku, qty, prod["unit_price"]))
                    total += qty * prod["unit_price"]
                cur = conn.execute(
                    "INSERT INTO orders (customer_id, reference, notes, delivery_date, total, idempotency_key) VALUES (?,?,?,?,?,?)",
                    (
                        body.customer_id,
                        body.reference,
                        body.notes,
                        body.delivery_date.isoformat() if body.delivery_date else None,
                        total,
                        idempotency_key,
                    ),
                )
                order_id = cur.lastrowid
                for sku, qty, price in priced:
                    conn.execute("INSERT INTO order_lines VALUES (?,?,?,?)", (order_id, sku, qty, price))
                    conn.execute("UPDATE products SET stock = stock - ? WHERE sku = ?", (qty, sku))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return _order_dict(conn, order_id)

    @app.patch("/orders/{order_id}", dependencies=[Depends(require_write)])
    def update_order(order_id: int, body: OrderPatch) -> dict:
        with connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if row is None:
                    raise HTTPException(404, f"order {order_id} not found")
                if body.status and body.status != row["status"]:
                    if body.status not in ALLOWED_TRANSITIONS[row["status"]]:
                        raise HTTPException(409, f"cannot move order from {row['status']} to {body.status}")
                    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (body.status, order_id))
                    if body.status == "cancelled":
                        for line in conn.execute("SELECT sku, qty FROM order_lines WHERE order_id = ?", (order_id,)).fetchall():
                            conn.execute("UPDATE products SET stock = stock + ? WHERE sku = ?", (line["qty"], line["sku"]))
                if body.delivery_date is not None:
                    conn.execute("UPDATE orders SET delivery_date = ? WHERE id = ?", (body.delivery_date.isoformat(), order_id))
                if body.notes is not None:
                    conn.execute("UPDATE orders SET notes = ? WHERE id = ?", (body.notes, order_id))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return _order_dict(conn, order_id)

    @app.get("/invoices", dependencies=[Depends(require_read)])
    def invoices(status: str | None = None) -> list[dict]:
        sql, args = "SELECT i.*, o.customer_id FROM invoices i JOIN orders o ON o.id = i.order_id", []
        if status:
            sql += " WHERE i.status = ?"
            args.append(status)
        with connect(db_path) as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY i.due_date LIMIT 100", args)]

    @app.post("/invoices", status_code=201, dependencies=[Depends(require_write)])
    def create_invoice(body: InvoiceIn) -> dict:
        with connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                order_row = conn.execute("SELECT * FROM orders WHERE id = ?", (body.order_id,)).fetchone()
                if order_row is None:
                    raise HTTPException(404, f"order {body.order_id} not found")
                if order_row["status"] == "cancelled":
                    raise HTTPException(409, "cannot invoice a cancelled order")
                if conn.execute("SELECT 1 FROM invoices WHERE order_id = ?", (body.order_id,)).fetchone():
                    raise HTTPException(409, f"order {body.order_id} already has an invoice")
                due = (date.today() + timedelta(days=30)).isoformat()
                cur = conn.execute(
                    "INSERT INTO invoices (order_id, amount, due_date) VALUES (?,?,?)",
                    (body.order_id, order_row["total"], due),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            row = conn.execute("SELECT * FROM invoices WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    return app
