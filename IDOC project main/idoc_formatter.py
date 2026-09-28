"""idoc_formatter.py -- produces a human-readable, properly-structured
SAP IDoc printout identical in layout to what you'd see in WE02/WE05.

Format:
  ┌─ EDIDC ─── Control Record ───────────────────────────────────────┐
  │  TABNAM   : EDI_DC40           IDOCTYP  : ORDERS05              │
  │  DIRECTION: 2 (Inbound)        MESTYP   : ORDERS                │
  │  SNDPRN   : CUST_1001          RCVPRN   : SAPCLNT100            │
  └───────────────────────────────────────────────────────────────────┘
  ┌─ E1EDK01 ── Header Data ─────────────────────────────────────────┐
  │  ...                                                             │
  └───────────────────────────────────────────────────────────────────┘
  ... (one box per segment type, items enumerated inside)
"""
from __future__ import annotations
from models import IDoc


_W = 68  # total box width
_TOP_LEFT     = "+"
_TOP_RIGHT    = "+"
_BOT_LEFT     = "+"
_BOT_RIGHT    = "+"
_HORIZONTAL   = "-"
_VERTICAL     = "|"
_DOUBLE_HORIZ = "="


def _box(title: str, rows: list[tuple[str, str]]) -> str:
    """Render a single bordered segment box."""
    def row_line(k: str, v: str) -> str:
        label = f"  {k:<16}: {v}"
        pad = _W - len(label) - 1
        return f"{_VERTICAL}{label}{' ' * max(pad, 1)}{_VERTICAL}"

    top    = f"{_TOP_LEFT}- {title} {_HORIZONTAL * max(0, _W - len(title) - 4)}{_TOP_RIGHT}"
    bottom = f"{_BOT_LEFT}{_HORIZONTAL * _W}{_BOT_RIGHT}"
    lines  = [top] + [row_line(k, v) for k, v in rows] + [bottom]
    return "\n".join(lines)


def format_idoc(idoc: IDoc) -> str:
    """Return a formatted multi-section IDoc printout."""
    c = idoc.control
    h = idoc.header
    p = idoc.partners
    sections: list[str] = []

    # ── IDoc envelope line ─────────────────────────────────────────────────
    envelope = (
        f"{'=' * (_W + 2)}\n"
        f"  IDoc Number : {idoc.idoc_number}\n"
        f"{'=' * (_W + 2)}"
    )
    sections.append(envelope)

    # ── Control Record (EDI_DC40) ──────────────────────────────────────────
    direction_label = "1 (Outbound)" if c.direction == "1" else "2 (Inbound)"
    sections.append(_box(
        "EDI_DC40  Control Record",
        [
            ("TABNAM",    c.tabnam),
            ("DIRECTION", direction_label),
            ("IDOCTYP",   c.idoctyp or c.basic_type or "ORDERS05"),
            ("MESTYP",    c.mestyp or c.message_type or "ORDERS"),
            ("SNDPOR",    c.sndpor or "PORT_EDI"),
            ("SNDPRT",    c.sndprt),
            ("SNDPRN",    c.sndprn or c.sender_partner),
            ("RCVPOR",    c.rcvpor or "SAPPORT"),
            ("RCVPRT",    c.rcvprt),
            ("RCVPRN",    c.rcvprn or c.receiver_partner),
        ],
    ))

    # ── Header (E1EDK01) ───────────────────────────────────────────────────
    sections.append(_box(
        "E1EDK01   Header Data",
        [
            ("SEGNAM",  h.segnam),
            ("BELNR",   h.belnr),
            ("BSART",   h.bsart),
            ("CURCY",   h.curcy or h.currency or "USD"),
            ("WKURS",   h.wkurs),
            ("VKORG",   h.sales_org),
        ],
    ))

    # ── Dates (E1EDK03) ───────────────────────────────────────────────────
    for d in idoc.dates:
        qualifier = {"012": "Order Date", "010": "Delivery Date"}.get(d.iddat, d.iddat)
        sections.append(_box(
            f"E1EDK03   Date Segment ({qualifier})",
            [
                ("SEGNAM", d.segnam),
                ("IDDAT",  f"{d.iddat} ({qualifier})"),
                ("DATUM",  d.datum),
            ],
        ))

    # ── Partners (E1EDKA1) ────────────────────────────────────────────────
    sold_to_val = p.partn_ag or p.sold_to
    ship_to_val = p.partn_we or p.ship_to
    sections.append(_box(
        "E1EDKA1   Partner Data",
        [
            ("SEGNAM",   p.segnam),
            ("PARVW",    "AG (Sold-to)"),
            ("PARTN-AG", sold_to_val),
            ("PARVW",    "WE (Ship-to)"),
            ("PARTN-WE", ship_to_val),
        ],
    ))

    # ── Line Items (E1EDP01) + Material IDs (E1EDP19) ────────────────────
    for i, (item, mat) in enumerate(zip(idoc.items, idoc.materials), start=1):
        cust_mat = mat.customer_material or mat.idtnr
        sap_mat  = mat.sap_material or mat.ktext
        sections.append(_box(
            f"E1EDP01   Line Item {i}",
            [
                ("SEGNAM", item.segnam),
                ("POSEX",  item.posex),
                ("MENGE",  str(item.menge)),
                ("MENEE",  item.menee),
                ("NETPR",  f"${item.unit_price:.2f}"),
            ],
        ))
        sections.append(_box(
            f"E1EDP19   Material ID {i}",
            [
                ("SEGNAM", mat.segnam),
                ("QUALF",  mat.qualf),
                ("IDTNR",  sap_mat),
                ("KTEXT",  cust_mat),
            ],
        ))

    # ── Schedule Lines (E1EDP20) — backorder / partial fill ───────────────
    for sl in idoc.schedule_lines:
        sections.append(_box(
            f"E1EDP20   Schedule Line (Item {sl.posex})",
            [
                ("SEGNAM",    sl.segnam),
                ("POSEX",     sl.posex),
                ("WMENG",     f"{sl.wmeng} (confirmed)"),
                ("AMENG",     f"{sl.ameng} (backordered)"),
                ("EDATU",     sl.edatu or "TBD"),
            ],
        ))

    return "\n".join(sections)
