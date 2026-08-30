# How the document reading in this repo actually works

## The headline: there is no trained detection model

It is worth stating plainly, because the phrase "image detection model" implies
something this repo does not contain:

- There is **no CNN, no YOLO, no object detector**.
- There is **no training dataset, no labelling, no training run, no fine-tuning**.
- There are **no model weights** in this repository. `git ls-files` returns Python,
  JSON and sample PDFs, and nothing else.

What `app/extractor.py` does is call a **pretrained vision-language model** (Claude,
via the `anthropic` SDK) with page images and a schema, and get structured JSON back.
The "model" was not built here. What was built here is the **harness around it**, and
that harness is the entire engineering contribution.

This distinction matters for your next question ("how can we also build one"), because
the answer is *not* "collect 50,000 labelled invoices". For this class of problem, that
would be the wrong project.

## The actual pipeline, end to end

```
PDF ──PyMuPDF rasterise @170dpi──┐
                                 ├─→ base64 image blocks ─→ Claude vision ─→ forced
JPG/PNG ──read bytes─────────────┘                          tool call     ↓
                                                                  {8 fields, each
                                                                   value/confidence/
                                                                   source_snippet}
                                                                          ↓
                                              pydantic validation (schema.py)
                                                                          ↓
                                    ┌─────────────────────────────────────┘
                                    ↓
                    Validator (validator.py) ── DETERMINISTIC, no model
                    exact / fuzzy / regex vs the customer rule set
                                    ↓
                    Router (router.py) ── DETERMINISTIC decision
                    AUTO_APPROVE / REVIEW / AMEND
```

Six pieces, in the order they run:

**1. Rasterise (`_render_pdf_to_pngs`).** PyMuPDF renders each PDF page to PNG at
170 dpi. PDF user space is 72 dpi, so the zoom matrix is `170/72`. 170 is a deliberate
compromise: high enough that 6-point tariff-code text survives, low enough that the
base64 payload stays affordable. Images skip this and are sent as-is.

**2. Constrain the output shape.** This is the most important trick in the repo.
Rather than asking for JSON and hoping, both providers are given a schema the model
*must* fill:

- Gemini path: `response_schema=ExtractionResult` with `response_mime_type=application/json`.
- Claude path (`llm.py`): a tool named `record_trade_fields` plus
  `tool_choice={"type": "tool", "name": ...}`, which **forces** the call.

Either way the response cannot be prose, cannot omit a field, and parses into a
pydantic model. Whole categories of parsing bug simply do not exist.

**3. Ask for provenance, not just values.** Every field returns three things:

```json
{"value": "6109.10", "confidence": 0.96, "source_snippet": "HS Code: 6109.10"}
```

`source_snippet` must be a verbatim quote containing the value. This is a cheap and
surprisingly effective hallucination check — a fabricated value tends to come with a
fabricated or absent snippet, and both are visible to a reviewer.

**4. Make "I don't know" the easy answer.** The prompt says it outright: *"It is
correct and expected for some fields to be null. A wrong guess is far worse than a
null."* Combined with `temperature=0.0`, this is what makes
`02_missing_fields.pdf` return nulls instead of plausible fiction.

**5. Decide in Python, not in the model.** `validator.py` and `router.py` contain
**zero model calls**. The model proposes; deterministic code disposes:

- confidence `< 0.6` or `value is None` → `uncertain`, and `uncertain` can never
  become `match`. Nothing is silently approved.
- `fuzzy` requires *both* `token_set_ratio ≥ 0.95` **and** every expected word
  present — because "Import" vs "Imports" scores ~0.98 on the ratio alone.
- The route (`AUTO_APPROVE` / `REVIEW` / `AMEND`) is an `if` chain over those statuses.

This is the property that makes the system auditable: given the same extraction, the
decision is reproducible forever, and you can explain it to a regulator in Python.

**6. Checkpoint every step.** `pipeline.py` writes to SQLite after each agent, so a
crash resumes from the last completed step and never re-runs the expensive vision call.

## So how do you build one?

The order below is deliberate — most of the value is in steps 3–5, which people
usually skip.

1. **Get pages into images.** PyMuPDF for PDFs; raw bytes for photos. Tune DPI by
   measuring, not guessing: render your worst real document and check the smallest
   text is legible to you at 100% zoom.
2. **Define the output schema first.** Write the pydantic model before the prompt. The
   schema is your contract; the prompt is just how you explain it.
3. **Force the shape.** Tool-calling with `tool_choice` (Claude) or `response_schema`
   (Gemini). Never parse free-text JSON out of prose.
4. **Demand provenance and calibrated confidence per field**, not one score per document.
   You need to know *which* field is shaky to route it.
5. **Write the deterministic layer.** Whatever decision the system makes — approve,
   reject, escalate — make it in code the model cannot influence. This is what turns a
   demo into something you can run against real money.
6. **Build an adversarial sample set.** `data/samples/03`–`10` are glare, blur, faded
   thermal paper, occluding stamps, and deliberately planted wrong values. Test against
   the documents that actually arrive, not clean ones.

## When you *would* train a model instead

The VLM approach wins on: zero training data, day-one deployment, tolerance of layouts
you have never seen, and easy schema changes (edit a prompt, not a dataset). It loses on
per-document cost, latency, and the need to send data to a third party.

Train or self-host when you have **one fixed template at very high volume** (a template
matcher plus Tesseract/PaddleOCR is far cheaper per page), when you are **air-gapped or
cannot send data off-site**, or when you need **sub-100 ms latency**. Middle ground:
open document-understanding models such as Donut or LayoutLMv3, fine-tuned on a few
thousand labelled pages, self-hosted.

For the government-ID case, there is a third option that beats both — don't read the
image at all, and get the data from the issuer. See
[`GOVERNMENT_ID_SYSTEM.md`](GOVERNMENT_ID_SYSTEM.md).
