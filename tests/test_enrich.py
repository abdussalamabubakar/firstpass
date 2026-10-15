"""People Data Labs adapter tests — mock PDL server, zero real API calls.

Covers: normalization + Benchmarked labeling, dormant mode without a key,
error paths, the monthly LinkedIn review policy boundary (29/31 days), the
review store roundtrip, and the enrichment passthrough into the verdict's
revenue estimate.
"""

import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from firstpass import enrich  # noqa: E402
from firstpass.sanitize import sanitize_lead  # noqa: E402
from firstpass.verdict import revenue_estimate  # noqa: E402

PDL_PAYLOAD = {
    "status": 200,
    "id": "pdl_nimbus_123",
    "name": "Nimbus Analytics",
    "website": "nimbus.example",
    "linkedin_url": "https://www.linkedin.com/company/nimbus-analytics",
    "employee_count": 120,
    "industry": "Computer Software",
    "revenue_estimated": 25_000_000,
    "locality": "Lagos",
    "country": "Nigeria",
}


class _PDLHandler(BaseHTTPRequestHandler):
    payload: dict = PDL_PAYLOAD
    status: int = 200

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _make_server(handler_cls):
    class DualStack(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    server = DualStack(("::", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def pdl_server(monkeypatch):
    handler = type("Handler", (_PDLHandler,), {"payload": PDL_PAYLOAD, "status": 200})
    server = _make_server(handler)
    monkeypatch.setenv("PDL_API_KEY", "test-key")
    monkeypatch.setenv("PDL_BASE_URL", f"http://localhost:{server.server_port}")
    yield handler
    server.shutdown()


def test_enrich_normalizes_and_labels_benchmarked(pdl_server):
    result = enrich.enrich_company(website="nimbus.example")
    assert result["employee_count"] == 120
    assert result["linkedin_url"].endswith("/company/nimbus-analytics")
    assert result["source"] == "People Data Labs (Benchmarked)"
    assert "fetched_at" in result


def test_dormant_without_key(monkeypatch):
    monkeypatch.delenv("PDL_API_KEY", raising=False)
    assert enrich.available() is False
    with pytest.raises(enrich.EnrichmentError):
        enrich.enrich_company(website="nimbus.example")


def test_pdl_error_surfaces(pdl_server):
    pdl_server.status = 500
    with pytest.raises(enrich.EnrichmentError):
        enrich.enrich_company(website="nimbus.example")


def test_monthly_review_boundary():
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=29)).isoformat()
    stale = (now - timedelta(days=30, seconds=1)).isoformat()
    assert enrich.needs_linkedin_review(None) is True
    assert enrich.needs_linkedin_review(fresh, now=now) is False
    assert enrich.needs_linkedin_review(stale, now=now) is True
    # Fail safe: an unparseable timestamp always counts as due.
    assert enrich.needs_linkedin_review("not-a-date") is True


def test_review_store_roundtrip(tmp_path):
    store = tmp_path / "linkedin_reviews.csv"
    enrich.record_linkedin_review(store, "nimbus.example", "checked the profile")
    enrich.record_linkedin_review(store, "other.example")
    reviews = enrich.load_reviews(store)
    assert set(reviews) == {"nimbus.example", "other.example"}
    assert reviews["nimbus.example"]
    # Re-review keeps the most recent timestamp.
    first = reviews["nimbus.example"]
    enrich.record_linkedin_review(store, "nimbus.example", "second pass")
    assert enrich.load_reviews(store)["nimbus.example"] >= first


def test_attach_review_status_due_then_ok(tmp_path):
    store = tmp_path / "linkedin_reviews.csv"
    e = {"website": "https://nimbus.example", "employee_count": 120}
    enrich.attach_review_status(e, store)
    assert e["linkedin_review"]["review_due"] is True
    assert "monthly" in e["linkedin_review"]["policy"]
    enrich.record_linkedin_review(store, "nimbus.example")
    enrich.attach_review_status(e, store)
    assert e["linkedin_review"]["review_due"] is False


def test_enrichment_flows_into_revenue_estimate():
    """PDL employee counts drive the revenue estimate with a Benchmarked
    label — never passed off as our own observation."""
    row = {
        "lead_id": "L-SCR-1", "submitted_at": "2026-10-15T00:00:00Z",
        "name": "", "email": "hello@nimbus.example", "company": "Nimbus Analytics",
        "website": "https://nimbus.example", "monthly_budget_usd": "",
        "message": "Live website scrape of https://nimbus.example. "
                   "People Data Labs (Benchmarked): 120 employees, "
                   "LinkedIn: https://www.linkedin.com/company/nimbus-analytics.",
        "source": "live_scrape",
        "enrichment": json.dumps({"employee_count": 120,
                                  "industry": "Computer Software",
                                  "revenue_estimated_usd": 25_000_000,
                                  "linkedin_url": "https://www.linkedin.com/company/nimbus-analytics",
                                  "source": "People Data Labs"}),
    }
    record = sanitize_lead(row)
    assert record["enrichment"]["employee_count"] == 120
    est = revenue_estimate(record)
    assert est["label"] == "Benchmarked"
    assert est["low_usd"] == 120 * 150_000
    assert "People Data Labs" in est["basis"]
