"""Canonical field vocabulary, document types, and PURPOSE-BOUND field allowlists.

This module is the heart of the consent design. Data minimisation is not a policy
document here -- it is a data structure. A purpose (e.g. "age_verification") names
the ONLY fields that may ever be read for that purpose, and the extractor is built
from that allowlist, so an un-consented field is never even asked for, let alone
stored.

    purpose  ->  allowed canonical fields  ->  extraction schema  ->  storage columns

Three hard rules are enforced in code, not by convention:
  1. A field absent from the purpose's allowlist is never sent to the model.
  2. A field the document type does not carry is dropped from the ask.
  3. AADHAAR.id_number is ALWAYS masked at rest (UIDAI rule) -- see `always_masked`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DocType(str, Enum):
    """Government identity documents this system can read."""

    AADHAAR = "aadhaar"
    PAN = "pan"
    PASSPORT = "passport"
    DRIVING_LICENCE = "driving_licence"
    VOTER_ID = "voter_id"


class Sensitivity(str, Enum):
    """How damaging this field is if it leaks. Drives masking and audit strictness."""

    NATIONAL_ID = "national_id"  # the unique government identifier itself
    PII = "pii"                  # directly identifies a person
    ATTRIBUTE = "attribute"      # a property of the person/document


@dataclass(frozen=True)
class FieldSpec:
    """One canonical field: how to read it, and how sensitive it is."""

    name: str
    instruction: str          # what the model is told to look for
    sensitivity: Sensitivity
    keep_prefix: int = 0      # characters kept at the start when masked
    keep_suffix: int = 4      # characters kept at the end when masked


# --- canonical field vocabulary --------------------------------------------
# Shared across document types so a purpose can be written once, not per document.

FIELDS: dict[str, FieldSpec] = {
    "full_name": FieldSpec(
        "full_name",
        "the document holder's full name exactly as printed",
        Sensitivity.PII,
    ),
    "date_of_birth": FieldSpec(
        "date_of_birth",
        "the date of birth exactly as printed (do not reformat)",
        Sensitivity.PII,
    ),
    "gender": FieldSpec(
        "gender",
        "the gender/sex as printed (e.g. MALE, FEMALE, M, F, TRANSGENDER)",
        Sensitivity.ATTRIBUTE,
    ),
    "address": FieldSpec(
        "address",
        "the full postal address as printed, including PIN/postal code",
        Sensitivity.PII,
    ),
    "id_number": FieldSpec(
        "id_number",
        "the document's primary identifying number as printed",
        Sensitivity.NATIONAL_ID,
    ),
    "issue_date": FieldSpec(
        "issue_date",
        "the date the document was issued, as printed",
        Sensitivity.ATTRIBUTE,
    ),
    "expiry_date": FieldSpec(
        "expiry_date",
        "the date the document expires / is valid until, as printed",
        Sensitivity.ATTRIBUTE,
    ),
    "guardian_name": FieldSpec(
        "guardian_name",
        "the father's / husband's / guardian's name as printed",
        Sensitivity.PII,
    ),
    "nationality": FieldSpec(
        "nationality",
        "the nationality as printed",
        Sensitivity.ATTRIBUTE,
    ),
    "place_of_birth": FieldSpec(
        "place_of_birth",
        "the place of birth as printed",
        Sensitivity.PII,
    ),
}

# --- which fields each document type actually carries -----------------------
# Asking a PAN card for an address wastes a call and invites a hallucinated value.

DOC_FIELDS: dict[DocType, frozenset[str]] = {
    DocType.AADHAAR: frozenset(
        {"full_name", "date_of_birth", "gender", "address", "id_number"}
    ),
    DocType.PAN: frozenset(
        {"full_name", "date_of_birth", "guardian_name", "id_number"}
    ),
    DocType.PASSPORT: frozenset(
        {"full_name", "date_of_birth", "gender", "place_of_birth", "nationality",
         "id_number", "issue_date", "expiry_date", "address", "guardian_name"}
    ),
    DocType.DRIVING_LICENCE: frozenset(
        {"full_name", "date_of_birth", "address", "id_number", "issue_date",
         "expiry_date", "guardian_name"}
    ),
    DocType.VOTER_ID: frozenset(
        {"full_name", "date_of_birth", "gender", "address", "id_number", "guardian_name"}
    ),
}

# Masking styles per document type for the NATIONAL_ID field. A document type
# listed in `always_masked` can NEVER have its id_number stored in the clear,
# whatever the purpose or the caller asks for.
ID_MASK: dict[DocType, tuple[int, int]] = {
    DocType.AADHAAR: (0, 4),          # UIDAI: only the last 4 digits may be shown
    DocType.PAN: (2, 2),
    DocType.PASSPORT: (1, 2),
    DocType.DRIVING_LICENCE: (4, 2),
    DocType.VOTER_ID: (3, 2),
}


@dataclass(frozen=True)
class PurposeSpec:
    """A lawful purpose: what it may read, for how long, and why."""

    key: str
    description: str            # shown verbatim to the person in the consent notice
    allowed_fields: frozenset[str]
    max_retention_days: int
    always_masked: frozenset[DocType] = field(default_factory=frozenset)

    def fields_for(self, doc_type: DocType) -> list[str]:
        """Intersect purpose allowlist with what this document actually carries."""
        available = DOC_FIELDS[doc_type]
        return sorted(f for f in self.allowed_fields if f in available)


# --- the purpose catalogue --------------------------------------------------
# Adding a purpose is a deliberate, reviewable act: it is the only way to widen
# what the system may read. Note that `age_verification` cannot read an id_number
# at all -- proving someone is over 18 never requires their Aadhaar number.

PURPOSES: dict[str, PurposeSpec] = {
    "age_verification": PurposeSpec(
        key="age_verification",
        description=(
            "Confirm you are above the required minimum age. We read only your name "
            "and date of birth. We do not read or store your ID number."
        ),
        allowed_fields=frozenset({"full_name", "date_of_birth"}),
        max_retention_days=30,
    ),
    "address_proof": PurposeSpec(
        key="address_proof",
        description=(
            "Confirm your residential address for delivery or service eligibility. "
            "We read your name and address only."
        ),
        allowed_fields=frozenset({"full_name", "address"}),
        max_retention_days=180,
    ),
    "identity_verification": PurposeSpec(
        key="identity_verification",
        description=(
            "Confirm the identity you have given us matches your government ID. We "
            "read your name, date of birth, gender and ID number. ID numbers are "
            "stored masked."
        ),
        allowed_fields=frozenset(
            {"full_name", "date_of_birth", "gender", "id_number"}
        ),
        max_retention_days=365,
        always_masked=frozenset({DocType.AADHAAR}),
    ),
    "kyc_onboarding": PurposeSpec(
        key="kyc_onboarding",
        description=(
            "Complete regulated Know-Your-Customer onboarding. We read the identity "
            "and address details printed on your document, plus its issue and expiry "
            "dates, and retain them for the period the regulator requires."
        ),
        allowed_fields=frozenset(
            {"full_name", "date_of_birth", "gender", "address", "id_number",
             "issue_date", "expiry_date", "nationality"}
        ),
        max_retention_days=1825,  # 5 years, the PMLA record-keeping period
        always_masked=frozenset({DocType.AADHAAR}),
    ),
}


def get_purpose(key: str) -> PurposeSpec:
    """Look up a purpose, with an actionable error listing the valid ones."""
    try:
        return PURPOSES[key]
    except KeyError:
        raise ValueError(
            f"unknown purpose {key!r}; valid purposes: {sorted(PURPOSES)}"
        ) from None
