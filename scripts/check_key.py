#!/usr/bin/env python3
"""Standalone API-key health check — minimal spend, provider-aware.

Loads config EXACTLY like the app (.env) and pings the ACTIVE provider
(LLM_PROVIDER = gemini | anthropic) with ONE tiny request. It never runs the
pipeline. The key is printed masked (last 4 chars only).

    python scripts/check_key.py

Exit codes:
    0  KEY OK
    2  Gemini daily quota exhausted
    3  rate-limited (key works)
    4  invalid / missing key (or billing issue)
    1  anything else
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `import app...` work when run as `python scripts/check_key.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402  -- imports run the SAME .env loading as the app


def _mask(key: str) -> str:
    """Show only the last 4 characters — never the full key."""
    return "****" + key[-4:] if key and len(key) >= 4 else "****"


def _check_anthropic() -> int:
    import anthropic
    key = config.ANTHROPIC_API_KEY
    if not key:
        print("KEY INVALID — ANTHROPIC_API_KEY is not set; check .env")
        return 4
    print(f"key source : ANTHROPIC_API_KEY = {_mask(key)}")
    print(f"model      : {config.ANTHROPIC_MODEL}")
    print("ping       : 1 request, max_tokens=1 ...")
    client = anthropic.Anthropic(api_key=key, max_retries=0)
    try:
        client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.AuthenticationError:
        print("KEY INVALID — authentication failed; check .env")
        return 4
    except anthropic.PermissionDeniedError as exc:
        print(f"KEY INVALID / no access — {getattr(exc, 'message', exc)}")
        return 4
    except anthropic.RateLimitError:
        print("RATE-LIMITED — key works, wait a few seconds")
        return 3
    except anthropic.BadRequestError as exc:
        msg = str(getattr(exc, "message", exc)).lower()
        if any(w in msg for w in ("credit", "billing", "balance")):
            print(f"KEY VALID but BILLING ISSUE — {getattr(exc, 'message', exc)}")
            return 4
        print(f"UNEXPECTED BadRequestError: {getattr(exc, 'message', exc)}")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"SERVER/OVERLOADED ({getattr(exc, 'status_code', '?')}) — retry later")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED {type(exc).__name__}: {exc}")
        return 1
    print("KEY OK — Claude reachable, model responded")
    return 0


def _check_gemini() -> int:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    def _haystack(exc: Exception) -> str:
        parts = (getattr(exc, "message", "") or "", getattr(exc, "status", "") or "",
                 getattr(exc, "details", "") or "", exc)
        return " ".join(str(p) for p in parts).lower().replace("-", " ")

    if os.getenv("GEMINI_API_KEY"):
        key_name = "GEMINI_API_KEY"
    elif os.getenv("GOOGLE_API_KEY"):
        key_name = "GOOGLE_API_KEY"
    else:
        key_name = None
    key = config.GEMINI_API_KEY
    if not key:
        print("KEY INVALID — neither GEMINI_API_KEY nor GOOGLE_API_KEY is set; check .env")
        return 4
    print(f"key source : {key_name} = {_mask(key)}")
    print(f"model      : {config.GEMINI_MODEL}")
    print("ping       : 1 request, max_output_tokens=1 ...")
    client = genai.Client(api_key=key)
    try:
        client.models.generate_content(
            model=config.GEMINI_MODEL, contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
    except genai_errors.ClientError as exc:
        code = getattr(exc, "code", None)
        hay = _haystack(exc)
        if code == 429:
            if "perday" in hay or "per day" in hay:
                print("DAILY QUOTA EXHAUSTED — do not run tests")
                return 2
            print("RATE-LIMITED (per-minute) — key works, wait a few seconds")
            return 3
        if code in (401, 403) or any(m in hay for m in (
                "api key not valid", "api_key_invalid", "invalid api key",
                "permission denied", "permission_denied", "unauthenticated")):
            print("KEY INVALID — check .env")
            return 4
        print(f"UNEXPECTED {type(exc).__name__} (code={code}): {getattr(exc, 'message', exc)}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED {type(exc).__name__}: {exc}")
        return 1
    print("KEY OK — model reachable, quota available")
    return 0


def main() -> int:
    print(f"provider   : {config.LLM_PROVIDER}")
    if config.LLM_PROVIDER == "anthropic":
        return _check_anthropic()
    return _check_gemini()


if __name__ == "__main__":
    raise SystemExit(main())
