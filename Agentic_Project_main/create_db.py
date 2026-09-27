"""
create_db.py
------------
refund_issued/refund_issued_at = idempotency lock (see order_lookup.py).
purchase_date (new) + request_date together drive the partial-refund
policy (see refund_policy.py): random rows always purchase_date close
to request_date (full refund eligible, unaffected). A handful of
HARDCODED demo orders (ORD-90001+) are purchase_date 20+ days before
request_date with reason "changed my mind"/"not as described", to
demonstrate the 50% partial-refund negotiation flow on demand.

    python create_db.py
    python create_db.py --force   # required once the DB has rows
"""
import os
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "support.db"))

CATEGORIES = ["electronics", "subscription", "shipping", "warranty", "accessories", "software"]
REASONS = [
    "damaged item", "changed my mind", "late shipment",
    "wrong item sent", "duplicate charge", "not as described",
]
STATUSES = ["approved", "rejected", "pending"]

SCHEMA = """
CREATE TABLE refund_requests (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id              TEXT UNIQUE NOT NULL,
    customer_name         TEXT NOT NULL,
    phone_number          TEXT NOT NULL,
    account_number        TEXT NOT NULL,
    product_category      TEXT NOT NULL,
    refund_amount         REAL NOT NULL,
    reason                TEXT NOT NULL,
    status                TEXT NOT NULL,
    resolved_by           TEXT NOT NULL,
    risk_level            TEXT NOT NULL,
    purchase_date         TEXT NOT NULL,
    request_date          TEXT NOT NULL,
    refund_issued         INTEGER NOT NULL DEFAULT 0,
    refund_issued_at      TEXT,
    refund_amount_issued  REAL
)
"""

INSERT_SQL = """
INSERT INTO refund_requests
    (order_id, customer_name, phone_number, account_number,
     product_category, refund_amount, reason, status, resolved_by,
     risk_level, purchase_date, request_date, refund_issued, refund_issued_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def risk_level(amount: float) -> str:
    if amount > 500:
        return "high"
    if amount > 100:
        return "medium"
    return "low"


def _phone(i: int) -> str:
    return f"+1-555-{(i * 137) % 10000:04d}"


def _account(i: int) -> str:
    r = random.Random(1000 + i)
    return "".join(r.choice("0123456789") for _ in range(16))


def _existing_row_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _demo_partial_refund_rows() -> list[tuple]:
    """Hardcoded orders to demonstrate the negotiation flow: purchased
    20-30 days before the refund was requested, reason is NOT
    'damaged item', so refund_policy.compute_eligible_amount() will
    return 50% instead of a full refund."""
    demo = [
        # order_id_num, customer, category, amount, reason, purchase_days_before_request
        (90001, "Priya Sharma", "electronics", 240.00, "changed my mind", 22),
        (90002, "Arjun Mehta", "accessories", 89.50, "not as described", 25),
        (90003, "Fatima Khan", "software", 620.00, "changed my mind", 30),
    ]
    today = date(2026, 9, 17)
    rows = []
    for order_num, name, category, amount, reason, gap in demo:
        i = order_num
        req_date = today - timedelta(days=2)
        purchase_date = req_date - timedelta(days=gap)
        rows.append((
            f"ORD-{i:05d}", name, _phone(i), _account(i),
            category, amount, reason, "pending", "ai",
            risk_level(amount), purchase_date.isoformat(), req_date.isoformat(),
            0, None,
        ))
    return rows


def build(db_path: str = DB_PATH, n_rows: int = 120, seed: int = 42, force: bool = False) -> None:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    existing = _existing_row_count(db_file)
    if existing > 0 and not force:
        print(
            f"⚠️  {db_file} already has {existing} rows, including refund_issued state.\n"
            f"    Refusing to overwrite. Re-run with --force to rebuild anyway."
        )
        return

    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS refund_requests")
    cur.execute(SCHEMA)

    start = date(2026, 1, 1)
    rows = []
    for i in range(n_rows):
        r = random.Random(seed * 1000 + i)
        amount = round(r.choice([r.uniform(5, 100), r.uniform(100, 500), r.uniform(500, 900)]), 2)
        req_date = start + timedelta(days=r.randint(0, 250))
        purchase_date = req_date - timedelta(days=r.randint(0, 10))  # always within the 15-day policy window
        status = r.choice(STATUSES)
        refund_issued = 1 if status == "approved" else 0
        refund_issued_at = req_date.isoformat() if refund_issued else None

        rows.append((
            f"ORD-{i + 1:05d}", f"Customer {i + 1:03d}", _phone(i), _account(i),
            r.choice(CATEGORIES), amount, r.choice(REASONS), status, r.choice(["ai", "human"]),
            risk_level(amount), purchase_date.isoformat(), req_date.isoformat(),
            refund_issued, refund_issued_at,
        ))

    rows += _demo_partial_refund_rows()

    cur.executemany(INSERT_SQL, rows)
    conn.commit()
    conn.close()

    already_issued = sum(1 for row in rows if row[12] == 1)
    print(
        f"Created {db_path} with {len(rows)} refund_requests rows "
        f"({already_issued} already refund_issued=1). "
        f"Demo partial-refund orders: ORD-90001, ORD-90002, ORD-90003."
    )


if __name__ == "__main__":
    build(force="--force" in sys.argv)
