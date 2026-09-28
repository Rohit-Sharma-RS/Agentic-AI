"""ordrsp_generator.py -- deterministic outbound pipeline stage.

Builds:
  SalesOrder + confirmed items + backorder lines
    → ORDRSP IDoc (ORDERS05 outbound, status 01 → 30 → 03 logged by caller)
    → Full-envelope EDI 855 (ISA/GS/ST/BAK/PO1/CTT/SE/GE/IEA)

The EDI 855 is the X12 Purchase Order Acknowledgment that is sent back
to the trading partner after a successful (or partially-confirmed) order.
"""
from __future__ import annotations
import time
from models import SalesOrder, ORDRSP, EDI_DC40, E1EDP01, E1EDP20


# ── ORDRSP IDoc builder ───────────────────────────────────────────────────────

def build_ordrsp(
    order: SalesOrder,
    items: list[E1EDP01],
    sender_partner: str,
    receiver_partner: str,
    backorder_lines: list[E1EDP20] | None = None,
    partial_fill: bool = False,
) -> ORDRSP:
    return ORDRSP(
        control=EDI_DC40(
            direction="1",
            idoctyp="ORDRSP01",
            mestyp="ORDRSP",
            sndpor="SAPPORT100",
            sndprt="LS",
            sndprn=sender_partner,
            rcvpor=f"PORT_{receiver_partner}",
            rcvprt="LS",
            rcvprn=receiver_partner,
            message_type="ORDRSP",
            basic_type="ORDRSP01",
            sender_partner=sender_partner,
            receiver_partner=receiver_partner,
        ),
        order_number=order.order_number,
        buyer_po_number=order.buyer_po_number,
        confirmed_items=items,
        partial_fill=partial_fill,
        backorder_lines=backorder_lines or [],
    )


# ── EDI 855 text generator ────────────────────────────────────────────────────

def _isa_control_number() -> str:
    """9-digit ISA control number based on current time."""
    return f"{int(time.time()) % 999_999_999:09d}"


def ordrsp_to_edi_855(ordrsp: ORDRSP) -> str:
    """
    Generate a properly-enveloped X12 EDI 855 (Purchase Order Acknowledgment).

    Envelope structure:
      ISA  — Interchange Control Header
        GS   — Functional Group Header
          ST   — Transaction Set Header
          BAK  — Beginning Segment for PO Acknowledgment
          PO1  — Confirmed line items (AC = accepted)
          ACK  — Line Item Acknowledgment detail (qty/date)
          CTT  — Transaction Totals
          SE   — Transaction Set Trailer
        GE   — Functional Group Trailer
      IEA  — Interchange Control Trailer
    """
    now_dt = time.strftime("%y%m%d")   # YYMMDD
    now_tm = time.strftime("%H%M")     # HHMM
    ctrl   = _isa_control_number()
    gs_ctrl = ctrl[:9]
    st_ctrl = "0001"

    sender   = ordrsp.control.sndprn or ordrsp.control.sender_partner
    receiver = ordrsp.control.rcvprn or ordrsp.control.receiver_partner

    # Pad/truncate to ISA standards (15 chars for IDs)
    sender_id   = f"{sender:<15}"[:15]
    receiver_id = f"{receiver:<15}"[:15]

    ack_code = "AD" if ordrsp.partial_fill else "AC"  # AD=accepted with detail, AC=accepted

    segments: list[str] = []

    # ── Interchange envelope ──────────────────────────────────────────────────
    segments.append(
        f"ISA*00*          *00*          "
        f"*ZZ*{sender_id}*ZZ*{receiver_id}"
        f"*{now_dt}*{now_tm}*^*00401*{ctrl}*0*P*:~"
    )

    # ── Functional group ──────────────────────────────────────────────────────
    segments.append(
        f"GS*PR*{sender.strip()}*{receiver.strip()}"
        f"*{now_dt}*{now_tm}*{gs_ctrl}*X*004010~"
    )

    # ── Transaction set ───────────────────────────────────────────────────────
    segments.append(f"ST*855*{st_ctrl}~")

    # BAK: Beginning Ack — purpose 00, ack code AC/AD, PO number, date
    segments.append(
        f"BAK*00*{ack_code}*{ordrsp.buyer_po_number}"
        f"*{now_dt}**{ordrsp.order_number}~"
    )

    # PO1 + ACK per confirmed line item
    po1_count = 0
    for item in ordrsp.confirmed_items:
        po1_count += 1
        segments.append(
            f"PO1*{item.posex}*{item.menge}*EA*{item.unit_price:.2f}"
            f"*PE*IN*{item.posex}~"
        )
        # ACK segment: quantity accepted, UOM EA
        segments.append(
            f"ACK*{ack_code}*{item.menge}*EA~"
        )

    # If partial fill, add backorder detail lines
    for sl in ordrsp.backorder_lines:
        segments.append(
            f"ACK*BP*{sl.ameng}*EA~"  # BP = backorder partial
        )

    # CTT — transaction totals (line count)
    total_lines = len(ordrsp.confirmed_items)
    segments.append(f"CTT*{total_lines}~")

    # SE — transaction set trailer (segment count excludes ISA/IEA/GS/GE)
    # SE counts from ST through SE inclusive
    st_index = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
    se_count = len(segments) - st_index + 1  # +1 for SE itself
    segments.append(f"SE*{se_count}*{st_ctrl}~")

    # ── Group and interchange trailers ────────────────────────────────────────
    segments.append(f"GE*1*{gs_ctrl}~")
    segments.append(f"IEA*1*{ctrl}~")

    return "\n".join(segments)
