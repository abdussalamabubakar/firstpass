"""Review app tests: landing page + upload flow + per-run review queues,
decision persistence, and the stored-XSS defense."""

import io
import json
from pathlib import Path

import pytest

pytest.importorskip("flask")

import review  # noqa: E402  (needs flask installed)

from conftest import FIXTURE  # noqa: E402
from firstpass.pipeline import run  # noqa: E402


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    # Seed the committed demo run the same way the repo ships it: a classic
    # pipeline output at the out root, which the app registers in history.
    run(FIXTURE, tmp_path, llm_mode="off", input_sha256="test-sha")
    monkeypatch.setenv("FIRSTPASS_OUT", str(tmp_path))
    import importlib
    importlib.reload(review)
    review.app.config["TESTING"] = True
    return review.app.test_client(), tmp_path


def test_landing_shows_upload_and_history(ui):
    client, _ = ui
    body = client.get("/").get_data(as_text=True)
    assert "Add a dataset to process" in body
    assert "Run history" in body
    assert "Bundled demo run" in body  # the committed run is seeded into history


def test_upload_processes_csv_and_opens_run(ui):
    client, out_root = ui
    with open(FIXTURE, "rb") as fh:
        resp = client.post("/upload",
                           data={"dataset": (fh, "my_leads.csv")},
                           content_type="multipart/form-data",
                           follow_redirects=True)
    assert resp.status_code == 200
    assert "BrightCart" in resp.get_data(as_text=True)  # landed on the new run's queue

    runs_dir = Path(out_root) / "runs"
    uploaded = [d for d in runs_dir.iterdir() if d.is_dir()]
    assert len(uploaded) == 1
    assert (uploaded[0] / "input.csv").exists()
    assert (uploaded[0] / "decisions.csv").exists()
    assert (uploaded[0] / "traces").is_dir()


def test_upload_rejects_missing_columns(ui):
    client, out_root = ui
    resp = client.post("/upload",
                       data={"dataset": (io.BytesIO(b"foo,bar\n1,2\n"), "bad.csv")},
                       content_type="multipart/form-data",
                       follow_redirects=True)
    assert "Missing required columns" in resp.get_data(as_text=True)
    entries = json.loads((Path(out_root) / "runs" / "index.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in entries] == [review.DEMO_RUN_ID]  # bad upload left no run


def test_upload_rejects_non_csv(ui):
    client, _ = ui
    resp = client.post("/upload",
                       data={"dataset": (io.BytesIO(b"hello"), "data.txt")},
                       content_type="multipart/form-data",
                       follow_redirects=True)
    # NOTE: the apostrophe is autoescaped in the HTML (&#39;) — check a
    # substring that survives escaping.
    assert "look like a CSV" in resp.get_data(as_text=True)


def test_unknown_run_404(ui):
    client, _ = ui
    assert client.get("/run/doesnotexist").status_code == 404


def test_review_page_renders_queues(ui):
    client, _ = ui
    body = client.get(f"/run/{review.DEMO_RUN_ID}").get_data(as_text=True)
    assert "BrightCart" in body             # sales queue card
    assert "Escalation queue" in body       # escalation section present
    assert "your click is the final say" in body
    assert "Approved" in body               # reviewer buttons present


def test_decision_persists_to_final_csv(ui):
    client, out_root = ui
    resp = client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-001",
                       json={"choice": "approved", "note": "looks great"})
    assert resp.status_code == 200
    assert resp.get_json()["final_decision"] == "QUALIFY"  # confirms the AI

    resp = client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-001",
                       json={"choice": "not_approved", "note": ""})
    assert resp.get_json()["final_decision"] == "REJECT"   # overrides the AI

    import csv
    with open(Path(out_root) / "final_decisions.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["lead_id"] == "L-001"
    assert rows[-1]["agent_decision"] == "QUALIFY"          # AI verdict recorded
    assert rows[-1]["human_choice"] == "not_approved"
    assert rows[-1]["final_decision"] == "REJECT"           # human's say is final


def test_human_click_overrides_agent_verdict(ui):
    """The AI gives the verdict; the human's click is the final say —
    including overriding it. L-002: AI says REJECT, human approves."""
    client, out_root = ui
    resp = client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-002",
                       json={"choice": "approved", "note": "I disagree — call them"})
    assert resp.get_json()["final_decision"] == "QUALIFY"  # human overrides

    import csv
    with open(Path(out_root) / "final_decisions.csv", newline="", encoding="utf-8") as fh:
        row = list(csv.DictReader(fh))[-1]
    assert row["agent_decision"] == "REJECT"    # AI verdict still recorded
    assert row["final_decision"] == "QUALIFY"   # but the human's say is final


def test_not_enough_maps_to_escalate(ui):
    client, _ = ui
    resp = client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-004",
                       json={"choice": "not_enough", "note": ""})
    assert resp.get_json()["final_decision"] == "ESCALATE"


def test_invalid_choice_rejected(ui):
    client, _ = ui
    resp = client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-001",
                       json={"choice": "qualify please"})
    assert resp.status_code == 400


def test_unknown_lead_404(ui):
    client, _ = ui
    assert client.post(f"/run/{review.DEMO_RUN_ID}/api/decide/L-999",
                       json={"choice": "approved"}).status_code == 404


def test_planted_html_cannot_execute(ui):
    """Stored-XSS defense: lead content is escaped in the rendered page."""
    client, out_dir = ui
    cards = json.loads((Path(out_dir) / "cards.json").read_text(encoding="utf-8"))
    cards[0]["company"] = "Evil <script>alert(1)</script> Co"
    (Path(out_dir) / "cards.json").write_text(
        json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    body = client.get(f"/run/{review.DEMO_RUN_ID}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
