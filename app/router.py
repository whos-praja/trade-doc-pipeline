"""Router / Decision agent.

The DECISION is fully deterministic: it is computed from the Validator's output
with NO model call. The ONLY LLM call in this module is to draft the supplier
amendment-email PROSE, and only on the AMEND path. The agent NEVER sends an
email -- it only drafts one for a human to review and send.

Decision priority (first match wins):
    any field status == "mismatch"   -> AMEND         (draft a correction request)
    else any status == "uncertain"   -> REVIEW        (human must look; no email)
    else (every field matched)       -> AUTO_APPROVE  (approval note; mark for storage)
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from app import config

logger = logging.getLogger("router")

AUTO_APPROVE = "AUTO_APPROVE"
REVIEW = "REVIEW"
AMEND = "AMEND"


class Discrepancy(BaseModel):
    field: str
    found: Optional[str] = None
    expected: Optional[str] = None


class UncertainField(BaseModel):
    field: str
    reason: str


class Decision(BaseModel):
    outcome: str
    reasoning: str
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    uncertain_fields: list[UncertainField] = Field(default_factory=list)
    draft_email: Optional[str] = None


# --- the single LLM call: amendment-email prose (AMEND path only) -----------

def _draft_amendment_email(customer_name: str, invoice_ref: Optional[str],
                           discrepancies: list[Discrepancy]) -> str:
    """Call gemini-2.5-flash ONCE to draft a supplier amendment email.

    Imports the SDK lazily so the deterministic decision path never needs it.
    """
    import time
    from google import genai
    from google.genai import types
    from app.extractor import _is_retryable  # reuse the project's transient-error policy

    disc_lines = "\n".join(
        f'  - {d.field} — found "{d.found}", expected "{d.expected}"' for d in discrepancies
    )
    ref = invoice_ref or "the referenced commercial document"
    prompt = f"""You are drafting an email on behalf of {customer_name} (the importer/buyer) to their \
supplier. Our automated document check on an incoming commercial document ({ref}) found discrepancies \
against the agreed order terms. Write a polite, professional email requesting a corrected document set.

Requirements:
- Begin with a "Subject:" line.
- Greet generically ("Dear Supplier Team,") as the contact name is unknown.
- State the document reference ({ref}) and that our check found the discrepancies listed below.
- List EVERY discrepancy exactly as given, one per line, in this form:
    <field> — found "<value>", expected "<value>"
- Politely ask them to amend the document(s) so each field matches the expected value and to re-issue a corrected set.
- Close professionally on behalf of the {customer_name} documentation team, ending with a "[Your Name]" placeholder.
- Be concise and ready to send after a single edit (the sender's name). Do NOT invent any facts beyond the discrepancies and reference provided.

Discrepancies:
{disc_lines}

Return ONLY the email text, beginning with the Subject line."""

    client = genai.Client(api_key=config.require_api_key())
    gen_config = types.GenerateContentConfig(temperature=0.3)
    backoffs = config.RETRY_BACKOFFS
    last_exc: Optional[Exception] = None
    for attempt in range(len(backoffs) + 1):
        try:
            resp = client.models.generate_content(
                model=config.GEMINI_MODEL, contents=prompt, config=gen_config,
            )
            text = (getattr(resp, "text", None) or "").strip()
            if not text:
                raise RuntimeError("model returned an empty email draft")
            return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < len(backoffs) and _is_retryable(exc):
                delay = backoffs[attempt]
                logger.warning("email draft transient error (%s); retry %d/%d in %ds",
                               type(exc).__name__, attempt + 1, len(backoffs), delay)
                time.sleep(delay)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _approval_note(customer_name: str, invoice_ref: Optional[str], total: int) -> str:
    """Deterministic short approval note (NO LLM call)."""
    ref = invoice_ref or "the submitted document"
    return (f"APPROVED — {ref} passed automated validation against the {customer_name} rule set "
            f"({total}/{total} fields matched). No discrepancies found; cleared for processing "
            f"and marked for storage.")


def _invoice_ref(fields: dict) -> Optional[str]:
    return (fields.get("invoice_number") or {}).get("found")


# --- public API: the deterministic decision --------------------------------

def route(validation: dict, *, draft: bool = True) -> Decision:
    """Decide AUTO_APPROVE / REVIEW / AMEND from a Validator result dict.

    The decision itself is deterministic. On AMEND (and only then, and only when
    draft=True) a single gemini-2.5-flash call drafts the supplier amendment email.
    """
    fields = validation.get("fields", {})
    summary = validation.get("summary", {})
    customer = summary.get("customer_name") or summary.get("customer_id") or "the customer"
    total = summary.get("total", len(fields))

    mismatches = [(f, r) for f, r in fields.items() if r.get("status") == "mismatch"]
    uncertains = [(f, r) for f, r in fields.items() if r.get("status") == "uncertain"]

    discrepancies = [Discrepancy(field=f, found=r.get("found"), expected=r.get("expected"))
                     for f, r in mismatches]
    uncertain_fields = [UncertainField(field=f, reason=r.get("reason") or "unverified")
                        for f, r in uncertains]
    invoice_ref = _invoice_ref(fields)

    if mismatches:
        outcome = AMEND
        disc_text = "; ".join(f'{d.field} (found "{d.found}", expected "{d.expected}")'
                              for d in discrepancies)
        reasoning = (f"AMEND: {len(mismatches)} of {total} fields do not match the {customer} "
                     f"rule set, so the document cannot be approved — a correction must be "
                     f"requested from the supplier. Discrepancies: {disc_text}.")
        if uncertain_fields:
            reasoning += (f" (Also {len(uncertain_fields)} unverified field(s): "
                          f"{', '.join(u.field for u in uncertain_fields)}.)")
        draft_email = _draft_amendment_email(customer, invoice_ref, discrepancies) if draft else None

    elif uncertains:
        outcome = REVIEW
        unc_text = "; ".join(f"{u.field} ({u.reason})" for u in uncertain_fields)
        reasoning = (f"REVIEW: no hard mismatches, but {len(uncertains)} of {total} field(s) could "
                     f"not be confirmed and must be checked by a human before approval — a document "
                     f"is NEVER auto-approved while anything is uncertain. Uncertain: {unc_text}.")
        draft_email = None

    else:
        outcome = AUTO_APPROVE
        reasoning = (f"AUTO_APPROVE: all {total} fields matched the {customer} rule set "
                     f"(no mismatches, nothing uncertain); the document is cleared and marked for storage.")
        draft_email = _approval_note(customer, invoice_ref, total)

    return Decision(outcome=outcome, reasoning=reasoning, discrepancies=discrepancies,
                    uncertain_fields=uncertain_fields, draft_email=draft_email)
