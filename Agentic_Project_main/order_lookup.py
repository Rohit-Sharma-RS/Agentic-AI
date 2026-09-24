"""
order_lookup.py
----------------
Direct, plain-Python SQLite access -- deliberately NOT an LLM tool and
NOT reachable through the guarded SQLAgent. This is the one place in
the whole codebase that reads account_number/phone_number off disk.

The rule enforced here: raw account_number and phone_number never leave
this module except through last4_digits() (a one-way, irreversible
transform). Everything else in the app -- prompts, PipelineResult,
the browser, notifications, console output -- only ever sees the
masked/last-4 form or no account info at all.
"""
from __future__ import annotations
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

# Anchored to this file's own folder, NOT the process's working directory.
# A bare "data/support.db" only resolves if you happen to launch Python
# from inside the project folder -- launch it any other way (double-click,
# a different shell, an IDE run button) and that relative path points
# nowhere, which is exactly what causes "unable to open database file".
PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "support.db"))

# Format enforced for every order ID a customer types in: ORD- + 5 digits
ORDER_ID_PATTERN = re.compile(r"^ORD-\d{5}$")

# Fields that must NEVER be forwarded into an LLM prompt or a PipelineResult
_SENSITIVE_FIELDS = {"account_number", "phone_number"}


def is_valid_order_id_format(order_id: str) -> bool:
    return bool(ORDER_ID_PATTERN.match(order_id.strip().upper()))


def _connect() -> sqlite3.Connection:
    """Centralized connect with a clear error if the DB hasn't been
    built yet, instead of sqlite3's cryptic 'unable to open database
    file' (which is what you get from a missing parent directory too)."""
    db_path = Path(DB_PATH)
    if not db_path.exists():
        raise RuntimeError(
            f"Database not found at {db_path}. Run `python create_db.py` "
            f"from the project folder first (or set DB_PATH in .env to the "
            f"correct absolute path)."
        )
    return sqlite3.connect(str(db_path))


def get_order_record(order_id: str) -> Optional[dict]:
    """Full row, INCLUDING sensitive fields. Callers other than
    refund_processor.py should immediately pass this through
    safe_fields_for_llm() before using it anywhere near a prompt."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM refund_requests WHERE order_id = ?",
            (order_id.strip().upper(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def safe_fields_for_llm(record: dict) -> dict:
    """Strips account_number and phone_number. Everything left is
    ordinary business data (order id, category, amount, reason,
    status, risk level, date) the LLM can reason about safely."""
    return {k: v for k, v in record.items() if k not in _SENSITIVE_FIELDS}


def last4_digits(account_number: str) -> str:
    """One-way transform: full account number in, last 4 digits out.
    This is the ONLY representation of the account number that is ever
    allowed into a notification, the UI, or a console print."""
    digits = "".join(ch for ch in account_number if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else "????"


def mark_order_rejected(order_id: str) -> None:
    """Guarded: refuses to reject an order that's already been refunded
    (refund_issued=1), since that would be a lie -- money already moved.
    Raises ValueError in that case rather than silently overwriting status."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE refund_requests SET status = 'rejected' "
            "WHERE order_id = ? AND refund_issued = 0",
            (order_id.strip().upper(),),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(
                f"Cannot reject order {order_id.strip().upper()}: it has already been "
                f"refunded (or the order ID doesn't exist)."
            )
    finally:
        conn.close()


def try_issue_refund(order_id: str, amount: float) -> bool:
    """THE idempotency lock. Atomically flips refund_issued 0 -> 1 (and
    records the actual amount paid -- may be a partial amount) in a
    single UPDATE ... WHERE refund_issued = 0 statement, reporting via
    rowcount whether THIS call is the one that flipped it.

    Safe under repeated calls (double-click, re-run, clicking Approve
    twice -- every call after the first returns False, no side effects)
    and concurrent calls (SQLite serializes writes; only one UPDATE can
    match WHERE refund_issued = 0).

    Callers MUST branch on the return value: True = you're responsible
    for notifying the customer; False = someone already did this.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE refund_requests "
            "SET refund_issued = 1, refund_issued_at = datetime('now', '+5.5 hours'), "
            "    status = 'approved', refund_amount_issued = ? "
            "WHERE order_id = ? AND refund_issued = 0",
            (amount, order_id.strip().upper()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def is_already_refunded(order_record: dict) -> bool:
    """Cheap check against an already-fetched record (no extra query) --
    used by the orchestrator to short-circuit BEFORE running any agent
    or LLM call at all for an order that's already been paid out."""
    return bool(order_record.get("refund_issued"))
