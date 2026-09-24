"""
refund_processor.py
--------------------
Executes an approved refund for a given amount (full or partial per
refund_policy.py). Idempotent via try_issue_refund -- safe to call any
number of times for the same order_id.
"""
from __future__ import annotations
from order_lookup import get_order_record, last4_digits, try_issue_refund
from notifications import send_message
from audit_log import safe_log


def process_refund(order_id: str, amount: float) -> tuple[str, bool]:
    """Returns (message_for_customer, newly_processed)."""
    record = get_order_record(order_id)
    if record is None:
        raise ValueError(f"Cannot process refund: order {order_id} not found.")

    issued_now = try_issue_refund(order_id, amount)

    if not issued_now:
        msg = f"Order {order_id} was already refunded previously. No further action was taken."
        print(f"⛔ DUPLICATE REFUND BLOCKED -- order {order_id}")
        safe_log("refund_duplicate_blocked", {"order_id": order_id})
        return msg, False

    last4 = last4_digits(record["account_number"])
    message = (
        f"Your refund of ${amount:.2f} for order {order_id} has been processed and "
        f"sent to the account ending in {last4}."
    )
    send_message(
        subject=f"Refund processed -- {order_id}",
        text_body=message,
        html_body=f"<p>{message}</p>",
    )
    print(f"✅ REFUND SUCCESSFUL -- order {order_id}, ${amount:.2f}, account ending {last4}")
    safe_log("refund_processed", {"order_id": order_id, "amount": amount})
    return message, True
