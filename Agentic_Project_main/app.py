import gradio as gr
from orchestrator import Orchestrator

_orchestrator = None


def get_orchestrator() -> Orchestrator:
    """Built lazily (and once) so the app can still load its UI even
    before an API key / network is available -- the error only surfaces
    when you actually click Run."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def run_pipeline(question: str, order_id: str, sql_password: str):
    if not question or not question.strip():
        return "", "", "", "", gr.update(visible=False), gr.update(value="Please enter a question."), None

    try:
        orch = get_orchestrator()
        result = orch.run(
            question,
            order_id=order_id.strip() or None,
            sql_password=sql_password.strip() or None,
        )
    except ValueError as e:
        # Guardrail rejections (missing/invalid order ID, wrong password,
        # blocked input, etc.) -- expected, shown as a plain status message.
        return "", "", "", "", gr.update(visible=False), gr.update(value=f"⚠️ {e}"), None
    except Exception as e:
        return "", "", "", "", gr.update(visible=False), gr.update(value=f"❌ Error: {e}"), None

    draft_md = (
        f"**Route:** {result.route}"
        + (f"  |  **Order:** {result.order_id}" if result.order_id else "")
        + (f"  |  **Eligible amount:** ${result.eligible_refund_amount:.2f}" if result.eligible_refund_amount is not None else "")
        + (f"  |  **Confirmation turn:** {result.is_confirmation_turn}" if result.is_confirmation_turn is not None else "")
        + f"\n\n**Answer:** {result.draft.answer}\n\n"
        f"**Sources:** {', '.join(result.draft.sources)}\n\n"
        f"**Confidence:** {result.draft.confidence:.2f}"
    )
    eval_md = (
        f"**Verdict:** {result.evaluation.verdict}  |  "
        f"**Groundedness:** {result.evaluation.groundedness_score:.2f}  |  "
        f"**Relevance:** {result.evaluation.relevance_score:.2f}  |  "
        f"**Hallucination risk:** {result.evaluation.hallucination_risk}\n\n"
        f"_{result.evaluation.notes}_\n\n"
        f"*(These scores are the Evaluator LLM's own judgment call, not a computed metric — "
        f"treat them as a flagging signal, not ground truth.)*"
    )
    compliance_md = (
        f"**Compliant:** {result.compliance.is_compliant}  |  "
        f"**Risk level:** {result.compliance.risk_level}\n\n"
        f"**Policy citations:** {', '.join(result.compliance.policy_citations)}\n\n"
        f"_{result.compliance.reasoning}_"
    )
    if result.compliance.revised_answer:
        compliance_md += f"\n\n**Suggested revision:** {result.compliance.revised_answer}"

    if result.requires_human_approval:
        status = gr.update(value="⚠️ Flagged for human review before this can be sent to the customer.")
        review_visible = gr.update(visible=True)
        final_md = ""
    else:
        status = gr.update(value="✅ Auto-approved (low risk, grounded, policy-compliant).")
        review_visible = gr.update(visible=False)
        final_md = f"**Final answer sent to customer:**\n\n{result.final_answer}"
        if result.refund_processed:
            final_md += f"\n\n💳 **{result.refund_notification}**"

    return draft_md, eval_md, compliance_md, final_md, review_visible, status, result


def submit_human_decision(decision: str, note_text: str, result):
    if result is None:
        return "No pipeline result to act on.", gr.update(visible=True)

    try:
        orch = get_orchestrator()
        key = {"Approve": "approved", "Edit": "edited", "Reject": "rejected"}[decision]
        result = orch.finalize_with_human(result, key, note_text)
    except Exception as e:
        return f"❌ Error while finalizing: {e}", gr.update(visible=True)

    final_md = f"**Final answer sent to customer** (human decision: {key}):\n\n{result.final_answer}"
    if result.refund_processed:
        final_md += f"\n\n💳 **{result.refund_notification}**"
    return final_md, gr.update(visible=False)


with gr.Blocks(title="Agentic RAG + SQL + Refunds, with Guardrails & Human-in-the-loop") as demo:
    gr.Markdown(
        "# Agentic RAG + SQL + Refund Pipeline\n"
        "**Router Agent** picks a specialist: **Refund Agent** (needs your Order ID) for "
        "your own order, **Knowledge Agent** (product-docs RAG) for general policy/how-to "
        "questions, or **SQL Agent** (password-protected) for counts/ranges/averages across "
        "many orders → **Evaluator Agent** → **Compliance Agent** → human review when flagged.\n\n"
        "All agents run on Gemini (`gemini-3.5-flash-lite` + `gemini-embedding-2`) via LangChain. "
        "🔒 Account numbers and phone numbers are never sent to the LLM in any path — see "
        "`order_lookup.py` and the SQL guardrail."
    )

    question_box = gr.Textbox(
        label="Question",
        placeholder="e.g. 'Can I get a refund for my damaged item?' or 'How many refunds over $100 last month?'",
    )
    with gr.Row():
        order_id_box = gr.Textbox(
            label="Order ID (only needed for refund questions)",
            placeholder="ORD-00001",
        )
        sql_password_box = gr.Textbox(
            label="Analyst password (only needed for data/SQL questions)",
            type="password",
            placeholder="default: abcdef",
        )
    run_btn = gr.Button("Run pipeline", variant="primary")
    status_box = gr.Markdown()

    with gr.Row():
        draft_out = gr.Markdown(label="Agent draft")
        eval_out = gr.Markdown(label="Evaluator Agent grading")
        compliance_out = gr.Markdown(label="Compliance Agent check")

    with gr.Group(visible=False) as review_group:
        gr.Markdown(
            "### Human-in-the-loop review required\n"
            "Nothing below is sent to the customer as-is — whatever you choose, a Response "
            "Writer agent rewrites it into a proper customer-facing message before it's sent. "
            "For refunds, Approve/Edit also actually processes the refund (see the confirmation "
            "line that appears below once submitted).\n\n"
            "- **Approve**: sends the (compliance-corrected) draft, polished for tone.\n"
            "- **Edit**: type a short instruction/correction below — it gets *incorporated and "
            "rewritten*, not sent verbatim.\n"
            "- **Reject**: writes a polite decline explaining why, using the compliance/"
            "evaluator reasoning — add a note below for extra context if useful."
        )
        decision_radio = gr.Radio(["Approve", "Edit", "Reject"], label="Decision", value="Approve")
        note_box = gr.Textbox(
            label="Note (instruction for Edit, optional context for Reject, ignored for Approve)",
            lines=3,
            placeholder="e.g. 'Also mention we've extended the return window to 45 days for this customer.'",
        )
        submit_btn = gr.Button("Submit decision")

    final_out = gr.Markdown(label="Final answer")
    state = gr.State()

    run_btn.click(
        run_pipeline,
        inputs=[question_box, order_id_box, sql_password_box],
        outputs=[draft_out, eval_out, compliance_out, final_out, review_group, status_box, state],
    )
    submit_btn.click(
        submit_human_decision,
        inputs=[decision_radio, note_box, state],
        outputs=[final_out, review_group],
    )

if __name__ == "__main__":
    demo.launch()
