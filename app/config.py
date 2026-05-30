"""Configuration + environment loading for the trade-document pipeline.

Loads `.env` from the project root and exposes typed settings. The Gemini API
key may be provided as either GEMINI_API_KEY or GOOGLE_API_KEY.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the `app` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from <root>/.env if present (no error if missing).
load_dotenv(PROJECT_ROOT / ".env")

# --- Gemini -----------------------------------------------------------------
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# --- PDF rendering ----------------------------------------------------------
# Pages are rasterised to PNG at this DPI before being sent to the model.
PDF_RENDER_DPI: int = int(os.getenv("PDF_RENDER_DPI", "170"))

# --- Retry ------------------------------------------------------------------
# Exponential backoff (seconds) applied on 429 / transient errors. The number
# of entries is the number of *retries* after the initial attempt.
RETRY_BACKOFFS: tuple[int, ...] = (1, 2, 4, 8)

# --- Paths ------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"


def require_api_key() -> str:
    """Return the configured API key or raise a clear, actionable error."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key "
            "(get a free one at https://aistudio.google.com/apikey), or "
            "`export GEMINI_API_KEY=...` in your shell."
        )
    return GEMINI_API_KEY
