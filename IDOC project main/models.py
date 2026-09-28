"""models.py -- typed contracts for the whole pipeline.
Representative subset of X12 / IDoc structures.  All fields are
explicit so the UI can render each IDoc segment separately.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ── EDI 850 inbound ──────────────────────────────────────────────────────────

class LineItem850(BaseModel):
    item_number: str
    customer_material: str
    quantity: float
    unit_price: float


class PurchaseOrder850(BaseModel):
    partner_id: str            # ISA sender, e.g. CUST_1001
    buyer_po_number: str       # BEG03
    order_date: str            # BEG05, YYYY-MM-DD
    ship_to_name: str
    ship_to_id: str
    delivery_date: Optional[str] = None
    items: list[LineItem850]


# ── IDoc segments (inbound ORDERS05) ─────────────────────────────────────────

class EDI_DC40(BaseModel):
    """IDoc Control Record (EDI_DC40) — routing + meta"""
    tabnam: str = "EDI_DC40"
    direction: Literal["1", "2"]   # 1=outbound, 2=inbound
    idoctyp: str                   # basic type e.g. ORDERS05
    mestyp: str                    # message type e.g. ORDERS
    sndpor: str                    # sender port
    sndprt: str = "LS"             # sender partner type
    sndprn: str                    # sender partner number
    rcvpor: str                    # receiver port
    rcvprt: str = "LS"             # receiver partner type
    rcvprn: str                    # receiver partner number
    # legacy aliases kept for backward-compat
    message_type: str = ""
    basic_type: str = ""
    sender_partner: str = ""
    receiver_partner: str = ""


class E1EDK01(BaseModel):
    """IDoc Header Data — E1EDK01"""
    segnam: str = "E1EDK01"
    belnr: str                     # Document (PO) number
    bsart: str = "NE"              # Order type
    curcy: str = "USD"             # Currency
    wkurs: str = "1.00"            # Exchange rate
    # legacy alias
    currency: str = ""
    sales_org: str = "1000"


class E1EDKA1(BaseModel):
    """IDoc Partner Segment — E1EDKA1"""
    segnam: str = "E1EDKA1"
    parvw_ag: str = ""             # Sold-to (AG)
    partn_ag: str = ""             # Sold-to partner number
    parvw_we: str = ""             # Ship-to (WE)
    partn_we: str = ""             # Ship-to partner number
    # legacy aliases
    sold_to: str = ""
    ship_to: str = ""


class E1EDK03(BaseModel):
    """IDoc Date Segment — E1EDK03"""
    segnam: str = "E1EDK03"
    iddat: str = "012"             # Date qualifier (012=order date, 010=delivery)
    datum: str                     # Date YYYYMMDD


class E1EDP01(BaseModel):
    """IDoc Line Item Segment — E1EDP01"""
    segnam: str = "E1EDP01"
    posex: str                     # Item number (external)
    menge: float                   # Quantity
    menee: str = "EA"              # Unit of measure
    unit_price: float


class E1EDP19(BaseModel):
    """IDoc Material Identification Segment — E1EDP19"""
    segnam: str = "E1EDP19"
    qualf: str = "001"             # Qualifier (001=customer material)
    idtnr: str = ""                # Material number (customer side)
    ktext: str = ""                # Short description
    # legacy aliases
    customer_material: str = ""
    sap_material: str = ""


class E1EDP20(BaseModel):
    """IDoc Schedule Line Segment — E1EDP20 (backorder / partial fill)"""
    segnam: str = "E1EDP20"
    posex: str                     # Item reference
    wmeng: float                   # Confirmed quantity
    ameng: float                   # Backorder quantity
    edatu: str = ""                # Confirmed delivery date


class IDoc(BaseModel):
    """Full inbound IDoc envelope (ORDERS05)"""
    idoc_number: str
    # Segments in canonical SAP order
    control: EDI_DC40
    header: E1EDK01
    dates: list[E1EDK03] = Field(default_factory=list)
    partners: E1EDKA1
    items: list[E1EDP01]
    materials: list[E1EDP19]
    schedule_lines: list[E1EDP20] = Field(default_factory=list)


# ── Business-rule checks ──────────────────────────────────────────────────────

class InboundCheckResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)   # non-fatal advisories
    partial_fill: bool = False                           # True when backorder split applied
    partial_fill_details: list[dict] = Field(default_factory=list)


# ── Sales Order ───────────────────────────────────────────────────────────────

class SalesOrder(BaseModel):
    order_number: str
    buyer_po_number: str
    total_value: float
    backorder_note: str = ""


# ── Outbound ORDRSP / EDI 855 ─────────────────────────────────────────────────

class ORDRSP(BaseModel):
    control: EDI_DC40
    order_number: str
    buyer_po_number: str
    confirmed_items: list[E1EDP01]
    partial_fill: bool = False
    backorder_lines: list[E1EDP20] = Field(default_factory=list)


# ── LLM diagnosis ────────────────────────────────────────────────────────────

class ErrorDiagnosis(BaseModel):
    plain_explanation: str
    suggested_fix: str
    auto_resolution: Optional[str] = None   # e.g. "PARTIAL_FILL_APPLIED"
    human_actions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


# ── Final pipeline result ─────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    idoc_number: str
    buyer_po_number: str
    status_history: list[str]
    idoc: Optional[IDoc] = None
    check_result: Optional[InboundCheckResult] = None
    diagnosis: Optional[ErrorDiagnosis] = None
    sales_order: Optional[SalesOrder] = None
    ordrsp: Optional[ORDRSP] = None
    edi_855: Optional[str] = None
    idoc_formatted: Optional[str] = None   # human-readable IDoc print
    requires_human: bool = False
    final_status: str   # "53" success, "51" error/pending human, "duplicate", "partial"
