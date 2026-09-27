"""
agents.py
---------
Three small agents, each with its OWN independent vector store (its own
"private knowledge"), all backed purely by Gemini via LangChain:

  - KnowledgeAgent   -> RAG over product/support docs, drafts an answer
  - EvaluatorAgent    -> no RAG, grades the draft for groundedness/relevance
  - ComplianceAgent  -> RAG over policy docs, checks the draft against policy

Every agent returns a validated Pydantic object (see guardrails.py),
never a raw string -- that's what makes the pipeline "evaluatable" and
safe to hand off between agents.
"""

from __future__ import annotations
import os
import sqlite3
from pathlib import Path

# Anchored to this file's own folder, not the process's working
# directory -- see order_lookup.py's PROJECT_ROOT for why this matters
# (this is also what was causing "unable to open database file").
PROJECT_ROOT = Path(__file__).resolve().parent

# Load .env if it exists (not required if GOOGLE_API_KEY is already in environment)
if (PROJECT_ROOT / ".env").exists():
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import CharacterTextSplitter
from langchain_core.prompts import ChatPromptTemplate

from guardrails import (
    RAGAnswer,
    RAGEvaluation,
    ComplianceCheck,
    IntentRoute,
    SQLQueryPlan,
    FinalResponse,
    RefundConfirmation,
    sql_guardrail,
)
from order_lookup import safe_fields_for_llm

CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.5-flash-lite")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-2")

# Default knowledge-base/DB paths -- anchored to PROJECT_ROOT so they
# resolve correctly no matter what directory the process is launched
# from. Override any of these via .env if you keep data/ elsewhere.
PRODUCT_DOCS_PATH = os.getenv("PRODUCT_DOCS_PATH", str(PROJECT_ROOT / "data" / "product_docs.txt"))
POLICY_DOCS_PATH = os.getenv("POLICY_DOCS_PATH", str(PROJECT_ROOT / "data" / "policy_docs.txt"))
SQL_KB_PATH = os.getenv("SQL_KB_PATH", str(PROJECT_ROOT / "data" / "sql_knowledge_base.txt"))
SUPPORT_DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "support.db"))


def _require_api_key() -> None:
    if not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError(
            "GOOGLE_API_KEY not found. Either:\n"
            "  1. Set it in .env: GOOGLE_API_KEY=your-key-here\n"
            "  2. Export it: export GOOGLE_API_KEY=your-key-here\n"
            "Get a free key at https://ai.google.dev/aistudio"
        )


def build_vectorstore(doc_path: str) -> FAISS:
    """Loads one plain-text file split into '### DOC: id' or '### POLICY: id' blocks
    and embeds each block independently. Each agent gets its own FAISS index."""
    import re
    _require_api_key()
    with open(doc_path, encoding="utf-8") as f:
        raw = f.read()

    chunks, ids = [], []
    blocks = re.split(r"###\s*(?:DOC|POLICY|[A-Z_]+):\s*", raw)
    if len(blocks) > 1:
        for block in blocks[1:]:
            if not block.strip():
                continue
            doc_id, _, body = block.partition("\n")
            if body.strip():
                chunks.append(body.strip())
                ids.append(doc_id.strip())
            else:
                chunks.append(block.strip())
                ids.append("doc")

    if not chunks and raw.strip():
        chunks = [raw.strip()]
        ids = [Path(doc_path).stem]

    if not chunks:
        raise ValueError(f"No document content found in {doc_path}")

    splitter = CharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    docs = splitter.create_documents(chunks, metadatas=[{"doc_id": i} for i in ids])

    if not docs:
        raise ValueError(f"Failed to create documents from {doc_path}")

    embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)
    return FAISS.from_documents(docs, embeddings)


def _llm(temperature: float = 0.0) -> ChatGoogleGenerativeAI:
    _require_api_key()
    return ChatGoogleGenerativeAI(model=CHAT_MODEL, temperature=temperature)


class KnowledgeAgent:
    """Owns the product-docs RAG. Answers the customer's question."""

    def __init__(self, doc_path: str = PRODUCT_DOCS_PATH):
        self.store = build_vectorstore(doc_path)
        self.llm = _llm().with_structured_output(RAGAnswer)
        self.prompt = ChatPromptTemplate.from_template(
            "You are a support agent. Answer the customer using ONLY the context below.\n"
            "If the context doesn't cover it, say so honestly instead of guessing.\n\n"
            "Context:\n{context}\n\nQuestion: {question}\n\n"
            "List which doc_ids you actually relied on in `sources`."
        )

    def answer(self, question: str) -> tuple[RAGAnswer, str]:
        hits = self.store.similarity_search(question, k=3)
        context = "\n---\n".join(f"[{d.metadata['doc_id']}] {d.page_content}" for d in hits)
        result: RAGAnswer = (self.prompt | self.llm).invoke({"context": context, "question": question})
        return result, context


class RefundAgent:
    """Handles a refund tied to a SPECIFIC order, across MULTIPLE turns
    (customer can argue, agent must stay consistent). Reuses the
    product-docs RAG; also grounds in the order's safe fields, the
    Python-computed eligible amount (never LLM math), and prior
    conversation turns for this order.

    IMPORTANT: order_record here has already been through
    order_lookup.safe_fields_for_llm() -- account_number/phone_number
    are never present."""

    def __init__(self, doc_path: str = PRODUCT_DOCS_PATH):
        self.store = build_vectorstore(doc_path)
        self.llm = _llm().with_structured_output(RAGAnswer)
        self.prompt = ChatPromptTemplate.from_template(
            "You are a support agent handling a refund request for a specific order, "
            "across a multi-turn conversation. Use ONLY the policy context, order details, "
            "and the eligible-refund-amount figure below -- these are FACTS computed by the "
            "system, never invent or recompute a different amount yourself. Never reference "
            "or assume any account/payment/phone information.\n\n"
            "Policy context:\n{context}\n\n"
            "Order details:\n{order_info}\n\n"
            "Eligible refund (already computed, do not change): {eligible_info}\n\n"
            "Conversation so far:\n{history}\n\n"
            "Customer's latest message: {question}\n\n"
            "If the customer is arguing or asking why, explain the policy reason clearly and "
            "empathetically using the context above. If they've accepted the eligible amount, "
            "acknowledge that and say it will now be processed. List which doc_ids you relied "
            "on in `sources`."
        )

    def answer(
        self,
        question: str,
        order_record: dict,
        history: list[dict] | None = None,
        eligible_amount: float | None = None,
        eligible_reason: str = "",
    ) -> tuple[RAGAnswer, str]:
        safe_order = safe_fields_for_llm(order_record)
        order_info = "\n".join(f"- {k}: {v}" for k, v in safe_order.items())
        eligible_info = f"${eligible_amount:.2f} -- {eligible_reason}" if eligible_amount is not None else "(not yet determined)"
        history_text = "\n".join(f"{t['role']}: {t['message']}" for t in (history or [])) or "(none yet -- this is the first message)"

        hits = self.store.similarity_search(question + " " + str(safe_order.get("reason", "")), k=3)
        policy_context = "\n---\n".join(f"[{d.metadata['doc_id']}] {d.page_content}" for d in hits)

        result: RAGAnswer = (self.prompt | self.llm).invoke({
            "context": policy_context, "order_info": order_info,
            "eligible_info": eligible_info, "history": history_text, "question": question,
        })
        full_context = f"{policy_context}\n---\nORDER DETAILS:\n{order_info}\nELIGIBLE: {eligible_info}"
        return result, full_context


class RefundConfirmationAgent:
    """Decides whether the customer's latest message is an explicit
    ACCEPTANCE of a previously offered refund amount, vs. a question,
    argument, or a fresh/first-time request. Only True triggers actual
    money movement -- misclassifying an argument as agreement would be
    a real-money mistake, so this is a dedicated, narrow check."""

    def __init__(self):
        self.llm = _llm().with_structured_output(RefundConfirmation)
        self.prompt = ChatPromptTemplate.from_template(
            "Conversation history for this order:\n{history}\n\n"
            "Customer's latest message: {question}\n\n"
            "Eligible refund amount already offered: ${eligible_amount:.2f}\n\n"
            "Is the customer's latest message an EXPLICIT acceptance of this refund amount "
            "(e.g. 'ok fine', 'I agree', 'go ahead', 'that's fine, refund me')? "
            "If they're arguing, asking a question, or this is their first message, confirmed=False."
        )

    def check(self, question: str, history: list[dict], eligible_amount: float) -> "RefundConfirmation":
        history_text = "\n".join(f"{t['role']}: {t['message']}" for t in history) or "(none)"
        return (self.prompt | self.llm).invoke({
            "history": history_text, "question": question, "eligible_amount": eligible_amount,
        })


class EvaluatorAgent:
    """No RAG of its own -- grades another agent's answer against the
    context it was given. This is the 'structured output for RAG
    evaluation' piece."""

    def __init__(self):
        self.llm = _llm().with_structured_output(RAGEvaluation)
        self.prompt = ChatPromptTemplate.from_template(
            "Grade this RAG answer strictly.\n\n"
            "Question: {question}\nRetrieved context:\n{context}\n\nDraft answer: {answer}\n\n"
            "Score groundedness (is every FACTUAL claim traceable to the context?) and relevance. "
            "verdict='fail' only if the answer states a fact/number NOT in the context. "
            "Procedural statements like 'I'll process this now' or acknowledging the customer's "
            "own confirmation are NOT factual claims -- do not fail on those alone."
        )

    def evaluate(self, question: str, context: str, draft: RAGAnswer) -> RAGEvaluation:
        return (self.prompt | self.llm).invoke(
            {"question": question, "context": context, "answer": draft.answer}
        )


class ComplianceAgent:
    """Owns the policy-docs RAG, independent of KnowledgeAgent's index."""

    def __init__(self, doc_path: str = POLICY_DOCS_PATH):
        self.store = build_vectorstore(doc_path)
        self.llm = _llm().with_structured_output(ComplianceCheck)
        self.prompt = ChatPromptTemplate.from_template(
            "You are a policy-compliance reviewer. Check the draft support answer "
            "against ONLY the policy context below. If it violates policy (e.g. promises "
            "a refund/credit it shouldn't, exposes private data), set is_compliant=False "
            "and provide a corrected `revised_answer`.\n\n"
            "Policy context:\n{context}\n\nCustomer question: {question}\n"
            "Draft answer: {draft}"
        )

    def check(self, question: str, answer_text: str) -> ComplianceCheck:
        hits = self.store.similarity_search(answer_text + " " + question, k=3)
        context = "\n---\n".join(f"[{d.metadata['doc_id']}] {d.page_content}" for d in hits)
        return (self.prompt | self.llm).invoke(
            {"context": context, "question": question, "draft": answer_text}
        )


class IntentRouterAgent:
    """The 'agentic' routing step: decides which of three specialists
    should handle the question -- a specific order's refund, general
    product/policy knowledge, or a password-protected SQL/analytics
    lookup."""

    def __init__(self):
        self.llm = _llm().with_structured_output(IntentRoute)
        self.prompt = ChatPromptTemplate.from_template(
            "Decide how a support message should be answered. Pick exactly one route.\n\n"
            "route='refund' -> the person is asking about THEIR OWN specific order/refund "
            "(e.g. 'can I get a refund for my order', 'where's my refund', 'my item was "
            "damaged, I want my money back'). This is the default for personal refund asks, "
            "even if they haven't given an order ID yet -- that gets requested separately.\n\n"
            "route='sql' -> the person is an internal analyst/employee asking for a count, "
            "sum, average, range, or any aggregate/filtered lookup over refund records across "
            "MANY orders (e.g. 'how many refunds over $100 in March', 'average refund amount', "
            "'refunds between $50 and $200'). This is NOT about one specific order.\n\n"
            "route='knowledge' -> a general policy/how-to/product question not tied to any "
            "specific order (e.g. 'how does shipping work', 'what's your warranty policy').\n\n"
            "Question: {question}"
        )

    def classify(self, question: str) -> IntentRoute:
        return (self.prompt | self.llm).invoke({"question": question})


class SQLAgent:
    """Owns its own knowledge base too -- but unlike the RAG agents above,
    text-to-SQL needs the FULL schema every time (you can't answer 'average
    refund amount' from a semantically retrieved chunk of the schema), so
    this knowledge base is loaded whole rather than similarity-searched."""

    def __init__(
        self,
        db_path: str = SUPPORT_DB_PATH,
        knowledge_path: str = SQL_KB_PATH,
    ):
        self.db_path = db_path
        with open(knowledge_path, encoding="utf-8") as f:
            self.schema_knowledge = f.read()

        self.planner = _llm().with_structured_output(SQLQueryPlan)
        self.plan_prompt = ChatPromptTemplate.from_template(
            "Write ONE SQLite SELECT query to answer the question, using only the schema below.\n"
            "NEVER select account_number or phone_number -- they are restricted columns and any "
            "query touching them will be rejected. Never use SELECT * either; list explicit columns.\n\n"
            "{schema}\n\nQuestion: {question}"
        )

        self.answer_llm = _llm().with_structured_output(RAGAnswer)
        self.answer_prompt = ChatPromptTemplate.from_template(
            "Question: {question}\nSQL used: {sql}\nQuery results:\n{results}\n\n"
            "Write a short natural-language answer using ONLY these results -- never invent "
            "numbers not present in them. Put the SQL query itself in `sources`."
        )

    def answer(self, question: str) -> tuple[RAGAnswer, str]:
        plan: SQLQueryPlan = (self.plan_prompt | self.planner).invoke(
            {"schema": self.schema_knowledge, "question": question}
        )

        guard = sql_guardrail(plan.sql)
        if not guard.passed:
            raise ValueError(f"Generated SQL blocked by guardrail: {guard.reason}")

        results_text = self._execute(plan.sql)
        draft: RAGAnswer = (self.answer_prompt | self.answer_llm).invoke(
            {"question": question, "sql": plan.sql, "results": results_text}
        )
        return draft, results_text

    def _execute(self, sql: str) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchmany(50)  # cap what comes back, keeps context small
            columns = [d[0] for d in cur.description] if cur.description else []
        finally:
            conn.close()

        if not rows:
            return "Query returned no rows."
        header = " | ".join(columns)
        body = "\n".join(" | ".join(str(v) for v in r) for r in rows)
        return f"{header}\n{body}"


class ResponseWriterAgent:
    """The very last step before anything reaches the customer.

    Neither 'auto-approved' nor 'human-approved/edited/rejected' answers
    are sent to the customer as raw internal text -- this agent turns
    the internal draft + the decision (and any human note) into an
    actual, polished, ready-to-send message. It never introduces new
    facts; it only rewrites tone/clarity or incorporates what the human
    reviewer said.
    """

    def __init__(self):
        self.llm = _llm(temperature=0.3).with_structured_output(FinalResponse)

        self.approve_prompt = ChatPromptTemplate.from_template(
            "Customer question: {question}\n"
            "Internally-approved draft answer: {draft}\n\n"
            "Rewrite this as a warm, professional, ready-to-send message directly to the "
            "customer. Keep every fact exactly the same -- do not add, remove, or change any "
            "number, date, or promise. Only polish tone, clarity, and phrasing."
        )

        self.edit_prompt = ChatPromptTemplate.from_template(
            "Customer question: {question}\n"
            "Original internal draft: {draft}\n"
            "Human reviewer's note: {human_note}\n\n"
            "The human reviewer read the draft above and wrote that note to correct or adjust "
            "it. Incorporate the reviewer's note into the draft -- where the note and the draft "
            "disagree, the reviewer's note wins -- and rewrite the result as a warm, "
            "professional, ready-to-send message directly to the customer."
        )

        self.reject_prompt = ChatPromptTemplate.from_template(
            "Customer question: {question}\n"
            "Internal reason this request cannot be approved as asked: {reason}\n"
            "Human reviewer's additional note (may be empty): {human_note}\n\n"
            "Write a polite, professional message telling the customer this specific request "
            "can't be completed exactly as asked. Explain briefly in plain language WHY (never "
            "mention internal policy IDs, doc names, or risk scores), and say what happens next "
            "(e.g. it's being escalated, a specialist will follow up). Keep it empathetic, not "
            "robotic, and do not apologize excessively."
        )

    def approve(self, question: str, draft_text: str) -> str:
        result: FinalResponse = (self.approve_prompt | self.llm).invoke(
            {"question": question, "draft": draft_text}
        )
        return result.customer_message

    def edit(self, question: str, draft_text: str, human_note: str) -> str:
        result: FinalResponse = (self.edit_prompt | self.llm).invoke(
            {"question": question, "draft": draft_text, "human_note": human_note or "(none)"}
        )
        return result.customer_message

    def reject(self, question: str, reason: str, human_note: str = "") -> str:
        result: FinalResponse = (self.reject_prompt | self.llm).invoke(
            {"question": question, "reason": reason, "human_note": human_note or "(none)"}
        )
        return result.customer_message
