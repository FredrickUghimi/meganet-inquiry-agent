"""Outbound prospecting copilot (assessment: docs/outbound-prospecting-assessment.md).

PHASE 1 = FOUNDATIONS ONLY: tables, settings, emergency stop, audit log, the gate, owner-only approval, the review
checkpoint, the spend cap and consent rules. It sends nothing, collects no prospect data and scrapes nothing."""
from . import models  # noqa: F401  (registers the tables with SQLAlchemy before create_all)
