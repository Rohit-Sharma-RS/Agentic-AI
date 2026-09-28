"""edi850_parser.py -- deterministic segment/element parser for a
simplified, representative subset of X12 850.

Segment layout:
  ISA*<partner_id>~
  ST*850*<ctrl>~
  BEG*<purpose>*<po_type>*<buyer_po_number>**<order_date YYYYMMDD>~
  N1*ST*<ship_to_name>*92*<ship_to_id>~
  PO1*<item_number>*<qty>*<uom>*<unit_price>*PE*IN*<customer_material>~  (repeatable)
  DTM*002*<delivery_date YYYYMMDD>~    (optional)
  CTT*<line_count>~
  SE*<segment_count>*<ctrl>~
"""
from __future__ import annotations
import time
from models import PurchaseOrder850, LineItem850

class EDIParseError(ValueError):
    pass


def _ymd(raw: str) -> str:
    """Parse YYYYMMDD → YYYY-MM-DD, raise EDIParseError on bad input."""
    if len(raw) != 8 or not raw.isdigit():
        raise EDIParseError(
            f"Bad date segment: {raw!r} (expected YYYYMMDD). "
            "Check BEG05 (order date) and DTM*002 (delivery date)."
        )
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def parse_edi_850(raw: str) -> PurchaseOrder850:
    """Parse a raw EDI 850 string into a PurchaseOrder850.
    Raises EDIParseError with a descriptive message on any issue.
    """
    segments = [s.strip() for s in raw.strip().split("~") if s.strip()]
    if not segments:
        raise EDIParseError("Empty EDI document — no segments found after splitting on '~'.")

    partner_id    = None
    buyer_po_number = None
    order_date    = None
    ship_to_name  = None
    ship_to_id    = None
    delivery_date = None
    items: list[LineItem850] = []

    for seg in segments:
        parts = seg.split("*")
        tag   = parts[0]
        try:
            if tag == "ISA":
                if len(parts) < 2:
                    raise EDIParseError("ISA segment has no sender ID (element 1 missing).")
                partner_id = parts[1].strip()

            elif tag == "BEG":
                if len(parts) < 6:
                    raise EDIParseError(
                        f"BEG segment requires at least 6 elements, got {len(parts)}: '{seg}'"
                    )
                buyer_po_number = parts[3]
                order_date = _ymd(parts[5])

            elif tag == "N1" and len(parts) > 1 and parts[1] == "ST":
                if len(parts) < 5:
                    raise EDIParseError(
                        f"N1*ST segment requires 5 elements (ST, name, qualifier, ID): '{seg}'"
                    )
                ship_to_name = parts[2]
                ship_to_id   = parts[4]

            elif tag == "PO1":
                if len(parts) < 8:
                    raise EDIParseError(
                        f"PO1 segment requires at least 8 elements "
                        f"(item, qty, uom, price, q1, q2, material): '{seg}'"
                    )
                items.append(LineItem850(
                    item_number=parts[1],
                    quantity=float(parts[2]),
                    unit_price=float(parts[4]),
                    customer_material=parts[7],
                ))

            elif tag == "DTM" and len(parts) > 1 and parts[1] == "002":
                delivery_date = _ymd(parts[2])

        except EDIParseError:
            raise
        except (IndexError, ValueError) as e:
            raise EDIParseError(f"Malformed segment '{seg}': {e}") from e

    # Required-field validation
    missing = [
        name for name, val in [
            ("ISA partner_id",      partner_id),
            ("BEG buyer_po_number", buyer_po_number),
            ("BEG order_date",      order_date),
            ("N1 ship_to_name",     ship_to_name),
            ("N1 ship_to_id",       ship_to_id),
        ]
        if not val
    ]
    if missing:
        raise EDIParseError(
            f"Missing required fields: {', '.join(missing)}. "
            "Check that ISA, BEG, and N1*ST segments are present and well-formed."
        )
    if not items:
        raise EDIParseError(
            "No PO1 line items found. Every valid EDI 850 must have at least one PO1 segment."
        )

    return PurchaseOrder850(
        partner_id=partner_id,
        buyer_po_number=buyer_po_number,
        order_date=order_date,
        ship_to_name=ship_to_name,
        ship_to_id=ship_to_id,
        delivery_date=delivery_date,
        items=items,
    )


def _new_po() -> str:
    """Generate a unique PO number for demo submissions."""
    return f"PO-{int(time.time()) % 1_000_000:06d}"


def sample_edi_850(
    partner_id: str   = "CUST_1001",
    po_number: str    = "",
    ship_to_id: str   = "200050",
    material: str     = "WIDGET-X",
    qty: float        = 50,
    unit_price: float = 120.00,
    delivery_date: str = "20271201",   # YYYYMMDD, default well into future
) -> str:
    """Generate a single-item EDI 850 for demo/test purposes."""
    po = po_number or _new_po()
    dlv_seg = f"DTM*002*{delivery_date}~\n" if delivery_date else ""
    return (
        f"ISA*{partner_id}~\n"
        f"ST*850*0001~\n"
        f"BEG*00*NE*{po}**20260115~\n"
        f"N1*ST*Ship To Customer*92*{ship_to_id}~\n"
        f"PO1*000010*{qty}*EA*{unit_price}*PE*IN*{material}~\n"
        f"{dlv_seg}"
        f"CTT*1~\n"
        f"SE*7*0001~"
    )


class LineItemSpec:
    """Simple spec for one line item in a multi-item EDI 850."""
    __slots__ = ("material", "qty", "unit_price")
    def __init__(self, material: str, qty: float, unit_price: float):
        self.material   = material
        self.qty        = qty
        self.unit_price = unit_price


def sample_edi_850_multi(
    partner_id: str = "CUST_1001",
    po_number: str  = "",
    ship_to_id: str = "200050",
    line_items: list | None = None,
    delivery_date: str = "20271201",
) -> str:
    """
    Generate a multi-item EDI 850 (multiple PO1 segments).

    Default: WIDGET-X + GADGET-Y if line_items not provided.
    Each item gets its own 6-digit item number (000010, 000020, ...).

    Example:
        raw = sample_edi_850_multi(line_items=[
            LineItemSpec("WIDGET-X",        50, 120.00),
            LineItemSpec("GADGET-Y",        30,  45.50),
            LineItemSpec("WIDGET-LOWSTOCK", 20,  30.00),  # triggers partial fill
        ])
    """
    if line_items is None:
        line_items = [
            LineItemSpec("WIDGET-X",  50, 120.00),
            LineItemSpec("GADGET-Y",  30,  45.50),
        ]

    po = po_number or _new_po()
    dlv_seg = f"DTM*002*{delivery_date}~\n" if delivery_date else ""

    po1_lines = ""
    for i, item in enumerate(line_items, start=1):
        item_num = f"{i * 10:06d}"    # 000010, 000020, ...
        po1_lines += (
            f"PO1*{item_num}*{item.qty}*EA*{item.unit_price}*PE*IN*{item.material}~\n"
        )

    total_segs = 4 + len(line_items) + (1 if delivery_date else 0) + 2
    return (
        f"ISA*{partner_id}~\n"
        f"ST*850*0001~\n"
        f"BEG*00*NE*{po}**20260115~\n"
        f"N1*ST*Ship To Customer*92*{ship_to_id}~\n"
        f"{po1_lines}"
        f"{dlv_seg}"
        f"CTT*{len(line_items)}~\n"
        f"SE*{total_segs}*0001~"
    )
