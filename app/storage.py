"""Storage layer (STUB).

Planned role: persist extraction + validation results to a local SQLite
database so the Streamlit UI and the query agent can read them back. Will
expose `init_db()`, `save_extraction(...)`, and simple fetch helpers.
"""
from __future__ import annotations

from pathlib import Path

from app.schema import ExtractionResult

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline.db"


def init_db() -> None:
    """Create the SQLite schema if it does not exist. Not implemented yet."""
    raise NotImplementedError("Storage layer is a POC stub.")


def save_extraction(source_path: str, result: ExtractionResult) -> int:
    """Persist one extraction and return its row id. Not implemented yet."""
    raise NotImplementedError("Storage layer is a POC stub.")
