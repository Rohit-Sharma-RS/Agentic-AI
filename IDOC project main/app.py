import gradio as gr
from orchestrator import Orchestrator
from edi850_parser import sample_edi_850, sample_edi_850_multi, LineItemSpec
from business_rules import (
    configure_partner, unblock_material, raise_credit_limit,
    add_customer, add_material_mapping, replenish_stock,
)
from fix_validator import validate_fix_inputs, infer_required_fixes, remaining_fixes_after
from models import InboundCheckResult

_orch = None
# Track last check_result for fix validation
_last_check_result: InboundCheckResult | None = None


def get_orch():
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch

SAMPLES = {
    "[OK] Happy path - single item (WIDGET-X, qty 50)":
        sample_edi_850(),

    "[OK] Multi-item happy path (WIDGET-X + GADGET-Y)":
        sample_edi_850_multi(),

    "[WARN] Multi-item MIXED: 1 good + 1 partial fill + 1 blocked":
        sample_edi_850_multi(line_items=[
            LineItemSpec("WIDGET-X",        50, 120.00),  # OK
            LineItemSpec("WIDGET-LOWSTOCK", 20,  30.00),  # partial fill
            LineItemSpec("WIDGET-BLOCKED",   5,  75.00),  # blocked -> error
        ]),

    "[ERROR] Multi-item ALL FAIL: blocked + zero-stock + unknown material":
        sample_edi_850_multi(line_items=[
            LineItemSpec("WIDGET-BLOCKED",  5,  75.00),   # blocked
            LineItemSpec("WIDGET-ZERO",    10,  55.00),   # zero stock
            LineItemSpec("GADGET-UNKNOWN",  3,  99.00),   # unknown material
        ]),

    "[ERROR] Unknown partner (CUST_9999 - no WE20 profile)":
        sample_edi_850(partner_id="CUST_9999"),

    "[ERROR] Blocked material (WIDGET-BLOCKED)":
        sample_edi_850(material="WIDGET-BLOCKED"),

    "[WARN] Low stock -> PARTIAL FILL + backorder (WIDGET-LOWSTOCK, qty=20, stock=5)":
        sample_edi_850(material="WIDGET-LOWSTOCK", qty=20, unit_price=30.00),

    "[ERROR] Zero stock -> human replenishment required (WIDGET-ZERO)":
        sample_edi_850(material="WIDGET-ZERO", qty=10, unit_price=55.00),

    "[ERROR] Credit limit exceeded (large order to customer 200099)":
        sample_edi_850(ship_to_id="200099", qty=500, unit_price=120.00),

    "[ERROR] Unknown customer / ship-to (200001 - not in customer master)":
        sample_edi_850(ship_to_id="200001"),

    "[ERROR] Price mismatch >5% (GADGET-PREMIUM agreed $200, submitting $280)":
        sample_edi_850(material="GADGET-PREMIUM", unit_price=280.00, qty=5),

    "[ERROR] Delivery date in the past":
        sample_edi_850(delivery_date="20200101"),

    "[ERROR] Multi-item: credit exceeded + price mismatch (two independent errors)":
        sample_edi_850_multi(
            ship_to_id="200099",
            line_items=[
                LineItemSpec("GADGET-PREMIUM", 5,  280.00),  # price mismatch
                LineItemSpec("WIDGET-X",      500, 120.00),  # credit exceeded
            ]
        ),

    "[ERROR] Malformed EDI (missing BEG segment)":
        "ISA*CUST_1001~\nN1*ST*Customer*92*200050~\nSE*3*0001~",
}


def load_sample(name):
    return SAMPLES.get(name, "")


def run_process(raw_edi):
    global _last_check_result
    _last_check_result = None

    try:
        result = get_orch().process(raw_edi)
    except Exception as e:
        return (
            f"**[ERROR] Unexpected pipeline error:** `{e}`\n\n"
            "Check that the database exists (`python create_db.py`) and "
            "that your `.env` file has a valid `GOOGLE_API_KEY`.\n\n"
            "**Tip:** Run `python create_db.py --force` to rebuild the database.",
            "",
            gr.update(visible=False),
            None,
            "",
        )

    if result.check_result:
        _last_check_result = result.check_result

    arrow_chain = " -> ".join(result.status_history)
    status_icon = {
        "53":          "[OK]",
        "partial":     "[PARTIAL]",
        "51":          "[ERROR]",
        "duplicate":   "[DUPLICATE]",
        "parse_error": "[PARSE ERROR]",
    }.get(result.final_status, "[?]")

    status_md = (
        f"## {status_icon} Final Status: `{result.final_status}`\n\n"
        f"**IDoc Number:** `{result.idoc_number}`  |  "
        f"**Buyer PO:** `{result.buyer_po_number}`\n\n"
        f"**Status lifecycle:** {arrow_chain}\n"
    )

    # ── Failures & warnings per item ──────────────────────────────────────────
    if result.check_result:
        cr = result.check_result
        if cr.failures:
            status_md += f"\n### Failures ({len(cr.failures)} total)\n"
            for i, f in enumerate(cr.failures, 1):
                status_md += f"**{i}.** {f}\n\n"

        if cr.warnings:
            status_md += f"\n### Warnings - Auto-resolved ({len(cr.warnings)} total)\n"
            for w in cr.warnings:
                status_md += f"- {w}\n"

        if cr.partial_fill:
            status_md += "\n> **Partial Fill applied:** system confirmed available stock and placed remainder on backorder. EDI 855 sent with BP backorder lines.\n"

        # Show all required fix types when multiple errors
        if cr.failures:
            needed = infer_required_fixes(cr)
            if needed:
                status_md += "\n### Required Fix Actions (in order)\n"
                for i, fix in enumerate(needed, 1):
                    status_md += f"{i}. **{fix}**\n"
                if any(f == "Ask trading partner to resubmit" for f in needed):
                    status_md += (
                        "\n> **Note:** Some errors require the trading partner to "
                        "correct and resubmit the EDI 850. These cannot be fixed from the admin panel.\n"
                    )

    # ── LLM Diagnosis ─────────────────────────────────────────────────────────
    if result.diagnosis:
        d = result.diagnosis
        status_md += "\n### Agent Diagnosis (LLM + Policy RAG)\n"
        status_md += f"**Explanation:** {d.plain_explanation}\n\n"
        status_md += f"**Suggested fix:** {d.suggested_fix}\n\n"
        if d.auto_resolution:
            status_md += f"**Auto-resolution applied:** `{d.auto_resolution}`\n\n"
        if d.human_actions:
            status_md += "**Human actions required (step by step):**\n"
            for i, a in enumerate(d.human_actions, 1):
                status_md += f"  {i}. {a}\n"
        if d.citations:
            status_md += f"\n**Policy citations:** {', '.join(d.citations)}\n"

    # ── Sales order ────────────────────────────────────────────────────────────
    if result.sales_order:
        so = result.sales_order
        status_md += "\n### Sales Order Created\n"
        status_md += f"**Order Number:** `{so.order_number}`  |  **Value:** `${so.total_value:,.2f}`\n"
        if so.backorder_note:
            status_md += f"\n> {so.backorder_note}\n"

    # ── EDI 855 ────────────────────────────────────────────────────────────────
    if result.edi_855:
        status_md += "\n### Outbound EDI 855 (Purchase Order Acknowledgment)\n"
        status_md += f"```\n{result.edi_855}\n```\n"

    idoc_out = result.idoc_formatted or ""
    fix_visible = gr.update(visible=result.requires_human)
    fix_hint = _build_fix_hint(result.check_result) if result.requires_human else ""

    return status_md, idoc_out, fix_visible, raw_edi, fix_hint


def _build_fix_hint(check_result: InboundCheckResult | None) -> str:
    """Build a hint string showing all required fix actions in sequence."""
    if not check_result or not check_result.failures:
        return ""
    needed = infer_required_fixes(check_result)
    if not needed:
        return ""
    lines = ["**This IDoc has multiple issues. Apply these fixes IN ORDER, reprocessing after each:**"]
    for i, fix in enumerate(needed, 1):
        lines.append(f"  {i}. {fix}")
    lines.append("\nAfter each fix, click 'Apply fix & Reprocess'. Remaining issues will show again.")
    return "\n".join(lines)

def apply_fix_and_reprocess(
    fix_type,
    partner_id_in, partner_name_in,
    customer_id_in, credit_limit_in,
    cust_mat_in, sap_mat_in, price_in, init_stock_in,
    material_unblock_in,
    replenish_mat_in, replenish_qty_in,
    credit_sold_to_in, credit_new_limit_in,
    raw_edi,
):
    global _last_check_result

    if not raw_edi:
        return (
            "**[ERROR]** No pending IDoc to reprocess. Process an EDI 850 first.",
            "", gr.update(visible=True), None, "",
        )

    # Collect all inputs into dict for validator
    inputs = {
        "partner_id_in":      partner_id_in      or "",
        "partner_name_in":    partner_name_in     or "",
        "customer_id_in":     customer_id_in      or "",
        "credit_limit_in":    credit_limit_in     or "",
        "cust_mat_in":        cust_mat_in         or "",
        "sap_mat_in":         sap_mat_in          or "",
        "price_in":           price_in            or "",
        "init_stock_in":      init_stock_in       or "",
        "material_unblock_in":material_unblock_in or "",
        "replenish_mat_in":   replenish_mat_in    or "",
        "replenish_qty_in":   replenish_qty_in    or "",
        "credit_sold_to_in":  credit_sold_to_in   or "",
        "credit_new_limit_in":credit_new_limit_in or "",
    }

    validation_errors = validate_fix_inputs(fix_type, inputs, _last_check_result)

    # Separate hard errors from mismatch warnings
    hard_errors   = [e for e in validation_errors if not e.startswith("WARNING:")]
    mismatch_warn = [e for e in validation_errors if e.startswith("WARNING:")]

    if hard_errors:
        err_md = "### Fix Validation Failed\n\nPlease correct these issues before applying:\n\n"
        for e in hard_errors:
            err_md += f"- {e}\n"
        if mismatch_warn:
            err_md += "\n**Also note:**\n"
            for w in mismatch_warn:
                err_md += f"- {w}\n"
        return err_md, "", gr.update(visible=True), raw_edi, _build_fix_hint(_last_check_result)

    prefix_md = ""
    if mismatch_warn:
        prefix_md = "### Mismatch Warning\n\n"
        for w in mismatch_warn:
            prefix_md += f"> {w}\n\n"
        prefix_md += "Proceeding with fix anyway as requested...\n\n---\n\n"

    try:
        if fix_type == "Add partner profile":
            name = inputs["partner_name_in"].strip() or inputs["partner_id_in"].strip()
            configure_partner(inputs["partner_id_in"].strip(), name, "ORDE")

        elif fix_type == "Add customer master":
            add_customer(
                inputs["customer_id_in"].strip(),
                float(inputs["credit_limit_in"].strip()),
            )

        elif fix_type == "Add material mapping":
            add_material_mapping(
                inputs["cust_mat_in"].strip(),
                inputs["sap_mat_in"].strip(),
                float(inputs["price_in"].strip()),
                float(inputs["init_stock_in"].strip() or "0"),
            )

        elif fix_type == "Unblock material":
            unblock_material(inputs["material_unblock_in"].strip())

        elif fix_type == "Replenish stock":
            ok = replenish_stock(
                inputs["replenish_mat_in"].strip(),
                float(inputs["replenish_qty_in"].strip()),
            )
            if not ok:
                return (
                    f"**[ERROR]** Material `{inputs['replenish_mat_in']}` not found in "
                    "material master. Use 'Add material mapping' first.",
                    "", gr.update(visible=True), raw_edi, _build_fix_hint(_last_check_result),
                )

        elif fix_type == "Raise credit limit":
            raise_credit_limit(
                inputs["credit_sold_to_in"].strip(),
                float(inputs["credit_new_limit_in"].strip()),
            )

        elif fix_type == "Ask trading partner to resubmit":
            return (
                "**[INFO]** This error requires the trading partner to correct and "
                "resubmit the EDI 850. No DB fix can be applied from this panel.\n\n"
                "Notify the trading partner of the specific issues listed above.",
                "", gr.update(visible=True), raw_edi, _build_fix_hint(_last_check_result),
            )

    except Exception as e:
        return (
            f"**[ERROR]** Failed to apply fix `{fix_type}`: `{e}`\n\n"
            "Check field values and try again.",
            "", gr.update(visible=True), raw_edi, _build_fix_hint(_last_check_result),
        )

    # ── Step 3: Reprocess ─────────────────────────────────────────────────────
    try:
        status_md, idoc_out, fix_visible, state_val, fix_hint = run_process(raw_edi)
    except Exception as e:
        return (
            f"**[ERROR]** Reprocessing failed: `{e}`",
            "", gr.update(visible=True), raw_edi, "",
        )

    # ── Step 4: Check remaining fixes after reprocess ─────────────────────────
    remaining = []
    if _last_check_result and _last_check_result.failures:
        remaining = remaining_fixes_after(fix_type, _last_check_result)

    if remaining:
        prefix_md += (
            f"\n\n---\n**Fix `{fix_type}` applied.** "
            f"Still {len(_last_check_result.failures)} failure(s) remaining. "
            f"Next required fix: **{remaining[0]}**\n"
        )

    return prefix_md + status_md, idoc_out, fix_visible, state_val, fix_hint

with gr.Blocks(
    title="SAP IDoc Pipeline - EDI 850 -> ORDERS05 -> EDI 855",
    theme=gr.themes.Soft(),
) as demo:
    gr.Markdown(
        "# SAP IDoc Processing Pipeline\n"
        "### EDI 850 -> IDoc ORDERS05 -> ORDRSP -> EDI 855\n\n"
    )

    with gr.Row():
        with gr.Column(scale=2):
            sample_dd   = gr.Dropdown(list(SAMPLES.keys()), label="Load a demo scenario")
            edi_box     = gr.Textbox(
                label="Raw EDI 850 Input", lines=14,
                placeholder="Paste or load an EDI 850 here... (supports multiple PO1 line items)"
            )
            process_btn = gr.Button("Process EDI 850", variant="primary", size="lg")
        with gr.Column(scale=1):
            gr.Markdown(
                
            )

    sample_dd.change(load_sample, inputs=sample_dd, outputs=edi_box)

    status_out = gr.Markdown(label="Pipeline Result")
    state      = gr.State()

    with gr.Accordion("IDoc Segment Detail (WE02/WE05 format)", open=False):
        idoc_out = gr.Code(label="IDoc Printout", language=None, lines=30)

    with gr.Group(visible=False) as fix_group:
        gr.Markdown("---\n### Human-in-the-Loop Remediation (BD87)")
        fix_hint_box = gr.Markdown(label="Fix guidance")

        fix_type = gr.Radio(
            [
                "Add partner profile",
                "Add customer master",
                "Add material mapping",
                "Unblock material",
                "Replenish stock",
                "Raise credit limit",
                "Ask trading partner to resubmit",
            ],
            label="Fix action (select one, apply, reprocess; repeat for each remaining error)",
        )

        with gr.Row():
            with gr.Column():
                gr.Markdown("**Partner Profile**")
                partner_id_in   = gr.Textbox(label="Partner ID",   placeholder="CUST_9999")
                partner_name_in = gr.Textbox(label="Partner Name", placeholder="Acme Corp")

            with gr.Column():
                gr.Markdown("**New Customer**")
                customer_id_in  = gr.Textbox(label="Customer / Ship-to ID", placeholder="200001")
                credit_limit_in = gr.Textbox(label="Initial credit limit",  placeholder="25000")

        with gr.Row():
            with gr.Column():
                gr.Markdown("**Material Mapping (for unknown materials)**")
                cust_mat_in   = gr.Textbox(label="Customer material code", placeholder="GADGET-UNKNOWN")
                sap_mat_in    = gr.Textbox(label="SAP material number",    placeholder="MAT-20001")
                price_in      = gr.Textbox(label="Agreed unit price",       placeholder="99.00")
                init_stock_in = gr.Textbox(label="Initial stock qty",       placeholder="500")

            with gr.Column():
                gr.Markdown("**Unblock Material**")
                material_unblock_in = gr.Textbox(
                    label="Customer material to unblock", placeholder="WIDGET-BLOCKED"
                )

                gr.Markdown("**Stock Replenishment**")
                replenish_mat_in = gr.Textbox(label="Material to replenish", placeholder="WIDGET-ZERO")
                replenish_qty_in = gr.Textbox(label="Quantity to add",       placeholder="100")

                gr.Markdown("**Credit Limit**")
                credit_sold_to_in   = gr.Textbox(label="Sold-to ID",       placeholder="200099")
                credit_new_limit_in = gr.Textbox(label="New credit limit",  placeholder="20000")

        reprocess_btn = gr.Button("Apply fix & Reprocess", variant="secondary")

    process_btn.click(
        run_process,
        inputs=edi_box,
        outputs=[status_out, idoc_out, fix_group, state, fix_hint_box],
    )
    reprocess_btn.click(
        apply_fix_and_reprocess,
        inputs=[
            fix_type,
            partner_id_in, partner_name_in,
            customer_id_in, credit_limit_in,
            cust_mat_in, sap_mat_in, price_in, init_stock_in,
            material_unblock_in,
            replenish_mat_in, replenish_qty_in,
            credit_sold_to_in, credit_new_limit_in,
            state,
        ],
        outputs=[status_out, idoc_out, fix_group, state, fix_hint_box],
    )


if __name__ == "__main__":
    demo.launch()
