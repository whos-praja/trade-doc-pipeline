"""Multi-agent trade-document pipeline (POC).

Agents:
  - extractor  (IMPLEMENTED) : document image(s) -> 8 structured fields via Gemini
  - router     (stub)        : classify document type, route to rule set
  - validator  (stub)        : check extracted fields against business rules
  - storage    (stub)        : persist results to SQLite
  - query      (stub)        : natural-language Q&A over stored extractions
  - pipeline   (stub)        : orchestrate the agents end to end
"""

__version__ = "0.1.0"
