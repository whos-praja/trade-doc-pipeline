#!/usr/bin/env python3
"""CLI: ask a natural-language question over the stored pipeline runs.

    python run_query.py "how many shipments were flagged this week?"
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from app.query import answer_question


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask a natural-language question over pipeline_runs (text-to-SQL + grounded answer).",
    )
    parser.add_argument("question", help="the question, in quotes")
    parser.add_argument("--json", action="store_true", help="emit the raw {answer, sql, rows} as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        result = answer_question(args.question)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"\nQ: {args.question}")
    print(f"\nSQL{'  (canned fallback)' if result.get('fallback') else ''}:")
    print(f"  {result['sql']}")
    rows = result["rows"]
    print(f"\nRows ({len(rows)}):")
    for row in rows[:20]:
        print(f"  {row}")
    if len(rows) > 20:
        print(f"  ... ({len(rows) - 20} more)")
    print(f"\nAnswer: {result['answer']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
