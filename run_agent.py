#!/usr/bin/env python3
"""firstpass CLI — run the lead triage pipeline.

    python run_agent.py                          # demo defaults
    python run_agent.py --input path/to/leads.csv --out out
    python run_agent.py --llm off                # force deterministic engine
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from firstpass.pipeline import run


def _console_utf8() -> None:
    # Windows consoles may default to a legacy code page; the fixture and
    # outputs contain non-ASCII text, so keep output lossless-but-safe.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="firstpass",
        description="Inbound lead triage agent — the agent does the first pass, humans do the thinking.",
    )
    parser.add_argument("--input", default="fixtures/inbound_leads.csv",
                        help="CSV of leads (any file with the fixture schema)")
    parser.add_argument("--out", default="out", help="output directory")
    parser.add_argument("--llm", choices=["auto", "off", "force"], default="auto",
                        help="LLM usage: auto = use if LLM_API_KEY is set, off = deterministic only, force = require the key")
    args = parser.parse_args(argv)

    _console_utf8()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: input not found: {input_path}", file=sys.stderr)
        return 2

    sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        summary = run(input_path, out_dir, llm_mode=args.llm, input_sha256=sha256)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"firstpass run complete -> {out_dir}")
    print(f"  input     : {summary['input_file']} (sha256 {summary['input_sha256'][:16]}...)")
    print(f"  leads     : {summary['total_leads']}")
    for decision, count in sorted(summary["decisions"].items()):
        print(f"  {decision:<9}: {count}")
    print(f"  trap rows surfaced: {len(summary['traps_found'])}")
    for trap in summary["traps_found"]:
        print(f"    {trap['lead_id']}: {', '.join(trap['flags'])}")
    print(f"  engine    : {'LLM + guardrails' if summary['llm_used'] else 'deterministic (no LLM key set)'}")
    if summary.get("llm_error"):
        print(f"  llm note  : {summary['llm_error']} — completed deterministically")
    print(f"  timing    : {summary['elapsed_ms']} ms total ({summary['ms_per_lead']} ms/lead, observed)")
    print(f"  outputs   : decisions.csv, sanitized_leads.json, cards.json, traces/, summary.json, run.log")
    print(f"  next step : python review.py  -> http://127.0.0.1:5057 (human review)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
