"""Pipeline orchestration.

    raw CSV -> sanitize (security boundary) -> trap signals -> dedupe/link
            -> verdict (deterministic engine, optional LLM through the same
               guardrail gate) -> outputs

Every stage keeps its evidence: sanitization stats, trap flags, per-lead
timings, and traces with raw-vs-clean forensics.
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from datetime import datetime, timezone

from . import llm as llm_mod
from . import report as report_mod
from . import sanitize as sanitize_mod
from . import signals as signals_mod
from . import verdict as verdict_mod

TRAP_FLAGS = {
    "injection_attempt", "security_suspicious", "sensitive_request", "junk",
    "competitor_fishing", "signal_conflict", "pressure_tactics",
    "obfuscated_content",
}
LINK_FLAGS_PREFIXES = ("thread_followup_of:", "duplicate_of:")


def read_csv(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def _trap_evidence(records: list[dict]) -> list[dict]:
    evidence = []
    for rec in records:
        trap_flags = [f for f in rec["flags"] if f in TRAP_FLAGS or f.startswith(LINK_FLAGS_PREFIXES)]
        if trap_flags:
            evidence.append({"lead_id": rec["lead_id"], "flags": trap_flags})
    return evidence


def run(input_path, out_dir, llm_mode: str = "auto", input_sha256: str | None = None) -> dict:
    started = time.perf_counter()

    if llm_mode == "force" and not llm_mod.available():
        raise RuntimeError("llm mode 'force' requires LLM_API_KEY to be set")

    rows = read_csv(input_path)
    records = [sanitize_mod.sanitize_lead(row) for row in rows]
    records = [signals_mod.apply_trap_flags(rec) for rec in records]
    signals_mod.link_duplicates(records)

    use_llm = llm_mode != "off" and llm_mod.available()
    llm_error = None
    llm_calls_ok = 0

    cards = []
    timings = []
    for rec in records:
        t0 = time.perf_counter()
        llm_verdict = None
        if use_llm:
            try:
                llm_verdict = llm_mod.verdict_for(sanitize_mod.ai_view(rec))
                llm_calls_ok += 1
            except llm_mod.LLMError as exc:
                # One documented fallback: if the LLM fails mid-run, the run
                # completes deterministically and says so in the summary.
                llm_error = str(exc)
                use_llm = False
        verdict = verdict_mod.make_verdict(rec, llm_verdict)
        timings.append(time.perf_counter() - t0)
        cards.append(report_mod.build_card(rec, verdict))

    elapsed_ms = (time.perf_counter() - started) * 1000
    decisions = Counter(card["decision"] for card in cards)
    flags_seen = Counter(f for rec in records for f in rec["flags"])

    summary = {
        "input_file": str(input_path),
        "input_sha256": input_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_used": llm_calls_ok > 0,
        "llm_error": llm_error,
        "total_leads": len(records),
        "decisions": dict(decisions),
        "flags_seen": dict(flags_seen),
        "traps_found": _trap_evidence(records),
        "elapsed_ms": round(elapsed_ms, 1),
        "ms_per_lead": round(elapsed_ms / len(records), 2) if records else 0.0,
        "verdict_ms_per_lead": round(sum(timings) / len(timings) * 1000, 2) if timings else 0.0,
    }

    report_mod.write_outputs(out_dir, records, cards, summary)
    return summary
