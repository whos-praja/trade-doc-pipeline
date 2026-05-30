"""Streamlit UI (STUB).

Planned role: upload a trade document, run the pipeline, and display the 8
extracted fields with their confidence + source snippet, plus a query box.

Run with:  streamlit run ui/streamlit_app.py
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Trade Document Pipeline (POC)", layout="wide")

st.title("📄 Trade Document Pipeline — POC")
st.caption("Extractor agent is implemented; the rest of the pipeline is stubbed.")

st.info(
    "UI stub. The Extractor works today via the CLI:\n\n"
    "`python run_extractor.py data/samples/clean_commercial_invoice.pdf`\n\n"
    "Wire the upload box to `app.pipeline.run_pipeline` once the downstream "
    "agents (router, validator, storage, query) are built."
)

uploaded = st.file_uploader("Upload a trade document (PDF/PNG/JPG)", type=["pdf", "png", "jpg", "jpeg"])
if uploaded is not None:
    st.warning("Pipeline not wired up yet — see app/pipeline.py (stub).")
