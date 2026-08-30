"""Consent records: tamper-evident, purpose-bound, expiring, and withdrawable.

Consent here is a *capability*, not a checkbox. A ConsentRecord is the only thing
that authorises reading a document, and it carries its own limits:

    who        subject_ref     an opaque reference to the person (never their ID number)
    what       doc_types       which document types may be read
    which      fields          the exact fields, derived from the purpose allowlist
    why        purpose         a key from schema.PURPOSES, whose text was shown to them
    how long   expires_at      consent expiry; separate from data retention
    proof      evidence        how consent was captured (channel, timestamp, notice version)

Each record is signed with HMAC-SHA256 over its canonical JSON, so a record that
was widened after the fact (extra fields, longer retention) fails verification and
the pipeline refuses to run. The signing key lives in CONSENT_SIGNING_KEY.

Withdrawal is first-class: `withdraw()` marks the record and every downstream read
stops immediately. Under India's DPDP Act 2023 s.6(4)-(6) and GDPR Art. 7(3),
withdrawal must be as easy as granting -- so it is one call, and it also drives
erasure of the data collected under it (see storage.erase_for_consent).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.gov.schema import DocType, PurposeSpec, get_purpose

# The notice text version shown to the person. Bump whenever the wording changes,
# so an old consent is never silently reused under new terms.
NOTICE_VERSION = "2026-08-30.1"

DEFAULT_CONSENT_VALIDITY_DAYS = 90


class ConsentError(RuntimeError):
    """Consent is missing, expired, withdrawn, tampered with, or out of scope."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _signing_key() -> bytes:
    """The HMAC key for consent records.

    Raises rather than inventing a key: an unsigned or randomly-signed consent
    record proves nothing, and silently degrading here would defeat the point.
    """
    key = os.getenv("CONSENT_SIGNING_KEY")
    if not key:
        raise ConsentError(
            "CONSENT_SIGNING_KEY is not set. Consent records must be signed to be "
            "tamper-evident. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "then add CONSENT_SIGNING_KEY=... to your .env"
        )
    return key.encode("utf-8")


@dataclass
class ConsentRecord:
    """A signed, purpose-bound, expiring authorisation to read a document."""

    consent_id: str
    subject_ref: str            # opaque; NEVER the person's ID number
    purpose: str
    doc_types: list[str]
    fields: list[str]
    retention_days: int
    granted_at: str
    expires_at: str
    notice_version: str
    evidence: dict              # channel, notice text hash, IP/device if lawful to keep
    withdrawn_at: Optional[str] = None
    signature: str = ""

    # --- signing ------------------------------------------------------------

    def _payload(self) -> bytes:
        """Canonical bytes covered by the signature (everything but the signature)."""
        data = {k: v for k, v in asdict(self).items() if k != "signature"}
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sign(self) -> "ConsentRecord":
        self.signature = hmac.new(_signing_key(), self._payload(), hashlib.sha256).hexdigest()
        return self

    def signature_ok(self) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(_signing_key(), self._payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    # --- state --------------------------------------------------------------

    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    @property
    def is_expired(self) -> bool:
        return _now() > datetime.fromisoformat(self.expires_at)

    def purpose_spec(self) -> PurposeSpec:
        return get_purpose(self.purpose)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ConsentRecord":
        return cls(**json.loads(raw))


# --- granting ---------------------------------------------------------------

def grant(subject_ref: str, purpose: str, doc_types: list[DocType], *,
          validity_days: int = DEFAULT_CONSENT_VALIDITY_DAYS,
          retention_days: Optional[int] = None,
          channel: str = "web_form",
          notice_shown: Optional[str] = None) -> ConsentRecord:
    """Create a signed consent record.

    The caller does NOT choose the fields -- they are derived from the purpose, so
    a caller cannot widen the ask beyond what the notice told the person. Retention
    is likewise capped at the purpose's maximum.

    `notice_shown` is the exact notice text displayed; its SHA-256 is stored so you
    can later prove what the person actually agreed to.
    """
    spec = get_purpose(purpose)

    if not doc_types:
        raise ConsentError("consent must name at least one document type")

    requested_retention = retention_days if retention_days is not None else spec.max_retention_days
    if requested_retention > spec.max_retention_days:
        raise ConsentError(
            f"retention {requested_retention}d exceeds the maximum {spec.max_retention_days}d "
            f"for purpose '{purpose}'"
        )
    if requested_retention < 1:
        raise ConsentError("retention must be at least 1 day")

    # The union of fields readable across the named document types.
    fields: set[str] = set()
    for dt in doc_types:
        fields.update(spec.fields_for(dt))
    if not fields:
        raise ConsentError(
            f"purpose '{purpose}' reads no fields from {[d.value for d in doc_types]}"
        )

    notice_text = notice_shown if notice_shown is not None else spec.description
    granted = _now()

    record = ConsentRecord(
        consent_id=f"cns_{secrets.token_urlsafe(16)}",
        subject_ref=subject_ref,
        purpose=purpose,
        doc_types=sorted(d.value for d in doc_types),
        fields=sorted(fields),
        retention_days=requested_retention,
        granted_at=_iso(granted),
        expires_at=_iso(granted + timedelta(days=validity_days)),
        notice_version=NOTICE_VERSION,
        evidence={
            "channel": channel,
            "notice_sha256": hashlib.sha256(notice_text.encode("utf-8")).hexdigest(),
            "notice_text": notice_text,
        },
    )
    return record.sign()


def withdraw(record: ConsentRecord) -> ConsentRecord:
    """Mark consent withdrawn and re-sign. Data erasure is the caller's next step."""
    if record.is_withdrawn:
        return record
    record.withdrawn_at = _iso(_now())
    return record.sign()


# --- the gate ---------------------------------------------------------------

def authorise(record: ConsentRecord, doc_type: DocType) -> list[str]:
    """Check consent covers reading `doc_type` NOW; return the permitted fields.

    Raises ConsentError on every failure path. This is the single choke point the
    pipeline calls before touching a document -- there is no way past it.
    """
    if not record.signature_ok():
        raise ConsentError(
            f"consent {record.consent_id} failed signature verification -- the record was "
            f"altered after it was granted, or signed with a different key. Refusing to proceed."
        )
    if record.is_withdrawn:
        raise ConsentError(
            f"consent {record.consent_id} was withdrawn at {record.withdrawn_at}. "
            f"Processing must stop and collected data must be erased."
        )
    if record.is_expired:
        raise ConsentError(
            f"consent {record.consent_id} expired at {record.expires_at}. "
            f"Ask the person again -- expired consent is not consent."
        )
    if doc_type.value not in record.doc_types:
        raise ConsentError(
            f"consent {record.consent_id} covers {record.doc_types}, not '{doc_type.value}'."
        )

    spec = record.purpose_spec()
    permitted = [f for f in spec.fields_for(doc_type) if f in record.fields]
    if not permitted:
        raise ConsentError(
            f"consent {record.consent_id} permits no readable field on a {doc_type.value}."
        )
    return permitted
