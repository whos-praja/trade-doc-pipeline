"""Pipeline orchestrator.

run_pipeline(doc, customer): create a run row, then chain
    Extractor -> Validator -> Router -> store,
persisting each step's output to SQLite as it completes. If any step raises, the
run is marked FAILED and the pipeline STOPS -- a document is never silently
approved. resume_pipeline(run_id) continues a failed/incomplete run from the last
completed step, reusing stored results (it NEVER re-runs the extraction call).

CLI:
    python -m app.pipeline data/samples/scan_1.pdf --customer meridian
    python -m app.pipeline --resume 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from typing import Optional

from app import storage
from app.extractor import extract
from app.router import Decision, route
from app.schema import EXTRACTION_FIELDS, ExtractionResult
from app.validator import load_ruleset, validate

logger = logging.getLogger("pipeline")

# Linear step order used to decide what to (re)run.
_ORDER = ("extract", "validate", "route", "store")
# Given the last completed `current_step`, which step does resume start from?
_RESUME_FROM = {
    "created": "extract",     # extraction never finished -> must run it
    "extracted": "validate",  # extraction done -> reuse it, never re-run
    "validated": "route",
    "routed": "store",
    "completed": None,
}


class PipelineError(RuntimeError):
    """Raised when a step fails; the run row is already marked FAILED."""

    def __init__(self, run_id: int, step: str, original: Exception):
        self.run_id, self.step, self.original = run_id, step, original
        super().__init__(
            f"run {run_id} failed at step '{step}': {type(original).__name__}: {original}"
        )


def _confidence_summary(result: ExtractionResult) -> dict:
    confs = {f: getattr(result, f).confidence for f in EXTRACTION_FIELDS}
    vals = list(confs.values())
    return {
        "per_field": confs,
        "min": min(vals),
        "mean": round(sum(vals) / len(vals), 3),
        "below_0.6": [f for f, c in confs.items() if c < 0.6],
    }


# --- individual steps: do the work, then checkpoint to storage --------------

def _do_extract(run_id: int, source_file: str) -> ExtractionResult:
    result = extract(source_file)
    storage.update_run_step(
        run_id, "extracted",
        extraction_json=result.model_dump_json(),
        confidence_summary=json.dumps(_confidence_summary(result)),
    )
    logger.info("run %d: extracted", run_id)
    return result


def _do_validate(run_id: int, result: ExtractionResult, ruleset: dict) -> dict:
    validation = validate(result, ruleset)
    storage.update_run_step(run_id, "validated", validation_json=json.dumps(validation))
    logger.info("run %d: validated -> %s", run_id, validation["summary"])
    return validation


def _do_route(run_id: int, validation: dict) -> Decision:
    decision = route(validation)  # the single LLM call (AMEND email) lives in here
    storage.update_run_step(
        run_id, "routed",
        decision_json=decision.model_dump_json(),
        outcome=decision.outcome, reasoning=decision.reasoning, draft_email=decision.draft_email,
    )
    logger.info("run %d: routed -> %s", run_id, decision.outcome)
    return decision


def _do_store(run_id: int, decision: Decision) -> None:
    storage.complete_run(run_id, outcome=decision.outcome, reasoning=decision.reasoning,
                         draft_email=decision.draft_email, decision_json=decision.model_dump_json())
    logger.info("run %d: stored -> COMPLETED", run_id)


def _guard(run_id: int, step: str, fn, *args):
    """Run one step; on ANY failure mark the run FAILED and STOP (raise)."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        storage.fail_run(run_id, f"step '{step}': {type(exc).__name__}: {exc}")
        logger.error("run %d FAILED at step '%s': %s", run_id, step, exc)
        raise PipelineError(run_id, step, exc) from exc


def _drive(run_id: int, ruleset: dict, source_file: str, *, from_step: str,
           result: Optional[ExtractionResult] = None, validation: Optional[dict] = None,
           decision: Optional[Decision] = None) -> int:
    """Execute steps from `from_step` onward, each guarded + checkpointed."""
    start = _ORDER.index(from_step)
    if start <= 0:
        result = _guard(run_id, "extract", _do_extract, run_id, source_file)
    if start <= 1:
        validation = _guard(run_id, "validate", _do_validate, run_id, result, ruleset)
    if start <= 2:
        decision = _guard(run_id, "route", _do_route, run_id, validation)
    elif decision is None:  # resuming at 'store' -> rehydrate the decision
        decision = Decision.model_validate_json(storage.get_run(run_id)["decision_json"])
    if start <= 3:
        _guard(run_id, "store", _do_store, run_id, decision)
    return run_id


def run_pipeline(doc_path: str, customer: str) -> int:
    """Create a run and process the document end to end. Returns the run_id."""
    ruleset = load_ruleset(customer)  # fail fast on an unknown customer (no row, no API call)
    run_id = storage.create_run(customer, str(doc_path))
    logger.info("run %d created: %s (customer=%s)", run_id, doc_path, customer)
    return _drive(run_id, ruleset, str(doc_path), from_step="extract")


def resume_pipeline(run_id: int) -> int:
    """Resume a failed/incomplete run from the last completed step.

    Stored results are reused -- in particular, extraction is NEVER re-run.
    """
    row = storage.get_run(run_id)
    if row is None:
        raise ValueError(f"no pipeline run with id {run_id}")
    if row["status"] == "COMPLETED":
        logger.info("run %d already COMPLETED; nothing to resume", run_id)
        return run_id

    from_step = _RESUME_FROM.get(row["current_step"] or "created", "extract")
    if from_step is None:
        return run_id
    ruleset = load_ruleset(row["customer"])

    # Rehydrate whatever earlier steps already produced (deserialize only, no rerun).
    result = (ExtractionResult.model_validate_json(row["extraction_json"])
              if row["extraction_json"] else None)
    validation = json.loads(row["validation_json"]) if row["validation_json"] else None
    decision = (Decision.model_validate_json(row["decision_json"])
                if row["decision_json"] else None)

    logger.info("run %d: resuming from step '%s' (last completed: %s)",
                run_id, from_step, row["current_step"])
    # Back to RUNNING and clear the prior error; keep current_step where it was.
    storage.update_run_step(run_id, row["current_step"] or "created", status="RUNNING", error=None)
    return _drive(run_id, ruleset, row["source_file"], from_step=from_step,
                  result=result, validation=validation, decision=decision)


# --- CLI --------------------------------------------------------------------

def _print_summary(run: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(run, indent=2, ensure_ascii=False))
        return
    print(f"\nrun_id       : {run['run_id']}")
    print(f"status       : {run['status']}")
    print(f"current_step : {run['current_step']}")
    print(f"customer     : {run['customer']}")
    print(f"source_file  : {run['source_file']}")
    if run.get("outcome"):
        print(f"outcome      : {run['outcome']}")
    if run.get("reasoning"):
        print(f"reasoning    : {run['reasoning']}")
    if run.get("error"):
        print(f"error        : {run['error']}")
    if run.get("draft_email"):
        print("draft_email  :")
        print(textwrap.indent(run["draft_email"], "  "))
    print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline",
        description="Run the trade-document pipeline (Extractor -> Validator -> Router -> store).",
    )
    parser.add_argument("document", nargs="?", help="PDF/image to process")
    parser.add_argument("--customer", help="customer rule set id (app/rules/<id>.json)")
    parser.add_argument("--resume", type=int, metavar="RUN_ID", help="resume a failed/incomplete run")
    parser.add_argument("--json", action="store_true", help="print the run row as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="log step progress to stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.resume is not None:
            run_id = resume_pipeline(args.resume)
        else:
            if not args.document or not args.customer:
                parser.error("provide <document> and --customer, or --resume RUN_ID")
            run_id = run_pipeline(args.document, args.customer)
    except PipelineError as exc:
        print(f"PIPELINE FAILED: {exc}", file=sys.stderr)
        run = storage.get_run(exc.run_id)
        if run:
            _print_summary(run, args.json)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run = storage.get_run(run_id)
    _print_summary(run, args.json)
    return 0 if run and run["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
