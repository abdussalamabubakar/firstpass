"""Review UI tests: decisions persist correctly, and planted HTML in lead
content can never execute in the page (stored-XSS defense)."""

import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("flask")

import review  # noqa: E402  (needs flask installed)

from conftest import FIXTURE  # noqa: E402
from firstpass.pipeline import run  # noqa: E402


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    run(FIXTURE, tmp_path, llm_mode="off", input_sha256="test-sha")
    monkeypatch.setenv("FIRSTPASS_OUT", str(tmp_path))
    import importlib
    importlib.reload(review)
    review.app.config["TESTING"] = True
    return review.app.test_client(), tmp_path


def test_index_renders_queues(ui):
    client, _ = ui
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "BrightCart" in body            # sales queue card
    assert "Escalation queue" in body      # escalation section present
    assert "Approved" in body              # reviewer buttons present


def test_decision_persists_to_final_csv(ui):
    client, out_dir = ui
    resp = client.post("/api/decide/L-001",
                       json={"choice": "approved", "note": "looks great"})
    assert resp.status_code == 200
    assert resp.get_json()["final_decision"] == "QUALIFY"  # agent verdict stands

    resp = client.post("/api/decide/L-001",
                       json={"choice": "not_approved", "note": ""})
    assert resp.get_json()["final_decision"] == "REJECT"   # human overrides agent

    import csv
    with open(Path(out_dir) / "final_decisions.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["lead_id"] == "L-001"
    assert rows[-1]["human_choice"] == "not_approved"
    assert rows[-1]["final_decision"] == "REJECT"


def test_not_enough_maps_to_escalate(ui):
    client, _ = ui
    resp = client.post("/api/decide/L-004", json={"choice": "not_enough", "note": ""})
    assert resp.get_json()["final_decision"] == "ESCALATE"


def test_human_click_overrides_agent_verdict(ui):
    """The AI gives the verdict; the human's click is the final say —
    including overriding it. L-002: AI says REJECT, human approves."""
    client, out_dir = ui
    resp = client.post("/api/decide/L-002",
                       json={"choice": "approved", "note": "I disagree — call them"})
    assert resp.get_json()["final_decision"] == "QUALIFY"  # human overrides

    import csv
    with open(Path(out_dir) / "final_decisions.csv", newline="", encoding="utf-8") as fh:
        row = list(csv.DictReader(fh))[-1]
    assert row["agent_decision"] == "REJECT"    # AI verdict still recorded
    assert row["final_decision"] == "QUALIFY"   # but the human's say is final


def test_invalid_choice_rejected(ui):
    client, _ = ui
    resp = client.post("/api/decide/L-001", json={"choice": "qualify please"})
    assert resp.status_code == 400


def test_unknown_lead_404(ui):
    client, _ = ui
    assert client.post("/api/decide/L-999", json={"choice": "approved"}).status_code == 404


def test_planted_html_cannot_execute(ui):
    """Stored-XSS defense: lead content is escaped in the rendered page."""
    client, out_dir = ui
    cards = json.loads((Path(out_dir) / "cards.json").read_text(encoding="utf-8"))
    cards[0]["company"] = "Evil <script>alert(1)</script> Co"
    (Path(out_dir) / "cards.json").write_text(
        json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    body = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
