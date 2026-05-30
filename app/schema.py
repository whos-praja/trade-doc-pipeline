"""Pydantic schemas for the Extractor agent.

Each of the 8 target fields is returned as an `ExtractedField`:
    { "value": str | null, "confidence": float[0..1], "source_snippet": str | null }

The same `ExtractionResult` model is passed to Gemini as the `response_schema`
(forcing valid JSON) and used to validate the response on our side.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

# The exact 8 fields to extract, in canonical order.
EXTRACTION_FIELDS: list[str] = [
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "description_of_goods",
    "gross_weight",
    "invoice_number",
]


class ExtractedField(BaseModel):
    """A single extracted value with provenance and a calibrated confidence.

    Fields are declared WITHOUT defaults so the generated Gemini schema marks
    all three keys as required -- the model must always emit them (using a null
    `value` when the field is absent).
    """

    value: Optional[str] = Field(
        description=(
            "The value EXACTLY as it appears in the document, or null if the "
            "field is absent/unreadable. Never invent or normalize a value."
        ),
    )
    confidence: float = Field(
        description=(
            "Calibrated probability 0.0-1.0 that the value is present in the "
            "document and read correctly. Use a LOW value when null/uncertain."
        ),
    )
    source_snippet: Optional[str] = Field(
        description=(
            "A short verbatim quote copied from the document that contains the "
            "value. MUST be null whenever `value` is null."
        ),
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: object) -> float:
        """Coerce to float and clamp into [0, 1] so a stray value never crashes."""
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))


class ExtractionResult(BaseModel):
    """Structured extraction of the 8 target trade-document fields."""

    consignee_name: ExtractedField
    hs_code: ExtractedField
    port_of_loading: ExtractedField
    port_of_discharge: ExtractedField
    incoterms: ExtractedField
    description_of_goods: ExtractedField
    gross_weight: ExtractedField
    invoice_number: ExtractedField
