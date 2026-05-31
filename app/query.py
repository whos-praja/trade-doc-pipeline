"""Query agent: natural-language questions over pipeline_runs via gemini-2.5-flash.

answer_question(q) -> {answer, sql, rows, fallback}:
  1. ask the model for ONE read-only SQLite SELECT over pipeline_runs (schema supplied),
  2. sanitize it HARD -- a single SELECT only; no INSERT/UPDATE/DELETE/DROP; no multi-statement;
     add a LIMIT if missing -- then run it through storage.query_runs (which also enforces
     SELECT-only, so a single validated SELECT is effectively read-only),
  3. feed the result rows back to the model for a short answer grounded in (and citing) the numbers.

On any failure (model error, or bad / failing SQL) it falls back to canned intents
(count flagged / count approved / list pending for a customer) so a question is never left
unanswered; if the summariser itself is unavailable, a deterministic summary is returned.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from pydantic import BaseModel

from app import config, storage
from app.extractor import _is_retryable  # reuse the daily-quota-aware retry policy

logger = logging.getLogger("query")

DEFAULT_LIMIT = 100

# Write / DDL / transaction keywords that must never appear in a generated query.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|"
    r"reindex|truncate|grant|revoke|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class QueryError(ValueError):
    """A generated statement violated the read-only single-SELECT guardrails."""


class _SqlPlan(BaseModel):
    sql: str


_SCHEMA_DOC = """\
Table: pipeline_runs  (one row per document processed by the pipeline)
Columns:
  run_id INTEGER, created_at TEXT (ISO-8601 UTC, e.g. '2026-05-30T20:46:55+00:00'),
  customer TEXT (rule-set id, e.g. 'meridian'), source_file TEXT,
  status TEXT in (RUNNING, COMPLETED, FAILED),
  current_step TEXT in (created, extracted, validated, routed, completed),
  outcome TEXT in (AUTO_APPROVE, REVIEW, AMEND),
  reasoning TEXT, draft_email TEXT, confidence_summary TEXT(JSON),
  error TEXT, extraction_json TEXT(JSON), validation_json TEXT(JSON), decision_json TEXT(JSON)

Meaning / phrasing:
  - "approved"                         -> outcome = 'AUTO_APPROVE'
  - "flagged" / "needs amendment"      -> outcome = 'AMEND'
  - "pending review" / "needs review"  -> outcome = 'REVIEW'
  - "this week" / "last 7 days"        -> datetime(created_at) >= datetime('now', '-7 days')
  - A company name maps to the customer id: 'Meridian Imports GmbH' -> customer = 'meridian'.
  - decision_json holds a JSON array "discrepancies" of {field, found, expected}; to count
    discrepancy fields use json_each(decision_json, '$.discrepancies') with
    json_extract(value, '$.field').
"""


def _sql_prompt(question: str) -> str:
    return (
        "You translate a question into ONE read-only SQLite SELECT over the table below.\n\n"
        f"{_SCHEMA_DOC}\n"
        "Rules:\n"
        "  - Return EXACTLY ONE statement: a SELECT. No semicolons, no CTE/WITH, no "
        "INSERT/UPDATE/DELETE/DROP.\n"
        "  - Use aggregates (COUNT(*) etc.) for 'how many'; select run_id, customer, source_file,\n"
        "    outcome, created_at for 'list/show' questions. Use only the listed columns. SQLite syntax.\n\n"
        f"Question: {question}\n"
        'Return JSON: {"sql": "<the SELECT>"}'
    )


def _answer_prompt(question: str, sql: str, rows: list) -> str:
    return (
        "Answer the question using ONLY the SQL result rows below. Cite the specific "
        "numbers/values.\n1-3 sentences. If there are no rows, say no matching runs were found. "
        "Never invent data.\n\n"
        f"Question: {question}\n"
        f"SQL: {sql}\n"
        f"Rows (JSON): {json.dumps(rows, default=str)}\n\n"
        "Answer:"
    )


# --- LLM helper (reuses the daily-quota-aware retry from extractor) ----------

def _llm(prompt: str, schema: Optional[type] = None, temperature: float = 0.0):
    if config.LLM_PROVIDER == "anthropic":
        from app import llm
        text = llm.anthropic_complete(prompt, max_tokens=1024)
        return schema.model_validate_json(_json_from_text(text)) if schema is not None else text

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.require_api_key())
    kwargs = {"temperature": temperature}
    if schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_schema"] = schema
    gen_config = types.GenerateContentConfig(**kwargs)

    backoffs = config.RETRY_BACKOFFS
    last_exc: Optional[Exception] = None
    for attempt in range(len(backoffs) + 1):
        try:
            resp = client.models.generate_content(
                model=config.GEMINI_MODEL, contents=prompt, config=gen_config)
            if schema is not None:
                parsed = getattr(resp, "parsed", None)
                return parsed if parsed is not None else schema.model_validate_json(resp.text)
            return (getattr(resp, "text", None) or "").strip()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < len(backoffs) and _is_retryable(exc):  # daily-quota 429 is NOT retryable
                logger.warning("query LLM transient error (%s); retry %d/%d in %ds",
                               type(exc).__name__, attempt + 1, len(backoffs), backoffs[attempt])
                time.sleep(backoffs[attempt])
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _json_from_text(text: str) -> str:
    """Extract a JSON object from possibly fenced / prose model output."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start != -1 and end > start else t


# --- guardrails -------------------------------------------------------------

def _sanitize_sql(raw: str) -> str:
    """Enforce a single read-only SELECT and add a LIMIT if one is missing."""
    sql = (raw or "").strip()
    if sql.startswith("```"):  # strip accidental markdown fences
        sql = re.sub(r"^```[a-zA-Z]*\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql).strip()
    sql = sql.rstrip().rstrip(";").rstrip()          # drop one trailing ';'
    if ";" in sql:
        raise QueryError("multiple statements are not allowed")
    if not re.match(r"(?is)^\s*select\b", sql):
        raise QueryError("only a single SELECT is allowed")
    if _FORBIDDEN.search(sql):
        raise QueryError("write / DDL keywords are not allowed")
    if not re.search(r"(?is)\blimit\b", sql):
        sql = f"{sql} LIMIT {DEFAULT_LIMIT}"
    return sql


# --- canned fallbacks -------------------------------------------------------

def _guess_customer(ql: str) -> Optional[str]:
    try:
        known = [r["customer"] for r in storage.query_runs("SELECT DISTINCT customer FROM pipeline_runs")]
    except Exception:  # pragma: no cover
        known = []
    for c in known:
        if c and c.lower() in ql:
            return c
    return "meridian" if "meridian" in ql else None


def _canned_intent(question: str) -> tuple[str, list]:
    """Best-effort intent match when the NL->SQL path fails. Returns (sql, rows)."""
    ql = question.lower()
    if "approve" in ql:
        sql = "SELECT COUNT(*) AS approved FROM pipeline_runs WHERE outcome = 'AUTO_APPROVE'"
    elif "pending" in ql or "review" in ql:
        cust = _guess_customer(ql)
        cond = f" AND customer = '{cust}'" if cust else ""
        sql = ("SELECT run_id, customer, source_file, created_at FROM pipeline_runs "
               f"WHERE outcome = 'REVIEW'{cond} LIMIT {DEFAULT_LIMIT}")
    else:  # default: count flagged
        sql = "SELECT COUNT(*) AS flagged FROM pipeline_runs WHERE outcome = 'AMEND'"
    return sql, storage.query_runs(sql)


def _deterministic_answer(rows: list) -> str:
    if not rows:
        return "No matching pipeline runs were found."
    if len(rows) == 1 and len(rows[0]) == 1:
        (key, val), = rows[0].items()
        return f"{val} ({key})."
    return f"{len(rows)} matching run(s): " + "; ".join(str(r.get("run_id", r)) for r in rows[:10])


# --- public API -------------------------------------------------------------

def answer_question(question: str) -> dict:
    """Answer a natural-language question over pipeline_runs."""
    storage.init_db()
    fallback = False
    try:
        plan = _llm(_sql_prompt(question), schema=_SqlPlan)
        sql = _sanitize_sql(plan.sql)
        rows = storage.query_runs(sql)
    except Exception as exc:  # noqa: BLE001 - model error OR bad/failing SQL -> canned intent
        logger.warning("NL->SQL path failed (%s); falling back to a canned intent", exc)
        fallback = True
        sql, rows = _canned_intent(question)

    try:
        answer = _llm(_answer_prompt(question, sql, rows), temperature=0.2)
    except Exception as exc:  # noqa: BLE001 - summariser unavailable -> deterministic answer
        logger.warning("grounded-answer LLM failed (%s); using deterministic summary", exc)
        answer = _deterministic_answer(rows)

    return {"answer": answer, "sql": sql, "rows": rows, "fallback": fallback}
