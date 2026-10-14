# AI Usage Disclosure

Honest accounting, per the submission requirements.

## Tools used

- **ZCode** (Z.ai's coding agent, GLM model) — used as the AI pair-builder
  for this prototype: writing the Python modules, the Flask review UI, the
  pytest suite, and drafting these documents.
- **Web research** (public sources): the challenge repo
  (`ericosiu/beat-claude`) — the public briefs, the public scoring guide,
  and the public fixture CSV.

## What the human decided (and the AI did not)

The candidate led planning, design, and intention end to end:

- Chose Option B (lead qualification) and the "both challenges, one codebase"
  strategy after reviewing the briefs.
- Set the product shape: sanitize-then-analyze pipeline, a baked-in prompt
  contract, and the card-based human review loop with the three-button final
  call (Approved / Not Approved / Not Enough).
- Set the security posture: raw data is dirty by default, the sanitized file
  is the only thing the AI layer may read, and adversarial content hidden in
  HTML/entities/unicode must not evade detection.
- Chose the stack (Python + Flask + vanilla JS) and the scrappy-scope rule
  ("what a team of 20 can use right away, not a six-month system").
- Reviewed the agent's actual outputs, challenged them, and demanded the
  trap defenses be treated with the seriousness the graders intended —
  including pushing back mid-build when the plan under-emphasized them.
- Approved the final plan before implementation started.

## What the AI did

- Implemented the modules per the approved plan (`firstpass/`), wrote the
  test suite, generated the demo run artifacts in `out/`, and drafted the
  documentation.
- Proposed the concrete engineering mechanisms that express the human's
  decisions (deobfuscation order, guardrail hierarchy, confidence caps,
  escalation routes, thread linking), and flagged risks it saw in the design
  as it built.

## What was checked by the human vs. left to the AI

- The human reviewed the run outputs (the run log, per-lead verdicts, trap
  map) and the failure notes; the four documented bad outputs were found
  during that review loop and fixed before finalizing.
- The test suite (47 tests) is the mechanical check: anyone can re-run
  `python -m pytest tests/` and `python run_agent.py --llm off` to verify
  every claim in `docs/evidence-log.md` marked Observed.

## Weak spots in our AI usage (the honest part)

- The LLM path of the *agent itself* was not exercised live in the committed
  run (no API key was available); the deterministic engine did the deciding,
  and the write-up labels this clearly rather than implying an LLM run.
- AI-generated code is exactly as good as its review loop. Four real defects
  were found and fixed during output review (see `docs/failure-notes.md`) —
  which is both the evidence that review works and the evidence that
  unreviewed AI output would have shipped mistakes.
- These very documents were drafted with AI assistance and reviewed by the
  candidate; the personal answers in the submission (Part 1, Part 3) are the
  candidate's own content in their own voice.
