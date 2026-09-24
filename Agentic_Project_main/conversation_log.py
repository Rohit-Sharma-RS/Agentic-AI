"""
conversation_log.py
--------------------
Persistent per-order chat history -- lets a customer argue about a
refund across multiple turns, and gives an audit trail ("what did we
tell this customer and when") for their protection.
"""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "support.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id  TEXT NOT NULL,
            role      TEXT NOT NULL,   -- 'customer' or 'agent'
            message   TEXT NOT NULL,
            ts        TEXT NOT NULL DEFAULT (datetime('now', '+5.5 hours'))
        )
        """
    )
    return conn


def get_history(order_id: str) -> list[dict]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT role, message FROM conversation_log WHERE order_id = ? ORDER BY id",
            (order_id.strip().upper(),),
        )
        return [{"role": r, "message": m} for r, m in cur.fetchall()]
    finally:
        conn.close()


def append_turn(order_id: str, role: str, message: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO conversation_log (order_id, role, message) VALUES (?, ?, ?)",
            (order_id.strip().upper(), role, message),
        )
        conn.commit()
    finally:
        conn.close()
