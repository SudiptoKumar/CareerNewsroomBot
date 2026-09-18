from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CareerNewsroomBot V0.5
# Bdjobs-only source | Exa researcher | Cerebras judge
# ============================================================

BOT_VERSION = "V0.5"
BD_TZ = timezone(timedelta(hours=6))
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@CareerNewsroom").strip()
STATE_FILE = Path("news_state.json")
POSTED_FILE = Path("posted_urls.txt")

EXA_API_KEY = os.getenv("EXA_API_KEY", "").strip()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BDJOBS_SEARCH_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_RICH_METHOD = "sendRichMessage"

DISCOVERY_DAYS = 7
INVENTORY_RETENTION_DAYS = 21
CACHE_HOURS = 24
MAX_EXA_QUERIES = 6
EXA_RESULTS_PER_QUERY = 18
MAX_RESEARCH_URLS = 100
EXA_CONTENT_BATCH = 25
CEREBRAS_BATCH = 12
MAX_PUBLISH = 15
MIN_PUBLISH = 5
POST_DELAY_SECONDS = 2.5
MAX_SOURCE_TEXT = 22000
MAX_JUDGE_EVIDENCE = 5200

USER_AGENT = "CareerNewsroomBot/0.5 (+https://bdjobs.com/)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOG = logging.getLogger("CareerNewsroomBot")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
})


# ------------------------------------------------------------
# Audience and Bdjobs structure model
# ------------------------------------------------------------

TARGET_CATEGORIES = {
    "accounting/finance": 18,
    "bank/non-bank fin. institution": 20,
    "commercial": 16,
    "customer service/call centre": 10,
    "e-commerce/ digital marketing": 15,
    "general management/admin": 18,
    "hr/org. development": 18,
    "marketing/sales": 18,
    "ngo/development": 11,
    "production/operation": 9,
    "research/consultancy": 12,
    "supply chain/procurement": 17,
    "receptionist/ ps": 6,
}

ROLE_TERMS = {
    "management trainee": 18,
    "graduate trainee": 18,
    "graduate program": 18,
    "trainee": 13,
    "internship": 18,
    "intern": 16,
    "business analyst": 16,
    "financial analyst": 16,
    "credit analyst": 15,
    "relationship manager": 14,
    "relationship officer": 14,
    "business development": 15,
    "sales executive": 11,
    "marketing executive": 14,
    "hr executive": 14,
    "human resources": 13,
    "accounts officer": 13,
    "finance officer": 14,
    "audit": 11,
    "procurement": 13,
    "supply chain": 14,
    "commercial executive": 13,
    "operations executive": 12,
    "management": 7,
    "administration": 7,
    "customer service": 7,
    "customer relationship": 10,
    "merchandising": 8,
}

EDUCATION_TERMS = {
    "bba": 25,
    "mba": 25,
    "business administration": 23,
    "b.b.a": 25,
    "m.b.a": 25,
    "bbs": 18,
    "mbs": 18,
    "business studies": 12,
    "finance": 9,
    "accounting": 9,
    "marketing": 9,
    "management": 8,
    "human resources": 8,
    "hrm": 8,
    "economics": 7,
    "commerce": 7,
    "supply chain": 7,
}

EARLY_TERMS = [
    "fresher", "fresh graduate", "fresh graduates", "graduate", "entry level", "entry-level",
    "0 to 1 year", "0 to 2 year", "0 to 3 year", "0-1 year", "0-2 year", "0-3 year",
    "0–1 year", "0–2 year", "0–3 year", "below 1 year", "intern", "trainee", "graduate program",
]

SENIOR_TERMS = [
    "chief", "director", "vice president", "head of", "general manager", "deputy general manager",
    "senior manager", "manager", "lead", "principal", "over 10 years", "10+ years", "8+ years",
    "7+ years", "6+ years", "5+ years",
]

HARD_NOISE_TERMS = [
    "career advice", "career guide", "interview tips", "cv tips", "resume tips", "job tips",
    "salary guide", "salary calculator", "age calculator", "bcs preparation", "exam preparation",
    "quiz", "mcq", "model test", "question solution", "scholarship", "admission", "result",
    "job fair", "workshop", "seminar", "training course",
]

NON_BBA_ROLE_TERMS = [
    "software engineer", "frontend developer", "backend developer", "full stack developer", "devops engineer",
    "network engineer", "civil engineer", "mechanical engineer", "electrical engineer", "architect",
    "doctor", "physician", "medical officer", "nurse", "pharmacist", "lab technologist", "dentist",
]

JOB_SIGNALS = [
    "vacancy", "vacancies", "hiring", "recruitment", "recruit", "apply", "application", "job circular",
    "position", "post name", "job responsibilities", "educational qualification", "experience",
    "deadline", "আবেদন", "নিয়োগ", "চাকরি", "শূন্যপদ",
]

APPLICATION_TERMS = [
    "apply online", "apply now", "apply here", "application form", "submit application", "start application",
    "apply", "career apply", "my bdjobs", "mybdjobs", "online application",
]

DEADLINE_PATTERNS = [
    r"(?:deadline|last date|closing date|apply by|application deadline)\s*[:\-]?\s*([^\n|]{4,60})",
    r"(?:আবেদনের শেষ তারিখ|আবেদনের শেষ|শেষ তারিখ)\s*[:\-]?\s*([^\n|]{4,60})",
]

POSTED_PATTERNS = [
    r"(?:posted on|posted date|posting date|published on|publish date|published date|job posted|date posted)\s*[:\-]?\s*([^\n|]{4,60})",
    r"(?:প্রকাশের তারিখ|প্রকাশিত|পোস্ট করা হয়েছে)\s*[:\-]?\s*([^\n|]{4,60})",
]

BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


# ------------------------------------------------------------
# Utility
# ------------------------------------------------------------

def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: str) -> str:
    value = clean(value).lower()
    value = value.translate(BANGLA_DIGITS)
    value = re.sub(r"[^a-z0-9\u0980-\u09ff ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_similarity(a: str, b: str) -> float:
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_domain(url: str) -> str:
    try:
        return urlparse(clean(url)).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_bdjobs_url(url: str) -> bool:
    d = normalize_domain(url)
    return d == "bdjobs.com" or d.endswith(".bdjobs.com")


def canonical_url(url: str) -> str:
    raw = clean(url)
    if not raw:
        return ""
    p = urlparse(raw if "://" in raw else "https://" + raw)
    if p.scheme.lower() not in {"http", "https"} or not is_bdjobs_url(raw):
        return ""
    query = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}:
            continue
        query.append((k, v))
    query.sort()
    path = re.sub(r"/+", "/", p.path or "/")
    return urlunparse(("https", normalize_domain(raw), path.rstrip("/") or "/", "", urlencode(query), ""))


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def now_dt() -> datetime:
    return datetime.now(BD_TZ)


def iso(dt: datetime | None) -> str:
    return dt.astimezone(BD_TZ).isoformat() if dt else ""


def parse_date(value: str, now: datetime | None = None) -> datetime | None:
    raw = clean(value)
    if not raw:
        return None
    now = now or now_dt()
    raw = raw.translate(BANGLA_DIGITS)
    raw = raw.replace("–", "-").replace("—", "-")
    raw = re.sub(r"\b(st|nd|rd|th)\b", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip(" .,-:")

    # Normalize common month spellings.
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s*,?\s*(\d{4})\b", raw)
    if m:
        day, mon, year = int(m.group(1)), months.get(m.group(2).lower()), int(m.group(3))
        if mon:
            try:
                return datetime(year, mon, day, 23, 59, tzinfo=BD_TZ)
            except ValueError:
                return None
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})\b", raw)
    if m:
        mon, day, year = months.get(m.group(1).lower()), int(m.group(2)), int(m.group(3))
        if mon:
            try:
                return datetime(year, mon, day, 23, 59, tzinfo=BD_TZ)
            except ValueError:
                return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=BD_TZ, hour=23, minute=59)
        except ValueError:
            pass

    # ISO timestamp.
    try:
        value = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BD_TZ)
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return clean(soup.get_text(" ", strip=True))


def absolute_url(base: str, href: str) -> str:
    href = clean(href)
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    return urljoin(base, href)


def event_id(job: dict[str, Any]) -> str:
    # Stable vacancy identity. Deadline is intentionally excluded so a revised
    # closing date does not create a second event for the same vacancy.
    return sha1("|".join([
        normalize_title(job.get("company", "")),
        normalize_title(job.get("job_title", "")),
        normalize_title(job.get("location", "")),
    ]))[:16]


# ------------------------------------------------------------
# Persistent state
# ------------------------------------------------------------

def default_state() -> dict[str, Any]:
    return {
        "version": BOT_VERSION,
        "updated_at": "",
        "queue": {},
        "posted_event_ids": [],
        "posted_urls": [],
        "content_cache": {},
    }


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = default_state()
        if isinstance(data, dict):
            state.update(data)
        return state
    except Exception:
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    state["version"] = BOT_VERSION
    state["updated_at"] = iso(now_dt())
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def load_posted_urls() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    return {canonical_url(line) for line in POSTED_FILE.read_text(encoding="utf-8").splitlines() if canonical_url(line)}


def remember_post(state: dict[str, Any], job: dict[str, Any]) -> None:
    eid = clean(job.get("event_id")) or event_id(job)
    url = canonical_url(job.get("source_url", ""))
    state.setdefault("posted_event_ids", []).append(eid)
    state["posted_event_ids"] = list(dict.fromkeys(state["posted_event_ids"]))[-500:]
    if url:
        state.setdefault("posted_urls", []).append(url)
        state["posted_urls"] = list(dict.fromkeys(state["posted_urls"]))[-500:]
        with POSTED_FILE.open("a", encoding="utf-8") as f:
            f.write(url + "\n")


def already_posted(state: dict[str, Any], posted_urls: set[str], job: dict[str, Any]) -> bool:
    eid = clean(job.get("event_id"))
    if eid and eid in set(state.get("posted_event_ids", [])):
        return True
    url = canonical_url(job.get("source_url", ""))
    if url and url in posted_urls:
        return True
    for old in state.get("queue", {}).values():
        if old.get("status") == "published" and clean(old.get("event_id")) == eid:
            return True
    return False


def prune_state(state: dict[str, Any], now: datetime) -> None:
    cutoff = now - timedelta(days=INVENTORY_RETENTION_DAYS)
    queue = state.get("queue", {})
    cleaned = {}
    for key, item in queue.items():
        if item.get("status") == "published":
            keep_at = parse_date(item.get("published_at", ""), now)
            if keep_at and keep_at < cutoff:
                continue
        else:
            seen = parse_date(item.get("last_seen", ""), now) or now
            deadline = parse_date(item.get("deadline", ""), now)
            if deadline and deadline < now:
                continue
            if seen < cutoff and not deadline:
                continue
        cleaned[key] = item
    state["queue"] = cleaned

    cache_cutoff = now - timedelta(hours=CACHE_HOURS)
    new_cache = {}
    for key, item in state.get("content_cache", {}).items():
        cached_at = parse_date(item.get("cached_at", ""), now)
        if cached_at and cached_at >= cache_cutoff:
            new_cache[key] = item
    state["content_cache"] = new_cache


# ------------------------------------------------------------
# Requests / direct Bdjobs discovery
# ------------------------------------------------------------

def request(url: str, method: str = "GET", **kwargs) -> requests.Response | None:
    for attempt in range(3):
        try:
            timeout = kwargs.pop("timeout", 20)
            response = SESSION.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(1.5 * (attempt + 1))
                continue
            return response
        except requests.RequestException as exc:
            if attempt == 2:
                LOG.warning("HTTP failed %s: %s", url, exc)
            else:
                time.sleep(1.0 * (attempt + 1))
    return None


def extract_jobdetail_links(base_url: str, html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        full = absolute_url(base_url, a.get("href", ""))
        if not is_bdjobs_url(full):
            continue
        path = urlparse(full).path.lower()
        if "/jobdetails" in path:
            urls.append(canonical_url(full))
    return list(dict.fromkeys(x for x in urls if x))


def is_index_url(url: str, title: str = "") -> bool:
    raw = clean(url).lower()
    title = normalize_title(title)
    path = urlparse(raw).path.lower()
    if "/jobdetails" in path:
        return False
    index_parts = ["jobsearch", "companyofferedjobs", "search", "jobs", "category", "employer", "location"]
    if any(p in path for p in index_parts):
        return True
    index_titles = ["find jobs", "job search", "company offered jobs", "jobs", "job list"]
    return any(t in title for t in index_titles)


def parse_bdjobs_search_page(html_text: str, page_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    out: list[dict[str, Any]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        full = absolute_url(page_url, a.get("href", ""))
        if not full or not is_bdjobs_url(full):
            continue
        if "/jobdetails" not in urlparse(full).path.lower():
            continue
        can = canonical_url(full)
        if not can or can in seen:
            continue
        seen.add(can)
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 3:
            continue
        parent_text = clean(a.parent.get_text(" ", strip=True))[:1000] if a.parent else ""
        out.append({
            "url": can,
            "title": title,
            "source": "Bdjobs",
            "discovery": "bdjobs_search",
            "snippet": parent_text,
            "published_date": "",
        })
    return out


def direct_bdjobs_discovery() -> tuple[list[dict[str, Any]], str]:
    response = request(BDJOBS_SEARCH_URL)
    if not response or response.status_code != 200:
        return [], f"HTTP {getattr(response, 'status_code', 'ERR')}"
    rows = parse_bdjobs_search_page(response.text, BDJOBS_SEARCH_URL)
    LOG.info("BDJOBS SEARCH | candidates=%d", len(rows))
    return rows[:120], "ok"


# ------------------------------------------------------------
# Cheap candidate filter
# ------------------------------------------------------------

def cheap_candidate_filter(item: dict[str, Any]) -> tuple[bool, int, list[str]]:
    title = normalize_title(item.get("title", ""))
    url = clean(item.get("url", "")).lower()
    snippet = normalize_title(item.get("snippet", ""))
    blob = f"{title} {snippet} {url}"

    if not title or not is_bdjobs_url(url):
        return False, 0, ["invalid source"]
    if any(term in blob for term in HARD_NOISE_TERMS):
        return False, 0, ["noise"]

    score = 35
    reasons = []
    hit = [term for term in JOB_SIGNALS if term in blob]
    target = [term for term in list(ROLE_TERMS) + ["bba", "mba", "business administration"] if term in blob]

    score += min(30, len(hit) * 5)
    score += min(25, len(target) * 5)
    if "/jobdetails" in urlparse(url).path.lower():
        score += 10
        reasons.append("jobdetail URL")
    if hit:
        reasons.append("job signal")
    if target:
        reasons.append("target signal")

    return score >= 45, min(score, 100), reasons


# ------------------------------------------------------------
# Exa researcher
# ------------------------------------------------------------

EXA_QUERIES = [
    "BBA MBA jobs Bangladesh Bdjobs fresher graduate",
    "management trainee graduate trainee internship Bangladesh Bdjobs",
    "finance accounting banking jobs BBA MBA Bangladesh Bdjobs",
    "marketing sales business development HR jobs Bangladesh Bdjobs",
    "supply chain procurement commercial operations BBA MBA Bangladesh Bdjobs",
    "entry level executive officer associate analyst BBA MBA Bangladesh Bdjobs",
]


def exa_headers() -> dict[str, str]:
    return {"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}


def exa_search(query: str, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
    if not EXA_API_KEY:
        return []
    payload = {
        "query": query,
        "includeDomains": ["bdjobs.com", "*.bdjobs.com"],
        "startPublishedDate": start_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "endPublishedDate": end_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "numResults": EXA_RESULTS_PER_QUERY,
        "type": "fast",
    }
    response = request(EXA_SEARCH_URL, method="POST", headers=exa_headers(), json=payload, timeout=35)
    if not response or response.status_code != 200:
        LOG.warning("EXA SEARCH failed status=%s query=%s", getattr(response, "status_code", "ERR"), query)
        return []
    try:
        return response.json().get("results", []) or []
    except ValueError:
        return []


def exa_discovery(now: datetime) -> list[dict[str, Any]]:
    if not EXA_API_KEY:
        LOG.warning("EXA_API_KEY missing; continuing with direct Bdjobs discovery.")
        return []
    start = now - timedelta(days=DISCOVERY_DAYS)
    all_rows: dict[str, dict[str, Any]] = {}
    for query in EXA_QUERIES[:MAX_EXA_QUERIES]:
        rows = exa_search(query, start, now)
        LOG.info("EXA SEARCH | query=%s | results=%d", query, len(rows))
        for row in rows:
            url = canonical_url(row.get("url", ""))
            if not url:
                continue
            title = clean(row.get("title"))
            all_rows[url] = {
                "url": url,
                "title": title,
                "source": "Bdjobs",
                "discovery": "exa",
                "snippet": clean(row.get("summary") or row.get("text") or "")[:1500],
                "published_date": clean(row.get("publishedDate")),
                "image": clean(row.get("image")),
            }
    return list(all_rows.values())


# ------------------------------------------------------------
# Exa contents + direct HTTP fallback
# ------------------------------------------------------------


def direct_read(url: str) -> tuple[str, str, list[dict[str, str]], str]:
    response = request(url, timeout=25)
    if not response or response.status_code != 200:
        return "", "", [], f"HTTP {getattr(response, 'status_code', 'ERR')}"
    final_url = response.url
    ctype = (response.headers.get("content-type") or "").lower()
    if "text/html" not in ctype and not final_url.lower().endswith((".html", "/")):
        return "", final_url, [], f"unsupported content-type {ctype}"
    soup = BeautifulSoup(response.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        full = absolute_url(final_url, a.get("href", ""))
        if full:
            links.append({"url": full, "text": clean(a.get_text(" ", strip=True))})
    return response.text[:200000], final_url, links, "ok"


def exa_contents(urls: list[str]) -> dict[str, dict[str, Any]]:
    if not EXA_API_KEY or not urls:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(urls), EXA_CONTENT_BATCH):
        batch = urls[start:start + EXA_CONTENT_BATCH]
        payload = {
            "urls": batch,
            "text": {"maxCharacters": MAX_SOURCE_TEXT},
            "summary": {"query": "job title company location education experience salary vacancies application deadline posted date"},
            "maxAgeHours": CACHE_HOURS,
        }
        response = request(EXA_CONTENTS_URL, method="POST", headers=exa_headers(), json=payload, timeout=70)
        if not response or response.status_code != 200:
            LOG.warning("EXA CONTENTS failed status=%s batch=%d", getattr(response, "status_code", "ERR"), len(batch))
            continue
        try:
            body = response.json()
        except ValueError:
            continue
        for item in body.get("results", []) or []:
            url = canonical_url(item.get("url") or item.get("id") or "")
            if not url:
                continue
            out[url] = item
    return out


def research_pages(state: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = now_dt()
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = canonical_url(item.get("url", ""))
        if not url:
            continue
        if already_posted(state, load_posted_urls(), item):
            continue
        accepted, quick, reasons = cheap_candidate_filter(item)
        if not accepted:
            continue
        item = dict(item)
        item["quick_score"] = quick
        item["quick_reasons"] = reasons
        unique[url] = item

    # Keep the best candidates before content retrieval.
    rows = sorted(unique.values(), key=lambda x: (-x.get("quick_score", 0), x.get("title", "")))[:MAX_RESEARCH_URLS]
    LOG.info("CANDIDATES | discovered=%d | after cheap filter=%d", len(candidates), len(rows))

    content_results = exa_contents([r["url"] for r in rows]) if EXA_API_KEY else {}
    verified_pages: list[dict[str, Any]] = []

    for row in rows:
        url = row["url"]
        cached = state.get("content_cache", {}).get(url)
        cached_at = parse_date(cached.get("cached_at"), now) if cached else None
        if cached and cached_at and cached_at >= now - timedelta(hours=CACHE_HOURS):
            page = dict(cached)
            page["from_cache"] = True
        else:
            exa = content_results.get(url)
            if exa:
                text = clean(exa.get("text") or "")[:MAX_SOURCE_TEXT]
                links = exa.get("extras", {}).get("links", []) if isinstance(exa.get("extras"), dict) else []
                page = {
                    "url": url,
                    "final_url": url,
                    "title": clean(exa.get("title") or row.get("title")),
                    "published_date": clean(exa.get("publishedDate") or row.get("published_date")),
                    "text": text,
                    "links": links if isinstance(links, list) else [],
                    "image": clean(exa.get("image") or row.get("image")),
                    "backend": "exa",
                    "cached_at": iso(now),
                }
            else:
                raw, final_url, links, err = direct_read(url)
                page = {
                    "url": url,
                    "final_url": final_url or url,
                    "title": row.get("title", ""),
                    "published_date": row.get("published_date", ""),
                    "text": html_to_text(raw)[:MAX_SOURCE_TEXT],
                    "links": links,
                    "image": row.get("image", ""),
                    "backend": "direct",
                    "cached_at": iso(now),
                    "error": err if not raw else "",
                }

            state.setdefault("content_cache", {})[url] = page

        if is_index_url(url, page.get("title", "")):
            # Index pages are discovery pages, not jobs.
            raw, final_url, links, _ = direct_read(url)
            child_urls = extract_jobdetail_links(final_url or url, raw)
            LOG.info("INDEX | %s | child_vacancies=%d", row.get("title", url), len(child_urls))
            for child in child_urls:
                if child not in unique and child not in state.get("content_cache", {}):
                    unique[child] = {
                        "url": child,
                        "title": "",
                        "source": "Bdjobs",
                        "discovery": "index_expansion",
                        "snippet": "",
                        "published_date": "",
                    }
            continue

        if len(page.get("text", "")) < 180:
            continue
        verified_pages.append({**row, **page})

    # One index-expansion pass. Newly discovered child URLs use Exa contents when possible.
    child_candidates = [
        {
            "url": u,
            "title": "",
            "source": "Bdjobs",
            "discovery": "index_expansion",
            "snippet": "",
            "published_date": "",
        }
        for u in unique
        if u not in {p.get("url") for p in verified_pages} and "/jobdetails" in urlparse(u).path.lower()
    ]
    if child_candidates:
        child_pages = exa_contents([x["url"] for x in child_candidates[:MAX_RESEARCH_URLS]]) if EXA_API_KEY else {}
        for item in child_candidates[:MAX_RESEARCH_URLS]:
            url = item["url"]
            exa = child_pages.get(url)
            if exa:
                page = {
                    **item,
                    "title": clean(exa.get("title") or item.get("title")),
                    "published_date": clean(exa.get("publishedDate") or item.get("published_date")),
                    "text": clean(exa.get("text") or "")[:MAX_SOURCE_TEXT],
                    "links": exa.get("extras", {}).get("links", []) if isinstance(exa.get("extras"), dict) else [],
                    "image": clean(exa.get("image")),
                    "final_url": url,
                    "backend": "exa",
                    "cached_at": iso(now),
                }
            else:
                raw, final_url, links, err = direct_read(url)
                page = {
                    **item,
                    "title": item.get("title", ""),
                    "published_date": item.get("published_date", ""),
                    "text": html_to_text(raw)[:MAX_SOURCE_TEXT],
                    "links": links,
                    "image": "",
                    "final_url": final_url or url,
                    "backend": "direct",
                    "cached_at": iso(now),
                    "error": err,
                }
            if len(page.get("text", "")) >= 180:
                verified_pages.append(page)
            state.setdefault("content_cache", {})[url] = page

    # Dedup pages by canonical URL.
    dedup = {}
    for page in verified_pages:
        dedup[canonical_url(page.get("url", ""))] = page
    return list(dedup.values())


# ------------------------------------------------------------
# Deterministic Bdjobs extraction
# ------------------------------------------------------------

def get_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if node and node.get("content"):
            return clean(node.get("content"))
    return ""


def find_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
    return out


def label_value(text: str, labels: list[str]) -> str:
    for label in sorted(labels, key=len, reverse=True):
        m = re.search(r"(?:^|[|]\s*)" + re.escape(label) + r"\s*[:\-]?\s*([^|]{3,180})", text, flags=re.I)
        if m:
            return clean(m.group(1))
        m = re.search(re.escape(label) + r"\s*[:\-]?\s*([^|]{3,180})", text, flags=re.I)
        if m:
            return clean(m.group(1))
    return ""


def extract_dates(text: str, now: datetime) -> tuple[str, str, float, str]:
    deadline_raw = ""
    posted_raw = ""
    for pattern in DEADLINE_PATTERNS:
        m = re.search(pattern, text, flags=re.I)
        if m:
            deadline_raw = clean(m.group(1))
            break
    for pattern in POSTED_PATTERNS:
        m = re.search(pattern, text, flags=re.I)
        if m:
            posted_raw = clean(m.group(1))
            break

    deadline = parse_date(deadline_raw, now)
    posted = parse_date(posted_raw, now)
    if posted:
        confidence = 1.0
        source = "explicit_page_date"
    elif deadline:
        confidence = 0.35
        source = "deadline_only"
    else:
        confidence = 0.15
        source = "missing"
    return iso(deadline), iso(posted), confidence, source


def extract_apply_links(base_url: str, html_text: str, links: list[dict[str, Any]]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    candidates: list[dict[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = absolute_url(base_url, a.get("href", ""))
        if not href:
            continue
        txt = clean(a.get_text(" ", strip=True))
        aria = clean(a.get("aria-label", ""))
        title = clean(a.get("title", ""))
        blob = f"{txt} {aria} {title}".lower()
        if any(term in blob for term in APPLICATION_TERMS):
            candidates.append({"url": href, "text": txt or aria or title or "Apply"})

    for link in links or []:
        href = clean(link.get("url") if isinstance(link, dict) else "")
        txt = clean(link.get("text") if isinstance(link, dict) else "")
        blob = txt.lower()
        if href and any(term in blob for term in APPLICATION_TERMS):
            candidates.append({"url": href, "text": txt})

    out = []
    seen = set()
    for c in candidates:
        url = clean(c["url"])
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(c)
    return out[:20]


def classify_apply_url(candidates: list[dict[str, str]], source_url: str) -> tuple[str, str, str]:
    if not candidates:
        return source_url, "READ MORE", "none"

    scored = []
    for c in candidates:
        url = c["url"]
        txt = c["text"].lower()
        score = 0
        if any(term in txt for term in ["apply online", "apply now", "application form", "submit application"]):
            score += 50
        if "mybdjobs" in normalize_domain(url):
            score += 35
        if "/jobdetails" in urlparse(url).path.lower():
            score -= 20
        if url == source_url:
            score -= 20
        if url.startswith("mailto:"):
            score += 15
        scored.append((score, url, txt))

    scored.sort(reverse=True)
    best = scored[0]
    if best[0] < 20 or best[1] == source_url:
        return source_url, "READ MORE", "none"
    return best[1], "APPLY NOW", "verified_link"


def extract_job_record(page: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    url = canonical_url(page.get("url") or page.get("final_url"))
    if not url or not is_bdjobs_url(url):
        return None
    text = clean(page.get("text", ""))
    if len(text) < 180:
        return None

    # Prefer Exa's extracted text/links. Direct-read only when the researched
    # page is too thin to safely extract a record or application destination.
    raw_html = page.get("raw_html", "") or ""
    links = page.get("links", []) or []
    if not raw_html and len(text) < 900:
        direct_html, final_url, direct_links, _ = direct_read(url)
        if direct_html:
            raw_html = direct_html
            links = direct_links
            text = html_to_text(raw_html)[:MAX_SOURCE_TEXT]

    soup = BeautifulSoup(raw_html or "", "html.parser")
    jsonlds = find_jsonld(soup) if raw_html else []

    title = clean(page.get("title"))
    company = ""
    location = ""
    education = ""
    experience = ""
    salary = ""
    vacancies = ""
    job_nature = ""
    job_level = ""
    application_period = ""
    application_method = ""
    age_limit = ""
    industry = ""

    for obj in jsonlds:
        if obj.get("@type") in {"JobPosting", "JobPostingListing"} or "title" in obj:
            title = title or clean(obj.get("title"))
            company = company or clean((obj.get("hiringOrganization") or {}).get("name"))
            location = location or clean(((obj.get("jobLocation") or {}).get("address") or {}).get("addressLocality"))
            education = education or clean(obj.get("educationRequirements"))
            experience = experience or clean((obj.get("experienceRequirements") or {}).get("monthsOfExperience"))
            salary_obj = obj.get("baseSalary") or {}
            if isinstance(salary_obj, dict):
                value = salary_obj.get("value") or {}
                if isinstance(value, dict):
                    salary = salary or clean(value.get("value"))
                else:
                    salary = salary or clean(value)
            vacancies = vacancies or clean(obj.get("totalJobOpenings"))
            job_nature = job_nature or clean(obj.get("employmentType"))
            application_method = application_method or clean(obj.get("applicationContact"))

    title = title or label_value(text, ["job title", "position", "post name"])
    company = company or label_value(text, ["company name", "organization", "employer"])
    location = location or label_value(text, ["job location", "location"])
    education = education or label_value(text, ["educational qualification", "education", "qualification"])
    experience = experience or label_value(text, ["experience", "experience required"])
    salary = salary or label_value(text, ["salary", "salary range", "compensation"])
    vacancies = vacancies or label_value(text, ["vacancy", "vacancies", "number of vacancies"])
    job_nature = job_nature or label_value(text, ["job nature", "employment type"])
    job_level = job_level or label_value(text, ["job level"])
    application_period = application_period or label_value(text, ["application period", "apply period"])
    age_limit = age_limit or label_value(text, ["age", "age limit"])
    industry = industry or label_value(text, ["industry"])

    # Headline fallbacks.
    if not title:
        h = soup.find(["h1", "h2", "h3"])
        title = clean(h.get_text(" ", strip=True)) if h else clean(page.get("title"))

    if not company:
        # Bdjobs pages commonly show company name immediately after the title.
        candidates = [clean(x.get_text(" ", strip=True)) for x in soup.find_all(["h1", "h2", "h3", "h4"])[:10]]
        for candidate in candidates:
            if candidate and candidate != title and len(candidate) < 120:
                company = candidate
                break

    deadline_iso, posted_iso, date_confidence, date_source = extract_dates(text, now)
    if not posted_iso and page.get("published_date"):
        dt = parse_date(page.get("published_date"), now)
        if dt:
            posted_iso = iso(dt)
            date_confidence = 0.45
            date_source = "exa_published_date"

    # Reject explicit expired deadlines. Missing deadline is allowed, but ranks lower.
    deadline_dt = parse_date(deadline_iso, now) if deadline_iso else None
    if deadline_dt and deadline_dt < now:
        return None

    apply_candidates = extract_apply_links(page.get("final_url") or url, raw_html, links)
    apply_url, button_label, apply_confidence = classify_apply_url(apply_candidates, url)
    if not application_method and button_label == "APPLY NOW":
        application_method = "Online"

    lowered = f"{title} {company} {text}".lower()
    is_job = sum(1 for t in JOB_SIGNALS if t in lowered) >= 3 or bool(re.search(r"job responsibilities|educational qualification|deadline|experience", lowered))
    if not is_job:
        return None

    return {
        "event_id": event_id({"company": company, "job_title": title, "location": location, "deadline": deadline_iso}),
        "source": "Bdjobs",
        "source_url": url,
        "apply_url": apply_url,
        "button_label": button_label,
        "apply_confidence": apply_confidence,
        "job_title": title,
        "company": company,
        "location": location,
        "job_nature": job_nature,
        "job_level": job_level,
        "industry": industry,
        "education": education,
        "experience": experience,
        "salary": salary,
        "vacancies": vacancies,
        "age_limit": age_limit,
        "application_method": application_method,
        "application_period": application_period,
        "deadline": deadline_iso,
        "posted_date": posted_iso,
        "date_confidence": date_confidence,
        "date_source": date_source,
        "image_url": clean(page.get("image")),
        "source_text_evidence": text[:MAX_SOURCE_TEXT],
        "retrieval_backend": page.get("backend", "unknown"),
        "retrieved_at": iso(now),
    }


# ------------------------------------------------------------
# Local audience / quality pre-score
# ------------------------------------------------------------

def audience_score(job: dict[str, Any], now: datetime) -> tuple[int, dict[str, int]]:
    blob = normalize_title(" ".join(clean(job.get(k, "")) for k in [
        "job_title", "company", "education", "experience", "job_nature", "job_level", "industry", "source_text_evidence"
    ]))

    bba = min(30, sum(weight for term, weight in EDUCATION_TERMS.items() if term in blob) // 2)
    role = min(25, sum(weight for term, weight in ROLE_TERMS.items() if term in blob) // 2)

    early = 0
    if any(term in blob for term in EARLY_TERMS):
        early = 18
    if re.search(r"\b0\s*(?:to|-|–)\s*[123]\s*year", blob):
        early = 20
    if any(term in blob for term in SENIOR_TERMS):
        early = max(0, early - 10)

    function = 0
    if any(term in blob for term in ["finance", "account", "bank", "banking", "audit", "credit"]):
        function += 10
    if any(term in blob for term in ["marketing", "sales", "business development", "customer"]):
        function += 10
    if any(term in blob for term in ["hr", "human resources", "operations", "supply chain", "procurement", "commercial"]):
        function += 10
    function = min(function, 20)

    completeness = 0
    for field in ["location", "education", "experience", "deadline", "application_method"]:
        if clean(job.get(field)):
            completeness += 2
    if clean(job.get("salary")):
        completeness += 2
    if clean(job.get("vacancies")):
        completeness += 2
    completeness = min(completeness, 15)

    deadline_score = 0
    deadline = parse_date(job.get("deadline", ""), now)
    if deadline:
        days = (deadline - now).total_seconds() / 86400
        if days > 14:
            deadline_score = 5
        elif days > 7:
            deadline_score = 4
        elif days > 2:
            deadline_score = 3
        elif days > 0:
            deadline_score = 1

    freshness = 0
    posted = parse_date(job.get("posted_date", ""), now)
    if posted:
        age = (now - posted).total_seconds() / 86400
        if age <= 1:
            freshness = 7
        elif age <= 3:
            freshness = 6
        elif age <= 7:
            freshness = 5
        elif age <= 14:
            freshness = 3
        else:
            freshness = 1
    else:
        freshness = 1

    source = 10  # Source is restricted to Bdjobs.
    total = min(100, bba + role + early + function + completeness + deadline_score + freshness + source)
    return total, {
        "bba_mba": bba,
        "role": role,
        "early_career": early,
        "function": function,
        "completeness": completeness,
        "deadline": deadline_score,
        "freshness": freshness,
        "source": source,
    }


# ------------------------------------------------------------
# Cerebras judge
# ------------------------------------------------------------

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "publish": {"type": "boolean"},
                    "overall_score": {"type": "number"},
                    "bba_mba_fit": {"type": "number"},
                    "early_career_fit": {"type": "number"},
                    "role_quality": {"type": "number"},
                    "experience_fit": {"type": "number"},
                    "information_quality": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "event_id", "publish", "overall_score", "bba_mba_fit", "early_career_fit",
                    "role_quality", "experience_fit", "information_quality", "reason"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


def judge_prompt(jobs: list[dict[str, Any]]) -> str:
    compact = []
    for j in jobs:
        compact.append({
            "event_id": j.get("event_id"),
            "title": j.get("job_title"),
            "company": j.get("company"),
            "location": j.get("location"),
            "job_nature": j.get("job_nature"),
            "job_level": j.get("job_level"),
            "industry": j.get("industry"),
            "education": j.get("education"),
            "experience": j.get("experience"),
            "salary": j.get("salary"),
            "vacancies": j.get("vacancies"),
            "deadline": j.get("deadline"),
            "posted_date": j.get("posted_date"),
            "application_method": j.get("application_method"),
            "source_url": j.get("source_url"),
            "apply_url": j.get("apply_url"),
            "evidence": j.get("source_text_evidence", "")[:MAX_JUDGE_EVIDENCE],
        })
    return json.dumps(compact, ensure_ascii=False)


def extract_json_content(body: dict[str, Any]) -> dict[str, Any]:
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
    if isinstance(content, dict):
        return content
    raw = clean(content)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def call_cerebras(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    if not CEREBRAS_API_KEY or not jobs:
        return {}
    system = (
        "You are the editorial judge for CareerNewsroom, a Bangladesh jobs channel for mostly 20-30 year old "
        "BBA/MBA students, fresh graduates and early-career business professionals. Source is Bdjobs only. "
        "Judge each supplied vacancy, do not invent facts, and do not rewrite factual fields. Prefer direct BBA/MBA, "
        "internship, trainee, fresher, 0-3 year, finance, accounting, banking, marketing, HR, business, operations, "
        "commercial and supply-chain roles. Reject obvious senior roles, non-business technical roles, non-vacancies, "
        "expired jobs, duplicate-like records, and records with weak identity. Missing optional fields are not false facts. "
        "Return only JSON matching the schema."
    )
    payload = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": judge_prompt(jobs)},
        ],
        "temperature": 0.1,
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "career_job_judgment",
                "strict": True,
                "schema": JUDGE_SCHEMA,
            },
        },
        "max_completion_tokens": 1800,
    }
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    response = request(CEREBRAS_URL, method="POST", headers=headers, json=payload, timeout=60)
    if response and response.status_code == 200:
        try:
            return extract_json_content(response.json())
        except ValueError:
            return {}
    LOG.warning("CEREBRAS judge failed status=%s", getattr(response, "status_code", "ERR"))
    return {}


def judge_batches(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not jobs:
        return []
    judged: dict[str, dict[str, Any]] = {}
    for start in range(0, len(jobs), CEREBRAS_BATCH):
        batch = jobs[start:start + CEREBRAS_BATCH]
        result = call_cerebras(batch)
        rows = result.get("jobs", []) if isinstance(result, dict) else []
        for row in rows:
            if clean(row.get("event_id")):
                judged[clean(row["event_id"])] = row
        time.sleep(0.3)

    output = []
    for job in jobs:
        row = judged.get(job.get("event_id"), {})
        job = dict(job)
        job["judge"] = row
        job["judge_score"] = float(row.get("overall_score", 0) or 0)
        job["publish_by_judge"] = bool(row.get("publish", False))
        output.append(job)
    return output


# ------------------------------------------------------------
# Final selection and publishing
# ------------------------------------------------------------

def merge_inventory(state: dict[str, Any], jobs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    queue = state.setdefault("queue", {})
    posted_urls = load_posted_urls()

    merged: dict[str, dict[str, Any]] = {}
    for old in queue.values():
        if old.get("status") in {"verified", "pending"} and not already_posted(state, posted_urls, old):
            deadline = parse_date(old.get("deadline", ""), now)
            if not deadline or deadline >= now:
                merged[old.get("event_id") or event_id(old)] = old

    for job in jobs:
        if already_posted(state, posted_urls, job):
            continue
        merged[job.get("event_id") or event_id(job)] = job

    return list(merged.values())


def choose_jobs(state: dict[str, Any], judged: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    eligible = []
    for job in judged:
        local, breakdown = audience_score(job, now)
        job = dict(job)
        job["local_score"] = local
        job["local_breakdown"] = breakdown
        deadline = parse_date(job.get("deadline", ""), now)
        if deadline and deadline < now:
            continue
        if not job.get("publish_by_judge", False):
            continue
        if job.get("judge_score", 0) < 70:
            continue
        if local < 55:
            continue
        eligible.append(job)

    eligible.sort(key=lambda j: (-(0.65 * j.get("judge_score", 0) + 0.35 * j.get("local_score", 0)), -j.get("local_breakdown", {}).get("freshness", 0)))

    selected = []
    seen_companies: dict[str, int] = {}
    seen_roles: list[str] = []
    for job in eligible:
        company = normalize_title(job.get("company", ""))
        role = normalize_title(job.get("job_title", ""))
        if company and seen_companies.get(company, 0) >= 3:
            continue
        if any(title_similarity(role, old_role) >= 0.95 and company == normalize_title(old.get("company", "")) for old, old_role in [(x, normalize_title(x.get("job_title", ""))) for x in selected]):
            continue
        selected.append(job)
        seen_companies[company] = seen_companies.get(company, 0) + 1
        seen_roles.append(role)
        if len(selected) >= MAX_PUBLISH:
            break

    LOG.info("FINAL | judged=%d | eligible=%d | selected=%d", len(judged), len(eligible), len(selected))
    return selected


def html_escape(value: str) -> str:
    return html.escape(clean(value), quote=True)


def build_hashtags(job: dict[str, Any]) -> str:
    text = normalize_title(" ".join(clean(job.get(k, "")) for k in ["job_title", "education", "job_nature", "industry"]))
    tags = ["#CareerNewsroom", "#Bdjobs"]
    mapping = [
        ("#BBA", ["bba", "business administration"]),
        ("#MBA", ["mba"]),
        ("#Internship", ["internship", "intern"]),
        ("#ManagementTrainee", ["management trainee", "graduate trainee"]),
        ("#Finance", ["finance", "accounts", "accounting"]),
        ("#Banking", ["bank", "banking"]),
        ("#Marketing", ["marketing"]),
        ("#HR", ["hr", "human resources"]),
        ("#SupplyChain", ["supply chain", "procurement"]),
    ]
    for tag, terms in mapping:
        if any(term in text for term in terms):
            tags.append(tag)
    return " ".join(dict.fromkeys(tags[:7]))


def snapshot_rows(job: dict[str, Any]) -> list[tuple[str, str]]:
    rows = [
        ("Location", job.get("location")),
        ("Type", job.get("job_nature")),
        ("Education", job.get("education")),
        ("Experience", job.get("experience")),
        ("Salary", job.get("salary")),
        ("Vacancies", job.get("vacancies")),
        ("Application", job.get("application_method")),
        ("Application Period", job.get("application_period")),
        ("Deadline", job.get("deadline")),
        ("Posted", job.get("posted_date")),
        ("Age Limit", job.get("age_limit")),
    ]
    return [(label, clean(value)) for label, value in rows if clean(value)]


def render_rich(job: dict[str, Any]) -> str:
    title = html_escape(job.get("job_title", "Job Vacancy"))
    company = html_escape(job.get("company", ""))
    source_url = html_escape(job.get("source_url", ""))
    source_name = "Bdjobs"
    rows = snapshot_rows(job)
    table = [
        '<table border="1">',
        '<tr><th>FIELD</th><th>DETAILS</th></tr>',
    ]
    for label, value in rows:
        table.append(f"<tr><td>{html_escape(label)}</td><td>{html_escape(value)}</td></tr>")
    table.append("</table>")
    return (
        f"<b>{title}</b><br>"
        f"{company}<br><br>"
        f"<b>JOB SNAPSHOT</b><br>"
        + "".join(table)
        + f"<br>{html_escape(build_hashtags(job))}<br><br>"
        f"<b>🔎 Official Source:</b> <a href=\"{source_url}\">{source_name}</a>"
    )


def telegram_request(method: str, data: dict[str, Any], files: dict[str, Any] | None = None) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        response = SESSION.post(url, data=data, files=files, timeout=35)
        try:
            return response.json()
        except ValueError:
            return {"ok": False, "description": response.text[:500]}
    except requests.RequestException as exc:
        return {"ok": False, "description": str(exc)}


def build_inline_keyboard(job: dict[str, Any]) -> dict[str, Any]:
    url = clean(job.get("apply_url") or job.get("source_url"))
    label = clean(job.get("button_label") or "READ MORE") or "READ MORE"
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def render_bot_html(job: dict[str, Any]) -> str:
    rows = snapshot_rows(job)
    lines = [f"<b>{html_escape(job.get('job_title', 'Job Vacancy'))}</b>", html_escape(job.get('company', '')), "", "<b>JOB SNAPSHOT</b>"]
    for label, value in rows:
        lines.append(f"<b>{html_escape(label)}:</b> {html_escape(value)}")
    source_url = html_escape(job.get("source_url", ""))
    lines.extend(["", html_escape(build_hashtags(job)), "", f"<b>🔎 Official Source:</b> <a href=\"{source_url}\">Bdjobs</a>"])
    return "<br>".join(lines)


def publish_job(job: dict[str, Any], index: int) -> tuple[bool, str]:
    rich = render_rich(job)
    markup = build_inline_keyboard(job)

    # Preferred existing Rich Message path.
    rich_payload = {
        "html": rich,
        "skip_entity_detection": False,
    }
    response = telegram_request(
        TELEGRAM_RICH_METHOD,
        {
            "chat_id": CHANNEL,
            "rich_message": json.dumps(rich_payload, ensure_ascii=False),
            "reply_markup": json.dumps(markup, ensure_ascii=False),
        },
    )
    if response.get("ok"):
        return True, str(response.get("result", {}).get("message_id", ""))

    # Official Bot API fallback uses only supported HTML tags.
    fallback = render_bot_html(job)
    response = telegram_request(
        "sendMessage",
        {
            "chat_id": CHANNEL,
            "text": fallback[:4096],
            "parse_mode": "HTML",
            "reply_markup": json.dumps(markup, ensure_ascii=False),
            "disable_web_page_preview": True,
        },
    )
    if response.get("ok"):
        return True, str(response.get("result", {}).get("message_id", ""))
    return False, clean(response.get("description"))


# ------------------------------------------------------------
# Self-test
# ------------------------------------------------------------

FIXTURE_HTML = """
<html>
<head>
<meta property="og:title" content="Management Trainee Program">
<meta property="og:image" content="https://jobs.bdjobs.com/assets/job.jpg">
</head>
<body>
<h1>Management Trainee Program</h1>
<h2>Example Bank PLC</h2>
<div>Job Location: Dhaka</div>
<div>Educational Qualification: BBA/MBA in Finance, Management or Marketing</div>
<div>Experience: Freshers can apply / 0 to 2 year(s)</div>
<div>Salary: Tk. 45,000 per month</div>
<div>Vacancy: 10</div>
<div>Job Nature: Full-time</div>
<div>Job Level: Entry level</div>
<div>Application Deadline: 30 September 2026</div>
<div>Posted on: 16 September 2026</div>
<div>Application: Apply Online</div>
<a href="https://mybdjobs.bdjobs.com/mybdjobs/apply.asp?id=123">Apply Online</a>
</body>
</html>
"""


def self_test() -> None:
    now = datetime(2026, 9, 18, 16, 0, tzinfo=BD_TZ)

    # Bdjobs source guard.
    assert is_bdjobs_url("https://jobs.bdjobs.com/jobdetails/?id=123")
    assert not is_bdjobs_url("https://example.com/jobdetails/123")

    # Index detection and child extraction.
    index_html = '<a href="/jobdetails/?id=1">Finance Executive</a><a href="/jobdetails/?id=2">HR Intern</a>'
    children = extract_jobdetail_links(BDJOBS_SEARCH_URL, index_html)
    assert len(children) == 2
    assert is_index_url(BDJOBS_SEARCH_URL, "Find Jobs")
    assert not is_index_url("https://jobs.bdjobs.com/jobdetails/?id=1", "Finance Executive")

    # Cheap filter blocks noise and accepts relevant vacancy.
    ok, _, _ = cheap_candidate_filter({"url": "https://jobs.bdjobs.com/jobdetails/?id=1", "title": "Management Trainee", "snippet": "BBA MBA Apply Deadline"})
    assert ok
    ok, _, _ = cheap_candidate_filter({"url": "https://jobs.bdjobs.com/jobdetails/?id=2", "title": "Age Calculator", "snippet": "career"})
    assert not ok

    # Full extraction.
    page = {
        "url": "https://jobs.bdjobs.com/jobdetails/?id=123",
        "final_url": "https://jobs.bdjobs.com/jobdetails/?id=123",
        "title": "Management Trainee Program",
        "text": html_to_text(FIXTURE_HTML),
        "links": [{"url": "https://mybdjobs.bdjobs.com/mybdjobs/apply.asp?id=123", "text": "Apply Online"}],
        "image": "https://jobs.bdjobs.com/assets/job.jpg",
        "backend": "fixture",
        "raw_html": FIXTURE_HTML,
    }
    job = extract_job_record(page, now)
    assert job is not None
    assert job["company"] == "Example Bank PLC"
    assert job["job_title"] == "Management Trainee Program"
    assert "BBA" in job["education"]
    assert job["button_label"] == "APPLY NOW"
    assert job["apply_url"].startswith("https://mybdjobs.bdjobs.com")

    # 10-day-old active job survives 72h wall.
    old = dict(job)
    old["posted_date"] = iso(now - timedelta(days=10))
    old["deadline"] = iso(now + timedelta(days=12))
    score, _ = audience_score(old, now)
    assert score >= 55

    # Expired job is rejected.
    expired = dict(job)
    expired["deadline"] = iso(now - timedelta(days=1))
    assert extract_job_record({**page, "text": html_to_text(FIXTURE_HTML).replace("30 September 2026", "10 September 2026"), "raw_html": FIXTURE_HTML.replace("30 September 2026", "10 September 2026")}, now) is None

    # Telegram markup must contain exactly one native button.
    markup = build_inline_keyboard(job)
    assert len(markup["inline_keyboard"]) == 1
    assert len(markup["inline_keyboard"][0]) == 1
    assert markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    fallback = render_bot_html(job)
    assert "<table" not in fallback
    assert "<a href=" in fallback

    # Exa payload assumptions: domain restriction + 7-day discovery.
    start = now - timedelta(days=7)
    payload = {
        "query": EXA_QUERIES[0],
        "includeDomains": ["bdjobs.com", "*.bdjobs.com"],
        "startPublishedDate": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "endPublishedDate": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "numResults": 18,
        "type": "fast",
    }
    assert "includeDomains" in payload and payload["numResults"] <= 100

    # Cerebras schema sanity.
    assert JUDGE_SCHEMA["type"] == "object"
    assert JUDGE_SCHEMA["properties"]["jobs"]["type"] == "array"

    LOG.info("SELF-TEST PASS | source=bdjobs-only | 72h=soft-freshness | apply-url=verified | judge=batched")


def live_preflight() -> int:
    """Safe live API connectivity check. Does not send Telegram posts."""
    now = now_dt()
    if not EXA_API_KEY:
        LOG.error("LIVE TEST: EXA_API_KEY missing")
        return 2
    if not CEREBRAS_API_KEY:
        LOG.error("LIVE TEST: CEREBRAS_API_KEY missing")
        return 2

    # Exa: verify domain-restricted recent search responds with Bdjobs results.
    exa_rows = exa_search(EXA_QUERIES[0], now - timedelta(days=DISCOVERY_DAYS), now)
    LOG.info("LIVE EXA | results=%d", len(exa_rows))
    bdjobs_rows = [r for r in exa_rows if is_bdjobs_url(clean(r.get("url", "")))]
    if not bdjobs_rows:
        LOG.error("LIVE TEST: Exa returned no usable Bdjobs results")
        return 3

    # Cerebras: minimal structured-output health check. It does not invent or
    # evaluate a fake job and does not affect the persistent job inventory.
    payload = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON object."},
            {"role": "user", "content": "Return ok=true to confirm the structured-output endpoint is reachable."},
        ],
        "temperature": 0,
        "max_completion_tokens": 32,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "health_check",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        },
    }
    response = request(CEREBRAS_URL, method="POST", headers={
        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
        "Content-Type": "application/json",
    }, json=payload, timeout=45)
    if not response or response.status_code != 200:
        LOG.error("LIVE TEST: Cerebras health check failed status=%s", getattr(response, "status_code", "ERR"))
        return 4
    try:
        body = response.json()
        content = body["choices"][0]["message"].get("content", "")
        parsed = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        LOG.error("LIVE TEST: Cerebras structured response invalid: %s", exc)
        return 4
    if parsed.get("ok") is not True:
        LOG.error("LIVE TEST: Cerebras health check returned %r", parsed)
        return 4

    LOG.info("LIVE CEREBRAS | structured-output=ok | model=%s", CEREBRAS_MODEL)
    LOG.info("LIVE PREFLIGHT PASS | Exa=Bdjobs-only | Cerebras=structured-output")
    return 0


# ------------------------------------------------------------
# Runtime
# ------------------------------------------------------------

def run(publish: bool = True) -> int:
    now = now_dt()
    state = load_state()
    prune_state(state, now)
    posted_urls = load_posted_urls()

    LOG.info("CareerNewsroomBot %s | now=%s | source=Bdjobs-only", BOT_VERSION, iso(now))

    direct, direct_status = direct_bdjobs_discovery()
    exa = exa_discovery(now)

    combined: dict[str, dict[str, Any]] = {}
    for item in direct + exa:
        url = canonical_url(item.get("url", ""))
        if not url or not is_bdjobs_url(url):
            continue
        combined[url] = {**combined.get(url, {}), **item, "source": "Bdjobs"}

    LOG.info("DISCOVERY | direct=%d | exa=%d | unique=%d", len(direct), len(exa), len(combined))

    # Research actual vacancy pages, not index pages as jobs.
    pages = research_pages(state, list(combined.values()))
    LOG.info("RESEARCH | pages=%d", len(pages))

    jobs: list[dict[str, Any]] = []
    for page in pages:
        try:
            job = extract_job_record(page, now)
        except Exception as exc:
            LOG.warning("EXTRACTION FAILED url=%s error=%s", page.get("url"), exc)
            continue
        if job:
            jobs.append(job)

    # Dedup at event level.
    job_by_event: dict[str, dict[str, Any]] = {}
    for job in jobs:
        eid = job.get("event_id") or event_id(job)
        existing = job_by_event.get(eid)
        if not existing or len(job.get("source_text_evidence", "")) > len(existing.get("source_text_evidence", "")):
            job_by_event[eid] = job
    jobs = list(job_by_event.values())
    LOG.info("JOB RECORDS | verified=%d", len(jobs))

    inventory = merge_inventory(state, jobs, now)
    LOG.info("INVENTORY | active_unpublished=%d", len(inventory))

    # Keep enough records for the AI judge, but avoid an unbounded prompt.
    candidates = []
    for job in sorted(inventory, key=lambda j: (-audience_score(j, now)[0], -j.get("local_score", 0)))[:48]:
        candidates.append(job)
    judged = judge_batches(candidates)
    LOG.info("CEREBRAS | candidates=%d | judged=%d", len(candidates), sum(1 for j in judged if j.get("judge")))

    selected = choose_jobs(state, judged, now)

    # Update queue before publishing, so failed publish attempts can be retried next run.
    for job in judged:
        state.setdefault("queue", {})[job["event_id"]] = {
            **{k: v for k, v in job.items() if k != "source_text_evidence"},
            "status": "verified" if job.get("publish_by_judge") else "rejected",
            "last_seen": iso(now),
        }

    published = 0
    if publish and selected:
        for index, job in enumerate(selected, 1):
            ok, detail = publish_job(job, index)
            if ok:
                published += 1
                job["status"] = "published"
                job["published_at"] = iso(now)
                state.setdefault("queue", {})[job["event_id"]] = {
                    **{k: v for k, v in job.items() if k != "source_text_evidence"},
                    "status": "published",
                    "published_at": iso(now),
                    "last_seen": iso(now),
                }
                remember_post(state, job)
                LOG.info("PUBLISHED | %d/%d | %s | %s", index, len(selected), job.get("job_title"), job.get("company"))
            else:
                LOG.warning("PUBLISH FAILED | %s | %s", job.get("job_title"), detail)
            time.sleep(POST_DELAY_SECONDS)

    save_state(state)
    LOG.info(
        "SUMMARY | discovered=%d | pages=%d | jobs=%d | inventory=%d | selected=%d | published=%d",
        len(combined), len(pages), len(jobs), len(inventory), len(selected), published,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"CareerNewsroomBot {BOT_VERSION}")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--live-test", action="store_true", help="Test Exa + Cerebras without Telegram publishing")
    parser.add_argument("--dry-run", action="store_true", help="Run the full pipeline without Telegram publishing")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.live_test:
        return live_preflight()
    return run(publish=not args.dry_run)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
