"""Pipeline orchestrator (STUB).

Planned role: wire the agents together into a single entry point used by both
the CLI and the Streamlit UI:

    Router -> Extractor -> Validator -> Storage

Only the Extractor is implemented today; this orchestrator will call it once the
other agents are built.
"""
from __future__ import annotations

from pathlib import Path

from app.extractor import extract  # the one implemented step


def run_pipeline(path: str | Path) -> dict:
    """Run the full document pipeline end to end. Not implemented yet.

    For now, use `app.extractor.extract(path)` directly (see run_extractor.py).
    """
    raise NotImplementedError(
        "Pipeline orchestrator is a POC stub. The Extractor works standalone: "
        "`from app.extractor import extract`."
    )
