"""Router agent (STUB).

Planned role: classify the incoming document (commercial invoice, bill of
lading, packing list, certificate of origin, ...) and route it to the correct
rule set / downstream handler before extraction and validation run.
"""
from __future__ import annotations

from pathlib import Path


def route(path: str | Path) -> str:
    """Return the detected document type. Not implemented yet."""
    raise NotImplementedError("Router agent is a POC stub.")
