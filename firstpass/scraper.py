"""Polite, bounded website scraper for company research.

Style follows the author's books-scraper (requests + BeautifulSoup4, polite
by design, retries with exponential backoff, CSV/JSON output) adapted to
company research: enter a company's website, compile what it publishes, and
hand the compiled data to the verdict pipeline.

Safety rules — non-negotiable, tested in tests/test_scraper.py:

- SSRF guard: only http/https, no embedded credentials, and every resolved
  address must be public (private / loopback / link-local / reserved are
  refused), so the tool can never be pointed at infrastructure.
- The L-020 rule: URLs with dangerous extensions (.exe, .zip, ...) are never
  fetched — the same denylist the sanitizer uses.
- robots.txt is fetched and honored; unreachable robots.txt is treated as
  "no restrictions discoverable".
- Hard bounds: single domain, max pages, per-page byte cap, request timeout,
  polite delay between requests.

The scraper is an INGESTION ADAPTER, not a parallel path: `build_lead_record`
emits a row in the exact fixture schema, so scraped content flows through the
same pipeline — sanitize → trap signals → guardrails → verdict — and a
malicious website planting "ignore all previous instructions" is caught by
the same machinery as the fixture's L-006.
"""

from __future__ import annotations

import csv
import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .sanitize import DANGEROUS_PATH_EXTS, normalize_url

USER_AGENT = "firstpass-lead-triage/1.0 (polite local research tool)"
MAX_PAGE_BYTES = 1_000_000
DEFAULT_MAX_PAGES = 6
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 1.0
TEXT_PREVIEW_CHARS = 600
MESSAGE_CHARS = 1800

CANDIDATE_PATHS = [
    "/about", "/about-us", "/pricing", "/services", "/products",
    "/contact", "/careers", "/blog",
]
SLUG_KEYWORDS = ("about", "pricing", "service", "product", "contact",
                 "career", "team", "blog", "company", "what-we-do")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SOCIAL_HOSTS = ("linkedin.com", "twitter.com", "x.com", "github.com")


class UnsafeUrlError(ValueError):
    """The URL fails a safety check and will not be fetched."""


class BlockedByRobotsError(RuntimeError):
    """robots.txt disallows fetching this URL."""


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

def validate_url(url: str, resolver=socket.getaddrinfo) -> str:
    """Validate and normalize the entry URL. Returns scheme://host origin.

    Raises UnsafeUrlError for anything we refuse to fetch.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme {parts.scheme!r} not allowed (use http/https)")
    if not parts.hostname:
        raise UnsafeUrlError("no hostname in URL")
    if "@" in (parts.netloc or ""):
        raise UnsafeUrlError("embedded credentials in URL not allowed")

    host = parts.hostname
    # Literal IPs are checked directly; names are resolved so that a DNS name
    # pointing INTO the private range cannot smuggle an internal target.
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
        try:
            infos = resolver(host, None)
        except (socket.gaierror, OSError) as exc:
            raise UnsafeUrlError(f"could not resolve host {host!r}: {exc}") from exc
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        raise UnsafeUrlError(f"could not determine any address for host {host!r}")
    for ip in addresses:
        if not ip.is_global:
            raise UnsafeUrlError(
                f"host {host!r} resolves to a non-public address ({ip}) — "
                "internal infrastructure is never fetched")
    return f"{parts.scheme}://{parts.netloc}"


def assert_fetchable(url: str) -> None:
    """The L-020 rule: URLs with dangerous extensions are never fetched."""
    info = normalize_url(url)
    if info["dangerous_ext"]:
        raise UnsafeUrlError(
            f"refusing to fetch {url!r} — executable/installer path (never fetched)")
    if urlsplit(url).scheme not in ("http", "https"):
        raise UnsafeUrlError(f"refusing to fetch non-http scheme: {url!r}")


class Robots:
    """robots.txt gate for one origin; unreachable robots = no known limits."""

    def __init__(self, origin: str, session: requests.Session, timeout: float):
        self.parser: urllib.robotparser.RobotFileParser | None = None
        url = origin + "/robots.txt"
        try:
            resp = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                self.parser = urllib.robotparser.RobotFileParser()
                self.parser.parse(resp.text.splitlines())
        except requests.RequestException:
            self.parser = None

    def allowed(self, url: str) -> bool:
        if self.parser is None:
            return True
        return self.parser.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# Fetching (retries with exponential backoff, like the books-scraper)
# ---------------------------------------------------------------------------

RETRY_STATUS = (429, 500, 502, 503, 504)


def _fetch(url: str, session: requests.Session, timeout: float, stats: dict) -> requests.Response:
    assert_fetchable(url)
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        stats["attempts"] += 1
        try:
            resp = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT},
                               allow_redirects=True)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise
        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            last_error = requests.HTTPError(f"HTTP {resp.status_code} from {url}")
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        if resp.status_code in RETRY_STATUS:
            resp.raise_for_status()
        return resp
    raise last_error or requests.RequestException(f"unreachable: {url}")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_page(html_text: str, url: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True)[:200] if soup.title else ""
    meta_desc = ""
    for key in ("description", "og:description"):
        tag = (soup.find("meta", attrs={"name": key})
               or soup.find("meta", attrs={"property": key}))
        if tag and tag.get("content", "").strip():
            meta_desc = tag["content"].strip()
            break
    og_site = ""
    tag = soup.find("meta", attrs={"property": "og:site_name"})
    if tag and tag.get("content", "").strip():
        og_site = tag["content"].strip()

    headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        text = h.get_text(" ", strip=True)
        if text and text not in headings:
            headings.append(text[:120])
        if len(headings) >= 25:
            break

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    emails: set[str] = set(EMAIL_RE.findall(text))
    for a in soup.select('a[href^="mailto:"]'):
        candidate = a["href"].split("?", 1)[0].replace("mailto:", "").strip()
        if candidate:
            emails.add(candidate)

    socials: set[str] = set()
    for a in soup.select("a[href]"):
        href = a["href"].lower()
        if any(host in href for host in SOCIAL_HOSTS):
            socials.add(a["href"])

    same_domain_links: list[str] = []
    base_host = urlsplit(url).hostname or ""
    for a in soup.select("a[href]"):
        href = urljoin(url, a["href"]).split("#", 1)[0]
        parts = urlsplit(href)
        if parts.hostname == base_host and parts.path and parts.path != "/":
            if any(k in parts.path.lower() for k in SLUG_KEYWORDS):
                same_domain_links.append(href)

    return {
        "url": url,
        "title": title,
        "description": meta_desc,
        "og_site_name": og_site,
        "headings": headings,
        "emails": sorted(emails)[:10],
        "socials": sorted(socials)[:10],
        "same_domain_links": sorted(set(same_domain_links))[:10],
        "text_chars": len(text),
        "text_preview": text[:TEXT_PREVIEW_CHARS],
        "_text": text[:4000],  # bounded raw text for the lead record
    }


def _derive_signals(pages: list[dict]) -> list[str]:
    signals: list[str] = []
    paths = {urlsplit(p["url"]).path.lower() for p in pages}
    joined = " ".join(p.get("_text", "") for p in pages).lower()
    if any("/career" in p or "/careers" in p for p in paths) or "we are hiring" in joined:
        signals.append("hiring")
    if any("/blog" in p for p in paths):
        signals.append("blog")
    if any("/pricing" in p for p in paths) or "per month" in joined or "pricing" in joined:
        signals.append("public_pricing")
    if any("/contact" in p for p in paths):
        signals.append("contact_page")
    if "login" in joined or "sign in" in joined:
        signals.append("login_portal")
    return signals


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape_site(url: str, *, max_pages: int = DEFAULT_MAX_PAGES,
                delay: float = DEFAULT_DELAY_SECONDS,
                timeout: float = DEFAULT_TIMEOUT_SECONDS,
                resolver=socket.getaddrinfo,
                session: requests.Session | None = None) -> dict:
    """Scrape one company website, politely and within bounds.

    Returns the compiled result dict (also what scraped_data.json holds).
    """
    started = time.perf_counter()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    origin = validate_url(url, resolver=resolver)
    assert_fetchable(url)

    session = session or requests.Session()
    stats = {"attempts": 0}
    robots = Robots(origin, session, timeout)
    host = urlsplit(origin).hostname or ""

    queue: list[str] = [origin + "/"] + [origin + p for p in CANDIDATE_PATHS]
    seen: set[str] = set()
    pages: list[dict] = []
    errors: list[dict] = []

    for target in queue:
        if len(pages) >= max_pages:
            break
        if target in seen:
            continue
        seen.add(target)
        parts = urlsplit(target)
        if parts.hostname != host:  # single-domain rule
            continue
        try:
            assert_fetchable(target)
        except UnsafeUrlError as exc:
            errors.append({"url": target, "reason": str(exc)})
            continue
        if not robots.allowed(target):
            errors.append({"url": target, "reason": "disallowed by robots.txt"})
            continue

        try:
            resp = _fetch(target, session, timeout, stats)
        except UnsafeUrlError as exc:
            errors.append({"url": target, "reason": str(exc)})
            continue
        except requests.RequestException as exc:
            errors.append({"url": target, "reason": f"unreachable after retries: {exc}"})
            continue
        if resp.status_code != 200:
            errors.append({"url": target, "reason": f"HTTP {resp.status_code}"})
            continue
        if len(resp.content) > MAX_PAGE_BYTES:
            errors.append({"url": target, "reason": "page exceeds size cap"})
            continue

        page = _extract_page(resp.text, str(resp.url))
        pages.append(page)

        for link in page["same_domain_links"]:
            if link not in seen and len(pages) + len(queue) < max_pages * 3:
                queue.append(link)  # discovered links extend the bounded queue

        if delay and len(pages) < max_pages:
            time.sleep(delay)

    emails: set[str] = set()
    socials: set[str] = set()
    company_name = ""
    description = ""
    services: list[str] = []
    for page in pages:
        emails.update(page["emails"])
        socials.update(page["socials"])
        if not company_name and page["og_site_name"]:
            company_name = page["og_site_name"]
        if not description and page["description"]:
            description = page["description"]
        for h in page["headings"]:
            if h not in services:
                services.append(h)

    if not company_name and pages:
        title = pages[0]["title"]
        company_name = title.split("|")[0].split("—")[0].split("-")[0].strip() if title else host

    for page in pages:
        page.pop("_text", None)  # bounded raw text is for the lead record only

    return {
        "url": url,
        "origin": origin,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company_name": company_name,
        "description": description,
        "emails": sorted(emails),
        "socials": sorted(socials),
        "services": services[:20],
        "signals": _derive_signals(pages),
        "pages": pages,
        "errors": errors,
        "stats": {
            "pages_fetched": len(pages),
            "attempts": stats["attempts"],
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        },
    }


# ---------------------------------------------------------------------------
# Ingestion adapter: scraped data -> the fixture-schema lead row
# ---------------------------------------------------------------------------

def build_lead_record(result: dict, lead_id: str | None = None) -> dict:
    """Compile a scrape result into ONE row of the fixture schema.

    The condensed scraped text goes into `message` — raw, untrusted website
    content that the pipeline's sanitizer and injection detectors will treat
    exactly like the fixture's adversarial rows.
    """
    host = urlsplit(result["origin"] if result.get("origin") else result["url"]).hostname or ""
    parts = [
        f"Live website scrape of {result['url']} (compiled {result['fetched_at']}).",
    ]
    if result.get("description"):
        parts.append(f"Site description: {result['description']}")
    if result.get("services"):
        parts.append("Sections/services published: " + "; ".join(result["services"][:12]) + ".")
    if result.get("signals"):
        parts.append("Signals: " + ", ".join(result["signals"]) + ".")
    if result.get("emails"):
        parts.append("Public contact emails: " + ", ".join(result["emails"][:5]) + ".")

    # People Data Labs enrichment (Benchmarked) — the "reviews their LinkedIn"
    # step of the manual workflow, adapter-provided and labeled.
    pdl = result.get("pdl_enrichment") or {}
    enrichment_json = ""
    if pdl and not pdl.get("error"):
        pdl_bits = []
        if isinstance(pdl.get("employee_count"), int):
            pdl_bits.append(f"{pdl['employee_count']:,} employees")
        if pdl.get("industry"):
            pdl_bits.append(f"industry: {pdl['industry']}")
        if isinstance(pdl.get("revenue_estimated_usd"), int):
            pdl_bits.append(f"revenue est. ${pdl['revenue_estimated_usd']:,}/yr")
        if pdl.get("linkedin_url"):
            pdl_bits.append(f"LinkedIn: {pdl['linkedin_url']}")
        if pdl_bits:
            parts.append("People Data Labs (Benchmarked): " + ", ".join(pdl_bits) + ".")
        enrichment_json = json.dumps({
            "employee_count": pdl.get("employee_count"),
            "revenue_estimated_usd": pdl.get("revenue_estimated_usd"),
            "industry": pdl.get("industry"),
            "linkedin_url": pdl.get("linkedin_url"),
            "source": "People Data Labs",
        })
    page_texts = [p.get("text_preview", "") for p in result.get("pages", [])]
    body = " ".join(t for t in page_texts if t).strip()
    message = " ".join(parts).strip()
    if body:
        message = (message + " Page content: " + body)[:MESSAGE_CHARS]

    return {
        "lead_id": lead_id or f"L-SCR-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "name": "",
        "email": (result.get("emails") or [""])[0],
        "company": result.get("company_name") or host,
        "website": result.get("origin") or result.get("url", ""),
        "monthly_budget_usd": "",
        "message": message,
        "source": "live_scrape",
        "enrichment": enrichment_json,
    }


def write_scraped_csv(result: dict, path) -> None:
    """One row per fetched page — the CSV form of the scraped data section."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["page_url", "status_note", "title", "description",
                         "headings", "emails", "text_chars", "text_preview"])
        for page in result.get("pages", []):
            writer.writerow([
                page["url"], "fetched", page["title"], page["description"],
                " | ".join(page["headings"]), " | ".join(page["emails"]),
                page["text_chars"], page["text_preview"],
            ])
        for err in result.get("errors", []):
            writer.writerow([err["url"], err["reason"], "", "", "", "", 0, ""])
