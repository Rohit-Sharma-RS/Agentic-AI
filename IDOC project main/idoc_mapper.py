"""idoc_mapper.py -- deterministic middleware mapping step.

Maps parsed EDI 850 fields to the SAP IDoc ORDERS05 segment structure:
  ISA sender      → EDI_DC40 SNDPRN
  PO Number       → E1EDK01  BELNR
  Order/Dlv dates → E1EDK03  DATUM
  Customer/ShipTo → E1EDKA1  PARTN-AG / PARTN-WE
  Line qty/price  → E1EDP01  MENGE / NETPR
  Material number → E1EDP19  IDTNR (SAP) / KTEXT (customer)
  Backorder lines → E1EDP20  WMENG / AMENG
"""
from __future__ import annotations
import time
from models import (
    PurchaseOrder850, IDoc, EDI_DC40, E1EDK01, E1EDK03, E1EDKA1,
    E1EDP01, E1EDP19, E1EDP20,
)


def next_idoc_number() -> str:
    """Generate a unique IDoc number (timestamp-based, 10-digit)."""
    ts = int(time.time() * 1000)
    return f"IDOC{ts % 10_000_000_000:010d}"


def map_to_idoc(
    po: PurchaseOrder850,
    items: list[E1EDP01],
    materials: list[E1EDP19],
    schedule_lines: list[E1EDP20] | None = None,
    receiver_partner: str = "SAPCLNT100",
) -> IDoc:
    """Map a parsed PO + check results into a fully populated IDoc."""
    dates: list[E1EDK03] = []

    # Order date → E1EDK03 qualifier 012
    order_date_raw = po.order_date.replace("-", "")  # YYYYMMDD
    dates.append(E1EDK03(iddat="012", datum=order_date_raw))

    # Delivery date → E1EDK03 qualifier 010 (if present)
    if po.delivery_date:
        dlv_raw = po.delivery_date.replace("-", "")
        dates.append(E1EDK03(iddat="010", datum=dlv_raw))

    return IDoc(
        idoc_number=next_idoc_number(),
        control=EDI_DC40(
            direction="2",
            idoctyp="ORDERS05",
            mestyp="ORDERS",
            sndpor=f"PORT_{po.partner_id}",
            sndprt="LS",
            sndprn=po.partner_id,
            rcvpor="SAPPORT100",
            rcvprt="LS",
            rcvprn=receiver_partner,
            # legacy aliases
            message_type="ORDERS",
            basic_type="ORDERS05",
            sender_partner=po.partner_id,
            receiver_partner=receiver_partner,
        ),
        header=E1EDK01(
            belnr=po.buyer_po_number,
            bsart="NE",
            curcy="USD",
            currency="USD",
            wkurs="1.00",
            sales_org="1000",
        ),
        dates=dates,
        partners=E1EDKA1(
            parvw_ag="AG",
            partn_ag=po.ship_to_id,
            parvw_we="WE",
            partn_we=po.ship_to_id,
            sold_to=po.ship_to_id,
            ship_to=po.ship_to_id,
        ),
        items=items,
        materials=materials,
        schedule_lines=schedule_lines or [],
    )
