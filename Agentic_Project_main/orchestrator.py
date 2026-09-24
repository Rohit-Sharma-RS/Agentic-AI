"""
orchestrator.py
----------------
Refund route is now multi-turn:
  1. eligible_amount computed deterministically (refund_policy.py) --
     full or 50% partial, LLM never invents this number.
  2. History (conversation_log.py, DB-persisted) is loaded for the order.
  3. RefundConfirmationAgent decides if THIS message is an explicit
     acceptance of the previously offered amount (always False on the
     first-ever message for an order -- nothing to confirm yet).
  4. If NOT a confirmation: informational/negotiation turn only --
     Evaluator runs for groundedness, but Compliance/human-loop/
     execution are skipped entirely. No money can move on this path.
  5. If IS a confirmation: normal Compliance + human-loop gate runs,
     and only then can _execute_refund fire.

Double-refund guards from before are unchanged and still layered under
all of this: is_already_refunded() short-circuits before any LLM call,
and try_issue_refund()'s atomic UPDATE is the final backstop.
"""

from __future__ import annotations
from guardrails import (
    input_guardrail,
    output_guardrail,
    order_id_guardrail,
    sql_password_guardrail,
    PipelineResult,
    RAGAnswer,
    RAGEvaluation,
    ComplianceCheck,
)
from agents import (
    KnowledgeAgent,
    RefundAgent,
    RefundConfirmationAgent,
    SQLAgent,
    EvaluatorAgent,
    ComplianceAgent,
    IntentRouterAgent,
    ResponseWriterAgent,
)
from order_lookup import get_order_record, is_already_refunded, mark_order_rejected
from refund_policy import compute_eligible_amount, risk_level_for_amount
from conversation_log import get_history, append_turn
from refund_processor import process_refund
from audit_log import safe_log


class Orchestrator:
    def __init__(self):
        self.router_agent = IntentRouterAgent()
        self.knowledge_agent = KnowledgeAgent()
        self.refund_agent = RefundAgent()
        self.confirmation_agent = RefundConfirmationAgent()
        self.sql_agent = SQLAgent()
        self.evaluator_agent = EvaluatorAgent()
        self.compliance_agent = ComplianceAgent()
        self.response_writer = ResponseWriterAgent()

    def run(
        self,
        question: str,
        order_id: str | None = None,
        sql_password: str | None = None,
    ) -> PipelineResult:
        guard = input_guardrail(question)
        if not guard.passed:
            safe_log("input_blocked", {"question": question, "reason": guard.reason})
            raise ValueError(f"Input blocked by guardrail: {guard.reason}")

        route = self.router_agent.classify(question)
        safe_log("routed", {"question": question, "route": route.route})

        eligible_amount = None
        is_confirmation = None

        if route.route == "refund":
            oid_guard = order_id_guardrail(order_id)
            if not oid_guard.passed:
                raise ValueError(oid_guard.reason)

            order_record = get_order_record(order_id)

            # GUARD #1 (unchanged): short-circuit before any LLM call.
            if is_already_refunded(order_record):
                safe_log("refund_already_issued_shortcircuit", {"order_id": order_id})
                return self._already_refunded_result(question, order_id, order_record, route.route)

            eligible_amount, eligible_reason = compute_eligible_amount(order_record)
            history = get_history(order_id)
            is_confirmation = bool(history) and self.confirmation_agent.check(
                question, history, eligible_amount
            ).confirmed

            append_turn(order_id, "customer", question)
            draft, context = self.refund_agent.answer(
                question, order_record, history, eligible_amount, eligible_reason
            )
            append_turn(order_id, "agent", draft.answer)

            if not is_confirmation:
                # Negotiation/informational turn -- never authorizes money movement.
                evaluation = self.evaluator_agent.evaluate(question, context, draft)
                polished = self.response_writer.approve(question, draft.answer)
                result = PipelineResult(
                    question=question, route=route.route, draft=draft, evaluation=evaluation,
                    compliance=ComplianceCheck(
                        is_compliant=True, risk_level="low", policy_citations=[],
                        revised_answer=None, reasoning="Informational turn -- no authorization needed yet.",
                    ),
                    requires_human_approval=False, final_answer=polished,
                    order_id=order_id.strip().upper(),
                    eligible_refund_amount=eligible_amount, is_confirmation_turn=False,
                )
                safe_log("refund_negotiation_turn", {"order_id": result.order_id, "eligible_amount": eligible_amount})
                return result

        elif route.route == "sql":
            pw_guard = sql_password_guardrail(sql_password)
            if not pw_guard.passed:
                safe_log("sql_password_rejected", {})
                raise ValueError(pw_guard.reason)
            draft, context = self.sql_agent.answer(question)

        else:  # "knowledge"
            draft, context = self.knowledge_agent.answer(question)

        evaluation = self.evaluator_agent.evaluate(question, context, draft)
        compliance = self.compliance_agent.check(question, draft.answer)

        needs_human = (
            evaluation.verdict != "pass"
            or not compliance.is_compliant
            or (
                risk_level_for_amount(eligible_amount) != "low"
                if (route.route == "refund" and eligible_amount is not None)
                else compliance.risk_level != "low"
            )
        )

        result = PipelineResult(
            question=question,
            route=route.route,
            draft=draft,
            evaluation=evaluation,
            compliance=compliance,
            requires_human_approval=needs_human,
            order_id=order_id.strip().upper() if (route.route == "refund" and order_id) else None,
            eligible_refund_amount=eligible_amount,
            is_confirmation_turn=is_confirmation,
        )
        safe_log("pipeline_result", {
            "route": route.route, "order_id": result.order_id,
            "needs_human": needs_human, "risk": compliance.risk_level,
        })

        if not needs_human:
            candidate = compliance.revised_answer or draft.answer
            polished = self.response_writer.approve(question, candidate)
            out_guard = output_guardrail(polished)
            if not out_guard.passed:
                result.requires_human_approval = True
                safe_log("output_guardrail_fallback_to_human", {"order_id": result.order_id})
                return result
            result.final_answer = polished
            result.human_decision = None

            if route.route == "refund" and is_confirmation:  # belt-and-suspenders check
                self._execute_refund(result)

        return result

    def finalize_with_human(
        self, result: PipelineResult, decision: str, human_note: str | None
    ) -> PipelineResult:
        base_answer = result.compliance.revised_answer or result.draft.answer

        if decision == "approved":
            result.final_answer = self.response_writer.approve(result.question, base_answer)
        elif decision == "edited":
            result.final_answer = self.response_writer.edit(result.question, base_answer, human_note or "")
        elif decision == "rejected":
            reason = result.compliance.reasoning or result.evaluation.notes
            result.final_answer = self.response_writer.reject(result.question, reason, human_note or "")

        result.human_decision = decision  # type: ignore[assignment]
        result.human_note = human_note
        safe_log("human_decision", {"order_id": result.order_id, "decision": decision})

        if result.route == "refund" and result.order_id:
            if decision in ("approved", "edited"):
                self._execute_refund(result)  # idempotent -- safe even if already done
            elif decision == "rejected":
                try:
                    mark_order_rejected(result.order_id)
                except ValueError as e:
                    result.final_answer += f"\n\n(Note: {e})"
                    safe_log("reject_after_already_refunded", {"order_id": result.order_id})

        return result

    def _execute_refund(self, result: PipelineResult) -> None:
        """GUARD #2 (the real backstop): process_refund() is itself
        idempotent via try_issue_refund's atomic UPDATE."""
        amount = result.eligible_refund_amount
        if amount is None:
            raise ValueError(f"No eligible refund amount computed for order {result.order_id}.")
        message, newly_processed = process_refund(result.order_id, amount)
        result.refund_processed = newly_processed
        result.refund_notification = message

    def _already_refunded_result(
        self, question: str, order_id: str, order_record: dict, route: str
    ) -> PipelineResult:
        when = order_record.get("refund_issued_at") or "previously"
        answer = (
            f"Order {order_id.strip().upper()} has already been refunded (on {when}). "
            f"No further action is needed, and it will not be refunded again."
        )
        draft = RAGAnswer(answer=answer, sources=["refund_issued flag (database)"], confidence=1.0)
        evaluation = RAGEvaluation(
            groundedness_score=1.0, relevance_score=1.0, hallucination_risk="low",
            verdict="pass", notes="Deterministic lookup, no LLM involved.",
        )
        compliance = ComplianceCheck(
            is_compliant=True, risk_level="low", policy_citations=[],
            revised_answer=None, reasoning="Informational only -- no new refund action taken.",
        )
        return PipelineResult(
            question=question, route=route, draft=draft, evaluation=evaluation,
            compliance=compliance, requires_human_approval=False, final_answer=answer,
            order_id=order_id.strip().upper(), refund_processed=False, refund_notification=None,
        )
