"""Shared helpers for the firstpass test suite."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE = PROJECT_ROOT / "fixtures" / "inbound_leads.csv"


def make_record(**overrides) -> dict:
    """A well-formed sanitized record; tests override single fields."""
    record = {
        "lead_id": "L-TEST",
        "source": "contact_form",
        "submitted_at": "2026-06-01T09:14:00Z",
        "submitted_at_valid": True,
        "name": "Jane Doe",
        "email": "jane@acme.com",
        "email_class": "corporate",
        "email_domain": "acme.com",
        "company": "Acme",
        "website": "https://acme.com",
        "website_host": "acme.com",
        "website_scheme": "https",
        "monthly_budget_usd": 20_000,
        "budget_raw": "20000",
        "budget_flag": "ok",
        "message": "We need help with paid acquisition and landing pages.",
        "urls": [],
        "flags": [],
        "sanitization": {"entities_decoded": 0, "tags_stripped": 0,
                         "invisible_removed": 0, "bidi_removed": 0,
                         "control_removed": 0},
        "message_language": "en",
        "intent": "commercial",
        "intent_score": 2,
        "growth_signal": False,
        "forensics": {"raw": {"message": "raw text"}},
    }
    record.update(overrides)
    return record
