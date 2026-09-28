"""business_rules.py -- ALL deterministic checks that mirror what
IDOC_INPUT_ORDERS would actually enforce. """

from __future__ import annotations
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional
from models import (
    PurchaseOrder850, E1EDP01, E1EDP19, E1EDP20,
    InboundCheckResult, SalesOrder,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "sap_sim.db"))

PRICE_TOLERANCE_PCT = float(os.getenv("PRICE_TOLERANCE_PCT", "0.05"))

def _connect() -> sqlite3.Connection:
    db_file = Path(DB_PATH)
    if not db_file.exists():
        raise RuntimeError(
            f"DB not found at {db_file}. Run `python create_db.py` first."
        )
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn

def get_partner(partner_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_material(customer_material: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM materials WHERE customer_material = ?",
            (customer_material,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_customer(sold_to: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM customers WHERE sold_to = ?", (sold_to,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def existing_order_for_po(buyer_po_number: str) -> Optional[dict]:
    """Idempotency check: has this exact PO already become a sales order?
    Prevents a resent/duplicated EDI 850 from double-creating."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sales_orders WHERE buyer_po_number = ?",
            (buyer_po_number,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def run_inbound_checks(
    po: PurchaseOrder850, ship_to_id: str
) -> tuple[InboundCheckResult, list[E1EDP01], list[E1EDP19], list[E1EDP20], float]:
    """
    Run ALL deterministic business-rule checks.

    Returns
    -------
    check_result    : InboundCheckResult (passed/failures/warnings/partial_fill)
    items_out       : list[E1EDP01]  – confirmed line items
    materials_out   : list[E1EDP19]  – material mapping segments
    schedule_lines  : list[E1EDP20]  – backorder schedule lines (may be empty)
    total           : float          – confirmed order value
    """
    failures: list[str] = []
    warnings: list[str] = []
    items_out: list[E1EDP01] = []
    materials_out: list[E1EDP19] = []
    confirmed_stock = []
    schedule_lines: list[E1EDP20] = []
    partial_fill = False
    partial_fill_details: list[dict] = []
    total = 0.0

    if po.delivery_date:
        try:
            dlv = date.fromisoformat(po.delivery_date)
            if dlv < date.today():
                failures.append(
                    f"Delivery date {po.delivery_date} is in the past. "
                    "Update or remove the DTM*002 segment."
                )
        except ValueError:
            failures.append(f"Delivery date '{po.delivery_date}' is not a valid ISO date.")

    for li in po.items:
        # Qty / price sanity
        if li.quantity <= 0:
            failures.append(
                f"Item {li.item_number} ({li.customer_material}): "
                f"quantity must be > 0, got {li.quantity}."
            )
            continue
        if li.unit_price < 0:
            failures.append(
                f"Item {li.item_number} ({li.customer_material}): "
                f"unit price cannot be negative (got {li.unit_price})."
            )
            continue

        mat = get_material(li.customer_material)
        if mat is None:
            failures.append(
                f"Item {li.item_number}: unknown customer material "
                f"'{li.customer_material}' — no SAP material mapping exists."
            )
            continue

        if mat["blocked"]:
            failures.append(
                f"Item {li.item_number}: material '{li.customer_material}' "
                f"(SAP {mat['sap_material']}) is BLOCKED — cannot be sold "
                "until the quality/regulatory hold is lifted."
            )
            items_out.append(E1EDP01(
                posex=li.item_number, menge=li.quantity, unit_price=li.unit_price
            ))
            materials_out.append(E1EDP19(
                customer_material=li.customer_material,
                sap_material=mat["sap_material"],
                idtnr=mat["sap_material"],
                ktext=li.customer_material,
            ))
            continue

        # Price mismatch check (tolerance-based)
        agreed_price = mat.get("unit_price", 0.0)
        if agreed_price > 0:
            deviation = abs(li.unit_price - agreed_price) / agreed_price
            if deviation > PRICE_TOLERANCE_PCT:
                failures.append(
                    f"Item {li.item_number} ({li.customer_material}): "
                    f"submitted price ${li.unit_price:.2f} deviates {deviation*100:.1f}% "
                    f"from agreed price ${agreed_price:.2f} "
                    f"(tolerance {PRICE_TOLERANCE_PCT*100:.0f}%). "
                    "Reject or obtain a new price agreement."
                )
                continue

        # low-stock handling 
        available = mat["available_qty"]
        confirmed_qty = li.quantity
        backorder_qty = 0.0

        if li.quantity > available:
            if available > 0:
                # AUTO-RESOLVE: partial fill + backorder schedule line
                confirmed_qty = available
                backorder_qty = li.quantity - available
                partial_fill = True
                partial_fill_details.append({
                    "item": li.item_number,
                    "material": li.customer_material,
                    "requested": li.quantity,
                    "confirmed": confirmed_qty,
                    "backordered": backorder_qty,
                })
                warnings.append(
                    f"Item {li.item_number} ({li.customer_material}): "
                    f"only {available} of {li.quantity} units available — "
                    f"{confirmed_qty} confirmed now, {backorder_qty} placed on backorder."
                )
                # Schedule line for the partial delivery
                schedule_lines.append(E1EDP20(
                    posex=li.item_number,
                    wmeng=confirmed_qty,
                    ameng=backorder_qty,
                    edatu=po.delivery_date.replace("-", "") if po.delivery_date else "",
                ))
            else:
                # Zero stock → full backorder, requires human replenishment
                failures.append(
                    f"Item {li.item_number} ({li.customer_material}): "
                    f"zero stock available (requested {li.quantity}). "
                    "Replenishment purchase order or manual stock entry required "
                    "before this order can be confirmed."
                )
                continue
        
        if confirmed_qty > 0:
            confirmed_stock.append(
                (li.customer_material, confirmed_qty)
            )

        items_out.append(E1EDP01(
            posex=li.item_number, menge=confirmed_qty, unit_price=li.unit_price
        ))
        materials_out.append(E1EDP19(
            customer_material=li.customer_material,
            sap_material=mat["sap_material"],
            idtnr=mat["sap_material"],
            ktext=li.customer_material,
        ))
        total += confirmed_qty * li.unit_price

    # ── Customer / credit check ───────────────────────────────────────────────
    customer = get_customer(ship_to_id)
    if customer is None:
        failures.append(
            f"Ship-to/customer '{ship_to_id}' not found in customer master. "
            "An OTC admin must create the customer master record before orders "
            "from this ship-to can be processed."
        )
    else:
        available_credit = customer["credit_limit"] - customer["credit_used"]
        if total > available_credit:
            failures.append(
                f"Credit check failed: confirmed order value ${total:.2f} "
                f"exceeds available credit ${available_credit:.2f} "
                f"(limit ${customer['credit_limit']:.2f}, "
                f"used ${customer['credit_used']:.2f}). "
                "Require finance approval to raise limit or customer to prepay."
            )

    result = InboundCheckResult(
        passed=not failures,
        failures=failures,
        warnings=warnings,
        partial_fill=partial_fill,
        partial_fill_details=partial_fill_details,
    )
    if not failures:
        for customer_material, qty in confirmed_stock:
            ok = consume_stock(customer_material, qty)

            if not ok:
                failures.append(
                    f"Could not consume {qty} units of "
                    f"{customer_material} from inventory."
                )
                break
    return result, items_out, materials_out, schedule_lines, total


# ── Sales order creation ──────────────────────────────────────────────────────

def try_create_sales_order(
    buyer_po_number: str,
    partner_id: str,
    total_value: float,
    backorder_note: str = "",
) -> tuple[Optional[SalesOrder], bool]:
    """Idempotent: UNIQUE constraint on buyer_po_number is the atomic guard."""
    order_number = f"ORD-{abs(hash(buyer_po_number)) % 900000 + 100000}"
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sales_orders "
            "(order_number, buyer_po_number, partner_id, total_value) "
            "VALUES (?, ?, ?, ?)",
            (order_number, buyer_po_number, partner_id, total_value),
        )
        conn.commit()
        return SalesOrder(
            order_number=order_number,
            buyer_po_number=buyer_po_number,
            total_value=total_value,
            backorder_note=backorder_note,
        ), True
    except sqlite3.IntegrityError:
        existing = existing_order_for_po(buyer_po_number)
        if existing:
            return SalesOrder(
                order_number=existing["order_number"],
                buyer_po_number=existing["buyer_po_number"],
                total_value=existing["total_value"],
            ), False
        return None, False
    finally:
        conn.close()

def log_status(
    idoc_number: str, buyer_po_number: str, status_code: str, description: str
) -> None:
    """Never allowed to raise into the pipeline."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO idoc_log "
                "(idoc_number, buyer_po_number, status_code, description) "
                "VALUES (?, ?, ?, ?)",
                (idoc_number, buyer_po_number, status_code, description),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"(idoc_log write failed, non-fatal: {e})")


def get_history(buyer_po_number: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status_code, description, ts "
            "FROM idoc_log WHERE buyer_po_number = ? ORDER BY id",
            (buyer_po_number,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def replenish_stock(customer_material: str, qty_to_add: float) -> bool:
    """
    Increase available_qty for a material (simulates a replenishment PO receipt).
    Returns True if the material was found and updated, False otherwise.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE materials SET available_qty = available_qty + ? "
            "WHERE customer_material = ?",
            (qty_to_add, customer_material),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_stock_level(customer_material: str) -> Optional[float]:
    """Return current available_qty for a material, or None if not found."""
    mat = get_material(customer_material)
    return mat["available_qty"] if mat else None

def consume_stock(customer_material: str, qty: float) -> bool:
    """Subtract confirmed quantity from available stock."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE materials
            SET available_qty = available_qty - ?
            WHERE customer_material = ?
              AND available_qty >= ?
            """,
            (qty, customer_material, qty),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def configure_partner(
    partner_id: str, name: str, process_code: str = "ORDE"
) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO partners (partner_id, name, process_code) "
            "VALUES (?, ?, ?)",
            (partner_id, name, process_code),
        )
        conn.commit()
    finally:
        conn.close()


def add_customer(sold_to: str, credit_limit: float) -> None:
    """Create a new customer master record (ship-to / sold-to)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO customers (sold_to, credit_limit, credit_used) "
            "VALUES (?, ?, 0)",
            (sold_to, credit_limit),
        )
        conn.commit()
    finally:
        conn.close()


def add_material_mapping(
    customer_material: str, sap_material: str, unit_price: float, available_qty: float = 0
) -> None:
    """Map a customer material code to a SAP material number."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO materials "
            "(customer_material, sap_material, blocked, unit_price, available_qty) "
            "VALUES (?, ?, 0, ?, ?)",
            (customer_material, sap_material, unit_price, available_qty),
        )
        conn.commit()
    finally:
        conn.close()


def unblock_material(customer_material: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE materials SET blocked = 0 WHERE customer_material = ?",
            (customer_material,),
        )
        conn.commit()
    finally:
        conn.close()


def raise_credit_limit(sold_to: str, new_limit: float) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE customers SET credit_limit = ? WHERE sold_to = ?",
            (new_limit, sold_to),
        )
        conn.commit()
    finally:
        conn.close()
