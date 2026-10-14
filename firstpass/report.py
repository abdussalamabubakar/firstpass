"""Output writers — the inspectable evidence trail.

Files produced per run (all under the out directory):

- sanitized_leads.json   the AI-facing view of every record (NO raw fields —
                         this file is what the verdict engine/AI layer reads)
- cards.json             merged card view consumed by the review UI
- decisions.csv          the 012 contract: lead_id, decision, reason, confidence
- traces/L-xxx.json      full per-lead record INCLUDING raw-vs-clean forensics
- summary.json           counts, trap evidence, timing
- run.log                human-readable run narrative
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .sanitize import ai_view


def build_card(rec: dict, verdict: dict) -> dict:
    budget = rec["monthly_budget_usd"]
    if budget is not None:
        budget_display = f"${budget:,}/mo"
    elif rec["budget_raw"]:
        budget_display = f"not usable ('{rec['budget_raw']}')"
    else:
        budget_display = "not stated"
    return {
        "lead_id": rec["lead_id"],
        "company": rec["company"] or "(not given)",
        "name": rec["name"] or "(not given)",
        "email": rec["email"] or "(not given)",
        "email_class": rec["email_class"],
        "website_host": rec["website_host"],
        "budget_display": budget_display,
        "budget_flag": rec["budget_flag"],
        "source": rec["source"],
        "submitted_at": rec["submitted_at"],
        "submitted_at_valid": rec["submitted_at_valid"],
        "message": rec["message"],
        "message_language": rec.get("message_language", "unknown"),
        "urls": rec["urls"],
        "flags": rec["flags"],
        "sanitization": rec["sanitization"],
        "intent": rec.get("intent"),
        "thread_parent": rec.get("thread_parent"),
        "decision": verdict["decision"],
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "briefing": verdict["briefing"],
        "revenue_estimate": verdict["revenue_estimate"],
        "escalation_route": verdict.get("escalation_route"),
        "review_recommendation": verdict["review_recommendation"],
    }


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_decisions_csv(path: Path, cards: list[dict]) -> None:
    """The exact 012 contract: lead_id, decision, reason, confidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["lead_id", "decision", "reason", "confidence"])
        for card in cards:
            writer.writerow([
                card["lead_id"], card["decision"], card["reason"], card["confidence"],
            ])


def write_run_log(path: Path, summary: dict, cards: list[dict]) -> None:
    lines = []
    lines.append(f"firstpass run — {summary['generated_at']}")
    lines.append(f"input: {summary['input_file']}")
    lines.append(f"input sha256: {summary['input_sha256']}")
    lines.append(f"llm: {'used' if summary['llm_used'] else 'deterministic engine (no LLM)'}")
    if summary.get("llm_error"):
        lines.append(f"llm note: {summary['llm_error']} — run completed deterministically")
    lines.append(
        f"leads: {summary['total_leads']} | "
        + " ".join(f"{k}={v}" for k, v in sorted(summary["decisions"].items()))
    )
    lines.append(f"trap rows surfaced: {len(summary['traps_found'])}")
    for trap in summary["traps_found"]:
        lines.append(f"  {trap['lead_id']}: {', '.join(trap['flags'])}")
    lines.append(f"timing: {summary['elapsed_ms']} ms total, {summary['ms_per_lead']} ms/lead (observed)")
    lines.append("per lead:")
    for card in cards:
        flags = ",".join(card["flags"]) if card["flags"] else "-"
        lines.append(
            f"  [{card['lead_id']}] {card['decision']} conf={card['confidence']} "
            f"flags={flags} :: {card['reason']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(out_dir, records: list[dict], cards: list[dict], summary: dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # AI-facing sanitized view: the security-boundary artifact (no raw fields).
    _write_json(out / "sanitized_leads.json", [ai_view(rec) for rec in records])
    _write_json(out / "cards.json", cards)
    _write_json(out / "summary.json", summary)
    write_decisions_csv(out / "decisions.csv", cards)
    write_run_log(out / "run.log", summary, cards)

    traces_dir = out / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    for rec, card in zip(records, cards):
        trace = dict(rec)
        trace["verdict"] = card
        _write_json(traces_dir / f"{rec['lead_id'] or 'unlabeled'}.json", trace)
