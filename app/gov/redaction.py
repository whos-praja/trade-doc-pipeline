"""Masking, blind indexing, and encryption at rest for identity data.

Three distinct jobs, deliberately kept apart:

  mask()          irreversible display form -- "XXXX XXXX 9012". What a support
                  agent, a log line, or a screen ever sees.

  blind_index()   a keyed HMAC of the normalised value. Lets you answer "have we
                  seen this ID before?" and de-duplicate WITHOUT storing the value.
                  Keyed (not a bare hash) because the space of Aadhaar numbers is
                  small enough to brute-force an unkeyed SHA-256 offline.

  encrypt()       reversible, for the few fields you are lawfully required to keep
                  in full. Fernet (AES-128-CBC + HMAC-SHA256) under a key that is
                  NOT in the database, so a stolen DB file alone yields nothing.

For AADHAAR the encrypt path is refused outright: UIDAI's regulations do not permit
an ordinary entity to store full Aadhaar numbers, so `protect()` masks it and keeps
a blind index, and there is no flag to override that.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Optional

from app.gov.schema import ID_MASK, DocType, FIELDS, Sensitivity

MASK_CHAR = "X"


class KeyMissing(RuntimeError):
    """A required key is not configured."""


# --- masking ----------------------------------------------------------------

def mask(value: str, keep_prefix: int = 0, keep_suffix: int = 4) -> str:
    """Irreversibly mask a value, keeping only the requested edges.

    Separators are preserved so the shape stays recognisable:
        mask("1234 5678 9012", 0, 4) -> "XXXX XXXX 9012"
    """
    if not value:
        return ""
    chars = list(value)
    alnum_positions = [i for i, c in enumerate(chars) if c.isalnum()]
    n = len(alnum_positions)
    keep_prefix = max(0, min(keep_prefix, n))
    keep_suffix = max(0, min(keep_suffix, n - keep_prefix))

    hide = set(alnum_positions[keep_prefix: n - keep_suffix])
    return "".join(MASK_CHAR if i in hide else c for i, c in enumerate(chars))


def mask_id(doc_type: DocType, value: str) -> str:
    """Mask an ID number using the style appropriate to its document type."""
    prefix, suffix = ID_MASK.get(doc_type, (0, 4))
    return mask(value, prefix, suffix)


def mask_field(field_name: str, value: str, doc_type: Optional[DocType] = None) -> str:
    """Mask any canonical field by its own spec (or the doc's ID style)."""
    if field_name == "id_number" and doc_type is not None:
        return mask_id(doc_type, value)
    spec = FIELDS.get(field_name)
    if spec is None:
        return mask(value)
    return mask(value, spec.keep_prefix, spec.keep_suffix)


# --- blind index ------------------------------------------------------------

def _pepper() -> bytes:
    key = os.getenv("BLIND_INDEX_PEPPER")
    if not key:
        raise KeyMissing(
            "BLIND_INDEX_PEPPER is not set. An unkeyed hash of an ID number is "
            "brute-forceable offline, so this must be configured. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    return key.encode("utf-8")


def normalise(value: str) -> str:
    """Canonical form for indexing: uppercase, separators stripped."""
    return re.sub(r"[\s\-/.]", "", value).upper()


def blind_index(value: str) -> str:
    """Keyed, irreversible lookup token for a sensitive value.

    Equal inputs give equal tokens (so you can de-duplicate and search), but the
    token cannot be reversed without the pepper.
    """
    return hmac.new(_pepper(), normalise(value).encode("utf-8"), hashlib.sha256).hexdigest()


# --- encryption at rest -----------------------------------------------------

def _fernet():
    """Build a Fernet cipher from FIELD_ENCRYPTION_KEY."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise KeyMissing(
            "the `cryptography` package is required to store any field in full; "
            "install it (pip install cryptography) or keep only masked values."
        ) from exc

    key = os.getenv("FIELD_ENCRYPTION_KEY")
    if not key:
        raise KeyMissing(
            "FIELD_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "Store it in a secrets manager or KMS -- NOT beside the database."
        )
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    """Encrypt a value for storage. Returns a URL-safe token."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a stored token. Every call belongs in the access log."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# --- the combined policy ----------------------------------------------------

def protect(field_name: str, value: str, doc_type: DocType, *,
            store_full: bool = False) -> dict:
    """Turn a raw extracted value into exactly what may be persisted.

    Returns {"masked", "blind_index"?, "ciphertext"?}. The raw value is never in
    the return. `store_full` is a request, not a guarantee:

      - AADHAAR id_number  -> full storage is REFUSED outright (UIDAI rule).
      - other NATIONAL_ID  -> full storage only when store_full is explicitly set.
      - PII / ATTRIBUTE    -> encrypted when store_full is set, else masked only.
    """
    spec = FIELDS.get(field_name)
    sensitivity = spec.sensitivity if spec else Sensitivity.PII
    out: dict = {"masked": mask_field(field_name, value, doc_type)}

    if sensitivity is Sensitivity.NATIONAL_ID:
        out["blind_index"] = blind_index(value)
        if doc_type is DocType.AADHAAR:
            out["full_storage"] = "refused_by_policy"
            out["policy"] = (
                "UIDAI regulations do not permit storing a full Aadhaar number here; "
                "only the last 4 digits are retained, plus a keyed lookup token."
            )
            return out

    if store_full:
        out["ciphertext"] = encrypt(value)

    return out
