"""agents.py -- the one real LLM decision point in this pipeline.

All status transitions and business-rule pass/fail are deterministic
(business_rules.py); this agent only EXPLAINS a failure and suggests
a concrete fix for the ops team, grounded in RAG over business_rules_kb.txt.

It also identifies whether the problem has an automated resolution
(e.g. partial fill already applied) or requires a human action, and
if so, which specific action to take from the UI fix panel.
"""
from __future__ import annotations
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if (PROJECT_ROOT / ".env").exists():
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import CharacterTextSplitter
from langchain_core.prompts import ChatPromptTemplate
from models import ErrorDiagnosis, InboundCheckResult

CHAT_MODEL      = os.getenv("CHAT_MODEL",      "gemini-3.5-flash-lite")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "models/gemini-embedding-001"
)
KB_PATH = os.getenv(
    "BUSINESS_RULES_KB_PATH",
    str(PROJECT_ROOT / "data" / "business_rules_kb.txt"),
)


def _build_vectorstore(doc_path: str) -> FAISS:
    with open(doc_path, encoding="utf-8") as f:
        raw = f.read()
    chunks, ids = [], []
    for block in raw.split("### DOC: ")[1:]:
        doc_id, _, body = block.partition("\n")
        chunks.append(body.strip())
        ids.append(doc_id.strip())
    splitter = CharacterTextSplitter(chunk_size=600, chunk_overlap=80)
    docs = splitter.create_documents(chunks, metadatas=[{"doc_id": i} for i in ids])
    return FAISS.from_documents(docs, GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL))


_PROMPT = ChatPromptTemplate.from_template(
    "An inbound SAP IDoc failed business-rule checks (status 51). "
    "Your job is to:\n"
    "  1. Explain WHY it failed in plain language for an ops/integration person.\n"
    "  2. State the EXACT human action needed to fix it (one of: "
    "'Add partner profile', 'Add customer', 'Add material mapping', "
    "'Unblock material', 'Raise credit limit', 'Replenish stock', "
    "'Ask trading partner to resubmit', 'Finance approval required', or 'No action — auto-resolved').\n"
    "  3. If the problem was already auto-resolved by the system (e.g. partial fill), "
    "set auto_resolution to a short label like 'PARTIAL_FILL_APPLIED'.\n"
    "  4. List the human_actions as a flat list of specific steps.\n\n"
    "Use ONLY the policy context below — never invent a policy not present.\n\n"
    "Policy context:\n{context}\n\n"
    "Failures:\n{failures}\n\n"
    "Warnings (auto-resolved):\n{warnings}\n\n"
    "List which doc_ids you relied on in `citations`."
)


class ErrorDiagnosisAgent:
    def __init__(self, kb_path: str = KB_PATH):
        self.store  = _build_vectorstore(kb_path)
        self.llm    = ChatGoogleGenerativeAI(
            model=CHAT_MODEL, temperature=0
        ).with_structured_output(ErrorDiagnosis)
        self.prompt = _PROMPT

    def diagnose(self, check_result: InboundCheckResult) -> ErrorDiagnosis:
        all_issues = check_result.failures + check_result.warnings
        issues_text   = "\n".join(f"- {f}" for f in check_result.failures) or "(none)"
        warnings_text = "\n".join(f"- {w}" for w in check_result.warnings)   or "(none)"

        # Retrieve relevant policy chunks
        query = " ".join(all_issues) if all_issues else "IDoc processing failure"
        hits  = self.store.similarity_search(query, k=5)
        context = "\n---\n".join(
            f"[{d.metadata['doc_id']}] {d.page_content}" for d in hits
        )
        return (self.prompt | self.llm).invoke({
            "context":  context,
            "failures": issues_text,
            "warnings": warnings_text,
        })
