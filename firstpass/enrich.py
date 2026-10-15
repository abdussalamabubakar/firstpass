"""People Data Labs enrichment adapter — key-gated, dormant without a key.

The brief's manual workflow includes "reviews their LinkedIn". We do that
through People Data Labs' Company Enrich API (which carries the LinkedIn
profile URL and firmographics). The adapter follows the same pattern as
llm.py: fully implemented, activated by an environment variable, and the
system is honest about which path ran.

    PDL_API_KEY      required to enable
    PDL_BASE_URL     default: https://api.peopledatalabs.com (overridable
                     for tests)

Data labeling: PDL-sourced numbers are labeled **Benchmarked** (from an
external data provider), never presented as our own observations.

The monthly-review policy (mandated design decision): PDL data refreshes on
a monthly basis, so a human must review each company's LinkedIn profile at
least once a month. :func:`needs_linkedin_review` enforces the boundary and
the review store (:func:`record_linkedin_review` / :func:`load_reviews`)
persists who-reviewed-what-when; the run page shows a due-badge and a
"Mark LinkedIn reviewed" action.
"""

from __future__ import annotations

import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_PDL_BASE_URL = "https://api.peopledatalabs.com"
REVIEW_INTERVAL_DAYS = 30


class EnrichmentError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("PDL_API_KEY"))


def enrich_company(website: str | None = None, name: str | None = None,
                   timeout: float = 15.0, base_url: str | None = None) -> dict:
    """Call PDL Company Enrich and normalize the useful fields.

    Returns a dict with PDL firmographics plus `fetched_at` and an explicit
    source label. Raises EnrichmentError on any failure — callers decide
    whether to proceed unenriched.
    """
    if not available():
        raise EnrichmentError("PDL_API_KEY not set")
    if not website and not name:
        raise EnrichmentError("website or name required")

    base = (base_url or os.environ.get("PDL_BASE_URL", DEFAULT_PDL_BASE_URL)).rstrip("/")
    params = {}
    if website:
        params["website"] = website
    if name:
        params["name"] = name
    url = base + "/v5/company/enrich?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-API-Key": os.environ["PDL_API_KEY"],
        "User-Agent": "firstpass-lead-triage/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise EnrichmentError(f"PDL request failed: {exc}") from exc

    if not isinstance(data, dict) or data.get("status") != 200:
        raise EnrichmentError("PDL returned non-200: " + str(data)[:200])

    normalized = {
        "pdl_id": data.get("id"),
        "name": data.get("name"),
        "website": data.get("website"),
        "linkedin_url": data.get("linkedin_url"),
        "employee_count": data.get("employee_count"),
        "industry": data.get("industry"),
        "revenue_estimated_usd": data.get("revenue_estimated"),
        "locality": data.get("locality"),
        "region": data.get("region"),
        "country": data.get("country"),
        "source": "People Data Labs (Benchmarked)",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return {k: v for k, v in normalized.items() if v is not None}


# ---------------------------------------------------------------------------
# Monthly human-review policy for LinkedIn data
# ---------------------------------------------------------------------------

def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def needs_linkedin_review(last_reviewed_iso: str | None,
                          now: datetime | None = None,
                          interval_days: int = REVIEW_INTERVAL_DAYS) -> bool:
    """True unless a human reviewed this company's LinkedIn within the
    interval. Invalid or missing timestamps always count as due — the safe
    direction for a compliance rule."""
    last = _parse_iso(last_reviewed_iso)
    if last is None:
        return True
    current = now or datetime.now(timezone.utc)
    return (current - last) > timedelta(days=interval_days)


def record_linkedin_review(store_path, website_host: str, note: str = "") -> None:
    """Persist a human LinkedIn review (who reviewed what, when)."""
    from pathlib import Path
    path = Path(store_path)
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["website_host", "reviewed_at", "note"])
        writer.writerow([
            website_host,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note,
        ])


def load_reviews(store_path) -> dict[str, str]:
    """Map of website_host -> most recent review timestamp."""
    from pathlib import Path
    path = Path(store_path)
    if not path.exists():
        return {}
    latest: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            host = (row.get("website_host") or "").strip()
            when = (row.get("reviewed_at") or "").strip()
            if host and (host not in latest or when > latest[host]):
                latest[host] = when
    return latest


def attach_review_status(enrichment: dict, store_path) -> dict:
    """Add the review-policy status to a PDL enrichment result."""
    host = url_host(enrichment.get("website") or enrichment.get("linkedin_url") or "")
    last_reviewed = load_reviews(store_path).get(host)
    enrichment["linkedin_review"] = {
        "last_reviewed": last_reviewed,
        "review_due": needs_linkedin_review(last_reviewed),
        "policy": "human review required at least monthly (PDL data refreshes monthly)",
    }
    return enrichment


def url_host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or url).lower()
    except ValueError:
        return url
