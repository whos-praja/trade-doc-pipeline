"""SQLite persistence for consented identity verifications (data/gov.db).

Three tables, each with a distinct job:

  consents        the signed consent records -- the authority for everything else
  verifications   one row per document processed; NEVER holds a raw field value
  access_log      append-only record of every read, write, and erasure

What is deliberately NOT stored anywhere:
  - the document image (hashed, then dropped; see pipeline.py)
  - any raw field value in the clear (masked + blind-indexed, or encrypted)
  - a full Aadhaar number, under any code path

`due_for_erasure()` and `purge_expired()` implement storage limitation: rows past
their consent's retention window are deleted, not merely hidden. Run purge on a
schedule -- retention that is documented but never executed is not retention.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app import config
from app.gov.consent import ConsentRecord

DB_PATH = config.DATA_DIR / "gov.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    consent_id     TEXT PRIMARY KEY,
    subject_ref    TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    doc_types      TEXT NOT NULL,      -- JSON array
    fields         TEXT NOT NULL,      -- JSON array
    retention_days INTEGER NOT NULL,
    granted_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    withdrawn_at   TEXT,
    notice_version TEXT NOT NULL,
    record_json    TEXT NOT NULL       -- the full signed record
);

CREATE TABLE IF NOT EXISTS verifications (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    consent_id      TEXT NOT NULL REFERENCES consents(consent_id),
    created_at      TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    image_sha256    TEXT NOT NULL,     -- proof of what was processed; not the image
    outcome         TEXT NOT NULL,     -- ACCEPTED | REVIEW | REJECTED
    reasoning       TEXT,
    fields_json     TEXT NOT NULL,     -- masked / blind-indexed / encrypted ONLY
    verification_json TEXT,            -- deterministic check results
    signals_json    TEXT,              -- capture signals
    erase_after     TEXT NOT NULL,     -- consent.granted_at + retention_days
    erased_at       TEXT
);

CREATE TABLE IF NOT EXISTS access_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,          -- who/what acted
    action     TEXT NOT NULL,          -- CONSENT_GRANTED | EXTRACT | READ | ERASE | ...
    consent_id TEXT,
    verification_id INTEGER,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ver_consent ON verifications(consent_id);
CREATE INDEX IF NOT EXISTS idx_ver_erase   ON verifications(erase_after) WHERE erased_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_log_consent ON access_log(consent_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)  # idempotent
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _db():
        pass


# --- audit ------------------------------------------------------------------

def log(actor: str, action: str, *, consent_id: Optional[str] = None,
        verification_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    """Append to the access log. Never write a field VALUE into `detail`."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO access_log (at, actor, action, consent_id, verification_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), actor, action, consent_id, verification_id, detail),
        )


def access_log_for(consent_id: str) -> list[dict]:
    """Everything ever done under one consent -- what a subject access request needs."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM access_log WHERE consent_id = ? ORDER BY log_id", (consent_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- consents ---------------------------------------------------------------

def save_consent(record: ConsentRecord, *, actor: str = "system") -> str:
    """Persist a signed consent record (insert or update on withdrawal)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO consents (consent_id, subject_ref, purpose, doc_types, fields, "
            "retention_days, granted_at, expires_at, withdrawn_at, notice_version, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(consent_id) DO UPDATE SET "
            "  withdrawn_at = excluded.withdrawn_at, record_json = excluded.record_json",
            (record.consent_id, record.subject_ref, record.purpose,
             json.dumps(record.doc_types), json.dumps(record.fields),
             record.retention_days, record.granted_at, record.expires_at,
             record.withdrawn_at, record.notice_version, record.to_json()),
        )
    log(actor, "CONSENT_WITHDRAWN" if record.is_withdrawn else "CONSENT_GRANTED",
        consent_id=record.consent_id,
        detail=f"purpose={record.purpose} fields={len(record.fields)}")
    return record.consent_id


def get_consent(consent_id: str) -> Optional[ConsentRecord]:
    with _db() as conn:
        row = conn.execute(
            "SELECT record_json FROM consents WHERE consent_id = ?", (consent_id,)
        ).fetchone()
    return ConsentRecord.from_json(row["record_json"]) if row else None


def list_consents(subject_ref: Optional[str] = None) -> list[dict]:
    sql = ("SELECT consent_id, subject_ref, purpose, doc_types, granted_at, expires_at, "
           "withdrawn_at, retention_days FROM consents")
    params: list[Any] = []
    if subject_ref:
        sql += " WHERE subject_ref = ?"
        params.append(subject_ref)
    sql += " ORDER BY granted_at DESC"
    with _db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --- verifications ----------------------------------------------------------

def save_verification(consent: ConsentRecord, *, doc_type: str, image_sha256: str,
                      outcome: str, reasoning: str, fields: dict,
                      verification: dict, signals: dict, actor: str = "system") -> int:
    """Store one processed document. `fields` must already be masked/encrypted."""
    erase_after = (
        datetime.fromisoformat(consent.granted_at) + timedelta(days=consent.retention_days)
    ).isoformat(timespec="seconds")

    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO verifications (consent_id, created_at, doc_type, image_sha256, "
            "outcome, reasoning, fields_json, verification_json, signals_json, erase_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (consent.consent_id, _now(), doc_type, image_sha256, outcome, reasoning,
             json.dumps(fields), json.dumps(verification), json.dumps(signals), erase_after),
        )
        vid = int(cur.lastrowid)
    log(actor, "VERIFICATION_STORED", consent_id=consent.consent_id, verification_id=vid,
        detail=f"outcome={outcome} erase_after={erase_after}")
    return vid


def get_verification(verification_id: int, *, actor: str = "system",
                     reason: str = "unspecified") -> Optional[dict]:
    """Read a verification. Every call is logged -- reads are auditable too."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM verifications WHERE verification_id = ? AND erased_at IS NULL",
            (verification_id,),
        ).fetchone()
    if row is None:
        return None
    log(actor, "READ", consent_id=row["consent_id"], verification_id=verification_id,
        detail=f"reason={reason}")
    return dict(row)


def find_by_blind_index(token: str) -> list[dict]:
    """Find prior verifications of the same ID number, without knowing the number."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT verification_id, consent_id, doc_type, created_at, outcome "
            "FROM verifications WHERE erased_at IS NULL "
            "AND json_extract(fields_json, '$.id_number.blind_index') = ?",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- erasure ----------------------------------------------------------------

def _erase_rows(conn, where: str, params: tuple) -> int:
    """Blank every data-bearing column and stamp erased_at. Keeps the audit stub."""
    cur = conn.execute(
        f"UPDATE verifications SET fields_json = '{{}}', verification_json = NULL, "
        f"signals_json = NULL, reasoning = 'erased', image_sha256 = '', "
        f"erased_at = ? WHERE {where} AND erased_at IS NULL",
        (_now(), *params),
    )
    return cur.rowcount


def erase_for_consent(consent_id: str, *, actor: str = "system",
                      reason: str = "consent_withdrawn") -> int:
    """Erase all data collected under one consent. Call this on withdrawal."""
    with _db() as conn:
        n = _erase_rows(conn, "consent_id = ?", (consent_id,))
    log(actor, "ERASE", consent_id=consent_id, detail=f"reason={reason} rows={n}")
    return n


def due_for_erasure(*, as_of: Optional[str] = None) -> list[dict]:
    """Rows whose retention window has passed but which are still present."""
    cutoff = as_of or _now()
    with _db() as conn:
        rows = conn.execute(
            "SELECT verification_id, consent_id, doc_type, erase_after FROM verifications "
            "WHERE erased_at IS NULL AND erase_after <= ? ORDER BY erase_after",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_expired(*, as_of: Optional[str] = None, actor: str = "retention_job") -> int:
    """Erase everything past its retention window. Run this on a schedule."""
    cutoff = as_of or _now()
    with _db() as conn:
        n = _erase_rows(conn, "erase_after <= ?", (cutoff,))
    if n:
        log(actor, "PURGE", detail=f"rows={n} cutoff={cutoff}")
    return n


def subject_export(subject_ref: str) -> dict:
    """Everything held about one person -- for a subject access / portability request."""
    with _db() as conn:
        consents = [dict(r) for r in conn.execute(
            "SELECT * FROM consents WHERE subject_ref = ?", (subject_ref,)).fetchall()]
        ids = [c["consent_id"] for c in consents]
        verifications: list[dict] = []
        logs: list[dict] = []
        if ids:
            marks = ",".join("?" * len(ids))
            verifications = [dict(r) for r in conn.execute(
                f"SELECT * FROM verifications WHERE consent_id IN ({marks})", ids).fetchall()]
            logs = [dict(r) for r in conn.execute(
                f"SELECT * FROM access_log WHERE consent_id IN ({marks}) ORDER BY log_id",
                ids).fetchall()]
    return {"subject_ref": subject_ref, "consents": consents,
            "verifications": verifications, "access_log": logs}
