# Failure Notes — real bad outputs, caught and fixed

These are genuine defects found in our own prototype's output during the
build, with how each was detected, how it was fixed, and what the human
review checkpoint would have caught anyway. Nothing here is hypothetical;
each item has a test that now guards against its return.

---

## Bad output #1 (the one we own hardest): L-018's reason overstated the evidence

**What the prototype produced.** L-018 (Quorum Data) submitted an **empty
message** with strong firmographics ($22,000/mo budget, corporate email,
website). The prototype output:

> `QUALIFY conf=0.60 :: Strong commercial intent with stated budget $22,000/mo, inside the typical retainer range...`

**Why it's bad.** The confidence (0.60) and the `missing_field:message` flag
were honest, but the *reason sentence* claimed "strong commercial intent"
that an empty message cannot express. A busy reviewer reading only the
reason would be misled about what the verdict rests on.

**How it was detected.** Manual review of the run log against the fixture
(the step we now call "reading every reason like a skeptical reviewer").

**The fix.** An honesty override in `firstpass/verdict.py`: when the message
is empty and the decision is QUALIFY/NURTURE, the reason is replaced with
what the decision actually rests on:

> `No message content was submitted — this decision rests only on the verifiable form fields (stated budget $22,000/mo, corporate email, website provided); confirm the actual needs on first contact.`

**Guard test:** `tests/test_verdict.py::test_empty_message_qualifies_with_honest_reason`.

**Human checkpoint anyway:** the card UI shows the empty-message flag and the
reviewer confirms needs on the first call.

---

## Bad output #2: confidence inflated by overlapping keyword counts (L-004)

**What the prototype produced.** L-004 ("We need help with content
strategy...") scored `intent_score=3` because the term list contained both
`"need help"` and `"help with"`, which both match the same span of text. The
confidence came out 0.80 instead of the intended 0.75 (missing email −0.10).

**How it was detected.** Comparing actual confidences against the
hand-computed expected table during the full-fixture review.

**The fix.** Removed the subsumed term `"need help"` from
`COMMERCIAL_TERMS` in `firstpass/signals.py`. L-004 now scores 0.75, matching
the data-completeness adjustment the design intends.

---

## Bad output #3: the GDPR detector was too blunt (found by its own test)

**What the prototype produced.** The sensitive-request detector matched the
bare word "GDPR". So the *legitimate* lead `"We need help with GDPR compliance
marketing."` was flagged `sensitive_request` — which the guardrails would
route to privacy/compliance, removing a real sales opportunity from the
pipeline. The test suite caught this before any fixture run did
(`test_gdpr_request` asserted the opposite behavior).

**Why it's bad.** Over-triggering a legal flag is a silent business failure:
the lead looks "handled" while a real opportunity disappears.

**The fix.** The detector now requires request-phrasing (e.g. `delete ... data`,
`right to be forgotten`, `Article 17`, `erasure request`) — a mere mention of
GDPR/CCPA no longer triggers. L-019 still routes correctly.

**Guard test:** `tests/test_signals.py::test_gdpr_request`.

---

## Bad output #4: an obedient LLM could still flip a decision on an injection row

**What the prototype produced.** In an early design, an LLM verdict on the
prompt-injection row (L-006) was confidence-capped (≤ 0.60) but the *decision
itself* could still be the injected one. A "QUALIFY" from an obedient model
would survive at 0.60.

**How it was detected.** The adversarial test
`test_llm_unverifiable_claim_cannot_override_form_budget` fed the engine a
simulated obedient LLM verdict and asserted the decision must not be QUALIFY.
It failed.

**The fix.** `make_verdict` now discards LLM verdicts entirely on
`injection_attempt` rows and decides them deterministically on verifiable
form fields. Rule: *if the text the model read contains instructions aimed at
the model, its judgment on that row is compromised by definition.*

**Guard tests:** `test_injection_discards_llm_verdict_entirely`,
`test_injection_cannot_flip_decision_and_caps_confidence`.

---

## Also documented: a misread of our own security boundary (process failure)

During verification we grepped `sanitized_leads.json` for the injection
string and got a hit, which looked like a boundary leak. It was not: the
boundary is **deobfuscation + projection control** (no raw un-decoded forms,
no forensics section reaches the AI view), not content redaction — the AI
must see the cleaned message as quoted untrusted data in `<lead_data>` in
order to judge it, with guardrails as the safety net. We corrected the
verification (checking for `<div>`, `&amp;`, `forensics`, zero-width chars
instead) and wrote it down here because an engineer who "fixes" a test by
quietly changing the assertion is worse than one who documents the confusion.

---

## What stays fragile (honest limits)

- Email classification is list-based: a domain that is neither free nor
  disposable is called "corporate". So `marcus.lee.2027@stateuniv.edu`
  (a university) and `asdf@asdf.com` (a placeholder) both render as
  "corporate". **No fixture decision depended on this** — L-002 was rejected
  on career intent and L-010 on junk heuristics — but at real volume an
  academic/placeholder-domain tier would earn its keep. Documented rather
  than quietly patched, because the honest scope line matters more than the
  cosmetic fix.
- The deterministic intent/language heuristics are keyword-based. They will
  misread sarcasm, unusual phrasing, and languages other than English/Spanish.
  The LLM adapter exists exactly for this; without a key, confidences are
  capped lower and everything is escalated or human-reviewed at the margins.
- The junk detector is multi-signal by design, but a sufficiently
  well-written spam message with a real-looking budget could pass as QUALIFY.
  The reviewer's "Not Approved" button is the control that matters.
- Budget thresholds ($1k floor / $5k entry / $10k qualify band) are **assumed**,
  not measured against Single Grain's real books — they are disclosed as
  assumptions, not presented as facts.
- Enrichment (People Data Labs / live website fetch) is designed as a
  pluggable adapter but not active in the demo run; verdicts rest on form
  data only, and the confidence adjustments reflect that honestly.
