"""Validator agent — DETERMINISTIC, no LLM call.

Design principle (the anti-hallucination guarantee): the Extractor (an LLM) only
*proposes* values; this module *decides* whether each proposed value matches the
customer's expected rule set, using pure Python. No model is consulted here, so
the decision is reproducible and auditable.

Per-field result shape:
    {status: "match"|"mismatch"|"uncertain", found, expected, confidence, reason}

Decision logic (in order):
  1. If the extractor's value is null OR its confidence < CONFIDENCE_THRESHOLD,
     the field is "uncertain". It is NEVER "match" -- silently approving a
     low-confidence field is the worst possible outcome.
  2. exact  : normalize (trim/lowercase/collapse-whitespace) both sides, compare.
  3. fuzzy  : MATCH iff token_set_ratio >= FUZZY_MATCH_THRESHOLD AND every expected
              word is present in the found value. The ratio tolerates casing,
              punctuation and extra qualifier words; the word-presence check
              catches a changed/dropped word (e.g. "Import" vs "Imports") that a
              bare ratio (~0.98) would wrongly accept.
  4. format : present + matches the required regex -> match; malformed -> mismatch.
              (A missing value is already "uncertain" from step 1.)

Fuzzy scoring uses rapidfuzz when available, with a difflib fallback.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from app.schema import EXTRACTION_FIELDS, ExtractionResult

try:  # rapidfuzz preferred; difflib is the fallback.
    from rapidfuzz import fuzz as _rf_fuzz
    _HAVE_RAPIDFUZZ = True
except Exception:  # pragma: no cover - exercised only when rapidfuzz is absent
    _rf_fuzz = None
    _HAVE_RAPIDFUZZ = False

# --- tunable thresholds (kept at the top so they are easy to tune) ----------
CONFIDENCE_THRESHOLD = 0.6    # extractor confidence below this -> "uncertain"
FUZZY_MATCH_THRESHOLD = 0.95  # token_set_ratio at/above this is required to match

RULES_DIR = Path(__file__).resolve().parent / "rules"

# Built-in named formats for "format" fields (value must be number + weight unit).
_WEIGHT_RE = re.compile(
    r"^\s*\d[\d,]*(\.\d+)?\s*"
    r"(kg|kgs|kilogram|kilograms|g|gram|grams|lb|lbs|pound|pounds|mt|t|ton|tons|tonne|tonnes)\s*$",
    re.IGNORECASE,
)
_NAMED_FORMATS = {"weight": _WEIGHT_RE}


# --- normalization helpers --------------------------------------------------

def _normalize(s: str) -> str:
    """Trim, lowercase, collapse internal whitespace (used for exact compare)."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _fold(s: str) -> str:
    """Lowercase, drop apostrophes/hyphens, turn other punctuation into spaces.

    "Men's Cotton T-Shirts" -> "mens cotton tshirts". Used for word-level
    comparison so that punctuation never causes a spurious word mismatch.
    """
    s = s.lower().replace("'", "").replace("-", "")
    return " ".join(t for t in re.split(r"[^a-z0-9]+", s) if t)


def _token_set_ratio(a: str, b: str) -> float:
    """token_set_ratio in [0, 1]; rapidfuzz if present, else a difflib clone."""
    if _HAVE_RAPIDFUZZ:
        return _rf_fuzz.token_set_ratio(a, b) / 100.0
    ta, tb = set(a.split()), set(b.split())
    inter = " ".join(sorted(ta & tb))
    sa = (inter + " " + " ".join(sorted(ta - tb))).strip()
    sb = (inter + " " + " ".join(sorted(tb - ta))).strip()

    def r(x: str, y: str) -> float:
        return difflib.SequenceMatcher(None, x, y).ratio()

    return max(r(inter, sa), r(inter, sb), r(sa, sb))


def _result(status: str, found, expected, confidence: float, reason: str) -> dict:
    return {
        "status": status,
        "found": found,
        "expected": expected,
        "confidence": round(float(confidence), 3),
        "reason": reason,
    }


def _format_pattern(field_rule: dict) -> re.Pattern:
    if "pattern" in field_rule:
        return re.compile(field_rule["pattern"], re.IGNORECASE)
    named = field_rule.get("format")
    if named in _NAMED_FORMATS:
        return _NAMED_FORMATS[named]
    raise ValueError(
        f"format rule needs a 'pattern' or a known 'format' name {sorted(_NAMED_FORMATS)}: {field_rule}"
    )


# --- per-field evaluation ---------------------------------------------------

def _evaluate_field(field_rule: dict, found, confidence: float) -> dict:
    match_type = field_rule.get("match")
    expected = field_rule.get("expected")
    expected_display = (
        expected
        if expected is not None
        else field_rule.get("expected_description") or field_rule.get("pattern")
    )

    # 1) confidence / presence gate -> uncertain (never "match")
    if found is None:
        return _result("uncertain", found, expected_display, confidence,
                       "extractor returned null (value not found in document)")
    if confidence < CONFIDENCE_THRESHOLD:
        return _result("uncertain", found, expected_display, confidence,
                       f"extractor confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD:.2f} threshold")

    # 2) exact
    if match_type == "exact":
        if _normalize(found) == _normalize(str(expected)):
            return _result("match", found, expected_display, confidence, "exact match (normalized)")
        return _result("mismatch", found, expected_display, confidence, "normalized values differ")

    # 3) fuzzy: high similarity AND every expected word present
    if match_type == "fuzzy":
        fe, ff = _fold(str(expected)), _fold(found)
        score = _token_set_ratio(fe, ff)
        missing = sorted(set(fe.split()) - set(ff.split()))
        if missing:
            return _result("mismatch", found, expected_display, confidence,
                           f"missing expected word(s): {', '.join(missing)} (similarity {score:.2f})")
        if score < FUZZY_MATCH_THRESHOLD:
            return _result("mismatch", found, expected_display, confidence,
                           f"similarity {score:.2f} < {FUZZY_MATCH_THRESHOLD:.2f}")
        return _result("match", found, expected_display, confidence,
                       f"similarity {score:.2f} >= {FUZZY_MATCH_THRESHOLD:.2f}; all expected words present")

    # 4) format: present (guaranteed by step 1) + matches regex
    if match_type == "format":
        pattern = _format_pattern(field_rule)
        if pattern.search(found.strip()):
            return _result("match", found, expected_display, confidence, "matches required format")
        return _result("mismatch", found, expected_display, confidence,
                       "value is present but malformed for the required format")

    return _result("uncertain", found, expected_display, confidence,
                   f"unknown match type {match_type!r} in rule set")


# --- public API -------------------------------------------------------------

def load_ruleset(customer: str) -> dict:
    """Load app/rules/<customer>.json (raises FileNotFoundError with hints)."""
    path = RULES_DIR / f"{customer}.json"
    if not path.exists():
        available = sorted(p.stem for p in RULES_DIR.glob("*.json"))
        raise FileNotFoundError(
            f"No rule set for customer '{customer}' (looked for {path}). "
            f"Available: {available or 'none'}"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate(result: ExtractionResult, ruleset: dict) -> dict:
    """Validate an ExtractionResult against a customer rule set.

    Returns {"fields": {field: result, ...}, "summary": {...}}.
    """
    field_rules = ruleset.get("fields", {})
    fields: dict[str, dict] = {}

    for name in EXTRACTION_FIELDS:
        extracted = getattr(result, name)
        rule = field_rules.get(name)
        if rule is None:
            fields[name] = _result("uncertain", extracted.value, None, extracted.confidence,
                                    "no rule defined for this field in the rule set")
        else:
            fields[name] = _evaluate_field(rule, extracted.value, extracted.confidence)

    counts = {"match": 0, "mismatch": 0, "uncertain": 0}
    for r in fields.values():
        counts[r["status"]] += 1

    total = len(fields)
    summary = {
        "customer_id": ruleset.get("customer_id"),
        "customer_name": ruleset.get("customer_name"),
        "match": counts["match"],
        "mismatch": counts["mismatch"],
        "uncertain": counts["uncertain"],
        "total": total,
        # all_clear is true ONLY if every field matched.
        "all_clear": counts["match"] == total,
    }
    return {"fields": fields, "summary": summary}
