"""
audit_log.py
------------
Logging must NEVER block or break the human-in-the-loop flow -- if the
log file is locked, disk is full, whatever: swallow it, print a
warning, keep going. Logging failures are never allowed to stop a
refund decision from reaching the customer.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_PATH = os.getenv("AUDIT_LOG_PATH", str(PROJECT_ROOT / "data" / "audit.log"))

logger = logging.getLogger("agentic_rag_audit")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_PATH)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except Exception:
        pass  # even failing to set up file logging must not crash the app


def safe_log(event: str, data: dict) -> None:
    """Never raises. Worst case: prints to console instead of the file."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    line = json.dumps(record)
    try:
        logger.info(line)
    except Exception as e:
        print(f"(audit log failed, non-fatal: {e}) {line}")
