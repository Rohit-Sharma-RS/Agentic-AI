# SAP IDoc Processing Pipeline

## Pushover setup

Human-in-the-loop failures send a push notification through Pushover. Set these
environment variables before starting the app:

```powershell
$env:PUSHOVER_USER = "your-pushover-user-key"
$env:PUSHOVER_TOKEN = "your-pushover-application-token"
```

The notification layer logs a warning and continues processing if either value
is missing or the Pushover request fails.

**EDI 850 → IDoc ORDERS05 → ORDRSP → EDI 855**

A fully deterministic, fail-safe, agentic order-processing pipeline that mirrors the
SAP `IDOC_INPUT_ORDERS` / `WE19` / `BD87` lifecycle.  An LLM agent (RAG over a policy
knowledge base) explains every failure and identifies the exact human fix action.

---

## Architecture

```
EDI 850 (raw text)
    │
    ▼
edi850_parser.py    ← parse_edi_850() — deterministic X12 parser
    │
    ▼
business_rules.py   ← run_inbound_checks() — ALL deterministic checks
    │                  (partner, customer, material, qty, price, credit, dates)
    │ partial fill AUTO-RESOLVED here (E1EDP20 schedule lines generated)
    ▼
idoc_mapper.py      ← map_to_idoc() — builds full ORDERS05 IDoc
    │
    ▼
idoc_formatter.py   ← format_idoc() — WE02/WE05-style segment printout
    │
    ├─[failures]─▶ agents.py  ← ErrorDiagnosisAgent (LLM + RAG)
    │                             explains failure, identifies human action
    │
    ▼
ordrsp_generator.py ← build_ordrsp() + ordrsp_to_edi_855()
    │                  Full X12 EDI 855 with ISA/GS/ST/BAK/PO1/ACK/CTT/SE/GE/IEA
    ▼
Gradio UI (app.py)  ← 10 demo scenarios, IDoc accordion, 6 fix actions
```

---

## Problem Coverage

| # | Problem | Auto-resolved? | Human fix action |
|---|---------|----------------|-----------------|
| 1 | Unknown partner (no WE20 profile) | No | Add partner profile |
| 2 | Blocked material | No | Unblock material |
| 3 | Low stock (some available) | **Yes — partial fill** | None needed |
| 4 | Zero stock (none available) | No | Replenish stock |
| 5 | Credit limit exceeded | No | Raise credit limit |
| 6 | Unknown customer / ship-to | No | Add customer master |
| 7 | Unknown material (no SAP mapping) | No | Add material mapping |
| 8 | Price mismatch > 5% tolerance | No | Resubmit correct price |
| 9 | Delivery date in the past | No | Resubmit with future date |
| 10 | Zero / negative quantity | No | Resubmit corrected PO |
| 11 | Negative unit price | No | Resubmit corrected PO |
| 12 | Malformed EDI (parse failure) | No | Trading partner resubmits |
| 13 | Duplicate PO (idempotency) | Auto-suppressed | None |
| 14 | DB missing / corrupt | Fail-safe RuntimeError | Run `create_db.py` |

---

## IDoc Segment Structure (ORDERS05)

Each processed IDoc is printed in WE02/WE05 style with separate boxes per segment:

```
======================================================================
  IDoc Number : IDOC0319095358
======================================================================
+- EDI_DC40  Control Record ----------------------------------------+
|  TABNAM          : EDI_DC40                                       |
|  DIRECTION       : 2 (Inbound)                                    |
|  IDOCTYP         : ORDERS05                                       |
|  MESTYP          : ORDERS                                         |
|  SNDPOR          : PORT_CUST_1001                                 |
|  SNDPRN          : CUST_1001                                      |
|  RCVPRN          : SAPCLNT100                                     |
+--------------------------------------------------------------------+
+- E1EDK01   Header Data -------------------------------------------+
|  BELNR           : PO-319095                                      |
|  BSART           : NE   CURCY: USD   VKORG: 1000                  |
+--------------------------------------------------------------------+
+- E1EDK03   Date Segment (Order Date) -----------------------------+
|  IDDAT           : 012 (Order Date)    DATUM: 20260115            |
+--------------------------------------------------------------------+
+- E1EDK03   Date Segment (Delivery Date) --------------------------+
|  IDDAT           : 010 (Delivery Date) DATUM: 20271201            |
+--------------------------------------------------------------------+
+- E1EDKA1   Partner Data ------------------------------------------+
|  PARTN-AG        : 200050 (Sold-to)                               |
|  PARTN-WE        : 200050 (Ship-to)                               |
+--------------------------------------------------------------------+
+- E1EDP01   Line Item 1 -------------------------------------------+
|  POSEX: 000010   MENGE: 5.0   MENEE: EA   NETPR: $30.00           |
+--------------------------------------------------------------------+
+- E1EDP19   Material ID 1 -----------------------------------------+
|  QUALF: 001   IDTNR: MAT-10500   KTEXT: WIDGET-LOWSTOCK           |
+--------------------------------------------------------------------+
+- E1EDP20   Schedule Line (Item 000010) ---------------------------+
|  WMENG: 5.0 (confirmed)   AMENG: 15.0 (backordered)              |
+--------------------------------------------------------------------+
```

---

## EDI 855 Output (Full Envelope)

```
ISA*00*          *00*          *ZZ*SAPCLNT100     *ZZ*CUST_1001      *260924*2351*^*00401*790319096*0*P*:~
GS*PR*SAPCLNT100*CUST_1001*260924*2351*790319096*X*004010~
ST*855*0001~
BAK*00*AD*PO-319095*260924**ORD-555555~
PO1*000010*5.0*EA*30.00*PE*IN*000010~
ACK*AD*5.0*EA~
ACK*BP*15.0*EA~          ← backorder partial
CTT*1~
SE*7*0001~
GE*1*790319096~
IEA*1*790319096~
```

Acknowledgment codes:
- `AC` = Accepted (full fill)
- `AD` = Accepted with detail (partial fill)
- `BP` = Backorder partial (quantity on backorder)

---

## Status Lifecycle

| Code | Meaning |
|------|---------|
| 50   | IDoc added to database |
| 64   | IDoc ready for application pass |
| 51   | Application error (business rule failure) |
| 53   | Application document posted (sales order created) |
| 01   | Outbound ORDRSP IDoc created |
| 30   | Outbound ORDRSP ready for dispatch |
| 03   | Outbound ORDRSP passed to port |

---

## Quick Start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create the SQLite demo database
python create_db.py

# 3. Set your Google AI API key in .env
# GOOGLE_API_KEY=your-key-here

# 4. Launch the Gradio UI
python app.py
```

---

## Files

| File | Purpose |
|------|---------|
| `models.py` | Pydantic models for all pipeline stages |
| `edi850_parser.py` | X12 EDI 850 parser with descriptive errors |
| `business_rules.py` | All 13 deterministic checks + fix functions |
| `idoc_mapper.py` | PO → ORDERS05 IDoc mapping |
| `idoc_formatter.py` | WE02/WE05-style IDoc printout |
| `ordrsp_generator.py` | ORDRSP IDoc + full EDI 855 envelope |
| `orchestrator.py` | Pipeline orchestration (15 problem paths) |
| `agents.py` | LLM diagnosis agent (RAG over policy KB) |
| `app.py` | Gradio UI with 10 demo scenarios + 6 fix actions |
| `create_db.py` | SQLite demo master data setup |
| `data/business_rules_kb.txt` | Policy knowledge base (15 documents) |
| `data/sap_sim.db` | SQLite demo database |

---

## Key Design Principles

1. **Deterministic core** — every pass/fail decision is in `business_rules.py`, never in the LLM
2. **Idempotent** — UNIQUE constraint on `buyer_po_number` prevents duplicate sales orders
3. **Fail-safe** — DB failures raise clean RuntimeError; log writes never crash the pipeline
4. **Auto-resolve where possible** — partial fill is fully automatic (no human needed)
5. **Full IDoc structure** — all SAP field names (TABNAM, IDOCTYP, SNDPRN, POSEX, etc.)
6. **Proper EDI 855** — full X12 envelope with ISA/GS/GE/IEA and correct ACK codes
