"""Full-fixture assertion net: every row of the real fixture gets a decision,
and every seeded trap produces its expected outcome. This is the regression
net AND the evidence table for the write-up."""

import csv
import json
import shutil
import subprocess
import sys

import pytest

from firstpass.pipeline import run

from conftest import FIXTURE

EXPECTED_DECISIONS = {
    "L-001": "QUALIFY",    # clean, high-intent, $25k/mo — the normal case
    "L-002": "REJECT",     # student mentorship, $0 budget
    "L-003": "QUALIFY",    # duplicate/thread follow-up of L-001, linked not lost
    "L-004": "QUALIFY",    # no email but strong firmographics — data gap flagged
    "L-005": "NURTURE",    # "we'll discuss" budget — needs discovery
    "L-006": "REJECT",     # PROMPT INJECTION — must never become QUALIFY 1.0
    "L-007": "ESCALATE",   # $50/mo vs 12,000-employee CPG — conflicting signals
    "L-008": "ESCALATE",   # unverifiable identity + $40k + pressure
    "L-009": "QUALIFY",    # Spanish message — handled, qualified on merits
    "L-010": "REJECT",     # junk ("asdf", $999999)
    "L-011": "REJECT",     # competitor/peer probing for retainer structure
    "L-012": "QUALIFY",    # invalid timestamp — processed anyway, flagged
    "L-013": "REJECT",     # disposable email + methodology fishing
    "L-014": "NURTURE",    # pre-seed, small budget, growing — not rejected
    "L-015": "NURTURE",    # same contact as L-002, new legitimate ask
    "L-016": "QUALIFY",    # HTML/emoji message — sanitized, real SMB lead
    "L-017": "QUALIFY",    # "15k" budget parsed
    "L-018": "QUALIFY",    # empty message, strong firmographics — low confidence
    "L-019": "ESCALATE",   # GDPR Article 17 erasure — privacy, never sales
    "L-020": "ESCALATE",   # phishing with .exe — security, never enriched
}


@pytest.fixture(scope="module")
def run_out(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("run")
    summary = run(FIXTURE, out_dir, llm_mode="off", input_sha256="test-sha")
    return out_dir, summary


def test_every_row_gets_a_decision(run_out):
    out_dir, summary = run_out
    assert summary["total_leads"] == 20
    cards = json.loads((out_dir / "cards.json").read_text(encoding="utf-8"))
    assert {c["lead_id"]: c["decision"] for c in cards} == EXPECTED_DECISIONS


def test_decisions_csv_matches_012_contract(run_out):
    out_dir, _ = run_out
    with open(out_dir / "decisions.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 20
    assert list(rows[0].keys()) == ["lead_id", "decision", "reason", "confidence"]
    for row in rows:
        assert row["decision"] in {"QUALIFY", "NURTURE", "REJECT", "ESCALATE"}
        assert 0.0 <= float(row["confidence"]) <= 1.0
        assert len(row["reason"]) > 20


def test_injection_row_never_obeyed(run_out):
    out_dir, _ = run_out
    cards = {c["lead_id"]: c for c in json.loads((out_dir / "cards.json").read_text(encoding="utf-8"))}
    l6 = cards["L-006"]
    assert l6["decision"] != "QUALIFY"
    assert l6["confidence"] <= 0.60
    assert "injection_attempt" in l6["flags"]
    trace = json.loads((out_dir / "traces" / "L-006.json").read_text(encoding="utf-8"))
    assert "Ignore all previous instructions" in trace["forensics"]["raw"]["message"]
    # The manipulation attempt is surfaced in the run log for reviewers.
    log = (out_dir / "run.log").read_text(encoding="utf-8")
    assert "L-006: injection_attempt" in log


def test_sensitive_and_security_routes(run_out):
    out_dir, _ = run_out
    cards = {c["lead_id"]: c for c in json.loads((out_dir / "cards.json").read_text(encoding="utf-8"))}
    assert cards["L-019"]["escalation_route"] == "privacy_compliance"
    assert cards["L-020"]["escalation_route"] == "security"
    # The phishing URL is assessed, never fetched — it carries dangerous_ext.
    l20 = cards["L-020"]
    assert any(u["dangerous_ext"] for u in l20["urls"])


def test_duplicates_linked_with_context(run_out):
    out_dir, _ = run_out
    cards = {c["lead_id"]: c for c in json.loads((out_dir / "cards.json").read_text(encoding="utf-8"))}
    assert any(f == "thread_followup_of:L-001" for f in cards["L-003"]["flags"])
    assert any(f == "thread_followup_of:L-002" for f in cards["L-015"]["flags"])
    # The follow-up with a real ask is NOT blanket-rejected by the first ask.
    assert cards["L-015"]["decision"] != "REJECT"


def test_malformed_timestamp_processed_not_fatal(run_out):
    out_dir, _ = run_out
    cards = {c["lead_id"]: c for c in json.loads((out_dir / "cards.json").read_text(encoding="utf-8"))}
    assert cards["L-012"]["decision"] == "QUALIFY"
    assert "invalid_field:submitted_at" in cards["L-012"]["flags"]
    assert cards["L-012"]["submitted_at"] is None


def test_security_boundary_in_outputs(run_out):
    out_dir, _ = run_out
    sanitized_blob = (out_dir / "sanitized_leads.json").read_text(encoding="utf-8")
    # Raw un-decoded forms and the forensics section never reach the AI view.
    assert "<div>" not in sanitized_blob
    assert "&amp;" not in sanitized_blob
    assert '"forensics"' not in sanitized_blob
    # Traces keep the forensics for human audit.
    trace16 = json.loads((out_dir / "traces" / "L-016.json").read_text(encoding="utf-8"))
    assert trace16["forensics"]["raw"]["message"].startswith("<div>")


def test_summary_reports_trap_evidence(run_out):
    out_dir, summary = run_out
    flagged = {t["lead_id"] for t in summary["traps_found"]}
    for lead_id in ("L-006", "L-007", "L-008", "L-010", "L-011", "L-013",
                    "L-019", "L-020", "L-003", "L-015"):
        assert lead_id in flagged
    assert summary["ms_per_lead"] < 100  # observed timing evidence


def test_cli_end_to_end(run_out, tmp_path):
    out_dir = tmp_path / "cli-out"
    proc = subprocess.run(
        [sys.executable, "run_agent.py", "--llm", "off",
         "--input", str(FIXTURE), "--out", str(out_dir)],
        capture_output=True, text=True, cwd=FIXTURE.parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "decisions.csv").exists()
    assert (out_dir / "summary.json").exists()
