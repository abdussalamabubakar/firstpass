"""Verdict engine — recommendation + guardrails.

Two sources feed a verdict:

1. The deterministic scoring engine below (always available, fully
   transparent, no API key needed).
2. An optional LLM pass (see llm.py) whose JSON output enters through the
   same ``make_verdict`` path.

Either way, EVERY verdict passes through :func:`apply_guardrails`, the final
gate that enforces the hierarchy:

    legal/safety flags  >  junk/conflict/identity  >  business signals

No claim inside a lead (budget numbers, "CEO approved", "unlimited budget")
can override a safety flag, and an ``injection_attempt`` flag caps
confidence and voids message-derived claims. The verdict is a
RECOMMENDATION for a human reviewer, never a final decision.
"""

from __future__ import annotations

from .signals import LOCATIONS_RE, detect_intent

DECISIONS = ("QUALIFY", "NURTURE", "REJECT", "ESCALATE")

# Human-review vocabulary (the three reviewer buttons in the card UI).
REVIEW_RECOMMENDATION = {
    "QUALIFY": "Approved",
    "NURTURE": "Not Approved (nurture)",
    "REJECT": "Not Approved",
    "ESCALATE": "Not Enough",
}

# ICP thresholds — internal assumptions, disclosed in the write-up.
SERVICE_FLOOR_USD = 1_000      # below this we don't take the engagement
QUALIFY_BAND_USD = 10_000      # comfortable agency retainer range
ENTRY_BAND_USD = 5_000         # entry retainer range

BASE_CONFIDENCE = {
    "QUALIFY": 0.85,
    "NURTURE": 0.70,
    "REJECT": 0.80,
    "ESCALATE": 0.80,
}


def _fmt_usd(value: int) -> str:
    return f"${value:,}"


def revenue_estimate(record: dict) -> dict:
    """Rough company-revenue estimate, honestly labeled.

    Every number this agent emits is labeled Observed / Estimated /
    Assumed. These are Assumed: they come from transparent multipliers,
    not from observed financials. When the data cannot support an
    estimate, we say so instead of inventing one.
    """
    message = record["message"]
    employees = None
    import re

    match = re.search(r"([\d][\d,]{1,})\s*[-\u2013]?\s*(?:employee|staff|people|person)", message, re.I)
    if match:
        employees = int(match.group(1).replace(",", ""))
    elif "fortune 500" in message.lower():
        employees = 1000

    budget = record["monthly_budget_usd"]

    if employees and employees >= 10:
        low, high = employees * 150_000, employees * 300_000
        return {
            "low_usd": low, "high_usd": high, "label": "Assumed",
            "basis": f"{employees:,} employees (stated in message) x assumed $150k-$300k revenue/employee",
        }
    if budget and budget >= 1_000:
        annual_spend = budget * 12
        return {
            "low_usd": annual_spend * 8, "high_usd": annual_spend * 20,
            "label": "Assumed",
            "basis": f"{_fmt_usd(budget)}/mo stated agency budget x assumed 5-12% of revenue spent on marketing",
        }
    return {
        "low_usd": None, "high_usd": None, "label": "Not estimable",
        "basis": "insufficient data (no size signal and no usable budget)",
    }


def _completeness_adjustment(record: dict, intent: str) -> float:
    adj = 0.0
    if record["email_class"] in ("missing", "invalid"):
        adj -= 0.10
    if not record["message"].strip():
        adj -= 0.15
    if not record["submitted_at_valid"]:
        adj -= 0.05
    if intent == "none":
        adj -= 0.10
    return adj


def _budget_band_verdict(record: dict) -> dict:
    """Business-logic scoring: budget band x intent x growth signals."""
    budget = record["monthly_budget_usd"]
    flag = record["budget_flag"]
    intent, intent_score = record["intent"], record["intent_score"]
    flags = set(record["flags"])

    if "competitor_fishing" in flags:
        disposable = record["email_class"] == "disposable"
        return {
            "decision": "REJECT",
            "confidence": 0.75 if disposable else 0.65,
            "reason": (
                "Message reads as peer-agency/competitive research rather than buying intent"
                + (" and the email is disposable" if disposable else "")
                + " — not a sales call; a human can route it to partnerships if desired."
            ),
        }

    if intent == "career":
        return {
            "decision": "REJECT",
            "confidence": 0.90,
            "reason": "No commercial intent — this is a career/mentorship request, not a lead.",
        }

    # Injection attempts void message-derived claims: score form fields only.
    if "injection_attempt" in flags:
        if budget is not None and budget < SERVICE_FLOOR_USD:
            return {
                "decision": "REJECT",
                "confidence": 0.60,
                "reason": (
                    f"Stated budget {_fmt_usd(budget)}/mo is below the service floor and the "
                    "message carried a manipulation attempt, so its claims (unlimited budget, "
                    "pre-approved contract) were discarded as unverified."
                ),
            }
        return {
            "decision": "NURTURE",
            "confidence": 0.60,
            "reason": (
                "Message contained a manipulation attempt; message-level evidence was discarded "
                "and the verifiable form fields alone do not support qualification."
            ),
        }

    if budget is None:
        if flag == "unspecified" and intent_score >= 1:
            return {
                "decision": "NURTURE",
                "confidence": 0.70,
                "reason": "Real commercial interest but budget unspecified — needs a discovery/education call before qualification.",
            }
        if intent_score >= 1:
            return {
                "decision": "NURTURE",
                "confidence": 0.60,
                "reason": "Commercial interest present but the budget field is unusable — needs human follow-up to establish budget.",
            }
        return {
            "decision": "REJECT",
            "confidence": 0.60,
            "reason": "No usable budget signal and no clear commercial intent in the message.",
        }

    if budget == 0:
        return {
            "decision": "REJECT",
            "confidence": 0.85,
            "reason": "Stated budget is zero — no commercial engagement available today.",
        }

    if budget >= QUALIFY_BAND_USD:
        base = 0.85 + (0.05 if intent_score >= 3 else 0.0)
        return {
            "decision": "QUALIFY",
            "confidence": base,
            "reason": f"Strong commercial intent with stated budget {_fmt_usd(budget)}/mo, inside the typical retainer range for an agency of this profile.",
        }

    if budget >= ENTRY_BAND_USD:
        if intent_score >= 1:
            return {
                "decision": "QUALIFY",
                "confidence": 0.70,
                "reason": f"Stated budget {_fmt_usd(budget)}/mo in the entry-retainer range with a clear ask — worth a discovery call.",
            }
        return {
            "decision": "NURTURE",
            "confidence": 0.65,
            "reason": f"Budget {_fmt_usd(budget)}/mo is serviceable but the message gives little intent evidence — nurture before spending sales time.",
        }

    if budget >= SERVICE_FLOOR_USD:
        notes = []
        if record["growth_signal"]:
            notes.append("clear growth signal suggests budget will grow")
        locations = LOCATIONS_RE.search(record["message"])
        if locations:
            notes.append(f"{locations.group(1)} locations broaden the engagement scope")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        return {
            "decision": "NURTURE",
            "confidence": 0.75 if record["growth_signal"] else 0.70,
            "reason": f"Stated budget {_fmt_usd(budget)}/mo is below typical retainers but the intent is real{suffix} — nurture and revisit.",
        }

    return {
        "decision": "REJECT",
        "confidence": 0.75,
        "reason": f"Stated budget {_fmt_usd(budget)}/mo is below the service floor for the engagement required.",
    }


def _thread_context(record: dict) -> str:
    for flag in record["flags"]:
        if flag.startswith("thread_followup_of:"):
            parent = flag.split(":", 1)[1]
            return f"Thread follow-up from the same contact as {parent}: "
        if flag.startswith("duplicate_of:"):
            parent = flag.split(":", 1)[1]
            return f"Duplicate submission linked to {parent}: "
    return ""


def deterministic_verdict(record: dict) -> dict:
    """Full deterministic verdict: scoring + briefing + revenue estimate."""
    core = _budget_band_verdict(record)
    confidence = core["confidence"] + _completeness_adjustment(record, record["intent"])
    core["confidence"] = round(min(0.95, max(0.05, confidence)), 2)

    # Empty-message honesty override: the budget-band templates speak of
    # "commercial intent" that an empty message cannot express. Say plainly
    # what the decision actually rests on.
    if not record["message"].strip() and core["decision"] in ("QUALIFY", "NURTURE"):
        budget = record["monthly_budget_usd"]
        core["reason"] = (
            "No message content was submitted — this decision rests only on the verifiable "
            f"form fields (stated budget {_fmt_usd(budget)}/mo, {record['email_class']} email, "
            "website provided); confirm the actual needs on first contact."
        )

    thread_prefix = _thread_context(record)
    if thread_prefix:
        core["reason"] = thread_prefix + core["reason"][0].lower() + core["reason"][1:]

    core["briefing"] = _briefing(record, core["decision"])
    core["revenue_estimate"] = revenue_estimate(record)
    core["escalation_route"] = None
    return core


def _briefing(record: dict, decision: str) -> list[str]:
    bullets = []
    company = record["company"] or "(company not given)"
    bullets.append(
        f"Company: {company} — {record['email_class']} email"
        + (f" ({record['email_domain']})" if record["email_domain"] else "")
        + f", website {record['website_host'] or 'not provided'}"
        + f", submitted via {record['source']}."
    )
    if record["monthly_budget_usd"] is not None:
        bullets.append(
            f"Stated budget: {_fmt_usd(record['monthly_budget_usd'])}/mo (Observed — from the form field)."
        )
    else:
        bullets.append(
            f"Stated budget: not usable (form field said '{record['budget_raw'] or 'empty'}')."
        )
    est = revenue_estimate(record)
    if est["low_usd"]:
        bullets.append(
            f"Estimated company revenue: {_fmt_usd(est['low_usd'])}-{_fmt_usd(est['high_usd'])}/yr "
            f"({est['label']} — {est['basis']})."
        )
    else:
        bullets.append(f"Estimated company revenue: not estimable ({est['basis']}).")

    if decision == "QUALIFY":
        bullets.append("Single Grain fit: inside ICP — budget and intent support a sales conversation.")
    elif decision == "NURTURE":
        bullets.append("Single Grain fit: real but early — keep warm in nurture rather than spending sales time now.")
    elif decision == "REJECT":
        bullets.append("Single Grain fit: outside ICP today — no sales follow-up recommended.")
    else:
        bullets.append("Single Grain fit: a human needs to resolve open questions before any sales step.")

    trap_flags = [f for f in record["flags"] if not f.startswith(("missing_field", "budget:", "invalid_field", "insecure"))]
    bullets.append("Risk flags: " + (", ".join(trap_flags) if trap_flags else "none") + ".")
    return bullets


def apply_guardrails(verdict: dict, record: dict) -> dict:
    """The final gate. Enforces the safety hierarchy on ANY verdict source.

    Order: legal/safety > junk > conflict > identity. Injection attempts
    cap confidence and void message claims regardless of the decision.
    """
    flags = set(record["flags"])
    v = dict(verdict)

    if "sensitive_request" in flags:
        v.update(
            decision="ESCALATE",
            escalation_route="privacy_compliance",
            confidence=max(v.get("confidence", 0.0), 0.95),
            reason="Legal/data-protection request (possible GDPR erasure), not a sales lead — route to privacy/compliance and pause all marketing processing for this contact.",
        )
    elif "security_suspicious" in flags:
        v.update(
            decision="ESCALATE",
            escalation_route="security",
            confidence=max(v.get("confidence", 0.0), 0.95),
            reason="Suspected phishing/malware delivery in the submission — route to security; do not reply, click, download, or enrich this row.",
        )
    elif "junk" in flags:
        v.update(
            decision="REJECT",
            escalation_route=None,
            confidence=max(v.get("confidence", 0.0), 0.80),
            reason="Submission matches junk heuristics (placeholder identity, low-signal message, implausible budget) — no sales follow-up.",
        )
    elif "signal_conflict" in flags:
        v.update(
            decision="ESCALATE",
            escalation_route="signal_verification",
            confidence=max(v.get("confidence", 0.0), 0.80),
            reason="Conflicting signals (stated budget contradicts company profile) — a human should verify the data before any sales decision.",
        )

    if v.get("decision") == "QUALIFY" \
            and (record["monthly_budget_usd"] or 0) >= QUALIFY_BAND_USD \
            and record["email_class"] in ("free", "invalid", "missing") \
            and not record["website_host"]:
        v.update(
            decision="ESCALATE",
            escalation_route="identity_verification",
            confidence=max(v.get("confidence", 0.0), 0.80),
            reason="High stated budget but unverifiable identity (free-mail address, no company/website) — verify who this is before committing sales time.",
        )

    if "injection_attempt" in flags:
        v["confidence"] = round(min(v.get("confidence", 0.6), 0.60), 2)
        if "manipulation attempt" not in v.get("reason", "").lower():
            v["reason"] = v.get("reason", "").rstrip(".") + ". Message contained a manipulation attempt; its claims were treated as unverified."

    v["decision"] = v["decision"] if v.get("decision") in DECISIONS else "ESCALATE"
    v["review_recommendation"] = REVIEW_RECOMMENDATION[v["decision"]]
    return v


def make_verdict(record: dict, llm_verdict: dict | None = None) -> dict:
    """Produce the final verdict for one sanitized record.

    ``llm_verdict`` (when the LLM adapter ran) enters through the same
    guardrail gate as the deterministic result — it is never trusted
    blindly. With no LLM verdict, the deterministic engine decides.

    Injection rows are always decided deterministically: the LLM verdict is
    discarded for them, because the message that LLM read contained
    instructions aimed at it, so its judgment on that row is compromised.
    """
    injection_row = "injection_attempt" in set(record["flags"])
    if llm_verdict is not None and not injection_row:
        base = _normalize_llm_verdict(llm_verdict, record)
    else:
        base = deterministic_verdict(record)
    return apply_guardrails(base, record)


def _normalize_llm_verdict(raw: dict, record: dict) -> dict:
    """Schema-validate an LLM verdict; fall back to deterministic on garbage."""
    try:
        decision = str(raw["decision"]).upper()
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        confidence = float(raw["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence out of range")
        reason = str(raw["reason"]).strip()
        if not reason:
            raise ValueError("empty reason")
        briefing = [str(b) for b in raw.get("briefing", [])][:10]
        return {
            "decision": decision,
            "confidence": round(confidence, 2),
            "reason": reason,
            "briefing": briefing or deterministic_verdict(record)["briefing"],
            "revenue_estimate": raw.get("revenue_estimate") or revenue_estimate(record),
            "escalation_route": raw.get("escalation_route"),
        }
    except (KeyError, ValueError, TypeError):
        return deterministic_verdict(record)
