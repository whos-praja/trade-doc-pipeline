"""SQLite storage for pipeline runs (data/pipeline.db).

One row per run in `pipeline_runs`. The orchestrator persists state after every
agent so a crashed/failed run can be resumed without re-running the expensive
extraction step. All writes go through small, parameterized helpers; the only
free-form entry point, query_runs(), is restricted to SELECT.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app import config

DB_PATH = config.DATA_DIR / "pipeline.db"

# Ordered steps recorded in `current_step` (the last SUCCESSFULLY completed step).
STEPS = ("created", "extracted", "validated", "routed", "completed")

_COLUMNS = (
    "run_id", "created_at", "customer", "source_file", "status", "current_step",
    "extraction_json", "validation_json", "decision_json", "outcome", "reasoning",
    "draft_email", "confidence_summary", "error",
)
# Columns that update_run_step() may set (run_id / created_at are immutable).
_UPDATABLE = {
    "status", "current_step", "extraction_json", "validation_json", "decision_json",
    "outcome", "reasoning", "draft_email", "confidence_summary", "error",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT    NOT NULL,
    customer           TEXT    NOT NULL,
    source_file        TEXT    NOT NULL,
    status             TEXT    NOT NULL,   -- RUNNING | COMPLETED | FAILED
    current_step       TEXT,               -- created|extracted|validated|routed|completed
    extraction_json    TEXT,
    validation_json    TEXT,
    decision_json      TEXT,
    outcome            TEXT,               -- AUTO_APPROVE | REVIEW | AMEND
    reasoning          TEXT,
    draft_email        TEXT,
    confidence_summary TEXT,
    error              TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    """Yield a connection (schema ensured); commit on success, rollback on error."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)  # idempotent
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the pipeline_runs table if it does not exist."""
    with _db():
        pass


def create_run(customer: str, source_file: str) -> int:
    """Insert a RUNNING row (current_step='created') and return its run_id."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (created_at, customer, source_file, status, current_step) "
            "VALUES (?, ?, ?, 'RUNNING', 'created')",
            (_now(), customer, str(source_file)),
        )
        return int(cur.lastrowid)


def update_run_step(run_id: int, step: str, **fields: Any) -> None:
    """Set current_step=step and persist any provided columns (e.g. *_json).

    This is the per-step checkpoint that makes a run resumable.
    """
    sets, params = ["current_step = ?"], [step]
    for col, val in fields.items():
        if col not in _UPDATABLE:
            raise ValueError(f"not an updatable column: {col!r}")
        sets.append(f"{col} = ?")
        params.append(val)
    params.append(run_id)
    with _db() as conn:
        conn.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE run_id = ?", params)


def complete_run(run_id: int, outcome: Optional[str] = None, reasoning: Optional[str] = None,
                 draft_email: Optional[str] = None, decision_json: Optional[str] = None,
                 confidence_summary: Optional[str] = None) -> None:
    """Mark a run COMPLETED (current_step='completed'); optionally set result columns."""
    sets = ["status = 'COMPLETED'", "current_step = 'completed'", "error = NULL"]
    params: list[Any] = []
    for col, val in (("outcome", outcome), ("reasoning", reasoning), ("draft_email", draft_email),
                     ("decision_json", decision_json), ("confidence_summary", confidence_summary)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    params.append(run_id)
    with _db() as conn:
        conn.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE run_id = ?", params)


def fail_run(run_id: int, error: str) -> None:
    """Mark a run FAILED with an error message.

    current_step is left at the last successfully completed step so resume_pipeline
    can continue from there (notably without re-running extraction).
    """
    with _db() as conn:
        conn.execute("UPDATE pipeline_runs SET status = 'FAILED', error = ? WHERE run_id = ?",
                     (str(error), run_id))


def get_run(run_id: int) -> Optional[dict]:
    """Return a run row as a dict, or None if not found."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(filters: Optional[dict] = None, limit: Optional[int] = None) -> list[dict]:
    """List runs (newest first), optionally filtered by exact column matches."""
    filters = filters or {}
    where, params = [], []
    for col, val in filters.items():
        if col not in _COLUMNS:
            raise ValueError(f"unknown filter column: {col!r}")
        where.append(f"{col} = ?")
        params.append(val)
    sql = "SELECT * FROM pipeline_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY run_id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_runs(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """Run an arbitrary read-only SELECT against the runs DB; return dict rows."""
    if not sql.lstrip().lower().startswith("select"):
        raise ValueError("query_runs only permits SELECT statements")
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
