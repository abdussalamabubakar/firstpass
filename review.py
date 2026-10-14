#!/usr/bin/env python3
"""firstpass — upload a dataset, process it, give the final human say.

Run it and you land on a page where you add the dataset you want verdicts on:

    python review.py        # opens http://127.0.0.1:5057

- **Landing page**: upload a CSV (the "already-scraped" dataset) on the main
  panel; past run history sits on the left. The bundled demo fixture is one
  click away.
- **Every upload becomes a run**: the pipeline sanitizes, detects traps,
  links duplicates, and produces verdicts — via OpenRouter when LLM_API_KEY
  is set, via the transparent deterministic engine otherwise. It then opens
  the review queue for that run.
- **The reviewer's click IS the final say** — it can confirm or override the
  AI, and the AI's verdict stays recorded next to it in the run's
  final_decisions.csv.

Safety notes: all lead content is rendered through Jinja autoescaping
(stored-XSS defense), and run ids in URLs are resolved through the run index
only — a URL segment can never traverse the filesystem.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

from firstpass.pipeline import run as run_pipeline

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = Path(os.environ.get("FIRSTPASS_OUT", BASE_DIR / "out"))
RUNS_DIR = OUT_ROOT / "runs"
INDEX_PATH = RUNS_DIR / "index.json"
FIXTURE_PATH = BASE_DIR / "fixtures" / "inbound_leads.csv"
DEMO_RUN_ID = "demo-fixture"

# The dataset contract. A upload missing any of these is rejected with a
# friendly message instead of failing mid-pipeline.
REQUIRED_COLUMNS = {
    "lead_id", "submitted_at", "name", "email", "company",
    "website", "monthly_budget_usd", "message", "source",
}

FINAL_COLUMNS = [
    "lead_id", "agent_decision", "agent_confidence",
    "human_choice", "final_decision", "note", "decided_at",
]

CHOICES = {
    # The reviewer's click IS the final say — it can confirm the AI's
    # verdict or override it. The AI verdict stays recorded alongside
    # so the packet can show agreement vs. override.
    "approved": "QUALIFY",
    "not_approved": "REJECT",
    "not_enough": "ESCALATE",  # reviewer needs more info — route to verification
}

ROUTE_LABELS = {
    "privacy_compliance": "Route to: Privacy / Compliance officer",
    "security": "Route to: Security team — do not interact with this submission",
    "signal_verification": "Route to: Sales ops — verify the conflicting data",
    "identity_verification": "Route to: Sales lead — verify identity before outreach",
}

app = Flask(__name__)
app.secret_key = "firstpass-local-review"  # local tool: flash messages only
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Run store
# ---------------------------------------------------------------------------

def _load_index() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_index(entries: list[dict]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def _seed_demo_run() -> None:
    """Register the committed demo run (classic out/ layout) in the history."""
    entries = _load_index()
    if any(e.get("id") == DEMO_RUN_ID for e in entries):
        return
    if not (OUT_ROOT / "cards.json").exists():
        return
    summary = {}
    summary_path = OUT_ROOT / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
    entries.insert(0, {
        "id": DEMO_RUN_ID,
        "dir": ".",
        "label": "Bundled demo run — inbound_leads.csv",
        "created_at": summary.get("generated_at", "committed with the repo"),
        "source_file": "fixtures/inbound_leads.csv",
        "total_leads": summary.get("total_leads"),
        "decisions": summary.get("decisions", {}),
        "engine": "LLM + guardrails" if summary.get("llm_used") else "deterministic engine",
        "sha256": summary.get("input_sha256"),
    })
    _save_index(entries)


def _run_dir(run_id: str) -> Path | None:
    """Resolve a run id to its directory via the index ONLY — no path
    is ever built from the raw URL segment."""
    for entry in _load_index():
        if entry.get("id") == run_id:
            return OUT_ROOT / entry["dir"]
    return None


def _register_run(run_id: str, run_dir: Path, label: str, source_file: str,
                  summary: dict, sha256: str) -> None:
    entries = _load_index()
    entries.insert(0, {
        "id": run_id,
        "dir": str(run_dir.relative_to(OUT_ROOT)),
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": source_file,
        "total_leads": summary.get("total_leads"),
        "decisions": summary.get("decisions", {}),
        "engine": "LLM + guardrails" if summary.get("llm_used") else "deterministic engine",
        "sha256": sha256,
    })
    _save_index(entries)


def _load_cards(run_dir: Path) -> list[dict]:
    return json.loads((run_dir / "cards.json").read_text(encoding="utf-8"))


def _load_final(run_dir: Path) -> dict[str, dict]:
    final_csv = run_dir / "final_decisions.csv"
    if not final_csv.exists():
        return {}
    with open(final_csv, newline="", encoding="utf-8") as fh:
        return {row["lead_id"]: row for row in csv.DictReader(fh)}


def _append_final(run_dir: Path, row: dict) -> None:
    final_csv = run_dir / "final_decisions.csv"
    new_file = not final_csv.exists()
    with open(final_csv, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FINAL_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _discard(run_dir: Path) -> None:
    """Remove a failed run directory so history never shows junk."""
    if run_dir.exists() and run_dir != OUT_ROOT:
        for child in sorted(run_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        run_dir.rmdir()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def landing():
    _seed_demo_run()
    return render_template("upload.html", runs=_load_index(),
                           schema_cols=sorted(REQUIRED_COLUMNS))


@app.post("/upload")
def upload():
    file = request.files.get("dataset")
    if file is None or not file.filename:
        flash("Choose a CSV file first.")
        return redirect(url_for("landing"))
    original_name = file.filename
    if not original_name.lower().endswith(".csv"):
        flash("That doesn't look like a CSV — upload a .csv file.")
        return redirect(url_for("landing"))

    run_id = (datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
              + "-" + os.urandom(2).hex())
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "input.csv"
    file.save(input_path)

    # Validate the header before spending any processing on the file.
    try:
        with open(input_path, newline="", encoding="utf-8-sig") as fh:
            header = next(csv.reader(fh), None)
    except (OSError, csv.Error) as exc:
        _discard(run_dir)
        flash(f"Could not read that file: {exc}")
        return redirect(url_for("landing"))
    if not header:
        _discard(run_dir)
        flash("That file is empty.")
        return redirect(url_for("landing"))
    columns = {h.strip().lstrip("\ufeff").lower() for h in header}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        _discard(run_dir)
        flash("Missing required columns: " + ", ".join(sorted(missing))
              + ". Expected columns: " + ", ".join(sorted(REQUIRED_COLUMNS)) + ".")
        return redirect(url_for("landing"))

    sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    try:
        summary = run_pipeline(input_path, run_dir, llm_mode="auto", input_sha256=sha256)
    except Exception as exc:  # surface any pipeline failure to the reviewer
        _discard(run_dir)
        flash(f"Processing failed: {exc}")
        return redirect(url_for("landing"))

    _register_run(run_id, run_dir, label=original_name, source_file=original_name,
                  summary=summary, sha256=sha256)
    return redirect(url_for("review_run", run_id=run_id))


@app.post("/demo-run")
def demo_run():
    """One-click run of the bundled fixture — the fastest way to see it work."""
    if not FIXTURE_PATH.exists():
        flash("Bundled fixture not found — use the upload button instead.")
        return redirect(url_for("landing"))
    run_id = (datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
              + "-" + os.urandom(2).hex())
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    summary = run_pipeline(FIXTURE_PATH, run_dir, llm_mode="auto", input_sha256=sha256)
    _register_run(run_id, run_dir, label="Bundled fixture — inbound_leads.csv",
                  source_file="fixtures/inbound_leads.csv", summary=summary, sha256=sha256)
    return redirect(url_for("review_run", run_id=run_id))


@app.get("/run/<run_id>")
def review_run(run_id: str):
    _seed_demo_run()
    run_dir = _run_dir(run_id)
    if run_dir is None or not (run_dir / "cards.json").exists():
        abort(404)
    entry = next(e for e in _load_index() if e.get("id") == run_id)
    cards = _load_cards(run_dir)
    existing = _load_final(run_dir)
    sales = [c for c in cards if c["decision"] != "ESCALATE"]
    escalations = [c for c in cards if c["decision"] == "ESCALATE"]
    for card in cards:
        card["route_label"] = ROUTE_LABELS.get(card.get("escalation_route"))
    counts: dict[str, int] = {}
    for card in cards:
        counts[card["decision"]] = counts.get(card["decision"], 0) + 1
    return render_template(
        "review.html",
        run=entry,
        sales=sales,
        escalations=escalations,
        existing=existing,
        counts=counts,
        total=len(cards),
    )


@app.post("/run/<run_id>/api/decide/<lead_id>")
def decide(run_id: str, lead_id: str):
    _seed_demo_run()
    run_dir = _run_dir(run_id)
    if run_dir is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")
    note = (data.get("note") or "").strip()
    if choice not in CHOICES:
        return jsonify(error="choice must be one of: approved, not_approved, not_enough"), 400
    try:
        card = next(c for c in _load_cards(run_dir) if c["lead_id"] == lead_id)
    except StopIteration:
        return jsonify(error=f"unknown lead {lead_id}"), 404

    final_decision = CHOICES[choice]
    _append_final(run_dir, {
        "lead_id": lead_id,
        "agent_decision": card["decision"],
        "agent_confidence": card["confidence"],
        "human_choice": choice,
        "final_decision": final_decision,
        "note": note,
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return jsonify(ok=True, lead_id=lead_id, final_decision=final_decision)


@app.get("/run/<run_id>/trace/<lead_id>")
def trace(run_id: str, lead_id: str):
    _seed_demo_run()
    run_dir = _run_dir(run_id)
    if run_dir is None:
        abort(404)
    path = run_dir / "traces" / f"{lead_id}.json"
    if not path.exists():
        return jsonify(error=f"no trace for {lead_id}"), 404
    return app.response_class(path.read_text(encoding="utf-8"), mimetype="application/json")


if __name__ == "__main__":
    print("firstpass -> http://127.0.0.1:5057  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5057, debug=False)
