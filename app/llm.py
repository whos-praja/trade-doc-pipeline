"""Anthropic Claude provider helpers (the default backend; LLM_PROVIDER=anthropic).

Every call hits the API live — there is no prompt or response caching. These return
exactly what the rest of the pipeline expects:
  - anthropic_extract(): page image(s) -> structured ExtractionResult via a FORCED
    tool call (Claude must call `record_trade_fields`), so we always get
    schema-shaped JSON back — the Claude analogue of Gemini's response_schema.
  - anthropic_complete(): text in -> text out (amendment email, NL->SQL, answer).

The Anthropic SDK auto-retries 429/5xx with backoff (max_retries), so there is no
manual retry loop here. `anthropic` is imported lazily so the default Gemini path
never requires it to be installed.
"""
from __future__ import annotations

import base64
import logging

from app import config
from app.schema import EXTRACTION_FIELDS, ExtractionResult

logger = logging.getLogger("llm.anthropic")

_EXTRACTION_TOOL = "record_trade_fields"


def _extraction_tool() -> dict:
    """Tool whose input schema is the 8 fields, each {value, confidence, source_snippet}."""
    field = {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
            "source_snippet": {"type": ["string", "null"]},
        },
        "required": ["value", "confidence", "source_snippet"],
        "additionalProperties": False,
    }
    return {
        "name": _EXTRACTION_TOOL,
        "description": "Record the 8 extracted trade-document fields.",
        "input_schema": {
            "type": "object",
            "properties": {name: dict(field) for name in EXTRACTION_FIELDS},
            "required": list(EXTRACTION_FIELDS),
            "additionalProperties": False,
        },
    }


def _client():
    import anthropic  # lazy: only needed when LLM_PROVIDER=anthropic
    return anthropic.Anthropic(api_key=config.require_anthropic_key(), max_retries=4)


def anthropic_extract(images: list[tuple[bytes, str]], instructions: str) -> ExtractionResult:
    """Vision extraction via a forced tool call; returns a validated ExtractionResult."""
    client = _client()
    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        }
        for data, media_type in images
    ]
    content.append({
        "type": "text",
        "text": "Extract the 8 fields from the document image(s) above by calling the "
                "record_trade_fields tool. Follow the rules in the system prompt exactly.",
    })

    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=2048,
        system=instructions,  # sent live every call — no prompt caching
        tools=[_extraction_tool()],
        tool_choice={"type": "tool", "name": _EXTRACTION_TOOL},
        messages=[{"role": "user", "content": content}],
    )
    logger.info("claude extract (%s): %d page(s)", config.ANTHROPIC_MODEL, len(images))
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _EXTRACTION_TOOL:
            return ExtractionResult.model_validate(block.input)
    raise RuntimeError("Claude did not return the expected record_trade_fields tool call")


def anthropic_complete(prompt: str, *, max_tokens: int = 1024) -> str:
    """Plain text completion (amendment email / NL->SQL / grounded answer)."""
    client = _client()
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
