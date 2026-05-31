"""Configuration + environment loading for the trade-document pipeline.

Loads `.env` from the project root and exposes typed settings. The pipeline runs
on Anthropic Claude by default (ANTHROPIC_API_KEY); set LLM_PROVIDER=gemini to use
Gemini instead (GEMINI_API_KEY or GOOGLE_API_KEY).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the `app` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from <root>/.env if present (no error if missing).
# override=True so the project's .env wins even if a stale/empty variable is
# already exported in the shell environment.
load_dotenv(PROJECT_ROOT / ".env", override=True)

# --- Provider selection -----------------------------------------------------
# Which LLM backend to use: "anthropic" (default, Claude) or "gemini".
LLM_PROVIDER: str = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()

# --- Gemini -----------------------------------------------------------------
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# --- Anthropic (Claude) -----------------------------------------------------
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

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


def require_anthropic_key() -> str:
    """Return the Anthropic API key or raise a clear, actionable error."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (key from "
            "https://console.anthropic.com/), or set LLM_PROVIDER=gemini to use Gemini."
        )
    return ANTHROPIC_API_KEY
