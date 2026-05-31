# Brutal invoice test set (06–10)

Five adversarial images, harder than the existing `01`–`05` hard docs and chosen to
fill gaps in that suite. Ground truth = the **correct Meridian values** unless a doc
says "WRONG DATA PLANTED". A clean read of a correct doc would AUTO_APPROVE.

**Cost reminder:** generating these was **0 Gemini calls**. *Running* each one is
~**1–2 Flash calls**. Your fail-fast retry fix means a failed call no longer
amplifies, so the cost is bounded — but the key has been exhausted twice tonight, so
pace them and don't run all five if you're tight on quota before submitting.

| File | Failure mode | Data | What "passing" looks like |
|------|--------------|------|---------------------------|
| `06_night_blur.jpg` | Low-light phone photo: dark, blue cast, heavy blur, tilt | correct | Either reads correctly (robust) **or** returns low-confidence / null on the faded fields → those go **uncertain → REVIEW**. No invented wrong value. |
| `07_inkblot_occlusion.jpg` | Opaque ink blots covering **exactly** the HS code + invoice number | correct | `hs_code` and `invoice_number` come back **null** → uncertain; the rest match. It must NOT invent `6109.10` / an invoice number from context. |
| `08_perspective_glare.jpg` | Shot at an angle (keystone) + glare band washing the shipment block | correct | Robust on the clear fields; honest **null / low-confidence** on the glared port/incoterms area. |
| `09_cutoff_partial.jpg` | Only the top ⅓ captured — goods table physically torn off | correct (for fields present) | `description_of_goods`, `hs_code`, `gross_weight` → **null** (not in frame) → uncertain → REVIEW. Strongest anti-hallucination test: the model "knows" t-shirts ship under 6109.10 — it must **not** fill that in. |
| `10_fax_dither_wrongdata.png` | 1-bit fax dither + crumple shadows **+ 3 planted errors** | **WRONG** | Validator flags the planted errors → Router → **AMEND** with a found-vs-expected email. Even on a brutal doc, **never a silent approve**. |

### Planted errors in `10_fax_dither_wrongdata.png`
- `consignee_name`: **"Meridien Imports GmbH"** (misspelled — should be *Meridian*) → fuzzy fails the "all expected words present" signal → **mismatch**
- `port_of_discharge`: **"Bremerhaven (DEBRV), Germany"** (should be *Hamburg (DEHAM)*) → exact **mismatch**
- `hs_code`: **"6205.20"** (should be *6109.10*) → exact **mismatch**

## How to run them (on your Mac)
Drop the files into `data/samples/` (the UI sample picker lists that folder) **or**
use the UI's upload control. Then:

- **UI:** pick / upload → customer = `meridian` → **Run**
- **CLI:** `python -m app.pipeline data/samples/09_cutoff_partial.jpg --customer meridian`

## The core thing every one of these tests
The model is allowed to *fail to read* a field — that's fine, it returns null and the
field becomes "uncertain". What it must **never** do is **confidently invent** a value
or let a bad doc slide through as approved. Null path + deterministic Validator =
the safety net. That's the whole story, stress-tested.
