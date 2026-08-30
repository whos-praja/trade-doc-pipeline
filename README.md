# Trade Document Pipeline — POC

A multi-agent pipeline that turns trade documents (commercial invoices, etc.) into structured,
auditable, decision-ready data. **Six agents** — Extractor, Validator, Router, Storage, a Pipeline
orchestrator, and a natural-language Query agent — run end to end behind a one-screen **Streamlit UI**.

**Architecture in one line:** the model *only extracts*; deterministic code (Validator + Router) makes
every decision; every run is a fresh live API call, and each step is checkpointed to SQLite for
crash recovery (an interrupted run resumes from its last completed step).

> **⚡ Every result is live — no caching.** There is **no prompt or response caching** anywhere. Every
> document is processed by a fresh call to the model API in real time. SQLite keeps a record of each run
> (for audit and the Query agent); it never serves a stored/stale answer in place of a live call.

## Agents

| Agent | File | Role |
|---|---|---|
| Extractor | `app/extractor.py` | Document image(s) → 8 fields `{value, confidence, source_snippet}` via Claude (`claude-opus-4-8`) vision. Anti-hallucination: literal values only; `null` when absent. |
| Validator | `app/validator.py` | **Deterministic, no LLM.** Each field → `match` / `mismatch` / `uncertain` vs the customer rule set. Confidence `< 0.6` or null → `uncertain` (never silently approved). |
| Router | `app/router.py` | **Deterministic decision** → `AUTO_APPROVE` / `REVIEW` / `AMEND`. On `AMEND`, one LLM call drafts a supplier amendment email (drafts only — never sends). |
| Storage | `app/storage.py` | SQLite (`data/pipeline.db`); one row per run, checkpointed after every step. |
| Pipeline | `app/pipeline.py` | Orchestrates Extractor → Validator → Router → store; resumable crash recovery. |
| Query | `app/query.py` | Natural-language → guardrailed read-only SQL over the runs, with grounded answers. |
| UI | `ui/streamlit_app.py` | One screen: run the pipeline, view fields / validation / decision / email, ask questions. |

## Setup (laptop, from scratch)

Requires **Python 3.11+**.

```bash
cd trade-doc-pipeline
python -m venv .venv && source .venv/bin/activate     # recommended
pip install -r requirements.txt

cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=...
# key from the Anthropic console: https://console.anthropic.com/
```

> **🔑 API key.** `.env` is gitignored, so the repo carries **no key** in version control. Set
> `ANTHROPIC_API_KEY=...` in `.env` (default model `claude-opus-4-8`, or `ANTHROPIC_MODEL=claude-sonnet-4-6`
> to roughly halve cost). Verify a key anytime — **without running the pipeline** — via
> `python scripts/check_key.py` (prints `KEY OK`, `RATE-LIMITED`, or `KEY INVALID`).

## Run

**Main entry — the UI** (pick a sample or upload → *Run pipeline* → browse results → ask questions):

```bash
streamlit run ui/streamlit_app.py
```

**Full pipeline on one document (CLI):**

```bash
python -m app.pipeline data/samples/scan_1.pdf --customer meridian
```

**Individual agents (CLIs):**

```bash
python run_extractor.py data/samples/clean_1.pdf                       # extract 8 fields → JSON
python run_validator.py data/samples/scan_1.pdf  --customer meridian   # + validate vs rules
python run_router.py    data/samples/scan_1.pdf  --customer meridian   # + decision + draft email
python run_query.py     "how many shipments were flagged this week?"   # NL question over stored runs
```

## Sample documents (`data/samples/`)

| File | What it shows |
|---|---|
| `clean_1.pdf` | Clean digital invoice — all 8 fields match the rule set → **AUTO_APPROVE**. |
| `scan_1.pdf` | A different invoice — `hs_code`, `port_of_discharge`, `consignee_name`, `description` differ → **AMEND** (drafts a supplier correction email). |
| `02_missing_fields.pdf` | HS code, Incoterms & gross weight are **physically absent** → extractor returns `null`/low-confidence → Validator marks them `uncertain` → **REVIEW** (the anti-hallucination win). |
| `01_hard_scan.pdf` | Skewed, low-quality scan — OCR robustness; the deterministic Validator catches OCR slips as mismatches. |
| `03`–`10` | Progressively harder / adversarial captures (occluded stamps, glare, faded thermal, night blur, perspective, planted errors) used to pressure-test extraction and the deterministic safety net. |

## Ask the data (sample NL queries)

```text
how many shipments were flagged this week?
show everything pending review for Meridian Imports GmbH
what is the most common discrepancy across flagged documents?
```

NL→SQL is guardrailed (single read-only `SELECT`, auto-`LIMIT`); on any failure it falls back to canned intents.

## Government ID subsystem (`app/gov/`) — consent-gated

A second, self-contained pipeline that reads **government identity documents** (Aadhaar,
PAN, passport, driving licence, voter ID) **only under explicit, recorded, revocable
consent**, and stores the least data the stated purpose requires.

> **Read the document before using it:** [`docs/GOVERNMENT_ID_SYSTEM.md`](docs/GOVERNMENT_ID_SYSTEM.md).
> Reading an ID with a vision model **does not prove the document is genuine**. Where a
> decision matters, use the issuer instead — UIDAI offline QR (signed, verifiable offline),
> DigiLocker Issued Documents, or the Protean PAN API. OCR is for documents with no digital
> route, and always routes to a human when anything is uncertain.

| Module | Role |
|---|---|
| `app/gov/schema.py` | Purpose-bound field allowlists. A purpose defines what may be read — `age_verification` cannot read an ID number at all. |
| `app/gov/consent.py` | HMAC-signed, expiring, withdrawable consent records. `authorise()` is the single gate. |
| `app/gov/extractor.py` | Vision extraction whose tool schema is **built from the consented fields**, so an un-consented field has no slot to be written into. |
| `app/gov/verify.py` | **Deterministic, no model.** Aadhaar Verhoeff checksum, ICAO 9303 MRZ check digits, PAN/DL/EPIC structure, date sanity. |
| `app/gov/redaction.py` | Masking, keyed blind indexing, Fernet encryption. Full Aadhaar storage is refused by construction. |
| `app/gov/storage.py` | SQLite with retention dates, append-only access log, erasure, subject export. |
| `app/gov/pipeline.py` | Consent gate → extract → verify → deterministic decide → minimise → store. |

```bash
export CONSENT_SIGNING_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export BLIND_INDEX_PEPPER=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

python -m app.gov.pipeline --purposes                                     # what may be collected, and why
python -m app.gov.pipeline --grant --subject user-42 \
    --purpose age_verification --doc-type aadhaar                         # → cns_...
python -m app.gov.pipeline --process card.jpg --consent cns_... \
    --doc-type aadhaar                                                    # needs ANTHROPIC_API_KEY
python -m app.gov.pipeline --export user-42                               # subject access request
python -m app.gov.pipeline --withdraw cns_...                             # withdraw + erase
python -m app.gov.pipeline --purge                                        # retention job

python -m tests.test_gov                                                  # 31 tests, no API key needed
```

Design notes, including why there is no trained detection model anywhere in this repo:
[`docs/HOW_EXTRACTION_WORKS.md`](docs/HOW_EXTRACTION_WORKS.md).

## Customer rules

Expected values live in **`app/rules/meridian.json`** — one JSON file per customer. Add a customer by
copying it to `app/rules/<id>.json` and passing `--customer <id>`. Match types: `exact`, `fuzzy`
(`token_set_ratio ≥ 0.95` **and** all expected words present), and `format` (regex / built-in `weight`).

## Project layout

```
trade-doc-pipeline/
├─ app/
│  ├─ config.py          # env + settings (ANTHROPIC_API_KEY, model, dpi)
│  ├─ schema.py          # pydantic models
│  ├─ llm.py             # Claude API calls (vision extraction + text)
│  ├─ extractor.py       # Extractor agent (the only LLM extraction)
│  ├─ validator.py       # Validator agent (deterministic)
│  ├─ router.py          # Router / Decision agent (+ amendment-email draft)
│  ├─ storage.py         # SQLite persistence
│  ├─ pipeline.py        # orchestrator (resumable)  ·  python -m app.pipeline
│  ├─ query.py           # NL → SQL query agent
│  ├─ rules/meridian.json
│  └─ gov/               # consent-gated government ID subsystem
│     ├─ schema.py       #   purposes → allowed fields (data minimisation)
│     ├─ consent.py      #   signed, expiring, withdrawable consent records
│     ├─ extractor.py    #   vision extraction scoped to consented fields
│     ├─ verify.py       #   deterministic checks (Verhoeff, MRZ, formats)
│     ├─ redaction.py    #   masking, blind index, encryption at rest
│     ├─ storage.py      #   retention, audit log, erasure, subject export
│     └─ pipeline.py     #   orchestrator  ·  python -m app.gov.pipeline
├─ docs/
│  ├─ HOW_EXTRACTION_WORKS.md    # how the vision extraction is built
│  └─ GOVERNMENT_ID_SYSTEM.md    # consent design + legal constraints
├─ tests/test_gov.py     # 31 tests for the ID subsystem (no API key needed)
├─ ui/streamlit_app.py   # Streamlit UI (main entry)
├─ data/samples/         # sample documents
├─ run_extractor.py  run_validator.py  run_router.py  run_query.py
├─ requirements.txt   .env.example
```

## Notes

- Model **`claude-opus-4-8`** (vision) via the official **`anthropic`** SDK; override with `ANTHROPIC_MODEL`.
  The SDK retries transient 429/5xx with backoff automatically. One to two model calls per document
  (one vision extraction; a second only to draft an amendment email on `AMEND`).
- **No caching** — every run is a fresh, live API call (see the note at the top).
- `.env` and `data/pipeline.db` are gitignored — keep your API key out of version control.
