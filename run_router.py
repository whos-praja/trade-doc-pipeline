#!/usr/bin/env python3
"""CLI: run Extractor -> Validator -> Router and print the decision.

Usage:
    python run_router.py data/samples/scan_1.pdf  --customer meridian
    python run_router.py data/samples/clean_1.pdf --customer meridian --json

Exit codes:
    0  AUTO_APPROVE
    1  AMEND or REVIEW (human action needed)
    2  could not run (missing document, unknown customer, extraction/routing failure)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap

from app.extractor import extract
from app.router import route
from app.validator import load_ruleset, validate


def render(decision, document: str, customer: str) -> str:
    out = [
        "",
        f"Document : {document}",
        f"Customer : {customer}",
        "",
        f"OUTCOME  : {decision.outcome}",
        "",
        "Reasoning:",
        textwrap.fill(decision.reasoning, width=96, initial_indent="  ", subsequent_indent="  "),
        "",
    ]
    if decision.discrepancies:
        out.append("Discrepancies:")
        for d in decision.discrepancies:
            out.append(f'  - {d.field} — found "{d.found}", expected "{d.expected}"')
        out.append("")
    if decision.uncertain_fields:
        out.append("Uncertain fields:")
        for u in decision.uncertain_fields:
            out.append(f"  - {u.field}: {u.reason}")
        out.append("")
    out.append("Draft email:")
    if decision.draft_email:
        out.append(textwrap.indent(decision.draft_email, "  "))
    else:
        out.append("  (none — human review required; never auto-approved)")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Extractor -> Validator -> Router and print the routing decision.",
    )
    parser.add_argument("document", help="Path to a PDF or image file")
    parser.add_argument("--customer", required=True, help="Customer rule set id (app/rules/<id>.json)")
    parser.add_argument("--json", action="store_true", help="Emit the Decision as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log progress to stderr")
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
    except Exception as exc:  # noqa: BLE001
        print(f"error: extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    validation = validate(result, ruleset)

    try:
        decision = route(validation)
    except Exception as exc:  # noqa: BLE001 - e.g. the amendment-email draft call failed
        print(f"error: routing failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(decision.model_dump(), indent=2, ensure_ascii=False))
    else:
        print(render(decision, args.document, args.customer))

    return 0 if decision.outcome == "AUTO_APPROVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
