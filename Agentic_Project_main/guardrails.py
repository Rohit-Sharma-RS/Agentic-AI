"""
guardrails.py
-------------
Two things live here, on purpose kept in one small file:

1. Pydantic schemas -> every agent-to-agent handoff and every RAG output
   is a typed, validated object instead of a raw string. This is what
   makes the pipeline inspectable and "evaluatable".

2. Guardrail functions -> cheap, deterministic checks that run BEFORE
   we spend an LLM call (input guardrail) and lightweight structural
   checks that run AFTER an LLM call (output guardrail), independent
   of the LLM's own self-evaluation.
"""

from __future__ import annotations
import os
import re
from typing import Literal, Optional
from pydantic import BaseModel, Field

from order_lookup import is_valid_order_id_format, get_order_record
from verify_password import verify_password


# ---------------------------------------------------------------------
# 1. STRUCTURED SCHEMAS (the contracts between agents)
# ---------------------------------------------------------------------

class RAGAnswer(BaseModel):
    """Output of the Knowledge Agent."""
    answer: str = Field(description="Draft answer to the customer, grounded in retrieved docs")
    sources: list[str] = Field(description="Doc IDs (e.g. 'refund_policy_basics') actually used")
    confidence: float = Field(ge=0, le=1, description="Agent's own confidence in the answer")


class RAGEvaluation(BaseModel):
    """Output of the Evaluator Agent -- grades the Knowledge Agent's answer."""
    groundedness_score: float = Field(ge=0, le=1, description="Is the answer supported by the retrieved context?")
    relevance_score: float = Field(ge=0, le=1, description="Does the answer actually address the question?")
    hallucination_risk: Literal["low", "medium", "high"]
    verdict: Literal["pass", "needs_review", "fail"]
    notes: str = Field(description="One-sentence justification")


class ComplianceCheck(BaseModel):
    """Output of the Compliance Agent -- checks the draft against policy docs."""
    is_compliant: bool
    risk_level: Literal["low", "medium", "high"]
    policy_citations: list[str] = Field(description="Policy doc IDs relied on")
    revised_answer: Optional[str] = Field(default=None, description="Corrected answer if the draft violated policy")
    reasoning: str


class IntentRoute(BaseModel):
    """Output of the Router Agent -- decides which agent should answer."""
    route: Literal["refund", "knowledge", "sql"]
    reasoning: str = Field(description="One-sentence justification for the chosen route")


class SQLQueryPlan(BaseModel):
    """Output of the SQL Agent's planning step -- the query BEFORE it's run."""
    sql: str = Field(description="A single SQLite SELECT statement")
    explanation: str = Field(description="Plain-English description of what the query computes")
    tables_used: list[str]


class FinalResponse(BaseModel):
    """Output of the Response Writer Agent -- the actual message the
    customer receives, after a human decision (approve/edit/reject)."""
    customer_message: str = Field(description="Polished, ready-to-send message for the end customer")


class RefundConfirmation(BaseModel):
    """Output of the Refund Confirmation Agent -- is this turn an
    explicit acceptance of the offered amount, or just a question/argument?"""
    confirmed: bool
    reasoning: str = Field(description="One-sentence justification")


class PipelineResult(BaseModel):
    """Everything the orchestrator produced for one question, in one object."""
    question: str
    route: Literal["refund", "knowledge", "sql"]
    draft: RAGAnswer
    evaluation: RAGEvaluation
    compliance: ComplianceCheck
    requires_human_approval: bool
    final_answer: Optional[str] = None
    human_decision: Optional[Literal["approved", "edited", "rejected"]] = None
    human_note: Optional[str] = Field(default=None, description="Raw note a human reviewer typed in, before rewriting")
    order_id: Optional[str] = Field(default=None, description="Set only for route='refund'")
    refund_processed: bool = Field(default=False, description="True once refund_processor actually ran")
    refund_notification: Optional[str] = Field(default=None, description="The message sent to the customer via push/email")
    eligible_refund_amount: Optional[float] = Field(default=None, description="Policy-computed amount (may be partial)")
    is_confirmation_turn: Optional[bool] = Field(default=None, description="Whether this turn was treated as acceptance")


# ---------------------------------------------------------------------
# 2. GUARDRAILS
# ---------------------------------------------------------------------

# Deliberately simple & deterministic -- no LLM call needed, so it's
# fast and cheap to run on every single message before we do anything
# expensive. Swap in a moderation model/API later if you want more rigor.
_BLOCKED_INPUT_KEYWORDS = [
    "social security", "ssn", "credit card number", "password is",
    "kill myself", "suicide",
]

CARD_NUMBER_PATTERN = r"\b(?:\d[ -]*?){13,16}\b"


class GuardrailResult(BaseModel):
    passed: bool
    reason: Optional[str] = None


_VOWELS = set("aeiouyAEIOUY")


def _looks_like_a_word(token: str) -> bool:
    """A token 'looks like a word' if it's letters-only (ignoring a
    little edge punctuation) and contains at least one vowel -- filters
    out number/symbol soup and pure keysmashes without needing a
    dictionary or language model."""
    core = token.strip(".,!?;:'\"()-")
    if not core or len(core) > 20 or not core.isalpha():
        return False
    return any(ch in _VOWELS for ch in core)


def _looks_like_gibberish(text: str) -> bool:
    """Cheap heuristic to block junk input ('asdkjhaskjdh', keysmashes,
    number/symbol soup, etc.) -- not a real language-detection model,
    just a ratio check on how many tokens look like real words."""
    words = text.strip().split()
    if len(words) < 2:
        return False  # short-but-real questions ("Refund?") shouldn't be caught here
    real_looking = [w for w in words if _looks_like_a_word(w)]
    return len(real_looking) / len(words) < 0.5


def input_guardrail(user_question: str) -> GuardrailResult:
    """Runs BEFORE any agent/LLM call. Blocks obviously unsafe or useless input cheaply."""
    q = user_question.lower()
    for kw in _BLOCKED_INPUT_KEYWORDS:
        if kw in q:
            return GuardrailResult(passed=False, reason=f"Blocked keyword detected: '{kw}'")
    if len(user_question.strip()) < 3:
        return GuardrailResult(passed=False, reason="Question too short/empty")
    if _looks_like_gibberish(user_question):
        return GuardrailResult(passed=False, reason="This doesn't look like a real question -- please rephrase.")
    return GuardrailResult(passed=True)


def order_id_guardrail(order_id: Optional[str]) -> GuardrailResult:
    """Runs BEFORE the refund flow does anything. Checks presence,
    format, AND that the order actually exists -- all deterministic,
    no LLM call needed."""
    if not order_id or not order_id.strip():
        return GuardrailResult(
            passed=False,
            reason="This looks like a refund request. Please provide your Order ID (format ORD-XXXXX) to continue.",
        )
    if not is_valid_order_id_format(order_id):
        return GuardrailResult(
            passed=False,
            reason=f"'{order_id}' doesn't look like a valid order ID (expected format ORD-XXXXX, e.g. ORD-00042).",
        )
    if get_order_record(order_id) is None:
        return GuardrailResult(
            passed=False,
            reason=f"No order found matching '{order_id.strip().upper()}'. Please double-check your order ID.",
        )
    return GuardrailResult(passed=True)


ANALYST_PASSWORD_HINT = "This looks like a data/analytics question. Please enter the analyst password to continue."


def sql_password_guardrail(password: Optional[str]) -> GuardrailResult:
    """Runs BEFORE the SQL Agent ever plans a query. No LLM call happens
    for the SQL path until this passes -- that's what makes SQL access
    'password-protected' rather than just a UI suggestion."""
    if not password:
        return GuardrailResult(passed=False, reason=ANALYST_PASSWORD_HINT)
    if not verify_password(password):
        return GuardrailResult(passed=False, reason="Incorrect analyst password. Access denied.")
    return GuardrailResult(passed=True)


_SQL_WRITE_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter",
    "attach", "pragma", "create", "replace", "vacuum",
]

# These columns exist in the DB (for realism) but must NEVER be
# retrievable through the LLM-driven SQL path -- see order_lookup.py
# for the one place they're allowed to be read at all.
_SQL_FORBIDDEN_COLUMNS = ["account_number", "phone_number"]


def sql_guardrail(sql: str) -> GuardrailResult:
    """Runs on every LLM-generated SQL query BEFORE it's executed.
    Deterministic allow-list approach: only a single SELECT is allowed,
    no matter what the agent's `explanation` claims, and account/phone
    data can never be selected -- not even via SELECT *."""
    s = sql.strip().lower().rstrip(";")
    if not s.startswith("select"):
        return GuardrailResult(passed=False, reason="Only SELECT queries are allowed")
    if ";" in s:
        return GuardrailResult(passed=False, reason="Multiple statements are not allowed")
    for kw in _SQL_WRITE_KEYWORDS:
        if kw in s.split():
            return GuardrailResult(passed=False, reason=f"Query contains disallowed keyword: '{kw}'")
    if re.search(r"select\s+\*\s+from", s):
        return GuardrailResult(
            passed=False,
            reason="Wildcard SELECT * is not allowed -- list specific columns (account_number/phone_number are restricted).",
        )
    for col in _SQL_FORBIDDEN_COLUMNS:
        if col in s:
            return GuardrailResult(
                passed=False,
                reason=f"Query references a restricted column ('{col}'). Account/phone data cannot be retrieved via chat.",
            )
    return GuardrailResult(passed=True)


def output_guardrail(final_text: str) -> GuardrailResult:
    """Runs AFTER the pipeline produces a final answer, independent of the
    LLM's self-reported compliance. Catches things like raw card numbers
    slipping through no matter what the model claimed."""
    import re
    if re.search(CARD_NUMBER_PATTERN, final_text):
        return GuardrailResult(passed=False, reason="Possible raw card/account number in output")
    if "guarantee" in final_text.lower() and "legal" in final_text.lower():
        return GuardrailResult(passed=False, reason="Output makes a legal guarantee -- not allowed")
    return GuardrailResult(passed=True)
