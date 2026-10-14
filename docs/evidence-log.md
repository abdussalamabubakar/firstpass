# Evidence Log

Every major claim in this submission, with its proof tier (per the public
SCORING.md tiers) and its number-source label (**Observed / Estimated /
Benchmarked / Assumed**). Unlabeled numbers are how trust dies; we label ours.

| # | Claim | Proof tier | Number label | Where to verify |
|---|---|---|---|---|
| 1 | The agent processes all 20 fixture rows and emits a decision, reason, and confidence for each | 3 (code + logs + per-row traces) | Observed | `out/decisions.csv`, `out/run.log`, `out/traces/*.json`, asserted by `tests/test_pipeline.py` |
| 2 | Full run completes in ~16–17 ms total, ~0.8 ms/lead | 3 (run log + summary) | **Observed** | `out/summary.json` (`elapsed_ms`, `ms_per_lead`), `out/run.log` |
| 3 | Manual triage takes 10–15 min/lead, 30–50 leads/week → roughly **5–12.5 hours/week** of junior time | 0 for our own measurement (we did not observe Single Grain's team); stated as the challenge's own premise | **Estimated** (derived from the brief's stated figures: 30–50 × 10–15 min) | The Intern 011 brief; arithmetic shown in submission-011.md |
| 4 | The agent's per-lead speed advantage vs manual is roughly **4 orders of magnitude** (0.8 ms vs 600,000–900,000 ms) | 3 | **Observed** (agent side) vs **Estimated** (manual side, from the brief) | `out/summary.json` vs brief figures |
| 5 | Every seeded trap was found and surfaced: 10 trap rows flagged (injection, phishing, GDPR, junk, 2× fishing, conflict, pressure, 2× thread links) | 3 | Observed | `out/run.log` ("trap rows surfaced: 10"), `docs/trap-map.md`, `tests/test_pipeline.py` |
| 6 | The prompt-injection row cannot flip the decision: L-006 → REJECT @ 0.50; an obedient simulated LLM verdict on that row is discarded entirely | 3 | Observed | `out/traces/L-006.json`, `tests/test_verdict.py::test_injection_discards_llm_verdict_entirely` |
| 7 | The phishing row is never enriched or fetched: URL assessed as `dangerous_ext` from text only | 3 | Observed | `out/traces/L-020.json` (`urls[].dangerous_ext: true`), `tests/test_signals.py` |
| 8 | 47 automated tests pass, including a full-fixture assertion net covering all 20 expected outcomes | 3 | Observed | `python -m pytest tests/` output; committed in `tests/` |
| 9 | Four real bad outputs were found in our own prototype during the build, each fixed with a guard test | 3 | Observed | `docs/failure-notes.md` |
| 10 | Fixture integrity: SHA-256 `cc1927ca771c37b186a2abdb7b9757594da79ec6dda074e5a514dd2761cc8599` | 3 | Observed | `sha256sum fixtures/inbound_leads.csv`; recorded in run.log |
| 11 | Security boundary holds: the AI-facing file contains no raw un-decoded forms (`<div>`, `&amp;`, zero-width chars) and no forensics section | 3 | Observed | `out/sanitized_leads.json` vs `out/traces/`; `tests/test_pipeline.py::test_security_boundary_in_outputs` |
| 12 | Lead data rendered in the review UI cannot execute as HTML (stored-XSS defense) | 3 | Observed | `tests/test_review.py::test_planted_html_cannot_execute` |
| 13 | ICP thresholds: $1,000/mo service floor, $5,000/mo entry retainer, $10,000/mo comfortable band | 2–3 (encoded in code + docs) | **Assumed** — not measured against Single Grain's real books; disclosed so reviewers can correct them | `firstpass/verdict.py` constants |
| 14 | Revenue estimates (e.g., "$240,000–$600,000/yr" for L-001) | 2 | **Assumed** — transparent multipliers (stated budget × 12 × 8–20x revenue multiple); labeled in every briefing bullet that carries them | `out/cards.json` `revenue_estimate.basis` |
| 15 | A human decision loop (Approved / Not Approved / Not Enough) finalizes every verdict; the agent's output is a recommendation only | 2–3 (UI code + persisted CSV) | Observed (mechanism) | `review.py`, `tests/test_review.py`, `out/final_decisions.csv` after a review session |
| 16 | The LLM path (OpenRouter-compatible) works as described | 2 (code + prompt artifact inspectable; not exercised in the committed run — no key available) | Observed for code structure; **Assumed** for live provider behavior | `firstpass/llm.py`, `prompts/system_prompt.md` |

## What we could NOT verify (and refuse to fake)

- We did not run this against Single Grain's real lead flow, so time-saved
  figures for the business are estimates from the brief's own numbers, not
  before/after measurements of a real team.
- The committed run used the deterministic engine (no LLM API key available
  for this submission). The LLM path is fully implemented and guarded, but
  its live behavior with a specific provider is not part of our evidence.
- Enrichment (People Data Labs / website fetching) is designed but not
  active in the demo; fixture companies are mostly fictional domains, and
  the honest behavior is to decide on form data with reduced confidence
  rather than to scrape invented companies.
