"""Consent-gated government identity document reading.

Public entry points:
    consent.grant / authorise / withdraw   -- the authorisation layer
    pipeline.process                       -- read one document under one consent
    storage.purge_expired                  -- the retention job
"""
