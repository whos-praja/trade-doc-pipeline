#!/usr/bin/env python3
"""CLI for the Extractor agent.

Usage:
    python run_extractor.py data/samples/clean_commercial_invoice.pdf
    python run_extractor.py path/to/scan.png --verbose

Prints pretty JSON of all 8 fields, each with its value, confidence, and
source snippet.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from app.extractor import extract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract 8 trade-document fields from a PDF or image using Gemini.",
    )
    parser.add_argument("document", help="Path to a PDF or image file")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Log rendering/retry progress to stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        result = extract(args.document)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface any failure cleanly to the CLI
        print(f"error: extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Pretty JSON of all 8 fields (declaration order is preserved).
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
