"""fix_validator.py -- Pre-flight validation for human-in-the-loop fix actions.

PROBLEM: A human may select the wrong fix type (e.g. "Unblock material"
when the actual error was "Credit limit exceeded"), or fill in wrong/empty
fields.  This module validates BEFORE any DB mutation so we catch
mismatches early with a clear message, not a silent no-op.

ALSO handles: multiple simultaneous failures across multiple items require
multiple sequential fixes.  This module detects which fix types are still
needed after a partial fix so the UI can guide the operator.
"""
from __future__ import annotations
from models import InboundCheckResult

# ── Fix-type → required input fields ─────────────────────────────────────────

FIX_REQUIRED_FIELDS: dict[str, list[tuple[str, str]]] = {
    "Add partner profile": [
        ("partner_id_in",   "Partner ID"),
    ],
    "Add customer master": [
        ("customer_id_in",  "Customer / Ship-to ID"),
        ("credit_limit_in", "Initial credit limit (must be a positive number)"),
    ],
    "Add material mapping": [
        ("cust_mat_in",  "Customer material code"),
        ("sap_mat_in",   "SAP material number"),
        ("price_in",     "Agreed unit price (must be a positive number)"),
        ("init_stock_in","Initial stock qty (must be >= 0)"),
    ],
    "Unblock material": [
        ("material_unblock_in", "Customer material to unblock"),
    ],
    "Replenish stock": [
        ("replenish_mat_in", "Material to replenish"),
        ("replenish_qty_in", "Quantity to add (must be > 0)"),
    ],
    "Raise credit limit": [
        ("credit_sold_to_in",   "Sold-to ID"),
        ("credit_new_limit_in", "New credit limit (must be > current limit)"),
    ],
}

# ── Keywords in failure messages that map to the correct fix type ─────────────

FAILURE_FIX_MAP: list[tuple[str, str]] = [
    ("no WE20 profile",         "Add partner profile"),
    ("partner",                 "Add partner profile"),
    ("BLOCKED",                 "Unblock material"),
    ("blocked",                 "Unblock material"),
    ("zero stock",              "Replenish stock"),
    ("zero stock available",    "Replenish stock"),
    ("credit check failed",     "Raise credit limit"),
    ("credit limit",            "Raise credit limit"),
    ("not found in customer master", "Add customer master"),
    ("ship-to",                 "Add customer master"),
    ("unknown customer",        "Add customer master"),
    ("no SAP material mapping", "Add material mapping"),
    ("unknown customer material","Add material mapping"),
    ("price",                   "Ask trading partner to resubmit"),
    ("delivery date",           "Ask trading partner to resubmit"),
    ("quantity must be",        "Ask trading partner to resubmit"),
    ("unit price cannot",       "Ask trading partner to resubmit"),
]

# Fix types that CAN'T be applied via the UI (partner must resubmit the EDI)
RESUBMIT_ONLY_FIXES = {"Ask trading partner to resubmit"}


def infer_required_fixes(check_result: InboundCheckResult) -> list[str]:
    """
    From a check result, infer ALL fix types still needed (in priority order).
    Returns a deduplicated list so the operator knows what to apply sequentially.
    """
    needed: list[str] = []
    seen: set[str] = set()
    for failure in check_result.failures:
        fl = failure.lower()
        for keyword, fix_type in FAILURE_FIX_MAP:
            if keyword.lower() in fl and fix_type not in seen:
                needed.append(fix_type)
                seen.add(fix_type)
    return needed


def validate_fix_inputs(
    fix_type: str,
    inputs: dict[str, str],
    check_result: InboundCheckResult | None = None,
) -> list[str]:
    """
    Return a list of validation error strings.
    Empty list = all good, proceed with fix.

    Checks:
      1. fix_type is not None / empty
      2. Required fields for the chosen fix_type are non-empty
      3. Numeric fields are actually numeric and in valid range
      4. (Optional) fix_type matches a known failure in check_result
         — warns but does NOT block (operator may know better)
    """
    errors: list[str] = []

    if not fix_type:
        errors.append("No fix action selected. Please choose a fix type from the panel.")
        return errors  # can't continue without a type

    if fix_type in RESUBMIT_ONLY_FIXES:
        errors.append(
            f"'{fix_type}' cannot be applied via the admin panel — "
            "the trading partner must correct and resubmit the EDI 850."
        )
        return errors

    # Required-field checks
    required = FIX_REQUIRED_FIELDS.get(fix_type, [])
    for field_key, label in required:
        val = inputs.get(field_key, "").strip()
        if not val:
            errors.append(f"Missing required field for '{fix_type}': {label}")

    # Numeric range checks
    def _check_positive(key: str, label: str) -> None:
        val = inputs.get(key, "").strip()
        if not val:
            return  # already caught above
        try:
            n = float(val)
            if n <= 0:
                errors.append(f"{label} must be > 0 (got {val}).")
        except ValueError:
            errors.append(f"{label} must be a number (got '{val}').")

    def _check_non_negative(key: str, label: str) -> None:
        val = inputs.get(key, "").strip()
        if not val:
            return
        try:
            n = float(val)
            if n < 0:
                errors.append(f"{label} must be >= 0 (got {val}).")
        except ValueError:
            errors.append(f"{label} must be a number (got '{val}').")

    if fix_type == "Add customer master":
        _check_positive("credit_limit_in", "Initial credit limit")

    elif fix_type == "Add material mapping":
        _check_non_negative("price_in",     "Agreed unit price")
        _check_non_negative("init_stock_in","Initial stock qty")

    elif fix_type == "Replenish stock":
        _check_positive("replenish_qty_in", "Quantity to add")

    elif fix_type == "Raise credit limit":
        _check_positive("credit_new_limit_in", "New credit limit")

    # Mismatch warning (non-blocking) — if check_result is provided
    if check_result and not errors:
        needed = infer_required_fixes(check_result)
        if needed and fix_type not in needed and fix_type not in RESUBMIT_ONLY_FIXES:
            errors.append(
                f"WARNING: Selected fix '{fix_type}' does not match any known "
                f"failure in this IDoc.  Expected fix(es): {', '.join(needed)}.  "
                "If you are sure, click Apply again to override — "
                "or select the correct fix type first."
            )

    return errors


def remaining_fixes_after(
    fix_type_applied: str,
    check_result: InboundCheckResult,
) -> list[str]:
    """
    Return the list of fix types still needed AFTER a specific fix was applied.
    Useful when multiple items have different errors.
    """
    all_needed = infer_required_fixes(check_result)
    return [f for f in all_needed if f != fix_type_applied]
