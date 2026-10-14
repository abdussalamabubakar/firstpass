#!/usr/bin/env python3
"""firstpass review — the human-in-the-loop card UI.

Run the agent first (python run_agent.py), then:

    python review.py        # opens http://127.0.0.1:5057

The reviewer sees one card per lead: bullet briefing, the agent's verdict,
confidence, and risk flags. Clicking Approved / Not Approved / Not Enough
finalizes that lead's decision. Agent verdicts are recommendations; this
screen is where the human decision happens. Escalation rows (privacy,
security, verification) are shown in a separate queue — they are not sales
decisions and are handled by the right human, not the reviewer.

All lead content is rendered through Jinja autoescaping, so HTML planted in
a lead message can never execute in this UI (stored-XSS defense).
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("FIRSTPASS_OUT", BASE_DIR / "out"))
CARDS_PATH = OUT_DIR / "cards.json"
TRACES_DIR = OUT_DIR / "traces"
FINAL_CSV = OUT_DIR / "final_decisions.csv"

CHOICES = {
    # human button -> final decision it produces
    "approved": None,        # agent's verdict stands
    "not_approved": "REJECT",
    "not_enough": "ESCALATE",  # reviewer needs more info — route to verification
}
FINAL_COLUMNS = [
    "lead_id", "agent_decision", "agent_confidence",
    "human_choice", "final_decision", "note", "decided_at",
]

app = Flask(__name__)


def load_cards() -> list[dict]:
    return json.loads(CARDS_PATH.read_text(encoding="utf-8"))


def load_existing() -> dict[str, dict]:
    if not FINAL_CSV.exists():
        return {}
    with open(FINAL_CSV, newline="", encoding="utf-8") as fh:
        return {row["lead_id"]: row for row in csv.DictReader(fh)}


def append_final(row: dict) -> None:
    FINAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not FINAL_CSV.exists()
    with open(FINAL_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FINAL_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


ROUTE_LABELS = {
    "privacy_compliance": "Route to: Privacy / Compliance officer",
    "security": "Route to: Security team — do not interact with this submission",
    "signal_verification": "Route to: Sales ops — verify the conflicting data",
    "identity_verification": "Route to: Sales lead — verify identity before outreach",
}


@app.get("/")
def index():
    cards = load_cards()
    existing = load_existing()
    sales = [c for c in cards if c["decision"] != "ESCALATE"]
    escalations = [c for c in cards if c["decision"] == "ESCALATE"]
    for card in cards:
        card["route_label"] = ROUTE_LABELS.get(card.get("escalation_route"))
    counts = {}
    for card in cards:
        counts[card["decision"]] = counts.get(card["decision"], 0) + 1
    return render_template(
        "index.html",
        sales=sales,
        escalations=escalations,
        existing=existing,
        counts=counts,
        total=len(cards),
    )


@app.post("/api/decide/<lead_id>")
def decide(lead_id: str):
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")
    note = (data.get("note") or "").strip()
    if choice not in CHOICES:
        return jsonify(error="choice must be one of: approved, not_approved, not_enough"), 400

    card = next((c for c in load_cards() if c["lead_id"] == lead_id), None)
    if card is None:
        return jsonify(error=f"unknown lead {lead_id}"), 404

    final_decision = CHOICES[choice] or card["decision"]
    append_final({
        "lead_id": lead_id,
        "agent_decision": card["decision"],
        "agent_confidence": card["confidence"],
        "human_choice": choice,
        "final_decision": final_decision,
        "note": note,
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return jsonify(ok=True, lead_id=lead_id, final_decision=final_decision)


@app.get("/trace/<lead_id>")
def trace(lead_id: str):
    path = TRACES_DIR / f"{lead_id}.json"
    if not path.exists():
        return jsonify(error=f"no trace for {lead_id}"), 404
    return app.response_class(path.read_text(encoding="utf-8"), mimetype="application/json")


if __name__ == "__main__":
    if not CARDS_PATH.exists():
        raise SystemExit("out/cards.json not found — run `python run_agent.py` first")
    print("firstpass review UI -> http://127.0.0.1:5057  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5057, debug=False)
