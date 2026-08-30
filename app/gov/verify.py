"""Deterministic verification of extracted government-ID fields. NO model call.

Same architectural principle as the trade pipeline's Validator: the model only
*proposes* what it read; this module *decides* whether the reading is structurally
credible, in pure Python, reproducibly and auditably.

What this module CAN do:
  - prove an Aadhaar number is not a typo (Verhoeff checksum, mandated by UIDAI)
  - prove a PAN / passport / DL / EPIC number is structurally well-formed
  - verify a passport MRZ line's own check digits
  - catch impossible dates (future DOB, age > 120, expired document)

What this module CANNOT do -- and no OCR system can:
  - prove the document is genuine, or that the person presenting it is its holder.
    A well-made forgery passes every check here. Genuineness requires the issuer:
    UIDAI offline QR signature verification, DigiLocker issued documents, or the
    NSDL/Protean PAN API. See docs/GOVERNMENT_ID_SYSTEM.md.

Every check returns one of three statuses, mirroring the trade Validator:
    "valid"      structurally verified
    "invalid"    present but fails a hard check -> route to human review
    "unverified" absent, low-confidence, or no check exists for this field
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from app.gov.schema import DocType

VALID = "valid"
INVALID = "invalid"
UNVERIFIED = "unverified"

# Below this extractor confidence, a value is never called "valid" -- the same
# never-silently-approve rule the trade Validator uses.
CONFIDENCE_THRESHOLD = 0.6

MAX_HUMAN_AGE_YEARS = 120


# --- Verhoeff checksum (Aadhaar) --------------------------------------------
# UIDAI requires the 12th digit of an Aadhaar number to be a Verhoeff check digit.
# Verhoeff catches all single-digit errors and all adjacent transpositions, which
# is exactly the error class OCR produces -- so this is a genuinely strong signal
# that a 12-digit reading is correct.

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)

_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    """True iff `digits` (a digit string) carries a valid trailing Verhoeff digit."""
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


# --- MRZ check digit (passport machine-readable zone) -----------------------

def _mrz_char_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if ch == "<":
        return 0
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 10
    return -1


def mrz_check_digit(field_value: str) -> Optional[int]:
    """ICAO 9303 check digit for an MRZ field: weights 7,3,1 cycling, sum mod 10."""
    weights = (7, 3, 1)
    total = 0
    for i, ch in enumerate(field_value.upper()):
        v = _mrz_char_value(ch)
        if v < 0:
            return None
        total += v * weights[i % 3]
    return total % 10


def mrz_field_ok(field_value: str, check_digit: str) -> bool:
    """True iff `check_digit` is the correct ICAO 9303 check digit for the field."""
    expected = mrz_check_digit(field_value)
    return expected is not None and check_digit.isdigit() and expected == int(check_digit)


# --- ID number structure per document type ----------------------------------

# PAN: 5 letters, 4 digits, 1 letter. The 4th letter encodes the holder type.
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_PAN_HOLDER_TYPES = set("ABCFGHJLPT")  # P=individual, C=company, H=HUF, F=firm, ...

# Indian passport: 8 characters -- a letter excluding Q, X and Z, then 7 digits
# whose first and last are non-zero. This is the widely-used structural rule; it
# is deliberately permissive, because wrongly rejecting a real passport is worse
# here than passing one through to a human reviewer.
_PASSPORT_IN_RE = re.compile(r"^[A-PR-WY][1-9]\d\d{4}[1-9]$")

# Driving licence: 2-letter state + 2-digit RTO + 4-digit year + 7 digits.
_DL_RE = re.compile(r"^[A-Z]{2}\d{2}(19|20)\d{2}\d{7}$")

# Voter EPIC: 3 letters + 7 digits.
_EPIC_RE = re.compile(r"^[A-Z]{3}\d{7}$")


def _squash(value: str) -> str:
    """Uppercase and strip separators so formatting never fails a structural check."""
    return re.sub(r"[\s\-/]", "", value).upper()


def check_id_number(doc_type: DocType, value: str) -> tuple[str, str]:
    """Structurally verify an ID number. Returns (status, reason)."""
    raw = _squash(value)
    if not raw:
        return UNVERIFIED, "empty ID number"

    if doc_type is DocType.AADHAAR:
        if not raw.isdigit() or len(raw) != 12:
            return INVALID, f"Aadhaar must be exactly 12 digits (got {len(raw)} characters)"
        if raw[0] in "01":
            return INVALID, "Aadhaar numbers never begin with 0 or 1"
        if not verhoeff_ok(raw):
            return INVALID, "Verhoeff checksum failed (likely a misread digit)"
        return VALID, "12 digits, valid Verhoeff checksum"

    if doc_type is DocType.PAN:
        if not _PAN_RE.match(raw):
            return INVALID, "PAN must be 5 letters, 4 digits, then 1 letter (AAAAA9999A)"
        if raw[3] not in _PAN_HOLDER_TYPES:
            return INVALID, f"'{raw[3]}' is not a valid PAN holder-type letter"
        return VALID, f"valid PAN structure (holder type '{raw[3]}')"

    if doc_type is DocType.PASSPORT:
        if not _PASSPORT_IN_RE.match(raw):
            return INVALID, "does not match the Indian passport number format (e.g. A1234567)"
        return VALID, "valid Indian passport number structure"

    if doc_type is DocType.DRIVING_LICENCE:
        if not _DL_RE.match(raw):
            return INVALID, "does not match the DL format (2-letter state + RTO + year + 7 digits)"
        return VALID, "valid driving licence structure"

    if doc_type is DocType.VOTER_ID:
        if not _EPIC_RE.match(raw):
            return INVALID, "EPIC must be 3 letters followed by 7 digits"
        return VALID, "valid EPIC structure"

    return UNVERIFIED, f"no structural check defined for {doc_type.value}"


# --- date handling ----------------------------------------------------------

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y",
    "%b %d, %Y", "%d/%m/%y", "%d-%m-%y", "%m/%Y", "%Y",
)


def parse_date(value: str) -> Optional[date]:
    """Parse the date formats Indian government documents actually print."""
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def check_date(field_name: str, value: str, *, today: Optional[date] = None) -> tuple[str, str]:
    """Sanity-check a date field. Returns (status, reason)."""
    today = today or date.today()
    parsed = parse_date(value)
    if parsed is None:
        return UNVERIFIED, f"could not parse {value!r} as a date"

    if field_name == "date_of_birth":
        if parsed > today:
            return INVALID, "date of birth is in the future"
        if (today - parsed).days > MAX_HUMAN_AGE_YEARS * 366:
            return INVALID, f"implies an age over {MAX_HUMAN_AGE_YEARS} years"
        return VALID, f"plausible date of birth ({parsed.isoformat()})"

    if field_name == "expiry_date":
        if parsed < today:
            return INVALID, f"document expired on {parsed.isoformat()}"
        return VALID, f"valid until {parsed.isoformat()}"

    if field_name == "issue_date":
        if parsed > today:
            return INVALID, "issue date is in the future"
        return VALID, f"issued {parsed.isoformat()}"

    return UNVERIFIED, "no date rule for this field"


def age_on(dob_value: str, *, today: Optional[date] = None) -> Optional[int]:
    """Whole years old today, or None if the DOB cannot be parsed.

    This is what an age check should actually return: a derived boolean/number,
    so the caller can store "is over 18" instead of retaining the date of birth.
    """
    today = today or date.today()
    dob = parse_date(dob_value)
    if dob is None:
        return None
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# --- public API -------------------------------------------------------------

def verify_fields(doc_type: DocType, extracted: dict[str, dict],
                  *, today: Optional[date] = None) -> dict:
    """Verify every extracted field deterministically.

    `extracted` maps field name -> {"value": str|None, "confidence": float, ...}.
    Returns {"fields": {name: {status, reason, confidence}}, "summary": {...}}.
    """
    results: dict[str, dict] = {}

    for name, payload in extracted.items():
        value = payload.get("value")
        confidence = float(payload.get("confidence") or 0.0)

        if value is None or not str(value).strip():
            results[name] = {"status": UNVERIFIED, "reason": "not found on the document",
                             "confidence": confidence}
            continue
        if confidence < CONFIDENCE_THRESHOLD:
            results[name] = {
                "status": UNVERIFIED,
                "reason": f"extractor confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD:.2f}",
                "confidence": confidence,
            }
            continue

        if name == "id_number":
            status, reason = check_id_number(doc_type, str(value))
        elif name in ("date_of_birth", "issue_date", "expiry_date"):
            status, reason = check_date(name, str(value), today=today)
        else:
            status, reason = UNVERIFIED, "no deterministic check exists for this field"

        results[name] = {"status": status, "reason": reason, "confidence": confidence}

    counts = {VALID: 0, INVALID: 0, UNVERIFIED: 0}
    for r in results.values():
        counts[r["status"]] += 1

    return {
        "fields": results,
        "summary": {
            "doc_type": doc_type.value,
            "valid": counts[VALID],
            "invalid": counts[INVALID],
            "unverified": counts[UNVERIFIED],
            "total": len(results),
            # Any hard failure blocks acceptance outright.
            "has_hard_failure": counts[INVALID] > 0,
        },
    }
