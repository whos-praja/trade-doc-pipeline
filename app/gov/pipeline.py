"""Orchestrator: consent gate -> extract -> verify -> minimise -> store -> drop image.

The order matters and is not negotiable:

  1. AUTHORISE  consent.authorise() runs BEFORE the file is opened. No valid consent
                means the image is never read, so nothing to leak.
  2. HASH       the image is hashed for the audit trail, then never persisted.
  3. EXTRACT    only the consented fields, via a schema built from them.
  4. VERIFY     deterministic checks -- checksums, formats, date sanity. No model.
  5. DECIDE     ACCEPTED / REVIEW / REJECTED, computed in pure Python.
  6. MINIMISE   values are masked / blind-indexed / encrypted. Raw values die here.
  7. STORE      only the protected forms, with an erase_after date.

The decision is deterministic, exactly like the trade pipeline's Router: the model
proposes a reading, code decides what happens to it. A hard checksum failure can
never be talked into an acceptance.

CLI:
    python -m app.gov.pipeline --grant --subject user-42 --purpose age_verification \\
        --doc-type aadhaar
    python -m app.gov.pipeline --process card.jpg --consent cns_... --doc-type aadhaar
    python -m app.gov.pipeline --withdraw cns_...
    python -m app.gov.pipeline --purge
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from app.gov import consent as consent_mod
from app.gov import redaction, storage
from app.gov.consent import ConsentError, ConsentRecord
from app.gov.schema import FIELDS, DocType, Sensitivity, get_purpose
from app.gov.verify import INVALID, UNVERIFIED, VALID, age_on, verify_fields

logger = logging.getLogger("gov.pipeline")

ACCEPTED = "ACCEPTED"
REVIEW = "REVIEW"
REJECTED = "REJECTED"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def decide(verification: dict, signals: dict) -> tuple[str, str]:
    """Deterministic outcome from the check results and capture signals. No LLM.

    Priority (first match wins):
      any hard check failed              -> REJECTED
      tampering seen / wrong doc type    -> REVIEW
      anything unverified                -> REVIEW
      everything checked and valid       -> ACCEPTED
    """
    summary = verification["summary"]
    fields = verification["fields"]

    failures = [f"{n} ({r['reason']})" for n, r in fields.items() if r["status"] == INVALID]
    if failures:
        return REJECTED, (
            f"REJECTED: {len(failures)} field(s) failed a deterministic check and cannot be "
            f"accepted: {'; '.join(failures)}."
        )

    if signals.get("visible_tampering"):
        return REVIEW, "REVIEW: the model reported visible signs of tampering; a human must look."

    seen = (signals.get("document_type_seen") or "").strip().lower()
    if seen and seen.replace(" ", "_") != summary["doc_type"]:
        return REVIEW, (
            f"REVIEW: consent covers a {summary['doc_type']} but the image appears to be "
            f"a '{seen}'."
        )

    unverified = [n for n, r in fields.items() if r["status"] == UNVERIFIED]
    if unverified:
        note = ""
        if signals.get("image_quality") == "poor":
            note = " Image quality was reported as poor; a clearer capture may resolve this."
        if signals.get("appears_to_be_screen_or_photocopy"):
            note += " The image appears to be a photo of a screen or a photocopy."
        return REVIEW, (
            f"REVIEW: {len(unverified)} of {summary['total']} field(s) could not be confirmed "
            f"({', '.join(unverified)}); a document is never accepted while anything is "
            f"unverified.{note}"
        )

    return ACCEPTED, (
        f"ACCEPTED: all {summary['total']} field(s) passed their deterministic checks "
        f"for this {summary['doc_type']}."
    )


def minimise(extracted: dict, doc_type: DocType, purpose_key: str,
             store_full: bool = False) -> dict:
    """Convert raw extracted values into the only forms that may be persisted.

    Two extra minimisations beyond masking:
      - a purpose that lists doc_type in `always_masked` never gets full storage;
      - for age_verification, the date of birth is replaced by a derived age and an
        over-18 boolean. Keeping a derived attribute instead of the raw date is the
        strongest form of minimisation available.
    """
    spec = get_purpose(purpose_key)
    masked_only = doc_type in spec.always_masked
    out: dict = {}

    for name, payload in extracted.items():
        value = payload.get("value")
        confidence = payload.get("confidence", 0.0)
        if value is None or not str(value).strip():
            out[name] = {"value": None, "confidence": confidence}
            continue

        field_spec = FIELDS.get(name)
        allow_full = store_full and not (
            masked_only and field_spec and field_spec.sensitivity is Sensitivity.NATIONAL_ID
        )
        protected = redaction.protect(name, str(value), doc_type, store_full=allow_full)
        protected["confidence"] = confidence
        out[name] = protected

    # Purpose-specific derivation: keep the answer, discard the input.
    if purpose_key == "age_verification" and "date_of_birth" in extracted:
        dob = extracted["date_of_birth"].get("value")
        years = age_on(str(dob)) if dob else None
        out["date_of_birth"] = {
            "derived_only": True,
            "age_years": years,
            "is_over_18": (years >= 18) if years is not None else None,
            "note": "date of birth discarded; only the derived age is retained",
            "confidence": extracted["date_of_birth"].get("confidence", 0.0),
        }
    return out


def process(document_path: str, consent_id: str, doc_type: DocType, *,
            actor: str = "system", store_full: bool = False) -> dict:
    """Process one document under one consent. Returns the stored summary.

    Raises ConsentError before ever opening the file if consent does not authorise
    this read.
    """
    record = storage.get_consent(consent_id)
    if record is None:
        raise ConsentError(f"no consent record with id {consent_id!r}")

    # 1. The gate. Before the file is touched.
    permitted = consent_mod.authorise(record, doc_type)
    storage.log(actor, "AUTHORISED", consent_id=consent_id,
                detail=f"doc_type={doc_type.value} fields={len(permitted)}")

    path = Path(document_path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    # 2. Hash for the audit trail; the image itself is never persisted.
    image_hash = _sha256_file(path)

    # 3. Extract only what consent permits.
    from app.gov.extractor import extract  # lazy: keeps the SDK off other paths
    storage.log(actor, "EXTRACT", consent_id=consent_id,
                detail=f"fields={','.join(permitted)} image_sha256={image_hash[:16]}")
    result = extract(path, doc_type, permitted)

    # 4-5. Deterministic verification, then a deterministic decision.
    verification = verify_fields(doc_type, result["fields"])
    outcome, reasoning = decide(verification, result["signals"])

    # 6-7. Minimise, then store. Raw values do not survive this line.
    protected = minimise(result["fields"], doc_type, record.purpose, store_full=store_full)
    verification_id = storage.save_verification(
        record, doc_type=doc_type.value, image_sha256=image_hash, outcome=outcome,
        reasoning=reasoning, fields=protected, verification=verification,
        signals=result["signals"], actor=actor,
    )

    return {
        "verification_id": verification_id,
        "consent_id": consent_id,
        "doc_type": doc_type.value,
        "outcome": outcome,
        "reasoning": reasoning,
        "fields": protected,
        "checks": verification,
        "signals": result["signals"],
    }


def withdraw_and_erase(consent_id: str, *, actor: str = "subject") -> dict:
    """Withdraw consent and erase everything collected under it, in one step."""
    record = storage.get_consent(consent_id)
    if record is None:
        raise ConsentError(f"no consent record with id {consent_id!r}")
    storage.save_consent(consent_mod.withdraw(record), actor=actor)
    erased = storage.erase_for_consent(consent_id, actor=actor)
    return {"consent_id": consent_id, "withdrawn": True, "rows_erased": erased}


# --- CLI --------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.gov.pipeline",
        description="Consent-gated government ID document reader.",
    )
    parser.add_argument("--grant", action="store_true", help="create a consent record")
    parser.add_argument("--subject", help="opaque subject reference (never an ID number)")
    parser.add_argument("--purpose", help="purpose key (see app/gov/schema.py PURPOSES)")
    parser.add_argument("--doc-type", help="document type: " +
                        ", ".join(d.value for d in DocType))
    parser.add_argument("--process", metavar="FILE", help="process a document")
    parser.add_argument("--consent", metavar="CONSENT_ID", help="consent authorising the read")
    parser.add_argument("--store-full", action="store_true",
                        help="store non-Aadhaar ID numbers encrypted in full (needs a lawful basis)")
    parser.add_argument("--withdraw", metavar="CONSENT_ID", help="withdraw consent and erase")
    parser.add_argument("--export", metavar="SUBJECT_REF", help="subject access export")
    parser.add_argument("--purge", action="store_true", help="erase everything past retention")
    parser.add_argument("--purposes", action="store_true", help="list available purposes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.purposes:
            from app.gov.schema import PURPOSES
            for key, spec in PURPOSES.items():
                print(f"\n{key}")
                print(f"  fields    : {', '.join(sorted(spec.allowed_fields))}")
                print(f"  retention : {spec.max_retention_days} days")
                print(f"  notice    : {spec.description}")
            print()
            return 0

        if args.grant:
            if not (args.subject and args.purpose and args.doc_type):
                parser.error("--grant needs --subject, --purpose and --doc-type")
            record = consent_mod.grant(args.subject, args.purpose, [DocType(args.doc_type)])
            storage.save_consent(record)
            print(f"consent_id : {record.consent_id}")
            print(f"purpose    : {record.purpose}")
            print(f"fields     : {', '.join(record.fields)}")
            print(f"expires    : {record.expires_at}")
            print(f"retention  : {record.retention_days} days")
            return 0

        if args.process:
            if not (args.consent and args.doc_type):
                parser.error("--process needs --consent and --doc-type")
            out = process(args.process, args.consent, DocType(args.doc_type),
                          store_full=args.store_full)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out["outcome"] != REJECTED else 1

        if args.withdraw:
            print(json.dumps(withdraw_and_erase(args.withdraw), indent=2))
            return 0

        if args.export:
            print(json.dumps(storage.subject_export(args.export), indent=2, ensure_ascii=False))
            return 0

        if args.purge:
            due = storage.due_for_erasure()
            n = storage.purge_expired()
            print(f"{len(due)} row(s) past retention; erased {n}")
            return 0

        parser.print_help()
        return 2

    except ConsentError as exc:
        print(f"CONSENT REFUSED: {exc}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
