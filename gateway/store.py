"""The gateway's own storage: the approval queue and the audit log (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_changes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]',
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    write_attempted INTEGER NOT NULL DEFAULT 0,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    result TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_changes_dedupe
    ON pending_changes(dedupe_key) WHERE dedupe_key IS NOT NULL AND status != 'rejected';
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    kind TEXT NOT NULL,
    arguments TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    duration_ms INTEGER
);
"""

# Statuses of a change:
#   pending   waiting for a human
#   applying  a human approved; the write is in flight
#   approved  written to the ERP
#   rejected  discarded by a human
#   failed    the ERP refused it; nothing was written; may be approved again or rejected
#   unknown   the ERP gave no usable answer, so it may or may not be written;
#             approve again to find out (safe), cannot be rejected
RETRYABLE = ("pending", "failed", "unknown")
REJECTABLE = ("pending", "failed")

MAX_ARG_CHARS = 8000
MAX_VALUE_CHARS = 200
# A change left in 'applying' this long means the gateway died mid-apply; it may be retried.
STALE_APPLYING = timedelta(minutes=5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shrink(value: Any, depth: int = 0) -> Any:
    """Cut long strings inside audit arguments, so one huge value cannot push the rest out."""
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else f"{value[:MAX_VALUE_CHARS]}...(+{len(value) - MAX_VALUE_CHARS} chars)"
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {str(k)[:60]: _shrink(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, depth + 1) for v in value[:150]]
    return value


class AlreadyDecided(Exception):
    pass


class Store:
    def __init__(self, db_path: str):
        self._path = db_path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- audit log -------------------------------------------------------
    def audit(self, actor: str, role: str, action: str, kind: str, arguments: Any, outcome: str,
              detail: str | None = None, duration_ms: int | None = None) -> None:
        args_json = json.dumps(_shrink(arguments), default=str, ensure_ascii=False)
        if len(args_json) > MAX_ARG_CHARS:
            args_json = json.dumps({"truncated": args_json[:MAX_ARG_CHARS]})
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, role, action, kind, arguments, outcome, detail, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), actor, role, str(action)[:80], kind, args_json, outcome, (detail or "")[:500] or None, duration_ms),
            )

    def audit_entries(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [dict(r) for r in rows]

    # ---- approval queue --------------------------------------------------
    @staticmethod
    def _change(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        out["meta"] = json.loads(out["meta"])
        out["warnings"] = json.loads(out["warnings"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        out["write_attempted"] = bool(out["write_attempted"])
        out.pop("dedupe_key", None)
        return out

    def add_change(self, kind: str, payload: dict, summary: str, warnings: list[str], requested_by: str,
                   dedupe_key: str | None = None, meta: dict | None = None) -> tuple[dict, bool]:
        """Queue a change. Returns (change, created). If a change with the same dedupe_key exists
        and was not rejected, that one is returned and nothing new is queued."""
        change_id = "chg_" + uuid.uuid4().hex[:12]
        meta = dict(meta or {})
        meta.setdefault("idempotency_key", change_id)
        for _ in range(3):  # the existing row can be rejected between our INSERT and SELECT
            with self._conn() as conn:
                try:
                    conn.execute(
                        "INSERT INTO pending_changes (id, kind, payload, meta, summary, warnings, dedupe_key, requested_by, requested_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (change_id, kind, json.dumps(payload), json.dumps(meta), summary, json.dumps(warnings),
                         dedupe_key, requested_by, _now()),
                    )
                except sqlite3.IntegrityError:
                    row = conn.execute(
                        "SELECT * FROM pending_changes WHERE dedupe_key = ? AND status != 'rejected'", (dedupe_key,)
                    ).fetchone()
                    if row is None:
                        continue
                    return self._change(row), False
                row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
                return self._change(row), True
        raise RuntimeError("could not queue the change; try again")

    def get_change(self, change_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
        return self._change(row) if row else None

    def list_changes(self, status: str | None = "pending") -> list[dict]:
        sql, args = "SELECT * FROM pending_changes", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY requested_at DESC, rowid DESC LIMIT 200", args).fetchall()
        return [self._change(r) for r in rows]

    def claim(self, change_id: str, approver: str) -> dict:
        """Atomically move a change to 'applying'. Exactly one caller wins.

        Allowed from pending, failed, unknown, or an 'applying' that has gone stale.
        Retrying is safe: order creation carries an idempotency key, the ERP allows one
        invoice per order, and a repeated status update is a no-op.
        """
        stale_before = (datetime.now(timezone.utc) - STALE_APPLYING).isoformat(timespec="seconds")
        marks = ",".join("?" * len(RETRYABLE))
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE pending_changes SET status = 'applying', decided_by = ?, decided_at = ?, error = NULL "
                f"WHERE id = ? AND (status IN ({marks}) OR (status = 'applying' AND decided_at < ?))",
                (approver, _now(), change_id, *RETRYABLE, stale_before),
            )
            row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise KeyError(change_id)
        if cur.rowcount != 1:
            raise AlreadyDecided(f"change {change_id} is already {row['status']}")
        return self._change(row)

    def mark_write_attempted(self, change_id: str) -> None:
        """Recorded just before the first write call. From then on the ERP may hold the change,
        so it can never again be reported as 'nothing was written' without the ERP saying so."""
        with self._conn() as conn:
            conn.execute("UPDATE pending_changes SET write_attempted = 1 WHERE id = ?", (change_id,))

    def finish(self, change_id: str, status: str, result: dict | None = None, error: str | None = None) -> dict:
        with self._conn() as conn:
            conn.execute(
                "UPDATE pending_changes SET status = ?, result = ?, error = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None, error, change_id),
            )
            row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
        return self._change(row)

    def reject(self, change_id: str, approver: str, reason: str) -> dict:
        marks = ",".join("?" * len(REJECTABLE))
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE pending_changes SET status = 'rejected', decided_by = ?, decided_at = ?, error = ? "
                f"WHERE id = ? AND status IN ({marks})",
                (approver, _now(), reason[:500], change_id, *REJECTABLE),
            )
            row = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise KeyError(change_id)
        if cur.rowcount != 1:
            if row["status"] == "unknown":
                raise AlreadyDecided(
                    f"change {change_id} has an unknown outcome and may already be in the ERP. "
                    "Approve it again to get the real result before deciding."
                )
            raise AlreadyDecided(f"change {change_id} is already {row['status']}")
        return self._change(row)
