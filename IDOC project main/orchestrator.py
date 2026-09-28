from __future__ import annotations
from edi850_parser import parse_edi_850, EDIParseError
from idoc_mapper import map_to_idoc
from idoc_formatter import format_idoc
from business_rules import (
    get_partner, run_inbound_checks, try_create_sales_order,
    existing_order_for_po, log_status, get_history,
)
from ordrsp_generator import build_ordrsp, ordrsp_to_edi_855
from agents import ErrorDiagnosisAgent
from notifications import send_human_notification, send_message
from models import PipelineResult, InboundCheckResult


class Orchestrator:
    def __init__(self):
        self.diagnosis_agent = ErrorDiagnosisAgent()

    def process(self, raw_edi: str) -> PipelineResult:
        try:
            po = parse_edi_850(raw_edi)
        except EDIParseError as e:
            # No LLM call — malformed input should not spend a token budget
            send_human_notification(
                "EDI Parse Error",
                f"Could not parse EDI 850: {e}. "
                "Trading partner must correct and resubmit."
            )
            return PipelineResult(
                idoc_number="N/A",
                buyer_po_number="UNKNOWN",
                status_history=[f"PARSE_ERROR: {e}"],
                final_status="parse_error",
                requires_human=True,
            )

        existing = existing_order_for_po(po.buyer_po_number)
        if existing:
            history = [
                f"{h['status_code']}: {h['description']}"
                for h in get_history(po.buyer_po_number)
            ]
            return PipelineResult(
                idoc_number="DUPLICATE",
                buyer_po_number=po.buyer_po_number,
                status_history=history + ["DUPLICATE SUBMISSION SUPPRESSED"],
                final_status="duplicate",
                requires_human=False,
            )

        check_result, items, materials, schedule_lines, total = run_inbound_checks(
            po, po.ship_to_id
        )

        idoc = map_to_idoc(po, items, materials, schedule_lines)
        idoc_print = format_idoc(idoc)
        log_status(idoc.idoc_number, po.buyer_po_number, "50", "IDoc added to database")

        partner = get_partner(po.partner_id)
        if partner is None:
            log_status(
                idoc.idoc_number, po.buyer_po_number, "51",
                f"No partner profile configured for '{po.partner_id}' (WE20)"
            )
            diagnosis = self.diagnosis_agent.diagnose(
                InboundCheckResult(
                    passed=False,
                    failures=[f"Partner '{po.partner_id}' has no WE20 profile configured"],
                )
            )
            send_human_notification(
                "IDoc error — unconfigured partner",
                f"PO {po.buyer_po_number} from unconfigured partner {po.partner_id}. "
                "Add partner profile in integration admin UI."
            )
            return PipelineResult(
                idoc_number=idoc.idoc_number,
                buyer_po_number=po.buyer_po_number,
                status_history=["50", "51"],
                idoc=idoc,
                idoc_formatted=idoc_print,
                final_status="51",
                requires_human=True,
                diagnosis=diagnosis,
            )

        log_status(
            idoc.idoc_number, po.buyer_po_number, "64",
            "IDoc ready to be passed for application"
        )

        if not check_result.passed:
            log_status(
                idoc.idoc_number, po.buyer_po_number, "51",
                "; ".join(check_result.failures)
            )
            diagnosis = self.diagnosis_agent.diagnose(check_result)
            send_human_notification(
                "IDoc error — business rule failure",
                f"PO {po.buyer_po_number}: {'; '.join(check_result.failures)}"
            )
            return PipelineResult(
                idoc_number=idoc.idoc_number,
                buyer_po_number=po.buyer_po_number,
                status_history=["50", "64", "51"],
                idoc=idoc,
                idoc_formatted=idoc_print,
                check_result=check_result,
                final_status="51",
                requires_human=True,
                diagnosis=diagnosis,
            )

        backorder_note = ""
        if check_result.partial_fill:
            details = "; ".join(
                f"Item {d['item']}: {d['confirmed']} confirmed, {d['backordered']} on backorder"
                for d in check_result.partial_fill_details
            )
            backorder_note = f"Partial fill — {details}"

        order, newly_created = try_create_sales_order(
            po.buyer_po_number, po.partner_id, total, backorder_note
        )

        if order is None:
            send_human_notification(
                "IDoc error — sales order creation failed",
                f"PO {po.buyer_po_number}: unexpected error creating sales order. "
                "Check DB integrity."
            )
            return PipelineResult(
                idoc_number=idoc.idoc_number,
                buyer_po_number=po.buyer_po_number,
                status_history=["50", "64", "ERROR: sales order creation failed"],
                idoc=idoc,
                idoc_formatted=idoc_print,
                final_status="51",
                requires_human=True,
            )

        if not newly_created:
            history = [
                f"{h['status_code']}: {h['description']}"
                for h in get_history(po.buyer_po_number)
            ]
            return PipelineResult(
                idoc_number=idoc.idoc_number,
                buyer_po_number=po.buyer_po_number,
                status_history=history + ["DUPLICATE SUBMISSION SUPPRESSED (race)"],
                idoc=idoc,
                idoc_formatted=idoc_print,
                final_status="duplicate",
                requires_human=False,
            )

        log_status(
            idoc.idoc_number, po.buyer_po_number, "53",
            f"Application document posted: {order.order_number}"
        )

        ordrsp = build_ordrsp(
            order, items,
            sender_partner="SAPCLNT100",
            receiver_partner=po.partner_id,
            backorder_lines=schedule_lines,
            partial_fill=check_result.partial_fill,
        )
        log_status(idoc.idoc_number, po.buyer_po_number, "01", "Outbound ORDRSP created")
        log_status(idoc.idoc_number, po.buyer_po_number, "30", "Outbound ORDRSP ready for dispatch")
        log_status(idoc.idoc_number, po.buyer_po_number, "03", "Outbound ORDRSP passed to port")

        edi_855 = ordrsp_to_edi_855(ordrsp)

        # Determine final status and messaging
        if check_result.partial_fill:
            final_status = "partial"
            status_history = ["50", "64", "53 (PARTIAL FILL)", "01", "30", "03"]
            send_message(
                "Order partially confirmed",
                f"PO {po.buyer_po_number} → Sales Order {order.order_number}, "
                f"${total:.2f} confirmed. {backorder_note}"
            )
        else:
            final_status = "53"
            status_history = ["50", "64", "53", "01", "30", "03"]
            send_message(
                "Order confirmed",
                f"PO {po.buyer_po_number} → Sales Order {order.order_number}, ${total:.2f}"
            )

        return PipelineResult(
            idoc_number=idoc.idoc_number,
            buyer_po_number=po.buyer_po_number,
            status_history=status_history,
            idoc=idoc,
            idoc_formatted=idoc_print,
            check_result=check_result,
            sales_order=order,
            ordrsp=ordrsp,
            edi_855=edi_855,
            final_status=final_status,
            requires_human=False,
        )

    def reprocess(self, raw_edi: str) -> PipelineResult:
        """BD87-equivalent: re-run the full pipeline from scratch after a
        human has fixed the underlying master data.  Never partially applies
        a fix — always a full re-run.  Idempotency guard still applies."""
        return self.process(raw_edi)
