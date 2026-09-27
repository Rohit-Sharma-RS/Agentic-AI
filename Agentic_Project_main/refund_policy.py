"""
refund_policy.py
-----------------
Deterministic policy math -- the LLM explains this, it never computes
or invents it. Full refund if damaged/defective or requested within
FULL_REFUND_DAYS of purchase; otherwise 50% partial refund.
"""
from __future__ import annotations
from datetime import date

FULL_REFUND_DAYS = 15
PARTIAL_REFUND_FRACTION = 0.5


def compute_eligible_amount(order_record: dict) -> tuple[float, str]:
    amount = order_record["refund_amount"]
    reason = order_record["reason"]

    if reason == "damaged item":
        return amount, "full refund (damaged items are always fully refundable regardless of timing)"

    purchase = date.fromisoformat(order_record.get("purchase_date") or order_record["request_date"])
    requested = date.fromisoformat(order_record["request_date"])
    days_elapsed = (requested - purchase).days

    if days_elapsed > FULL_REFUND_DAYS:
        partial = round(amount * PARTIAL_REFUND_FRACTION, 2)
        return partial, (
            f"partial refund (50% of ${amount:.2f}) -- requested {days_elapsed} days after "
            f"purchase, past the {FULL_REFUND_DAYS}-day full-refund window"
        )

    return amount, f"full refund (requested {days_elapsed} days after purchase, within the {FULL_REFUND_DAYS}-day window)"


def risk_level_for_amount(amount: float) -> str:
    if amount > 500:
        return "high"
    if amount > 100:
        return "medium"
    return "low"
