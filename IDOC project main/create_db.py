"""create_db.py -- synthetic SAP master data for demo/test.
Run with: python create_db.py [--force]
"""
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "sap_sim.db"))

SCHEMA = """
CREATE TABLE partners (
    partner_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    process_code  TEXT NOT NULL
);
CREATE TABLE materials (
    customer_material  TEXT PRIMARY KEY,
    sap_material        TEXT NOT NULL,
    blocked             INTEGER NOT NULL DEFAULT 0,
    unit_price           REAL NOT NULL,
    available_qty        REAL NOT NULL
);
CREATE TABLE customers (
    sold_to        TEXT PRIMARY KEY,
    credit_limit    REAL NOT NULL,
    credit_used     REAL NOT NULL DEFAULT 0
);
CREATE TABLE sales_orders (
    order_number      TEXT PRIMARY KEY,
    buyer_po_number   TEXT UNIQUE NOT NULL,
    partner_id        TEXT NOT NULL,
    total_value        REAL NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE idoc_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idoc_number     TEXT NOT NULL,
    buyer_po_number TEXT NOT NULL,
    status_code     TEXT NOT NULL,
    description      TEXT NOT NULL,
    ts               TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def build(db_path: str = DB_PATH, force: bool = False) -> None:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    if db_file.exists() and not force:
        print(f"⚠️  {db_file} already exists. Re-run with --force to rebuild.")
        return

    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        "DROP TABLE IF EXISTS partners; DROP TABLE IF EXISTS materials; "
        "DROP TABLE IF EXISTS customers; DROP TABLE IF EXISTS sales_orders; "
        "DROP TABLE IF EXISTS idoc_log;" + SCHEMA
    )

    # ── Partners ──────────────────────────────────────────────────────────────
    conn.executemany(
        "INSERT INTO partners VALUES (?, ?, ?)",
        [
            ("CUST_1001", "Walmart",  "ORDE"),
            ("CUST_1002", "Target",   "ORDE"),
            ("CUST_1003", "Costco",   "ORDE"),
            # CUST_9999 intentionally omitted → "unknown partner" scenario
        ],
    )

    # ── Materials ─────────────────────────────────────────────────────────────
    # (customer_material, sap_material, blocked, unit_price, available_qty)
    conn.executemany(
        "INSERT INTO materials VALUES (?, ?, ?, ?, ?)",
        [
            ("WIDGET-X",        "MAT-10023", 0, 120.00, 1000),  # happy path
            ("GADGET-Y",        "MAT-10088", 0,  45.50,  500),  # secondary happy path
            ("WIDGET-BLOCKED",  "MAT-99999", 1,  75.00,  200),  # blocked material
            ("WIDGET-LOWSTOCK", "MAT-10500", 0,  30.00,    5),  # partial fill (low stock)
            ("WIDGET-ZERO",     "MAT-10600", 0,  55.00,    0),  # zero stock → human replenish
            ("GADGET-PREMIUM",  "MAT-10700", 0, 200.00,  100),  # for price-mismatch demo
        ],
    )

    # ── Customers ─────────────────────────────────────────────────────────────
    # (sold_to, credit_limit, credit_used)
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?)",
        [
            ("200050",  50000.00, 10000.00),   # healthy credit
            ("200099",   1000.00,   950.00),   # near credit limit
            # 200001 intentionally omitted → "unknown customer" scenario
        ],
    )

    conn.commit()
    conn.close()
    print(
        f"[OK] Created {db_path}\n"
        "Demo scenarios:\n"
        "  CUST_9999       -> unknown partner (no WE20 profile)\n"
        "  WIDGET-BLOCKED  -> material blocked\n"
        "  WIDGET-LOWSTOCK qty>5 -> partial fill (auto-resolved)\n"
        "  WIDGET-ZERO     -> zero stock, human replenishment required\n"
        "  customer 200099 large order -> credit limit exceeded\n"
        "  ship-to 200001  -> unknown customer\n"
        "  GADGET-PREMIUM at wrong price -> price mismatch\n"
        "  past delivery date -> delivery date validation failure\n"
    )


if __name__ == "__main__":
    build(force="--force" in sys.argv)
