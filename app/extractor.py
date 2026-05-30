"""Extractor agent.

Input : path to a PDF or image of a trade document.
Output: an `ExtractionResult` with the 8 target fields, each carrying a value,
        a confidence, and a verbatim source snippet.

Pipeline:
  PDF  -> render each page to PNG (PyMuPDF, ~170 dpi) -> inline image Part(s)
  image-> load bytes directly                         -> inline image Part
  -> gemini-2.5-flash with response_mime_type=application/json + response_schema
  -> validated ExtractionResult

The Gemini call is wrapped in exponential-backoff retry (1s, 2s, 4s, 8s) on
429 / transient errors.

SDK note: uses the current `google-genai` package (verified against installed
v1.75.0): `from google import genai`, `genai.Client(api_key=...)`,
`client.models.generate_content(model, contents=[Part.from_bytes(...), prompt],
config=types.GenerateContentConfig(response_mime_type=..., response_schema=...))`.
"""
from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Union

import fitz  # PyMuPDF
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app import config
from app.schema import ExtractionResult

logger = logging.getLogger("extractor")

# --- prompt -----------------------------------------------------------------

EXTRACTION_PROMPT = """\
You are a meticulous trade-document data extraction agent. You are given one or
more page images of a SINGLE trade document (e.g. a commercial invoice, bill of
lading, or packing list).

Extract EXACTLY these 8 fields:
  1. consignee_name        - the party the goods are consigned to (buyer/receiver).
  2. hs_code               - the Harmonized System tariff code (digits, may include dots/spaces).
  3. port_of_loading       - the port/place where the goods are loaded for export.
  4. port_of_discharge     - the port/place where the goods are discharged/unloaded.
  5. incoterms             - the Incoterms trade term (e.g. FOB, CIF, EXW, DAP), with the named place if shown.
  6. description_of_goods  - the description of the goods/merchandise.
  7. gross_weight          - the gross weight, including its unit (e.g. "12,500 KGS").
  8. invoice_number        - the commercial invoice number/reference.

CRITICAL ANTI-HALLUCINATION RULES (follow exactly):
  - Only return a value that LITERALLY appears in the document. Read it verbatim.
  - If a field is absent, blank, or unreadable, set "value" to null and give a
    LOW "confidence" (<= 0.3). Do NOT guess, infer, normalize, translate, or fabricate.
  - Never invent a plausible value. It is correct and expected for some fields
    to be null. A wrong guess is far worse than a null.
  - "source_snippet" MUST be a short verbatim quote copied exactly from the
    document that contains the value. If "value" is null, "source_snippet" MUST
    also be null.
  - "confidence" is your calibrated probability (0.0-1.0) that the value is
    present in the document and read correctly. Use a high value only when the
    text is clear and unambiguous.

Return ONLY the JSON object matching the provided schema. Every one of the 8
fields must be present as an object with "value", "confidence", and "source_snippet".
"""

# Extensions we accept directly as images (anything else with a PDF suffix is rendered).
_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

# HTTP status codes worth retrying.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# --- image preparation ------------------------------------------------------

def _render_pdf_to_pngs(path: Path, dpi: int) -> list[bytes]:
    """Rasterise every page of a PDF to PNG bytes at the given DPI."""
    zoom = dpi / 72.0  # PDF user space is 72 dpi
    matrix = fitz.Matrix(zoom, zoom)
    pngs: list[bytes] = []
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pngs.append(pix.tobytes("png"))
    return pngs


def _load_image_bytes(path: Path) -> tuple[bytes, str]:
    """Read an image file and return (bytes, mime_type)."""
    mime = _IMAGE_MIME_BY_EXT.get(path.suffix.lower())
    if mime is None:
        guessed, _ = mimetypes.guess_type(str(path))
        mime = guessed or "image/png"
    return path.read_bytes(), mime


def _prepare_image_parts(path: Path) -> list[types.Part]:
    """Turn a PDF or image file into a list of inline image Parts for Gemini."""
    if path.suffix.lower() == ".pdf":
        pngs = _render_pdf_to_pngs(path, config.PDF_RENDER_DPI)
        if not pngs:
            raise ValueError(f"No pages could be rendered from PDF: {path}")
        logger.info("rendered %d page(s) from %s at %d dpi", len(pngs), path.name, config.PDF_RENDER_DPI)
        return [types.Part.from_bytes(data=png, mime_type="image/png") for png in pngs]

    data, mime = _load_image_bytes(path)
    logger.info("loaded image %s (%s, %d bytes)", path.name, mime, len(data))
    return [types.Part.from_bytes(data=data, mime_type=mime)]


# --- gemini call with retry -------------------------------------------------

def _status_code(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from a genai APIError."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    # Some errors carry the code nested in a response/details payload.
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_retryable(exc: Exception) -> bool:
    """True for 429 / 5xx API errors and transient network failures."""
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.APIError):  # ClientError is a subclass
        return _status_code(exc) in _RETRYABLE_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    return False


def _generate_with_retry(client: genai.Client, parts: list[types.Part]) -> types.GenerateContentResponse:
    """Call Gemini with exponential backoff on 429 / transient errors."""
    contents = [*parts, EXTRACTION_PROMPT]
    gen_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ExtractionResult,
        temperature=0.0,  # deterministic extraction; minimise drift/hallucination
    )

    backoffs = config.RETRY_BACKOFFS
    last_exc: Exception | None = None
    for attempt in range(len(backoffs) + 1):  # initial try + len(backoffs) retries
        try:
            return client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=gen_config,
            )
        except Exception as exc:  # noqa: BLE001 - we re-raise unless retryable
            last_exc = exc
            if attempt < len(backoffs) and _is_retryable(exc):
                delay = backoffs[attempt]
                logger.warning(
                    "transient error from Gemini (%s); retry %d/%d in %ds",
                    type(exc).__name__, attempt + 1, len(backoffs), delay,
                )
                time.sleep(delay)
                continue
            raise
    assert last_exc is not None  # unreachable, but keeps type-checkers happy
    raise last_exc


# --- public API -------------------------------------------------------------

def extract(path: Union[str, Path]) -> ExtractionResult:
    """Extract the 8 trade-document fields from a PDF or image file.

    Raises:
        FileNotFoundError: the document does not exist.
        RuntimeError: no API key configured, or the model returned no content.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    api_key = config.require_api_key()
    client = genai.Client(api_key=api_key)

    parts = _prepare_image_parts(path)
    response = _generate_with_retry(client, parts)

    # Prefer the SDK-parsed object (it validates against our pydantic model);
    # fall back to manual JSON parsing of the raw text if needed.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ExtractionResult):
        return parsed

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError(
            "Gemini returned no content (the response may have been blocked or empty)."
        )
    return ExtractionResult.model_validate_json(text)
