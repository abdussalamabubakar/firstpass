"""Security boundary: raw lead data is dirty by default.

Every byte arriving from the CSV is treated as untrusted input. This module
deobfuscates and normalizes it BEFORE any detection logic or the AI layer
sees it, so payloads hidden in HTML entities, tags, zero-width characters,
control characters, or unicode tricks cannot evade the trap detectors.

Two projections come out of this stage:

- ``ai_view(record)`` — the ONLY thing the verdict engine/AI layer may read.
  It contains no raw field values, so adversarial raw text never reaches the
  model context.
- ``record["forensics"]`` — raw values + what was removed/decoded, written to
  trace files so a human can audit exactly what the sanitizer did.

The verdict engine's signature accepts sanitized records only; there is no
parameter through which raw CSV text can enter the AI layer.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Deobfuscation machinery
# ---------------------------------------------------------------------------

# Entity references: numeric (&#8203;), hex (&#x200B;), and named (&amp;).
ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")
TAG_RE = re.compile(r"<[^>]{0,2048}>")
# Zero-width and invisible characters (ZWSP, ZWNJ, ZWJ, LRM, RLM, word
# joiner, invisible operators, BOM, soft hyphen).
INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\u2060-\u2064\ufeff\u00ad]")
# Unicode bidi controls — can visually reorder text to hide intent.
BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")
# C0/C1 control characters (except \n and \t, collapsed later anyway).
CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
WS_RUN_RE = re.compile(r"[^\S\n]+")

# Scheme'd URLs including the "hxxp://" evasion spelling; plus www.* forms.
URL_RE = re.compile(r"(?i)\b(?:h(?:tt|xx)ps?|ftp)://[^\s<>\"')\]]+|\bwww\.[^\s<>\"')\]]+")

DANGEROUS_PATH_EXTS = {
    ".exe", ".zip", ".scr", ".bat", ".cmd", ".msi", ".vbs", ".js",
    ".rar", ".7z", ".iso", ".dll", ".lnk", ".apk", ".dmg", ".hta",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.de", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "comcast.net", "verizon.net",
}

DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "yopmail.com",
    "temp-mail.org", "tempmail.com", "trashmail.com", "sharklasers.com",
    "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "inboxbear.com", "spam4.me", "grr.la", "mytemp.email",
}

BUDGET_SKIP_MARKERS = {"discuss", "tbd", "n/a", "na", "not sure", "unknown", "later", "-"}

BUDGET_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kkmM]?),?$")

FRESH_STATS = {
    "entities_decoded": 0,
    "tags_stripped": 0,
    "invisible_removed": 0,
    "bidi_removed": 0,
    "control_removed": 0,
}


def sanitize_text(text: str, stats: dict | None = None) -> str:
    """Deobfuscate + normalize one free-text field.

    Order matters: entities are decoded BEFORE tags are stripped, so
    ``&lt;script&gt;`` dies here too, and decoding runs repeatedly so
    double-encoded payloads cannot survive. Invisible-character removal
    happens after decoding because entities can encode invisible chars.
    Detection layers downstream therefore always see fully decoded text.
    """
    stats = dict(FRESH_STATS) if stats is None else stats
    for key, value in FRESH_STATS.items():
        stats.setdefault(key, value)
    if text is None:
        return ""
    current = str(text)

    for _ in range(3):  # bounded loop: decode -> strip, until stable
        n_entities = len(ENTITY_RE.findall(current))
        n_tags = len(TAG_RE.findall(current))
        if n_entities:
            stats["entities_decoded"] += n_entities
            current = html.unescape(current)
        if n_tags:
            stats["tags_stripped"] += n_tags
            current = TAG_RE.sub("", current)
        if n_entities == 0 and n_tags == 0:
            break

    current, n_inv = INVISIBLE_RE.subn("", current)
    stats["invisible_removed"] += n_inv
    current, n_bidi = BIDI_RE.subn("", current)
    stats["bidi_removed"] += n_bidi
    current = unicodedata.normalize("NFKC", current)
    current, n_ctl = CONTROL_RE.subn("", current)
    stats["control_removed"] += n_ctl
    current = current.replace("\n", " ")
    current = WS_RUN_RE.sub(" ", current).strip()
    return current


def extract_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:") for m in URL_RE.finditer(text or "")]


def normalize_url(raw_url: str) -> dict:
    """Normalize a URL and assess it for danger. Never fetches anything."""
    info = {
        "raw": raw_url,
        "normalized": raw_url,
        "scheme": None,
        "host": None,
        "path": None,
        "dangerous_ext": False,
        "non_ascii_host": False,
        "rewrites": [],
    }
    url = raw_url.strip()
    scheme_match = re.match(r"(?i)^(h(?:tt|xx)ps?|ftp)://", url)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        if scheme.startswith("hxx"):
            info["rewrites"].append("obfuscated 'hxxp' scheme spelling")
            scheme = scheme.replace("xx", "tt")
        info["scheme"] = scheme
        url = scheme + "://" + url[scheme_match.end():]
    elif re.match(r"(?i)^www\.", url):
        info["scheme"] = "http"
        url = "http://" + url

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path or ""
    except ValueError:
        return info

    if host:
        if any(ch > "\x7f" for ch in host):
            info["non_ascii_host"] = True
            info["rewrites"].append("non-ascii (possible homoglyph) hostname")
        info["host"] = host
    last_segment = path.rstrip("/").rsplit("/", 1)[-1].lower()
    for ext in DANGEROUS_PATH_EXTS:
        if last_segment.endswith(ext):
            info["dangerous_ext"] = True
            break
    info["path"] = path
    info["normalized"] = url
    return info


def classify_email(addr: str) -> tuple[str, str | None]:
    """Return (class, domain): missing | invalid | disposable | free | corporate."""
    if not addr or not addr.strip():
        return "missing", None
    addr = addr.strip().lower()
    if "@" not in addr:
        return "invalid", None
    local, _, domain = addr.rpartition("@")
    if not local or "." not in domain or " " in addr:
        return "invalid", None
    if domain in DISPOSABLE_EMAIL_DOMAINS:
        return "disposable", domain
    if domain in FREE_EMAIL_DOMAINS:
        return "free", domain
    return "corporate", domain


def parse_budget(raw: str) -> tuple[int | None, str]:
    """Parse the monthly-budget field.

    Returns (value_usd_or_None, flag) where flag is one of:
    ok | missing | unspecified | non_numeric | implausible.
    A value of 0 is meaningful and is returned as 0/"ok".
    """
    if raw is None or not str(raw).strip():
        return None, "missing"
    s = str(raw).strip().lower().replace("$", "").replace(",", "").replace("_", "")
    if any(marker in s for marker in BUDGET_SKIP_MARKERS):
        return None, "unspecified"
    m = BUDGET_RE.match(s)
    if not m:
        return None, "non_numeric"
    value = float(m.group(1))
    multiplier = {"k": 1_000, "m": 1_000_000, "": 1}[m.group(2).lower()]
    value = int(value * multiplier)
    if value >= 500_000:
        return value, "implausible"
    return value, "ok"


def parse_timestamp(raw: str) -> tuple[str | None, bool]:
    """Strict ISO-8601 validation. Returns (clean_iso_or_None, valid)."""
    if raw is None or not str(raw).strip():
        return None, False
    s = str(raw).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        return None, False
    return dt.isoformat().replace("+00:00", "Z"), True


def sanitize_lead(row: dict) -> dict:
    """Turn one raw CSV row into a sanitized record.

    The returned record carries clean business fields, data-quality flags,
    per-field sanitization stats, and a ``forensics`` section holding the
    raw values. Use :func:`ai_view` before handing this to the AI layer.
    """
    raw = {k: ("" if v is None else str(v)) for k, v in row.items()}
    stats = dict(FRESH_STATS)

    name = sanitize_text(raw.get("name", ""), stats)
    company = sanitize_text(raw.get("company", ""), stats)
    email = sanitize_text(raw.get("email", ""), stats).lower()
    message = sanitize_text(raw.get("message", ""), stats)
    website = sanitize_text(raw.get("website", ""), stats)
    source = sanitize_text(raw.get("source", ""), stats) or "unknown"

    submitted_at, ts_valid = parse_timestamp(raw.get("submitted_at", ""))

    email_class, email_domain = classify_email(email)

    website_host = None
    website_scheme = None
    if website:
        site_info = normalize_url(website if "://" in website else "https://" + website)
        website_host = site_info["host"]
        website_scheme = site_info["scheme"]

    budget_value, budget_flag = parse_budget(raw.get("monthly_budget_usd", ""))

    urls = [normalize_url(u) for u in extract_urls(message)]

    flags = []
    if email_class == "missing":
        flags.append("missing_field:email")
    if not company.strip():
        flags.append("missing_field:company")
    if not website.strip():
        flags.append("missing_field:website")
    if not message.strip():
        flags.append("missing_field:message")
    if not ts_valid:
        flags.append("invalid_field:submitted_at")
    if budget_flag != "ok":
        flags.append(f"budget:{budget_flag}")
    if website_scheme == "http":
        flags.append("insecure_website_scheme")

    record = {
        "lead_id": sanitize_text(raw.get("lead_id", "")),
        "source": source,
        "submitted_at": submitted_at,
        "submitted_at_valid": ts_valid,
        "name": name,
        "email": email,
        "email_class": email_class,
        "email_domain": email_domain,
        "company": company,
        "website": website,
        "website_host": website_host,
        "website_scheme": website_scheme,
        "monthly_budget_usd": budget_value,
        "budget_raw": sanitize_text(raw.get("monthly_budget_usd", "")),
        "budget_flag": budget_flag,
        "message": message,
        "urls": urls,
        "flags": flags,
        "sanitization": stats,
        "forensics": {
            "raw": {
                "lead_id": raw.get("lead_id", ""),
                "submitted_at": raw.get("submitted_at", ""),
                "name": raw.get("name", ""),
                "email": raw.get("email", ""),
                "company": raw.get("company", ""),
                "website": raw.get("website", ""),
                "monthly_budget_usd": raw.get("monthly_budget_usd", ""),
                "message": raw.get("message", ""),
                "source": raw.get("source", ""),
            },
        },
    }
    return record


def ai_view(record: dict) -> dict:
    """Project a sanitized record down to the AI-facing view.

    This is the enforceable security boundary: the projection drops the
    forensics section, so raw adversarial text cannot enter the model
    context through it.
    """
    return {k: v for k, v in record.items() if k != "forensics"}
