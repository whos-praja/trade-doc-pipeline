#!/usr/bin/env python3
"""CLI: run the Extractor then the Validator and print a field-by-field report.

Usage:
    python run_validator.py data/samples/scan_1.pdf --customer meridian
    python run_validator.py data/samples/clean_1.pdf --customer meridian --json

Exit codes:
    0  validated, every field matched (all_clear)
    1  validated, but at least one field is mismatch/uncertain
    2  could not run (missing document, unknown customer, extraction failure)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from app.extractor import extract
from app.validator import (
    CONFIDENCE_THRESHOLD,
    FUZZY_MATCH_THRESHOLD,
    load_ruleset,
    validate,
)

_COLORS = {"match": "\033[32m", "mismatch": "\033[31m", "uncertain": "\033[33m"}
_RESET = "\033[0m"


def _fmt_status(status: str, use_color: bool, width: int = 9) -> str:
    text = status.upper().ljust(width)
    if use_color and status in _COLORS:
        return f"{_COLORS[status]}{text}{_RESET}"
    return text


def _detail(r: dict) -> str:
    status, found, expected, reason = r["status"], r["found"], r["expected"], r["reason"]
    if status == "match":
        return f'"{found}"'
    if status == "mismatch":
        return f'found "{found}"  ≠  expected "{expected}"   ({reason})'
    # uncertain
    if found is not None:
        return f'{reason}  —  found "{found}"'
    return reason


def render_report(report: dict, customer: str, document: str, use_color: bool) -> str:
    fields, summary = report["fields"], report["summary"]
    name_w = max(len(n) for n in fields)

    out = [
        "",
        f"Document  : {document}",
        f"Customer  : {summary.get('customer_name')} ({summary.get('customer_id') or customer})",
        f"Thresholds: confidence ≥ {CONFIDENCE_THRESHOLD}, fuzzy ≥ {FUZZY_MATCH_THRESHOLD}",
        "",
        f"{'FIELD'.ljust(name_w)}  {'STATUS'.ljust(9)}  CONF  DETAIL",
        "-" * (name_w + 2 + 9 + 2 + 4 + 2 + 6),
    ]
    for name, r in fields.items():
        out.append(f"{name.ljust(name_w)}  {_fmt_status(r['status'], use_color)}  {r['confidence']:.2f}  {_detail(r)}")

    clear = summary["all_clear"]
    verdict = "ALL CLEAR ✓" if clear else "NOT CLEAR ✗"
    if use_color:
        verdict = f"{(_COLORS['match'] if clear else _COLORS['mismatch'])}{verdict}{_RESET}"
    out += [
        "",
        f"Summary   : {summary['match']} match, {summary['mismatch']} mismatch, "
        f"{summary['uncertain']} uncertain  (of {summary['total']})",
        f"Verdict   : {verdict}   (all_clear={clear})",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Extractor then the deterministic Validator on a trade document.",
    )
    parser.add_argument("document", help="Path to a PDF or image file")
    parser.add_argument("--customer", required=True, help="Customer rule set id (app/rules/<id>.json)")
    parser.add_argument("--json", action="store_true", help="Emit the raw JSON report instead of the table")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log extractor progress to stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        ruleset = load_ruleset(args.customer)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = extract(args.document)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface any extraction failure cleanly
        print(f"error: extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report = validate(result, ruleset)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        use_color = (not args.no_color) and sys.stdout.isatty()
        print(render_report(report, args.customer, args.document, use_color))

    return 0 if report["summary"]["all_clear"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
