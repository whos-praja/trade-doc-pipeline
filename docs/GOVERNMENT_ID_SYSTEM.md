# Consent-gated government ID document system

Reads government identity documents **only** under an explicit, recorded, revocable
consent, and stores the least data that satisfies the stated purpose.

> Not legal advice. The design below reflects how India's DPDP Act 2023, the Aadhaar
> Act and UIDAI regulations are commonly implemented, but obligations vary by sector
> (RBI/SEBI/IRDAI KYC rules differ) and change over time. Have counsel review before
> processing anyone's real documents.

## Read this first: OCR does not verify a document

This is the single most important thing to understand before building an ID system.

**Reading an ID with a vision model proves nothing about whether the ID is genuine.**
A competent forgery — or a real card with one digit edited in an image editor — passes
every check in `app/gov/verify.py`. Checksums prove the number is *well-formed*, not
that it was *issued*, and not that the person handing it over is its holder.

If your decision matters (opening an account, disbursing money, granting access), the
authoritative path is to ask **the issuer**, not the image:

| Document | Authoritative route | What you get |
|---|---|---|
| Aadhaar | **Offline QR / Paperless e-KYC ZIP** — digitally signed by UIDAI, verifiable offline against UIDAI's public key with a share code from the holder | Issuer-signed name, DOB, gender, address, photo. No UIDAI licence needed. |
| Aadhaar | Online authentication / e-KYC via a licensed AUA/KUA | Live yes/no or full e-KYC. Requires UIDAI authorisation. |
| Any | **DigiLocker Issued Documents API** (consent-driven OAuth) | Issuer-signed documents pulled with the holder's consent. |
| PAN | Protean (NSDL) / Income Tax PAN verification API | Authoritative name + status for a PAN. |
| Passport | MRZ check digits from the printed zone; the RFID chip for an e-passport | MRZ catches transcription errors. Chip data needs contactless hardware. |

**Use OCR when there is no digital route**, or as a pre-fill step that a human confirms.
That is the honest role for the code in `app/gov/`. It is built so that its output
routes to a human whenever anything is uncertain, and never auto-accepts on a guess.

## The consent design

Consent here is a **capability**, not a checkbox. A `ConsentRecord` is the only thing
that authorises reading a document, and it carries its own limits.

```
                    ┌──────────────────────────────────────────┐
                    │  PURPOSE  (schema.py PURPOSES)           │
                    │  the notice text the person actually saw │
                    │  ⇒ allowed fields  ⇒ max retention       │
                    └────────────────────┬─────────────────────┘
                                         │ grant() derives fields FROM the purpose
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │  ConsentRecord — HMAC-SHA256 signed      │
                    │  subject · purpose · doc_types · fields  │
                    │  retention · expiry · notice hash        │
                    └────────────────────┬─────────────────────┘
                                         │ authorise() — runs BEFORE the file opens
                                         ▼
   image ─→ sha256 (audit) ─→ extract(ONLY consented fields) ─→ verify (no model)
                                         │                            │
                                         │                    deterministic decide()
                                         ▼                            ▼
                          minimise: mask / blind-index / encrypt   ACCEPTED
                                         │                         REVIEW
                                         ▼                         REJECTED
                          store + erase_after   ·   raw values discarded
```

### Five properties worth copying

**1. The purpose defines the schema.** `grant()` does not let the caller choose fields —
they are derived from the purpose. `extractor.build_tool()` then builds the model's tool
schema from exactly that list, so an un-consented field **has no slot to be written
into**. Minimisation is structural, not a filter applied afterwards. A prompt-injection
string printed on the document cannot make the model return an ID number that has no
key in the schema.

```
consent for age_verification → tool schema properties:
  ['appears_to_be_screen_or_photocopy', 'date_of_birth', 'document_type_seen',
   'full_name', 'image_quality', 'visible_tampering']          ← no id_number
```

**2. Consent is tamper-evident.** Each record is HMAC-signed over its canonical JSON.
Widening it after the fact — adding a field, extending retention — breaks the signature
and `authorise()` refuses. Add a field to a granted consent and you get:

```
CONSENT REFUSED: consent cns_... failed signature verification -- the record was
altered after it was granted, or signed with a different key. Refusing to proceed.
```

**3. The gate runs before the file is opened.** In `pipeline.process()`, `authorise()`
is called before `Path.exists()`. No valid consent means the image is never read, so
there is nothing to leak. Withdrawal and expiry are checked on the same path — expired
consent is not consent.

**4. Derive the answer, discard the input.** For `age_verification` the date of birth
never reaches storage; only `age_years` and `is_over_18` do. Keeping the answer instead
of the input is the strongest minimisation available, and it is usually what the business
actually needed.

**5. Full Aadhaar numbers cannot be stored, by construction.** `redaction.protect()`
refuses full storage for Aadhaar even when `store_full=True` is explicitly passed. There
is no flag to override it — it keeps the last 4 digits plus a keyed blind index:

```python
protect("id_number", aadhaar, DocType.AADHAAR, store_full=True)
# {'masked': 'XXXX XXXX 0124',
#  'blind_index': '29b3a79e...',
#  'full_storage': 'refused_by_policy',
#  'policy': 'UIDAI regulations do not permit storing a full Aadhaar number here...'}
```

### Masking, blind indexing, encryption — three different jobs

| | Reversible | Purpose |
|---|---|---|
| `mask()` | no | What screens, logs and support agents see. `XXXX XXXX 0124` |
| `blind_index()` | no | "Have we seen this ID before?" without storing it. **Keyed** HMAC — an unkeyed SHA-256 of a 12-digit space is brute-forceable in seconds |
| `encrypt()` | yes | The few fields you are legally required to retain in full. Fernet; key in a KMS, **not** beside the database |

## What the deterministic layer can and cannot prove

`verify.py` runs **no model**. Same principle as the trade pipeline's Validator.

**Real signal:**
- **Verhoeff checksum** on Aadhaar (UIDAI-mandated 12th digit). It catches *every*
  single-digit error and *every* adjacent transposition — precisely the OCR failure
  mode. The test suite verifies this exhaustively over all 108 single-digit mutations.
- **ICAO 9303 MRZ check digits** for passports, verified against the published worked
  examples (`L898902C<` → 3, `740812` → 2, `120415` → 9).
- Structural rules for PAN (including the holder-type letter), DL, and EPIC.
- Date sanity: future DOB, age > 120, expired document.

**No signal at all:** whether the document is genuine, unaltered, or belongs to the
person presenting it. `verify.py` says so in its own module docstring, so nobody reading
the code mistakes a green tick for authenticity.

The decision in `pipeline.decide()` is a pure function of those results:
hard check failed → `REJECTED`; tampering or wrong document type → `REVIEW`; anything
unverified → `REVIEW`; everything checked and valid → `ACCEPTED`. A model cannot argue
its way past a failed checksum.

## Subject rights, implemented not just documented

| Right (DPDP 2023 / GDPR) | Call |
|---|---|
| Withdraw consent (as easy as giving it) | `pipeline.withdraw_and_erase(consent_id)` — withdraws **and** erases in one step |
| Erasure | `storage.erase_for_consent()` — blanks data columns, keeps an audit stub |
| Access / portability | `storage.subject_export(subject_ref)` — consents, verifications, full access log |
| Storage limitation | `storage.purge_expired()` — run it on a schedule; retention that is never executed is not retention |
| Transparency | `access_log` records every grant, authorise, extract, read, erase, and purge |

Erasure keeps a stub row (`erased_at` set, data columns blanked) so the audit trail can
still show that data existed and was destroyed on time — without retaining the data.

## Running it

```bash
# One-time key setup. Store these in a secrets manager, never in git.
export CONSENT_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export BLIND_INDEX_PEPPER=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export FIELD_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

python -m app.gov.pipeline --purposes                      # what may be collected, and why

python -m app.gov.pipeline --grant --subject user-42 \
    --purpose age_verification --doc-type aadhaar          # → cns_...

python -m app.gov.pipeline --process card.jpg \
    --consent cns_... --doc-type aadhaar                   # needs ANTHROPIC_API_KEY

python -m app.gov.pipeline --export user-42                # subject access request
python -m app.gov.pipeline --withdraw cns_...              # withdraw + erase
python -m app.gov.pipeline --purge                         # the retention job

python -m tests.test_gov                                   # 31 tests, no API key needed
```

Rotating `CONSENT_SIGNING_KEY` invalidates every existing consent record's signature, and
losing `BLIND_INDEX_PEPPER` or `FIELD_ENCRYPTION_KEY` is unrecoverable. Plan rotation
(keep old keys for verification) before you go anywhere near production.

## Before you deploy this

The code here is a working, tested reference architecture, not a production system.
At minimum you would still need:

- **A real consent UI.** The notice text must be shown in the person's language, be
  refusable without penalty, and be logged with its version. `evidence.notice_sha256`
  exists so you can later prove what was on screen.
- **Keys in a KMS**, with rotation and per-environment separation.
- **Liveness / presentation-attack detection** if you are matching a face — a photo of
  a photo defeats naive face matching, and `appears_to_be_screen_or_photocopy` is a
  triage hint, not a control.
- **Access control on the decrypt path.** `get_verification()` logs every read; it does
  not authorise them.
- **A DPIA**, a breach-notification process, a named grievance officer, and a data
  processing agreement with any model provider you send images to.
- **Transfer rules.** Sending an image to a hosted model API is a cross-border transfer.
  Check it is permitted for your sector before you send the first document.
