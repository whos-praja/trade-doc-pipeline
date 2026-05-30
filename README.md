# Trade Document Pipeline — POC

A multi-agent pipeline that turns trade documents (commercial invoices, bills of
lading, packing lists, …) into structured, auditable data.

**Status:** the **Extractor** agent is fully implemented. The other agents are
scaffolded stubs.

## Architecture

| Agent | File | Status | Role |
|------|------|--------|------|
| Router | `app/router.py` | stub | Classify document type, pick rule set |
| **Extractor** | `app/extractor.py` | **implemented** | Document image(s) → 8 structured fields via Gemini |
| **Validator** | `app/validator.py` | **implemented** | Deterministic (no LLM) match/mismatch/uncertain vs a customer rule set in `app/rules/` |
| Storage | `app/storage.py` | stub | Persist results to SQLite |
| Query | `app/query.py` | stub | Natural-language Q&A over stored extractions |
| Pipeline | `app/pipeline.py` | stub | Orchestrate Router → Extractor → Validator → Storage |
| UI | `ui/streamlit_app.py` | stub | Upload + review (`streamlit run ui/streamlit_app.py`) |

## The Extractor

Extracts exactly these 8 fields, each as `{ value, confidence, source_snippet }`:

`consignee_name`, `hs_code`, `port_of_loading`, `port_of_discharge`,
`incoterms`, `description_of_goods`, `gross_weight`, `invoice_number`.

How it works:
- **PDF → image:** every page is rasterised to PNG with PyMuPDF (`fitz`) at ~170 dpi.
  Image inputs (PNG/JPG/…) are sent as-is.
- **Model:** `gemini-2.5-flash` (free tier, vision-capable) via the current
  **`google-genai`** SDK.
- **Forced JSON:** the call sets `response_mime_type="application/json"` and a
  Pydantic `response_schema`, so the model must return schema-valid JSON.
- **Anti-hallucination:** the prompt requires every value to appear *literally*
  in the document; missing/unreadable fields are returned as `value: null` with
  low confidence, and `source_snippet` must be a verbatim quote (or `null`).
- **Resilience:** the API call retries with exponential backoff (1s, 2s, 4s, 8s)
  on `429` / transient errors.

### Verified SDK usage (google-genai 1.75.0)

The Gemini call uses the current SDK (not the deprecated `google-generativeai`):

```python
from google import genai
from google.genai import types

client = genai.Client(api_key=...)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[types.Part.from_bytes(data=png_bytes, mime_type="image/png"), prompt],
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ExtractionResult,   # a Pydantic model
    ),
)
result = response.parsed   # validated ExtractionResult (or parse response.text)
```

## Setup

Python 3.11+ required.

```bash
cd trade-doc-pipeline
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt

cp .env.example .env
# edit .env and set GEMINI_API_KEY=...   (free key: https://aistudio.google.com/apikey)
```

## Usage

```bash
python run_extractor.py data/samples/clean_commercial_invoice.pdf
python run_extractor.py path/to/scan.png --verbose
```

Prints pretty JSON of all 8 fields with confidence + source snippet.

### Validator

Runs the Extractor, then **deterministically** (no LLM) checks each field against a
customer rule set in `app/rules/<customer>.json`:

```bash
python run_validator.py data/samples/clean_1.pdf --customer meridian
python run_validator.py data/samples/scan_1.pdf  --customer meridian --json
```

Each field is reported as **match** / **mismatch** / **uncertain** and a summary with
`all_clear`. Key guarantee: a field whose extractor confidence is `< 0.6` (constant
`CONFIDENCE_THRESHOLD`) or whose value is null is **never** "match" — it is "uncertain".
Match types per field: `exact` (normalized equality), `fuzzy` (`token_set_ratio ≥ 0.95`
**and** all expected words present), `format` (regex / built-in like `weight`).
Exit code: `0` = all clear, `1` = issues found, `2` = could not run.

## Project layout

```
trade-doc-pipeline/
├─ app/
│  ├─ config.py          # env + settings
│  ├─ schema.py          # pydantic models (ExtractedField, ExtractionResult)
│  ├─ extractor.py       # ★ Extractor agent (implemented)
│  ├─ router.py          # stub
│  ├─ validator.py       # stub
│  ├─ storage.py         # stub
│  ├─ query.py           # stub
│  ├─ pipeline.py        # stub
│  └─ rules/             # business rules (empty for now)
├─ ui/streamlit_app.py   # stub
├─ data/samples/         # drop test documents here
├─ run_extractor.py      # CLI entry point
├─ requirements.txt
└─ .env.example
```

## Next steps

Build the Router, Storage (SQLite), and Query agent, wire them in `pipeline.py`,
and surface everything in the Streamlit UI. (Extractor and Validator are done.)
