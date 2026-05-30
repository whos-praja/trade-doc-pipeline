"""Streamlit UI — one screen over the REAL pipeline (no mocks).

    streamlit run ui/streamlit_app.py

Pick a sample (or upload) -> Run pipeline (app.pipeline.run_pipeline) -> the run is
persisted to SQLite and rendered here: extracted fields (value + confidence badge +
source snippets), validation chips, the routing decision, and the draft email (the
Send button is disabled by design — the agent never sends). A bottom box answers
natural-language questions via app.query.answer_question.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Make `import app...` work no matter where streamlit is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from app import config, storage
from app.pipeline import PipelineError, run_pipeline
from app.query import answer_question
from app.schema import EXTRACTION_FIELDS

RULES_DIR = config.PROJECT_ROOT / "app" / "rules"
SAMPLES_DIR = config.SAMPLES_DIR

_GREEN, _AMBER, _RED, _GREY = "#1a7f37", "#bf8700", "#c1121f", "#6c757d"
_OUTCOME = {
    "AUTO_APPROVE": (_GREEN, "✅"),
    "REVIEW": (_AMBER, "🔎"),
    "AMEND": (_RED, "✋"),
}
_STATUS_COLOR = {"match": _GREEN, "mismatch": _RED, "uncertain": _AMBER}


def _badge(text: str, color: str) -> str:
    return (f"<span style='background:{color};color:white;padding:2px 9px;"
            f"border-radius:10px;font-size:0.82em;white-space:nowrap'>{text}</span>")


def _conf_color(c: float) -> str:
    return _GREEN if c >= 0.8 else _AMBER if c >= 0.6 else _RED


# --- page -------------------------------------------------------------------

st.set_page_config(page_title="Trade Document Pipeline", layout="wide")
st.title("📄 Trade Document Pipeline")
st.caption("Extractor → Validator → Router → SQLite, with NL query. Runs the real pipeline; "
           "the agent drafts but never sends.")

samples = sorted(p.name for p in SAMPLES_DIR.glob("*.pdf"))
samples += sorted(p.name for p in SAMPLES_DIR.glob("*.png"))
samples += sorted(p.name for p in SAMPLES_DIR.glob("*.jpg"))
customers = sorted(p.stem for p in RULES_DIR.glob("*.json")) or ["meridian"]

with st.container(border=True):
    c1, c2, c3 = st.columns([3, 2, 1.2])
    with c1:
        mode = st.radio("Document source", ["Sample", "Upload"], horizontal=True)
        sample = uploaded = None
        if mode == "Sample":
            default_ix = samples.index("clean_1.pdf") if "clean_1.pdf" in samples else 0
            sample = st.selectbox("Sample document", samples, index=default_ix) if samples else None
        else:
            uploaded = st.file_uploader("Upload a PDF or image", type=["pdf", "png", "jpg", "jpeg"])
    with c2:
        cust_ix = customers.index("meridian") if "meridian" in customers else 0
        customer = st.selectbox("Customer rule set", customers, index=cust_ix)
    with c3:
        st.write("")
        st.write("")
        run_clicked = st.button("▶ Run pipeline", type="primary", use_container_width=True)

if run_clicked:
    if uploaded is not None:
        doc_path = Path(tempfile.gettempdir()) / f"upload_{uploaded.name}"
        doc_path.write_bytes(uploaded.getvalue())
        doc_path = str(doc_path)
    elif sample:
        doc_path = str(SAMPLES_DIR / sample)
    else:
        doc_path = None

    if not doc_path:
        st.warning("Pick a sample or upload a file first.")
    else:
        with st.spinner(f"Running the pipeline on {Path(doc_path).name} …"):
            try:
                st.session_state.run_id = run_pipeline(doc_path, customer)
                st.success(f"Pipeline completed — run #{st.session_state.run_id}")
            except PipelineError as exc:
                st.session_state.run_id = exc.run_id
                st.error(f"Pipeline FAILED at step '{exc.step}': {exc.original}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not run: {type(exc).__name__}: {exc}")

# Browse previously persisted runs (no API calls); default to the most recent.
recent = storage.list_runs(limit=25)
if recent:
    labels = {f"#{r['run_id']} · {Path(r['source_file']).name} · {r['status']}"
              f" · {r.get('outcome') or '—'}": r["run_id"] for r in recent}
    with st.expander("📂 View a previous run"):
        pick = st.selectbox("Previous runs", list(labels), label_visibility="collapsed")
        if st.button("Load this run"):
            st.session_state.run_id = labels[pick]
    st.session_state.setdefault("run_id", recent[0]["run_id"])


def _render_run(run: dict) -> None:
    st.divider()
    st.subheader(f"Run #{run['run_id']} — {Path(run['source_file']).name}")
    m = st.columns(4)
    m[0].metric("Status", run["status"])
    m[1].metric("Step", run["current_step"] or "—")
    m[2].metric("Customer", run["customer"])
    m[3].metric("Outcome", run.get("outcome") or "—")
    if run.get("error"):
        st.error(f"Error recorded: {run['error']}")

    # ---- Extracted fields ----
    st.markdown("#### Extracted fields")
    extraction = json.loads(run["extraction_json"]) if run.get("extraction_json") else None
    if extraction:
        h = st.columns([2.2, 5, 1.3])
        h[0].markdown("**Field**"); h[1].markdown("**Value**"); h[2].markdown("**Confidence**")
        for f in EXTRACTION_FIELDS:
            fld = extraction.get(f, {}) or {}
            val, conf = fld.get("value"), float(fld.get("confidence") or 0.0)
            row = st.columns([2.2, 5, 1.3])
            row[0].markdown(f"`{f}`")
            row[1].write(val if val is not None else "—")
            row[2].markdown(_badge(f"{conf:.2f}", _conf_color(conf)), unsafe_allow_html=True)
        with st.expander("Source snippets (verbatim quotes)"):
            for f in EXTRACTION_FIELDS:
                snip = (extraction.get(f, {}) or {}).get("source_snippet")
                st.markdown(f"**{f}** — {('`' + snip + '`') if snip else '_null_'}")
    else:
        st.write("_(extraction not available)_")

    # ---- Validation ----
    st.markdown("#### Validation")
    validation = json.loads(run["validation_json"]) if run.get("validation_json") else None
    if validation:
        for f, res in validation.get("fields", {}).items():
            status = res.get("status")
            row = st.columns([2.2, 1.6, 5])
            row[0].markdown(f"`{f}`")
            row[1].markdown(_badge(status, _STATUS_COLOR.get(status, _GREY)), unsafe_allow_html=True)
            if status == "mismatch":
                row[2].markdown(f'found "{res.get("found")}"  ≠  expected "{res.get("expected")}"')
            elif status == "uncertain":
                row[2].markdown(f"_{res.get('reason')}_")
            else:
                row[2].write("✓")
        sm = validation.get("summary", {})
        st.caption(f"{sm.get('match', 0)} match · {sm.get('mismatch', 0)} mismatch · "
                   f"{sm.get('uncertain', 0)} uncertain")
    else:
        st.write("_(validation not available)_")

    # ---- Decision ----
    st.markdown("#### Decision")
    outcome = run.get("outcome")
    color, emoji = _OUTCOME.get(outcome, (_GREY, "•"))
    st.markdown(
        f"<div style='background:{color};color:white;padding:12px 16px;border-radius:8px;"
        f"font-size:1.4em;font-weight:700'>{emoji} {outcome or 'pending'}</div>",
        unsafe_allow_html=True,
    )
    if run.get("reasoning"):
        st.write(run["reasoning"])

    # ---- Draft email ----
    st.markdown("#### Draft email")
    email = run.get("draft_email")
    if email:
        st.text_area("Editable draft", value=email, height=260, key=f"email_{run['run_id']}")
        b1, b2 = st.columns([1, 4])
        b1.button("📤 Send", disabled=True,
                  help="Disabled by design — the agent only drafts.")
        b2.caption("Send is disabled — **CG (compliance/ops) reviews & sends**. The agent never sends.")
    else:
        st.write("_No email — human review required (REVIEW); nothing to send._")


if st.session_state.get("run_id") is not None:
    current = storage.get_run(st.session_state.run_id)
    if current:
        _render_run(current)
else:
    st.info("Pick a document and click **Run pipeline** to see results.")

# --- Ask the data -----------------------------------------------------------

st.divider()
st.subheader("💬 Ask the data")
question = st.text_input("Question", placeholder="e.g. how many shipments were flagged this week?")
if st.button("Ask") and question.strip():
    with st.spinner("Translating to SQL and answering …"):
        try:
            st.session_state.last_answer = answer_question(question)
        except Exception as exc:  # noqa: BLE001
            st.session_state.last_answer = {"answer": f"Query failed: {exc}", "sql": "", "rows": [], "fallback": False}

ans = st.session_state.get("last_answer")
if ans:
    st.markdown(f"**Answer:** {ans['answer']}")
    with st.expander("SQL" + ("  (canned fallback)" if ans.get("fallback") else "")):
        st.code(ans.get("sql") or "(none)", language="sql")
        st.write(ans.get("rows"))
