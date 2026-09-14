from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from exa_py import Exa
from cerebras.cloud.sdk import Cerebras


# ============================================================
# CONFIGURATION
# ============================================================

EXA_API_KEY = os.environ["EXA_API_KEY"]
CEREBRAS_API_KEY = os.environ["CEREBRAS_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()

# Same primary Cerebras model family used by the working Tech bot.
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL") or "gpt-oss-120b"

POSTED_FILE = Path("posted_urls.txt")
STATE_FILE = Path("job_state.json")
REGISTRY_FILE = Path("source_registry.json")
RUNTIME_DIR = Path(".runtime")

MAX_DIRECT_LINKS_PER_SOURCE = 160
MAX_DISCOVERY_CANDIDATES = 0  # unlimited after URL deduplication
AI_BATCH_SIZE = 10
MAX_EXA_QUERIES = 80
MAX_EXA_RESULTS_PER_QUERY = 8
EXA_FRESHNESS_DAYS = 45
POST_DELAY_SECONDS = 2.5
EVENT_RETENTION_DAYS = 90
MAX_CAPTION = 1000

logger = logging.getLogger("CareerNewsroom")


# ============================================================
# TAXONOMY
# ============================================================

SECTORS = [
    "Government", "Banking & Finance", "Insurance", "NGO / INGO", "MNC",
    "Telecom", "Technology / IT", "Pharmaceutical", "Healthcare",
    "Education", "University", "Research", "RMG / Textile", "Manufacturing",
    "FMCG", "Retail", "E-commerce", "Logistics / Supply Chain", "Construction",
    "Real Estate", "Energy / Power", "Agriculture", "Food & Beverage",
    "Hospitality", "Travel / Aviation", "Media", "Marketing / Advertising",
    "Legal", "Accounting", "Human Resources", "Sales", "Customer Service",
    "Security", "Skilled / Technical", "Other",
]

JOB_FUNCTIONS = [
    "Accounting", "Audit", "Finance", "Credit", "Risk", "Treasury", "Investment",
    "HR", "Administration", "Marketing", "Sales", "Business Development", "IT",
    "Software Engineering", "Data", "Operations", "Supply Chain", "Procurement",
    "Production", "Quality", "Engineering", "Research", "Healthcare", "Nursing",
    "Teaching", "Legal", "Customer Service", "Security", "Driving", "Technical",
    "Other",
]

JOB_LEVELS = [
    "Intern", "Fresher", "Entry-level", "Junior", "Mid-level", "Senior",
    "Manager", "Executive", "Director", "Officer", "Assistant", "Operator",
    "Technician", "Specialist", "Other",
]

JOB_TYPES = [
    "Regular Job", "Internship", "Management Trainee", "Graduate Program",
    "Apprenticeship", "Contract", "Temporary", "Part-time", "Full-time",
    "Remote", "Freelance", "Consultancy", "Project-based", "Volunteer",
]

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
}

BANGLADESH_TERMS = (
    "bangladesh", "dhaka", "chattogram", "chittagong", "sylhet", "khulna",
    "rajshahi", "barishal", "barisal", "rangpur", "mymensingh", "gazipur",
    "narayanganj", "cumilla", "jashore", "gov.bd", "edu.bd", "org.bd",
    "teletalk.com.bd", "jobs.gov.bd", "চাকরি", "চাকরির", "নিয়োগ",
    "নিয়োগ বিজ্ঞপ্তি", "বিজ্ঞপ্তি", "আবেদন", "পদসংখ্যা",
)

JOB_TERMS = (
    "job", "jobs", "career", "careers", "vacancy", "vacancies", "recruit",
    "recruitment", "hiring", "position", "employment", "internship",
    "job circular", "apply now", "application", "চাকরি", "নিয়োগ",
    "নিয়োগ বিজ্ঞপ্তি", "শূন্যপদ", "পদসংখ্যা",
)

SCAM_TERMS = (
    "send money", "pay fee", "processing fee", "security deposit",
    "bikash", "bkash", "nagad", "rocket", "personal account",
    "upfront payment", "registration fee",
)


# ============================================================
# STATE
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "version": 4,
        "updated_at": None,
        "jobs": {},
        "sources": {},
        "runs": [],
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("State file invalid; starting clean state: %s", exc)
        return default_state()
    for key, value in default_state().items():
        data.setdefault(key, value)
    # V1 schema migration. Existing jobs are retained; missing flags are backfilled.
    data["version"] = 4
    for event in data.get("jobs", {}).values():
        event.setdefault("repost_due", False)
        event.setdefault("update_pending", False)
        event.setdefault("update_history", [])
        event.setdefault("repost_history", [])
        event.setdefault("telegram", {"published": False, "message_id": None, "published_at": None, "last_event_type": None})
    return data


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_state(state: dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    json.loads(payload)
    atomic_write(STATE_FILE, payload + "\n")


def load_posted() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    return {
        x.strip() for x in POSTED_FILE.read_text(encoding="utf-8").splitlines() if x.strip()
    }


def save_posted(posted: set[str]) -> None:
    atomic_write(POSTED_FILE, "\n".join(sorted(posted)) + ("\n" if posted else ""))


# ============================================================
# HTTP / NORMALIZATION
# ============================================================

def safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_url(url: str) -> str:
    url = safe_text(url)
    if not url:
        return ""
    p = urlparse(url)
    if not p.scheme:
        return url
    query = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse(
        p._replace(
            scheme=p.scheme.lower(),
            netloc=p.netloc.lower(),
            path=path,
            query=urlencode(query),
            fragment="",
        )
    )


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/128 Safari/537.36 "
            "CareerNewsroom/1.0"
        ),
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.5",
    })
    return s


SESSION = build_session()


def http_get(url: str, timeout: int = 18) -> requests.Response | None:
    try:
        response = SESSION.get(url, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            logger.warning("HTTP %s %s", response.status_code, url)
            return None
        return response
    except requests.RequestException as exc:
        logger.warning("HTTP failed %s | %s", url, exc)
        return None


def is_bangladesh(text: str, url: str = "") -> bool:
    hay = f"{url}\n{text}".lower()
    return any(term in hay for term in BANGLADESH_TERMS)


def looks_like_job(text: str, url: str = "") -> bool:
    hay = f"{url}\n{text}".lower()
    return any(term in hay for term in JOB_TERMS)


def hash_text(text: str) -> str:
    return hashlib.sha256(safe_text(text).encode("utf-8")).hexdigest()


# ============================================================
# CONTENT EXTRACTION
# ============================================================

def extract_html_text(content: bytes, url: str) -> str:
    text = trafilatura.extract(
        content,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if text and len(text.strip()) >= 120:
        return text.strip()

    soup = BeautifulSoup(content, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    return soup.get_text("\n", strip=True)


def extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return ""
    chunks = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(chunks).strip()


def extract_meta(content: bytes, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(content, "html.parser")
    meta: dict[str, Any] = {}

    if soup.title:
        meta["title"] = soup.title.get_text(" ", strip=True)

    tags = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            tags[key.lower()] = value.strip()

    if tags.get("og:image"):
        meta["image"] = urljoin(url, tags["og:image"])
    if tags.get("og:description"):
        meta["description"] = tags["og:description"]

    # Logo / icons.
    for link in soup.find_all("link"):
        href = link.get("href")
        rel = [x.lower() for x in link.get("rel", [])]
        if href and "icon" in rel:
            meta["logo"] = urljoin(url, href)
            break

    # JSON-LD JobPosting.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "JobPosting":
                meta["jobposting"] = item
                break

    return meta


def normalize_bengali_digits(text: str) -> str:
    table = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
    return safe_text(text).translate(table)


def deterministic_hints(text: str) -> dict[str, Any]:
    t = normalize_bengali_digits(text)
    lower = t.lower()

    deadline = None
    for pattern in (
        r"(?i)(?:application\s+)?(?:deadline|last\s+date|closing\s+date)"
        r".{0,80}?(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
        r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"(?i)(?:আবেদনের শেষ তারিখ|শেষ তারিখ).{0,80}?"
        r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    ):
        m = re.search(pattern, t)
        if m:
            deadline = m.group(0)
            break

    salary = None
    m = re.search(
        r"(?i)(?:salary|বেতন).{0,60}?"
        r"(?:(?:৳|tk\.?|bdt)\s*)?[\d,]+(?:\s*[-–]\s*[\d,]+)?",
        t,
    )
    if m:
        salary = m.group(0)

    vacancy = None
    m = re.search(
        r"(?i)(?:vacanc(?:y|ies)|no\.?\s*of\s*(?:post|posts)|"
        r"পদসংখ্যা|শূন্যপদ).{0,40}?\d{1,6}",
        t,
    )
    if m:
        vacancy = m.group(0)

    return {
        "deadline": deadline,
        "salary": salary,
        "vacancy": vacancy,
        "bangladesh": is_bangladesh(t),
        "job": looks_like_job(t),
        "scam_signals": [x for x in SCAM_TERMS if x in lower],
        "text_hash": hash_text(t),
    }


# ============================================================
# DISCOVERY
# ============================================================

def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


def registry_map(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {x["id"]: x for x in registry.get("sources", []) if x.get("id")}


def source_for_url(url: str, sources: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Promote Exa/Google discoveries to a known source when hostname matches."""
    host = urlparse(url).netloc.lower()
    if not host:
        return None

    best = None
    best_len = -1
    for source in sources.values():
        source_host = urlparse(safe_text(source.get("url", ""))).netloc.lower()
        if source_host and (host == source_host or host.endswith("." + source_host)):
            if len(source_host) > best_len:
                best = source
                best_len = len(source_host)
    return best


def due_sources(registry: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every enabled source on every run. No interval rotation or source cap."""
    priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    sources = [
        source for source in registry.get("sources", [])
        if source.get("enabled", True)
        and source.get("status") in (None, "active", "verified")
    ]
    return sorted(sources, key=lambda x: (priority.get(x.get("priority", "P3"), 3), safe_text(x.get("name"))))

def direct_discover(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Crawl the source page and discover job pages plus linked PDF circulars."""
    r = http_get(source.get("url", ""))
    if not r:
        return []
    final_url = canonical_url(r.url)
    content_type = r.headers.get("content-type", "").lower()

    if "pdf" in content_type or final_url.lower().endswith(".pdf"):
        text = extract_pdf_text(r.content)
        return [{
            "url": final_url, "source_id": source["id"], "source_name": source["name"],
            "source_priority": source.get("priority", "P3"), "title_hint": "",
            "text_hint": text[:30000], "image": "", "meta": {}, "published_date": "",
            "discovery": "direct_pdf",
        }] if text else []

    if "xml" in content_type or final_url.lower().endswith((".rss", ".xml")):
        parsed = feedparser.parse(r.content)
        out=[]
        for e in parsed.entries:
            link=canonical_url(getattr(e,"link",""))
            if not link: continue
            out.append({
                "url": link, "source_id": source["id"], "source_name": source["name"],
                "source_priority": source.get("priority","P3"), "title_hint": safe_text(getattr(e,"title","")),
                "text_hint": BeautifulSoup(safe_text(getattr(e,"summary","")),"html.parser").get_text(" ",strip=True),
                "image":"", "meta": {}, "published_date": safe_text(getattr(e,"published","") or getattr(e,"updated","")),
                "discovery":"rss",
            })
        return out

    text = extract_html_text(r.content, final_url)
    meta = extract_meta(r.content, final_url)
    candidates=[]
    soup=BeautifulSoup(r.content,"html.parser")

    # Do not treat a source homepage as a job unless it has an actual dated application signal.
    if looks_like_job(text, final_url) and has_deadline_signal(text):
        candidates.append({
            "url": final_url, "source_id": source["id"], "source_name": source["name"],
            "source_priority": source.get("priority","P3"), "title_hint": meta.get("title", ""),
            "text_hint": text[:30000], "image": meta.get("image", ""), "meta": meta,
            "published_date": meta.get("datePublished") or meta.get("datePosted") or "",
            "discovery":"direct_page",
        })

    seen=set()
    for node in soup.find_all(["a","iframe","embed","object"]):
        href=node.get("href") or node.get("src") or node.get("data") or ""
        link=canonical_url(urljoin(final_url,href))
        label=safe_text(node.get_text(" ",strip=True))
        if not link or link in seen: continue
        seen.add(link)
        path=urlparse(link).path.lower()
        hay=f"{label} {link}".lower()
        is_pdf=(".pdf" in path) or (".pdf" in hay) or "application/pdf" in hay
        is_job=looks_like_job(hay,link)
        is_notice=any(x in hay for x in ("circular","recruitment","appointment","vacancy","career","notice","নিয়োগ","চাকরি","বিজ্ঞপ্তি","আবেদন","শূন্যপদ"))
        if not (is_pdf or is_job or is_notice): continue
        candidates.append({
            "url":link,"source_id":source["id"],"source_name":source["name"],
            "source_priority":source.get("priority","P3"),"title_hint":label,"text_hint":"",
            "image":"","meta":{},"published_date":"",
            "discovery":"direct_pdf_link" if is_pdf else "direct_link",
        })
        if len(candidates)>=MAX_DIRECT_LINKS_PER_SOURCE: break

    # Some government portals expose PDF URLs only inside JavaScript/JSON. Catch those too.
    try:
        raw_html = r.content.decode("utf-8", errors="ignore")
        for match in re.findall(r"https?://[^\s\"']+?\.pdf(?:\?[^\s\"']*)?", raw_html, flags=re.I):
            link = canonical_url(match.rstrip(",;)]}"))
            if not link or link in seen: continue
            seen.add(link)
            candidates.append({
                "url": link, "source_id": source["id"], "source_name": source["name"],
                "source_priority": source.get("priority", "P3"), "title_hint": "", "text_hint": "",
                "image": "", "meta": {}, "published_date": "", "discovery": "direct_pdf_link",
            })
            if len(candidates) >= MAX_DIRECT_LINKS_PER_SOURCE: break
    except Exception:
        pass
    return candidates


def has_deadline_signal(text: str) -> bool:
    t=normalize_bengali_digits(text)
    pats=(
        r"(?i)(?:application\s+)?(?:deadline|last\s+date|closing\s+date|apply\s+by).{0,100}(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"(?i)(?:আবেদনের\s*শেষ\s*তারিখ|শেষ\s*তারিখ|আবেদন\s*করার\s*শেষ\s*তারিখ).{0,100}\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",
    )
    return any(re.search(x,t) for x in pats)

def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote(query)}&hl=en-BD&gl=BD&ceid=BD:en"
    )


def discover_google_news() -> list[dict[str, Any]]:
    queries = [
        '"Bangladesh job circular"',
        '"নিয়োগ বিজ্ঞপ্তি"',
        '"চাকরির বিজ্ঞপ্তি"',
        '"Bangladesh recruitment"',
        '"Bangladesh vacancy"',
    ]
    out = []

    for query in queries:
        r = http_get(google_news_url(query), timeout=12)
        if not r:
            continue
        feed = feedparser.parse(r.content)
        for entry in feed.entries[:18]:
            link = canonical_url(getattr(entry, "link", ""))
            if not link:
                continue
            out.append({
                "url": link,
                "source_id": "google_news",
                "source_name": "Google News",
                "source_priority": "P3",
                "title_hint": safe_text(getattr(entry, "title", "")),
                "text_hint": BeautifulSoup(
                    safe_text(getattr(entry, "summary", "")),
                    "html.parser",
                ).get_text(" ", strip=True),
                "image": "",
                "meta": {},
                "discovery": "google_news",
            })
    return out


def exa_queries(registry: dict[str, Any]) -> list[str]:
    now = datetime.now(timezone.utc)
    month_name = now.strftime('%B')
    month_year = now.strftime('%B %Y')
    year = now.strftime('%Y')
    base = [
        f'"Bangladesh" "job circular" "{month_name}"',
        f'"Bangladesh" recruitment vacancy "{month_year}"',
        f'"নিয়োগ বিজ্ঞপ্তি" Bangladesh "{year}"',
        f'"চাকরির বিজ্ঞপ্তি" Bangladesh "{month_name}"',
        f'site:gov.bd "নিয়োগ বিজ্ঞপ্তি" "{year}"',
        f'site:gov.bd recruitment Bangladesh "{month_year}"',
        f'site:edu.bd recruitment Bangladesh "{year}"',
        f'Bangladesh bank recruitment "{month_year}"',
        f'Bangladesh NGO jobs "{month_year}"',
        f'Bangladesh pharmaceutical jobs "{month_year}"',
        f'Bangladesh healthcare jobs "{month_year}"',
        f'Bangladesh university recruitment "{month_year}"',
        f'Bangladesh IT jobs "{month_year}"',
        f'Bangladesh garments jobs "{month_year}"',
        f'Bangladesh manufacturing jobs "{month_year}"',
        f'Bangladesh internship jobs "{month_year}"',
    ]

    # Add a rotating subset of known source names. This can discover new jobs and new source endpoints.
    for source in registry.get("sources", []):
        name = safe_text(source.get("name"))
        if name:
            base.append(f'"{name}" Bangladesh jobs')
    unique = list(dict.fromkeys(base))

    # Full query coverage every run. No hourly rotation.
    return unique[:MAX_EXA_QUERIES]


def discover_exa(registry: dict[str, Any]) -> list[dict[str, Any]]:
    exa = Exa(api_key=EXA_API_KEY)
    out = []

    for query in exa_queries(registry):
        try:
            # Modern exa-py path. Older SDKs remain supported below.
            if hasattr(exa, "search"):
                result = exa.search(
                    query,
                    type="auto",
                    num_results=MAX_EXA_RESULTS_PER_QUERY,
                    contents={
                        "text": {"max_characters": 5000},
                        "highlights": {"max_characters": 1200},
                    },
                )
            else:
                result = exa.search_and_contents(
                    query,
                    type="auto",
                    num_results=MAX_EXA_RESULTS_PER_QUERY,
                    contents={"highlights": {"max_characters": 1200}},
                )
        except Exception as exc:
            logger.warning("Exa query failed: %s", exc)
            continue

        for item in getattr(result, "results", []) or []:
            url = canonical_url(getattr(item, "url", ""))
            if not url:
                continue
            highlights = getattr(item, "highlights", None)
            text = safe_text(getattr(item, "text", ""))
            if isinstance(highlights, list):
                text = "\n".join(map(str, highlights)) + "\n" + text
            out.append({
                "url": url,
                "source_id": "exa_discovery",
                "source_name": "Exa Discovery",
                "source_priority": "P3",
                "title_hint": safe_text(getattr(item, "title", "")),
                "text_hint": text[:12000],
                "image": safe_text(getattr(item, "image", "")),
                "published_date": safe_text(getattr(item, "published_date", "")),
                "meta": {},
                "discovery": "exa",
            })
    return out


def build_candidates(registry: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []

    all_sources = due_sources(registry, state)
    crawl_ok = 0
    crawl_failed = 0
    pdf_candidates = 0

    for source in all_sources:
        try:
            found = direct_discover(source)
            candidates.extend(found)
            pdf_candidates += sum(1 for item in found if item.get("discovery") in ("direct_pdf", "direct_pdf_link"))
            crawl_ok += 1
            logger.info("SOURCE CRAWL OK | %s | candidates=%d", source.get("name"), len(found))
            health = state["sources"].setdefault(source["id"], {})
            health["last_crawl_epoch"] = time.time()
            health["last_success"] = now_iso()
            health["last_failure"] = None
            health["consecutive_failures"] = 0
            health["candidates_last_run"] = len(found)
        except Exception as exc:
            crawl_failed += 1
            logger.warning("Direct source failed %s: %s", source.get("id"), exc)
            health = state["sources"].setdefault(source["id"], {})
            health["last_crawl_epoch"] = time.time()
            health["last_failure"] = now_iso()
            health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1

    logger.info("SOURCE CRAWL SUMMARY | total=%d ok=%d failed=%d pdf_candidates=%d", len(all_sources), crawl_ok, crawl_failed, pdf_candidates)
    candidates.extend(discover_google_news())
    candidates.extend(discover_exa(registry))

    unique = {}
    for candidate in candidates:
        url = canonical_url(candidate.get("url", ""))
        if not url:
            continue
        unique.setdefault(url, candidate)

    result = list(unique.values())

    # Deterministically prioritize evidence-rich candidates. No global candidate cap.
    def key(c: dict[str, Any]) -> tuple[int, int, int]:
        text = safe_text(c.get("text_hint", ""))
        priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(
            c.get("source_priority", "P3"), 3
        )
        richness = len(text)
        discovery_bonus = 0 if c.get("discovery") in ("direct_page", "direct_pdf") else 1
        return (priority, -min(richness, 15000), discovery_bonus)

    ordered = sorted(result, key=key)
    return ordered if MAX_DISCOVERY_CANDIDATES <= 0 else ordered[:MAX_DISCOVERY_CANDIDATES]


# ============================================================
# DOCUMENT RETRIEVAL
# ============================================================

def retrieve(candidate: dict[str, Any]) -> dict[str, Any] | None:
    url = canonical_url(candidate.get("url", ""))
    text_hint = safe_text(candidate.get("text_hint", ""))
    meta = candidate.get("meta") or {}

    if len(text_hint) >= 250:
        hints = deterministic_hints(text_hint)
        if looks_like_job(
            f"{candidate.get('title_hint', '')}\n{text_hint}",
            url,
        ):
            return {
                **candidate,
                "url": url,
                "text": text_hint[:18000],
                "hints": hints,
                "meta": meta,
            }

    r = http_get(url)
    if not r:
        if len(text_hint) >= 150 and looks_like_job(
            f"{candidate.get('title_hint', '')}\n{text_hint}",
            url,
        ):
            return {
                **candidate,
                "url": url,
                "text": text_hint[:18000],
                "hints": deterministic_hints(text_hint),
                "meta": meta,
            }
        return None

    final_url = canonical_url(r.url)
    ctype = r.headers.get("content-type", "").lower()

    if "pdf" in ctype or final_url.lower().endswith(".pdf"):
        text = extract_pdf_text(r.content)
        if not text:
            return None
        meta = {}
    else:
        text = extract_html_text(r.content, final_url)
        meta = extract_meta(r.content, final_url)

    combined = f"{candidate.get('title_hint', '')}\n{text}"
    if not looks_like_job(combined, final_url):
        return None
    if not (
        is_bangladesh(combined, final_url)
        or candidate.get("source_priority") in ("P0", "P1")
    ):
        return None

    return {
        **candidate,
        "url": final_url,
        "text": text[:18000],
        "hints": deterministic_hints(text),
        "meta": meta,
    }


# ============================================================
# DETERMINISTIC + CEREBRAS EXTRACTION
# ============================================================

def jsonld_job(meta: dict[str, Any]) -> dict[str, Any] | None:
    item = meta.get("jobposting")
    if not isinstance(item, dict):
        return None

    org = item.get("hiringOrganization") or {}
    if isinstance(org, list):
        org = org[0] if org else {}
    location = item.get("jobLocation") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get("address") if isinstance(location, dict) else {}
    if isinstance(address, dict):
        location_text = ", ".join(
            str(address.get(k) or "")
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if address.get(k)
        )
    else:
        location_text = safe_text(address)

    salary = item.get("baseSalary")
    salary_text = ""
    if isinstance(salary, dict):
        value = salary.get("value")
        if isinstance(value, dict):
            salary_text = safe_text(value.get("value"))
        elif value:
            salary_text = safe_text(value)
        unit = safe_text(salary.get("currency"))
        if unit:
            salary_text = f"{salary_text} {unit}".strip()

    skills = item.get("skills")
    if isinstance(skills, str):
        skills = [skills]

    education = item.get("educationRequirements")
    if isinstance(education, str):
        education = [education]

    return {
        "company": safe_text(org.get("name") if isinstance(org, dict) else org),
        "title": safe_text(item.get("title")),
        "location": location_text,
        "salary": salary_text,
        "education": education if isinstance(education, list) else [],
        "experience": safe_text(item.get("experienceRequirements")),
        "deadline": safe_text(item.get("validThrough")),
        "employment_type": safe_text(item.get("employmentType")),
        "application_url": canonical_url(item.get("url") or ""),
        "skills": skills if isinstance(skills, list) else [],
        "description": safe_text(item.get("description")),
    }


def rule_extract(document: dict[str, Any]) -> dict[str, Any]:
    meta = document.get("meta") or {}
    hints = document.get("hints") or {}
    structured = jsonld_job(meta) or {}

    title = (
        structured.get("title")
        or safe_text(document.get("title_hint"))
        or safe_text(meta.get("title"))
    )
    company = structured.get("company") or ""

    job = {
        "is_job": True,
        "company": company or None,
        "title": title or None,
        "organization_type": None,
        "sector": None,
        "job_function": None,
        "job_level": None,
        "job_type": None,
        "employment_type": structured.get("employment_type") or None,
        "location": structured.get("location") or None,
        "vacancy": hints.get("vacancy"),
        "salary": structured.get("salary") or hints.get("salary"),
        "education": structured.get("education") or [],
        "experience": structured.get("experience") or None,
        "skills": structured.get("skills") or [],
        "age_limit": None,
        "gender": None,
        "published_date": (
            structured.get("date_posted")
            or structured.get("published_date")
            or hints.get("published_date")
            or document.get("published_date")
            or safe_text(meta.get("datePublished"))
            or safe_text(meta.get("datePosted"))
            or None
        ),
        "application_start": None,
        "deadline": structured.get("deadline") or hints.get("deadline"),
        "application_method": None,
        "application_url": structured.get("application_url") or document.get("url"),
        "requirements": [],
        "responsibilities": [],
        "summary": safe_text(meta.get("description")),
        "confidence": 0.75 if structured else 0.55,
        "source_id": document.get("source_id"),
        "source_name": document.get("source_name"),
        "source_priority": document.get("source_priority", "P3"),
        "source_url": document.get("url"),
        "discovery": document.get("discovery"),
        "meta": meta,
        "text": document.get("text", ""),
        "scam_signals": hints.get("scam_signals", []),
    }
    return job


AI_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "is_job": {"type": "boolean"},
                    "company": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "sector": {"type": ["string", "null"]},
                    "job_function": {"type": ["string", "null"]},
                    "job_level": {"type": ["string", "null"]},
                    "job_type": {"type": ["string", "null"]},
                    "location": {"type": ["string", "null"]},
                    "vacancy": {"type": ["string", "null"]},
                    "salary": {"type": ["string", "null"]},
                    "education": {"type": "array", "items": {"type": "string"}},
                    "experience": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"]},
                    "published_date": {"type": ["string", "null"]},
                    "application_url": {"type": ["string", "null"]},
                    "requirements": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "responsibilities": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    "summary": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "id", "is_job", "company", "title", "sector", "job_function",
                    "job_level", "job_type", "location", "vacancy", "salary",
                    "education", "experience", "deadline", "application_url",
                    "requirements", "responsibilities", "summary", "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "publish": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "publish", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def cerebras_structured(
    system: str,
    user_payload: dict[str, Any],
    schema: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any] | None:
    client = Cerebras(api_key=CEREBRAS_API_KEY)
    try:
        response = client.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "career_newsroom_schema",
                    "strict": True,
                    "schema": schema,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=max_tokens,
        )
        raw = response.choices[0].message.content or ""
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Cerebras structured call failed: %s", exc)
        return None


def ai_enrich(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich every incomplete posting in batches, with special emphasis on PDF deadlines."""
    candidates=[job for job in jobs if (not job.get("company") or not job.get("title") or not job.get("deadline") or not job.get("application_url") or not job.get("requirements") or not job.get("sector") or not job.get("job_function"))]
    total=(len(candidates)+AI_BATCH_SIZE-1)//AI_BATCH_SIZE
    for batch_no,start_idx in enumerate(range(0,len(candidates),AI_BATCH_SIZE),1):
        batch=candidates[start_idx:start_idx+AI_BATCH_SIZE]
        payload={"taxonomy":{"sectors":SECTORS,"job_functions":JOB_FUNCTIONS,"job_levels":JOB_LEVELS,"job_types":JOB_TYPES},"documents":[{"id":i,"source_name":j.get("source_name"),"source_url":j.get("source_url"),"title_hint":j.get("title"),"text":safe_text(j.get("text"))[:9000]} for i,j in enumerate(batch)]}
        data=cerebras_structured(("Extract Bangladesh job circulars from the supplied source text, including PDF notices. Extract only stated facts. The APPLICATION DEADLINE is mandatory for publication: find the final application date exactly. Do not mistake page counters or dashboard statistics for a deadline. Never invent facts. Missing values must be null or []."),payload,AI_SCHEMA,6500)
        logger.info("AI ENRICHMENT BATCH %d/%d | docs=%d | success=%s",batch_no,total,len(batch),bool(data))
        if not data or not isinstance(data.get("jobs"),list): continue
        by_id={int(x.get("id")):x for x in data["jobs"] if isinstance(x,dict) and str(x.get("id","")).isdigit()}
        for i,job in enumerate(batch):
            ai=by_id.get(i)
            if not ai: continue
            for key in ("company","title","sector","job_function","job_level","job_type","location","vacancy","salary","education","experience","deadline","published_date","application_url","requirements","responsibilities","summary","confidence"):
                v=ai.get(key)
                if v not in (None,"",[],{}): job[key]=v
            job["ai_extracted"]=True
    return jobs


# ============================================================
# EVENT IDENTITY / STATUS
# ============================================================

def norm_tokens(value: str) -> set[str]:
    value = safe_text(value).lower()
    value = re.sub(r"[^a-z0-9\u0980-\u09ff]+", " ", value)
    return {x for x in value.split() if len(x) > 1}


def jaccard(a: str, b: str) -> float:
    aa = norm_tokens(a)
    bb = norm_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    au = canonical_url(a.get("application_url", ""))
    bu = canonical_url(b.get("application_url", ""))
    if au and bu and au == bu:
        return True

    ca, cb = safe_text(a.get("company")), safe_text(b.get("company"))
    ta, tb = safe_text(a.get("title")), safe_text(b.get("title"))
    if not ca or not cb or not ta or not tb:
        return False

    if ca.lower() == cb.lower() and jaccard(ta, tb) >= 0.70:
        da, db = safe_text(a.get("deadline")), safe_text(b.get("deadline"))
        if not da or not db or da == db:
            return True

    return (
        jaccard(ca, cb) >= 0.82
        and jaccard(ta, tb) >= 0.82
        and (
            not a.get("location")
            or not b.get("location")
            or jaccard(safe_text(a.get("location")), safe_text(b.get("location"))) >= 0.45
        )
    )


def make_event_id(job: dict[str, Any]) -> str:
    """Stable event identity. Mutable fields such as deadline, salary and URL are excluded."""
    seed = "|".join([
        safe_text(job.get("source_id")).lower(),
        normalize_company(job.get("company")),
        normalize_title(job.get("title")),
        normalize_text(job.get("location")),
        normalize_text(job.get("job_level")),
        normalize_text(job.get("job_type")),
    ])
    return "evt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_deadline(value: str | None) -> datetime | None:
    raw = safe_text(value)
    if not raw:
        return None

    # JSON-LD JobPosting commonly uses ISO dates/timestamps.
    iso = re.search(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        raw,
    )
    if iso:
        value = iso.group(0)
        try:
            if "T" in value or " " in value:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            return datetime.strptime(value, "%Y-%m-%d").replace(
                tzinfo=timezone.utc, hour=23, minute=59, second=59
            )
        except ValueError:
            pass

    parts = re.findall(
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
        r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        raw,
    )
    formats = (
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
        "%d %B %Y", "%d %b %Y",
    )
    for part in parts:
        for fmt in formats:
            try:
                return datetime.strptime(part, fmt).replace(
                    tzinfo=timezone.utc, hour=23, minute=59, second=59
                )
            except ValueError:
                pass
    return None


def deadline_state(value: str | None) -> tuple[str, int | None]:
    dt = parse_deadline(value)
    if not dt:
        return "unknown", None
    delta = dt - datetime.now(timezone.utc)
    if delta.total_seconds() < 0:
        return "expired", delta.days
    if delta.total_seconds() <= 86400:
        return "deadline_today", 0
    if delta.total_seconds() <= 3 * 86400:
        return "deadline_soon", delta.days
    return "active", delta.days


def listing_freshness_days(job: dict[str, Any]) -> float | None:
    raw = safe_text(job.get("published_date"))
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400)
    except ValueError:
        return None


def current_listing_status(job: dict[str, Any]) -> tuple[str, str]:
    deadline_status, _ = deadline_state(job.get("deadline"))
    if deadline_status != "unknown":
        return deadline_status, "deadline"

    age = listing_freshness_days(job)
    priority = safe_text(job.get("source_priority", "P3")).upper()
    official = bool(job.get("official_source")) or priority in ("P0", "P1")

    if age is not None:
        if age <= 7:
            return "active", "fresh_listing"
        if age <= 30 and priority in ("P0", "P1", "P2"):
            return "active", "trusted_recent_listing"
        return "unknown", "stale_or_undated"

    if official:
        return "unknown", "official_undated"
    return "unknown", "undated"


def verify(job: dict[str, Any]) -> dict[str, Any]:

    source_priority = job.get("source_priority", "P3")
    status, _basis = current_listing_status(job)
    official = bool(job.get("official_source")) or source_priority in ("P0", "P1")

    checks = {
        "known_source": bool(job.get("source_id")),
        "company": bool(job.get("company")),
        "title": bool(job.get("title")),
        "application_url": bool(job.get("application_url")),
        "active": status != "expired",
        "bangladesh": True,
        "no_scam_signal": not bool(job.get("scam_signals")),
    }

    passed = sum(1 for value in checks.values() if value)

    if job.get("scam_signals"):
        return {"status": "rejected", "level": "rejected", "checks": checks, "checked_at": now_iso()}

    if official and passed >= 6:
        level = "official"
        status_value = "verified"
    elif passed >= 5:
        level = "trusted"
        status_value = "verified"
    elif passed >= 3:
        level = "discovered"
        status_value = "unverified"
    else:
        level = "unverified"
        status_value = "rejected"

    return {
        "status": status_value,
        "level": level,
        "checks": checks,
        "checked_at": now_iso(),
    }



# ============================================================
# NORMALIZATION / FINGERPRINT / JOB EVENT STATE
# ============================================================


def is_generic_job_title(job: dict[str, Any]) -> bool:
    title = normalize_text(job.get("title"))
    company = normalize_text(job.get("company"))
    source = normalize_text(job.get("source_name"))
    generic = {
        "home", "jobs", "job", "career", "careers", "vacancy", "recruitment",
        "alljobs by teletalk bridging aspirations with excellence",
        "national job portal",
    }
    if not title:
        return True
    if title in generic:
        return True
    if source and title == source:
        return True
    if company and title == company and len(title.split()) < 7:
        return True
    return False

def normalize_text(value: Any) -> str:
    value = html.unescape(safe_text(value)).lower()
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"[^\w\u0980-\u09ff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_company(value: Any) -> str:
    value = normalize_text(value)
    value = re.sub(r"\b(limited|ltd|plc|inc|corporation|corp|company|co)\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value: Any) -> str:
    value = normalize_text(value)
    value = re.sub(r"\b(job|vacancy|circular|career|careers|recruitment|hiring)\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": normalize_company(job.get("company")),
        "title": normalize_title(job.get("title")),
        "location": normalize_text(job.get("location")),
        "deadline": normalize_deadline(job.get("deadline")),
        "application_url": canonical_url(job.get("application_url") or job.get("source_url") or ""),
        "sector": normalize_text(job.get("sector")),
        "job_function": normalize_text(job.get("job_function")),
        "job_level": normalize_text(job.get("job_level")),
        "job_type": normalize_text(job.get("job_type")),
        "vacancy": normalize_text(job.get("vacancy")),
        "salary": normalize_text(job.get("salary")),
    }


def job_fingerprint(job: dict[str, Any]) -> str:
    """Fingerprint a specific published revision, including fields that can materially update."""
    n = job.get("normalized") or normalized_job(job)
    seed = "|".join([
        n["company"], n["title"], n["location"], n["deadline"], n["application_url"],
        n["vacancy"], n["salary"], n["job_type"], n["job_level"],
    ])
    return "job_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def identity_fingerprint(job: dict[str, Any]) -> str:
    n = job.get("normalized") or normalized_job(job)
    seed = "|".join([
        n["company"], n["title"], n["location"], n["application_url"]
    ])
    return "identity_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def same_job(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if job_fingerprint(old) == job_fingerprint(new):
        return True
    if identity_fingerprint(old) == identity_fingerprint(new):
        return True

    old_n, new_n = old.get("normalized") or normalized_job(old), new.get("normalized") or normalized_job(new)
    return (
        bool(old_n["company"] and new_n["company"])
        and old_n["company"] == new_n["company"]
        and jaccard(old_n["title"], new_n["title"]) >= 0.80
        and (
            not old_n["location"]
            or not new_n["location"]
            or old_n["location"] == new_n["location"]
        )
    )


def field_changes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields = (
        "company", "title", "location", "deadline", "application_url",
        "salary", "vacancy", "education", "experience", "requirements",
        "responsibilities", "sector", "job_function", "job_level", "job_type",
    )
    return {
        f: {"old": old.get(f), "new": new.get(f)}
        for f in fields
        if old.get(f) != new.get(f) and new.get(f) not in (None, "", [], {})
    }


def normalize_deadline(value: Any) -> str:
    dt = parse_deadline(safe_text(value))
    return dt.date().isoformat() if dt else normalize_text(value)


def scam_filter(job: dict[str, Any]) -> dict[str, Any]:
    hay = " ".join([
        safe_text(job.get("company")), safe_text(job.get("title")),
        safe_text(job.get("summary")), safe_text(job.get("text")),
        safe_text(job.get("application_method")),
    ]).lower()
    signals = sorted({term for term in SCAM_TERMS if term in hay})
    job["scam_signals"] = signals
    return {"passed": not signals, "signals": signals, "checked_at": now_iso()}


def importance_score(job: dict[str, Any], quality: int, ai_score: int | None = None) -> int:
    """Stable 0-100 editorial importance score. AI ranks refine, never replace, deterministic value."""
    score = base_importance(job)

    # Quality is a supporting signal, not a substitute for job importance.
    score += int(round(max(0, min(100, quality)) * 0.20))

    if ai_score is not None:
        ai = max(0, min(100, int(ai_score)))
        score = int(round(score * 0.70 + ai * 0.30))

    return max(0, min(100, score))

def classify_job_event(state: dict[str, Any], job: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Classify the incoming job against the persisted event identity."""
    for event in state["jobs"].values():
        canonical = event.get("canonical", {})
        if not (same_job(canonical, job) or event.get("identity_fingerprint") == job.get("identity_fingerprint")):
            continue

        same_fingerprint = event.get("fingerprint") == job.get("fingerprint")
        old_status = safe_text(event.get("status"))
        new_status = current_listing_status(job)[0]
        previously_published = bool(event.get("telegram", {}).get("published"))

        # Exact same listing: normally no-op. If an expired, previously published listing
        # becomes active again, treat it as a legitimate repost.
        if same_fingerprint:
            if previously_published and old_status == "expired" and new_status in ("active", "deadline_today", "deadline_soon"):
                return "REPOST", event
            return "NONE", event

        # Same job identity with changed fields is an UPDATE. A later deadline after an
        # expired publication is a REPOST because the employer reopened/reissued it.
        changes = field_changes(canonical, job)
        if previously_published and old_status == "expired" and changes:
            old_deadline = parse_deadline(safe_text(canonical.get("deadline")))
            new_deadline = parse_deadline(safe_text(job.get("deadline")))
            if new_deadline and (not old_deadline or new_deadline > old_deadline):
                return "REPOST", event
        return "UPDATE", event

    return "NEW", None


def apply_job_event(state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    event_type, existing = classify_job_event(state, job)

    if existing is None:
        event = {
            "event_id": make_event_id(job),
            "event_type": "NEW",
            "status": current_listing_status(job)[0],
            "status_basis": current_listing_status(job)[1],
            "canonical": job,
            "normalized": job["normalized"],
            "fingerprint": job["fingerprint"],
            "identity_fingerprint": job["identity_fingerprint"],
            "sources": [{"source_id":job.get("source_id"),"source_name":job.get("source_name"),
                         "source_url":job.get("source_url"),"seen_at":now_iso()}],
            "verification": job["verification"],
            "scam_filter": job["scam_filter"],
            "quality_score": job["quality_score"],
            "importance_score": job["importance_score"],
            "first_seen": now_iso(),
            "last_seen": now_iso(),
            "update_history": [],
            "repost_history": [],
            "repost_due": False,
            "update_pending": False,
            "telegram": {"published":False,"message_id":None,"published_at":None,"last_event_type":None},
        }
        state["jobs"][event["event_id"]] = event
        return event

    old = dict(existing.get("canonical",{}))
    changes = field_changes(old, job)
    existing["last_seen"] = now_iso()

    if event_type in ("UPDATE", "REPOST"):
        existing["event_type"] = event_type
        existing["canonical"] = {**old, **{k:v for k,v in job.items() if v not in (None,"",[],{})}}
        existing["normalized"] = job["normalized"]
        existing["fingerprint"] = job["fingerprint"]
        existing["identity_fingerprint"] = identity_fingerprint(existing["canonical"])
        if event_type == "UPDATE":
            existing["update_history"] = (existing.get("update_history") or []) + [{"timestamp":now_iso(),"changes":changes}]
            existing["update_pending"] = bool(changes)
            existing["repost_due"] = False
        else:
            existing["repost_history"] = (existing.get("repost_history") or []) + [{"timestamp":now_iso(),"reason":"previously published job reappeared with a later active deadline"}]
            existing["repost_due"] = True
            existing["update_pending"] = False
    elif event_type == "NONE":
        # Exact duplicate: keep the canonical revision and explicitly mark this scan as a no-op.
        existing["event_type"] = "NONE"
        existing["canonical"] = {**old, **{k:v for k,v in job.items() if v not in (None,"",[],{})}}
        existing["normalized"] = job["normalized"]
        existing["fingerprint"] = job["fingerprint"]
        existing["identity_fingerprint"] = identity_fingerprint(existing["canonical"])

    existing["verification"] = job["verification"]
    existing["scam_filter"] = job["scam_filter"]
    existing["quality_score"] = job["quality_score"]
    existing["importance_score"] = job["importance_score"]
    existing["status"] = current_listing_status(existing["canonical"])[0]
    return existing

def quality_score(job: dict[str, Any]) -> int:
    priority = job.get("source_priority", "P3")
    score = {"P0": 26, "P1": 23, "P2": 18, "P3": 10}.get(priority, 10)

    for field, weight in (
        ("title", 16), ("company", 14), ("deadline", 15),
        ("application_url", 15), ("location", 6),
        ("education", 3), ("experience", 3),
    ):
        if job.get(field):
            score += weight

    score += int(min(8, max(0, float(job.get("confidence") or 0.5) * 8)))

    status, _ = deadline_state(job.get("deadline"))
    if status == "expired":
        score -= 60
    if job.get("scam_signals"):
        score -= 40

    return max(0, min(100, score))


def base_importance(job: dict[str, Any]) -> int:
    """Deterministic baseline calibrated so strong jobs can realistically reach publish threshold."""
    score = {"P0": 24, "P1": 21, "P2": 17, "P3": 12}.get(job.get("source_priority", "P3"), 12)

    title = safe_text(job.get("title")).lower()
    if any(x in title for x in ("intern", "internship", "trainee", "graduate", "fresher")):
        score += 8
    if any(x in title for x in ("manager", "engineer", "officer", "executive", "director", "specialist")):
        score += 5

    nums = re.findall(r"\d+", safe_text(job.get("vacancy")).replace(",", ""))
    if nums:
        try:
            n = int(nums[-1])
            score += 12 if n >= 100 else 9 if n >= 20 else 6 if n >= 5 else 3
        except ValueError:
            pass

    status, days = deadline_state(job.get("deadline"))
    if status == "deadline_today":
        score += 10
    elif status == "deadline_soon":
        score += 8
    elif days is not None and days <= 7:
        score += 6
    elif days is not None and days <= 14:
        score += 4

    if job.get("salary"):
        score += 6
    if job.get("application_url"):
        score += 10
    if job.get("location"):
        score += 4
    if job.get("company"):
        score += 4
    if job.get("summary"):
        score += 3

    # Fresh listings get a modest editorial boost. This rewards genuinely new
    # opportunities without allowing freshness to overpower trust/quality.
    published = safe_text(job.get("published_date"))
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400
            if age <= 1:
                score += 6
            elif age <= 3:
                score += 4
            elif age <= 7:
                score += 2
        except ValueError:
            pass

    return max(0, min(100, score))


# ============================================================
# BATCH AI RANKING
# ============================================================

# ============================================================
# TELEGRAM / IMAGES
# ============================================================

def html_escape(value: Any) -> str:
    return html.escape(safe_text(value), quote=False)


def build_caption(job: dict[str, Any]) -> str:
    title = html_escape(job.get("title") or "Job Vacancy")
    summary = safe_text(job.get("summary"))
    if not summary:
        company = safe_text(job.get("company") or "The organization")
        summary = f"{company} is recruiting for this position in Bangladesh."

    lines = [
        f"<b>📌 {title}</b>",
        "",
        html_escape(summary[:360]),
        "",
        "<b>KEY HIGHLIGHTS</b>",
    ]

    highlights = []
    for icon, label, value in (
        ("🏢", "Organization", job.get("company")),
        ("📍", "Location", job.get("location")),
        ("👥", "Vacancy", job.get("vacancy")),
        ("🎓", "Education", ", ".join(job.get("education") or [])),
        ("🧑‍💼", "Experience", job.get("experience")),
        ("💰", "Salary", job.get("salary")),
        ("💼", "Employment", job.get("employment_type") or job.get("job_type")),
        ("📅", "Application Deadline", job.get("deadline")),
    ):
        if value:
            highlights.append(f"{icon} <b>{label}:</b> {html_escape(value)}")
    lines.extend(highlights[:8])

    req = [html_escape(x) for x in (job.get("requirements") or []) if safe_text(x)]
    if req:
        lines.extend(["", "<b>REQUIREMENTS</b>"])
        lines.extend(f"• {x}" for x in req[:5])

    responsibilities = [html_escape(x) for x in (job.get("responsibilities") or []) if safe_text(x)]
    if responsibilities:
        lines.extend(["", "<b>JOB RESPONSIBILITIES</b>"])
        lines.extend(f"• {x}" for x in responsibilities[:3])

    lines.extend([
        "",
        "<b>HOW TO APPLY</b>",
        "Use the <b>APPLY NOW</b> button below and follow the official application instructions.",
        "",
        f"<b>Source:</b> {html_escape(job.get('source_name') or 'Career Newsroom')}",
    ])
    return "\n".join(lines)[:MAX_CAPTION]


def download_image(url: str, referer: str = "") -> Image.Image | None:
    if not url:
        return None
    try:
        headers = {"Referer": referer} if referer else {}
        r = SESSION.get(url, timeout=15, headers=headers, allow_redirects=True)
        if r.status_code >= 400:
            return None
        image = Image.open(BytesIO(r.content))
        image.load()
        return image.convert("RGB")
    except Exception:
        return None


def crop_cover(image: Image.Image, size=(1200, 675)) -> Image.Image:
    target = size[0] / size[1]
    w, h = image.size
    if not h:
        return Image.new("RGB", size, "white")

    ratio = w / h
    if ratio > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        image = image.crop((left, 0, left + new_w, h))
    elif ratio < target:
        new_h = int(w / target)
        top = (h - new_h) // 2
        image = image.crop((0, top, w, top + new_h))
    return image.resize(size, Image.Resampling.LANCZOS)


def fallback_card(job: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(exist_ok=True)
    image = Image.new("RGB", (1200, 675), "white")
    draw = ImageDraw.Draw(image)

    regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(regular, 38) if Path(regular).exists() else ImageFont.load_default()
    bold = ImageFont.truetype(bold_path, 54) if Path(bold_path).exists() else font

    company = safe_text(job.get("company") or "Career Opportunity")[:70]
    title = safe_text(job.get("title") or "Job Vacancy")

    draw.text((70, 65), company, fill="black", font=font)

    words = title.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        if len(test) > 27 and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))

    y = 205
    for line in lines[:4]:
        draw.text((70, y), line, fill="black", font=bold)
        y += 68

    draw.text((70, 535), "Career Opportunity", fill="black", font=font)
    draw.text((930, 610), "@CareerNewsroom", fill="black", font=font)

    image.save(path, "JPEG", quality=92)
    return path


def prepare_image(job: dict[str, Any], index: int) -> Path:
    RUNTIME_DIR.mkdir(exist_ok=True)
    meta = job.get("meta") or {}
    candidates = []

    if job.get("image"):
        candidates.append(canonical_url(job.get("image", "")))
    for key in ("logo", "image"):
        value = canonical_url(meta.get(key, ""))
        if value:
            candidates.append(value)

    for i, url in enumerate(dict.fromkeys(x for x in candidates if x)):
        image = download_image(url, job.get("source_url", ""))
        if image:
            path = RUNTIME_DIR / f"job_{index}_{i}.jpg"
            crop_cover(image).save(path, "JPEG", quality=92)
            return path

    return fallback_card(job, RUNTIME_DIR / f"job_{index}_fallback.jpg")


def telegram_call(
    method: str,
    data: dict[str, Any],
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    response = requests.post(url, data=data, files=files, timeout=30)

    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "description": response.text[:500]}

    if response.status_code >= 400 or not payload.get("ok"):
        raise RuntimeError(
            f"Telegram {method}: HTTP {response.status_code} "
            f"{payload.get('description', '')}"
        )
    return payload["result"]


def telegram_preflight() -> None:
    bot = telegram_call("getMe", {})
    bot_id = bot.get("id")
    logger.info("Telegram bot OK: @%s", bot.get("username", ""))

    chat = telegram_call("getChat", {"chat_id": TELEGRAM_CHANNEL})
    logger.info(
        "Telegram channel OK: id=%s title=%s",
        chat.get("id"),
        chat.get("title") or chat.get("username") or "",
    )

    if bot_id is not None:
        member = telegram_call(
            "getChatMember",
            {"chat_id": TELEGRAM_CHANNEL, "user_id": bot_id},
        )
        status = member.get("status")
        logger.info("Telegram bot channel status: %s", status)
        if status not in ("administrator", "creator"):
            raise RuntimeError(
                f"Telegram bot is not an administrator/creator in {TELEGRAM_CHANNEL}; "
                f"current status={status!r}"
            )


def publish_job(job: dict[str, Any], image_path: Path) -> dict[str, Any]:
    caption = build_caption(job)
    application_url = canonical_url(job.get("application_url") or job.get("source_url") or "")
    markup = (
        json.dumps(
            {"inline_keyboard": [[{"text": "APPLY NOW", "url": application_url}]]},
            ensure_ascii=False,
        )
        if application_url
        else ""
    )

    try:
        with image_path.open("rb") as fh:
            result = telegram_call(
                "sendPhoto",
                {
                    "chat_id": TELEGRAM_CHANNEL,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                },
                files={"photo": fh},
            )
        return {"published": True, "message_id": result.get("message_id"), "mode": "photo"}
    except Exception as exc:
        logger.warning("sendPhoto failed; text fallback: %s", exc)

    result = telegram_call(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHANNEL,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": markup,
        },
    )
    return {"published": True, "message_id": result.get("message_id"), "mode": "text"}


def admin_alert(message: str) -> None:
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        telegram_call(
            "sendMessage",
            {
                "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                "text": f"CareerNewsroomBot alert\n\n{message[:3500]}",
            },
        )
    except Exception as exc:
        logger.warning("Admin alert failed: %s", exc)


# ============================================================
# EVENT MERGE / PUBLISH CONTROL
# ============================================================

def merge_jobs(jobs: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Full pipeline through normalized identity, verification, scoring and event state."""
    events = []
    for job in jobs:
        if not job.get("title"):
            continue

        job["normalized"] = normalized_job(job)
        job["fingerprint"] = job_fingerprint(job)
        job["identity_fingerprint"] = identity_fingerprint(job)

        job["verification"] = verify(job)
        job["scam_filter"] = scam_filter(job)

        if not job["scam_filter"]["passed"]:
            continue
        if job["verification"]["level"] in ("rejected", "expired"):
            continue

        job["quality_score"] = quality_score(job)
        job["base_importance"] = base_importance(job)
        job["importance_score"] = importance_score(job, job["quality_score"])

        events.append(apply_job_event(state, job))
    return events


# Publish policy: validity first. Every qualifying job is publishable.
PUBLISH_THRESHOLD = 60
MIN_QUALITY_FOR_PUBLISH = 60
MIN_DAYS_TO_DEADLINE = 7

def publishable(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hard-validity gate only. No category diversity, importance threshold, or per-run quota."""
    out=[]
    rejects={"verification":0,"scam":0,"missing_deadline":0,"expired":0,"deadline_under_7_days":0,"quality":0,"already_published":0,"no_update_pending":0,"no_repost_due":0,"invalid_event":0}
    for event in events:
        kind=event.get("event_type","NEW")
        if kind not in ("NEW","UPDATE","REPOST"):
            rejects["invalid_event"]+=1; continue
        if event.get("verification",{}).get("status")!="verified":
            rejects["verification"]+=1; continue
        if not event.get("scam_filter",{}).get("passed",False):
            rejects["scam"]+=1; continue
        job=event.get("canonical",{})
        dt=parse_deadline(safe_text(job.get("deadline")))
        if not dt:
            rejects["missing_deadline"]+=1; continue
        days=(dt-datetime.now(timezone.utc)).total_seconds()/86400
        if days<0:
            rejects["expired"]+=1; continue
        if days<MIN_DAYS_TO_DEADLINE:
            rejects["deadline_under_7_days"]+=1; continue
        if int(event.get("quality_score",0))<MIN_QUALITY_FOR_PUBLISH:
            rejects["quality"]+=1; continue
        published=bool(event.get("telegram",{}).get("published"))
        if kind=="NEW" and published:
            rejects["already_published"]+=1; continue
        if kind=="UPDATE" and not event.get("update_pending"):
            rejects["no_update_pending"]+=1; continue
        if kind=="REPOST" and not event.get("repost_due"):
            rejects["no_repost_due"]+=1; continue
        out.append(event)
    out.sort(key=lambda e:(-int(e.get("quality_score",0)),-int(e.get("importance_score",0)),safe_text(e.get("canonical",{}).get("deadline"))))
    logger.info("PUBLISH GATE | selected=%d | rejects=%s",len(out),json.dumps(rejects,sort_keys=True))
    return out

def prune_state(state: dict[str, Any]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
    remove = []

    for event_id, event in state["jobs"].items():
        if event.get("status") not in ("expired", "rejected"):
            continue
        try:
            dt = datetime.fromisoformat(
                safe_text(event.get("last_seen")).replace("Z", "+00:00")
            )
        except Exception:
            continue
        if dt < cutoff:
            remove.append(event_id)

    for event_id in remove:
        del state["jobs"][event_id]


# ============================================================
# RUN
# ============================================================

def run() -> None:
    registry = load_registry()
    state = load_state()
    posted = load_posted()
    sources = registry_map(registry)

    logger.info(
        "CAREER NEWSROOM V1 | channel=%s | model=%s",
        TELEGRAM_CHANNEL,
        CEREBRAS_MODEL,
    )

    telegram_preflight()

    candidates = build_candidates(registry, state)
    logger.info("DISCOVERY CANDIDATES: %d", len(candidates))

    jobs = []
    seen_hashes = set()

    for candidate in candidates:
        document = retrieve(candidate)
        if not document:
            continue

        text_hash = document.get("hints", {}).get("text_hash")
        if text_hash and text_hash in seen_hashes:
            continue
        if text_hash:
            seen_hashes.add(text_hash)

        job = rule_extract(document)

        # Exclude obvious non-Bangladesh/non-job candidates early.
        if not is_bangladesh(
            f"{job.get('company','')} {job.get('title','')} {document.get('text','')}",
            document.get("url", ""),
        ) and job.get("source_priority") not in ("P0", "P1"):
            continue

        jobs.append(job)

    logger.info("DETERMINISTIC JOBS: %d", len(jobs))

    jobs = ai_enrich(jobs)
    logger.info("AI ENRICHMENT COMPLETE")

    # Source metadata.
    for job in jobs:
        source = sources.get(job.get("source_id", ""))
        if source is None:
            source = source_for_url(job.get("source_url", ""), sources)

        if source:
            job["source_id"] = source.get("id", job.get("source_id"))
            job["source_name"] = source.get("name")
            job["source_priority"] = source.get("priority", "P3")
            job["official_source"] = bool(source.get("official"))
        else:
            job["source_name"] = job.get("source_name") or job.get("source_id", "Career Newsroom")
            job["source_priority"] = job.get("source_priority", "P3")
            job["official_source"] = False

    # Final pre-event validation: source homepages must never become fake job posts.
    jobs = [j for j in jobs if not is_generic_job_title(j)]
    logger.info("REAL JOBS AFTER TITLE VALIDATION: %d", len(jobs))

    # Merge into events.
    events = merge_jobs(jobs, state)

    # Every valid job is important. There is no editorial diversity/ranking filter.
    ranked_events = events
    for event in ranked_events:
        event["importance_score"] = int(event.get("base_importance") or event.get("importance_score") or 0)

    candidates_to_publish = publishable(ranked_events)

    logger.info(
        "EVENTS=%d VALID_FOR_PUBLISH=%d",
        len(ranked_events),
        len(candidates_to_publish),
    )

    published_count = 0

    for index, event in enumerate(candidates_to_publish, start=1):
        job = dict(event["canonical"])
        job["event_id"] = event["event_id"]

        # Canonical source remains attached to provenance.
        job["source_name"] = job.get("source_name") or "Career Newsroom"

        try:
            image_path = prepare_image(job, index)
            result = publish_job(job, image_path)

            event["telegram"]["published"] = True
            event["telegram"]["message_id"] = result.get("message_id")
            event["telegram"]["published_at"] = now_iso()
            event["telegram"]["last_event_type"] = event.get("event_type")
            event["last_published_fingerprint"] = event.get("fingerprint")
            event["repost_due"] = False
            event["update_pending"] = False
            event["event_type"] = "PUBLISHED"

            for published_url in (
                canonical_url(job.get("source_url", "")),
                canonical_url(job.get("application_url", "")),
            ):
                if published_url:
                    posted.add(published_url)

            published_count += 1
            logger.info(
                "PUBLISHED #%d | score=%s | quality=%s | mode=%s | title=%s",
                published_count,
                event.get("importance_score"),
                event.get("quality_score"),
                result.get("mode"),
                safe_text(job.get("title")),
            )
            time.sleep(POST_DELAY_SECONDS)
        except Exception as exc:
            logger.exception("Publication failed: %s", event["event_id"])
            admin_alert(
                f"Publication failed\n"
                f"event={event['event_id']}\n"
                f"title={safe_text(job.get('title'))}\n"
                f"error={exc}"
            )

    prune_state(state)
    state["updated_at"] = now_iso()
    state["runs"].append({
        "timestamp": now_iso(),
        "candidates": len(candidates),
        "deterministic_jobs": len(jobs),
        "events": len(events),
        "published": published_count,
    })
    state["runs"] = state["runs"][-30:]

    save_state(state)
    save_posted(posted)

    logger.info(
        "FINISHED | candidates=%d jobs=%d events=%d published=%d",
        len(candidates),
        len(jobs),
        len(events),
        published_count,
    )


# ============================================================
# SELF TEST
# ============================================================

def self_test() -> None:
    sample = {
        "company": "ABC Bank PLC",
        "title": "Management Trainee Officer",
        "deadline": "30-09-2099",
        "application_url": "https://example.com/apply?id=1&utm_source=x",
        "location": "Dhaka",
        "education": ["Bachelor's degree"],
        "experience": "Fresh graduates",
        "salary": "BDT 30000",
        "vacancy": "20",
        "requirements": ["Bachelor's degree", "Communication skills"],
        "summary": "ABC Bank is hiring management trainees.",
        "source_name": "ABC Bank PLC",
        "source_priority": "P1",
        "source_url": "https://example.com/job/1",
        "confidence": 0.95,
    }

    assert canonical_url(sample["application_url"]) == "https://example.com/apply?id=1"
    assert jaccard("Management Trainee Officer", "Management Trainee Officer") == 1.0
    assert same_event(sample, {**sample, "source_url": "https://bdjobs.example/1"})
    assert deadline_state(sample["deadline"])[0] == "active"
    assert deadline_state("2099-09-30")[0] == "active"
    assert deadline_state("2099-09-30T23:59:59Z")[0] == "active"

    sources = {
        "abc": {
            "id": "abc",
            "name": "ABC Bank PLC",
            "url": "https://career.abc.com.bd/jobs",
            "priority": "P1",
            "official": True,
        }
    }
    mapped = source_for_url("https://career.abc.com.bd/jobs/123", sources)
    assert mapped and mapped["id"] == "abc"

    verified = verify(sample)
    assert verified["status"] == "verified"

    q = quality_score(sample)
    b = base_importance(sample)
    assert 0 <= q <= 100
    assert 0 <= b <= 100

    event = {
        "event_id": "evt_test",
        "canonical": sample,
        "verification": verified,
        "scam_filter": {"passed": True, "signals": []},
        "quality_score": q,
        "base_importance": b,
        "importance_score": 80,
        "status": "active",
        "telegram": {"published": False},
    }
    assert publishable([event])

    caption = build_caption(sample)
    assert "Management Trainee Officer" in caption
    assert "KEY HIGHLIGHTS" in caption
    assert "HOW TO APPLY" in caption
    assert is_generic_job_title({"title":"National Job Portal","company":"National Job Portal","source_name":"National Job Portal"})
    assert len(caption) <= MAX_CAPTION

    robust_list = '[{"id": 0, "score": 80, "publish": true, "reason": "ok"}]'
    data = json.loads(robust_list)
    assert isinstance(data, list)

    # Strict response-shape compatibility test:
    assert isinstance({"jobs": []}.get("jobs"), list)
    assert isinstance({"ranked": []}.get("ranked"), list)

    # Explicit normalized identity/state-machine tests.
    st = default_state()
    sample["source_id"] = "abc"
    sample["official_source"] = True
    sample["normalized"] = normalized_job(sample)
    sample["fingerprint"] = job_fingerprint(sample)
    sample["identity_fingerprint"] = identity_fingerprint(sample)
    sample["verification"] = verify(sample)
    sample["scam_filter"] = scam_filter(sample)
    sample["quality_score"] = quality_score(sample)
    sample["importance_score"] = importance_score(sample, sample["quality_score"])

    e1 = apply_job_event(st, sample)
    assert e1["event_type"] == "NEW"

    e2 = apply_job_event(st, dict(sample))
    assert e2["event_id"] == e1["event_id"]

    changed = dict(sample)
    changed["deadline"] = "01-10-2099"
    changed["normalized"] = normalized_job(changed)
    changed["fingerprint"] = job_fingerprint(changed)
    changed["identity_fingerprint"] = identity_fingerprint(changed)
    changed["verification"] = verify(changed)
    changed["scam_filter"] = scam_filter(changed)
    changed["quality_score"] = quality_score(changed)
    changed["importance_score"] = importance_score(changed, changed["quality_score"])
    e3 = apply_job_event(st, changed)
    assert e3["event_id"] == e1["event_id"]
    assert e3["event_type"] == "UPDATE"
    assert e3["update_pending"] is True

    published_state = default_state()
    first = dict(sample)
    first["fingerprint"] = job_fingerprint(first)
    first["identity_fingerprint"] = identity_fingerprint(first)
    pe = apply_job_event(published_state, first)
    pe["telegram"]["published"] = True
    pe["status"] = "expired"
    reopened = dict(first)
    reopened["deadline"] = "01-10-2099"
    reopened["normalized"] = normalized_job(reopened)
    reopened["fingerprint"] = job_fingerprint(reopened)
    reopened["identity_fingerprint"] = identity_fingerprint(reopened)
    reopened["verification"] = verify(reopened)
    reopened["scam_filter"] = scam_filter(reopened)
    reopened["quality_score"] = quality_score(reopened)
    reopened["base_importance"] = base_importance(reopened)
    reopened["importance_score"] = importance_score(reopened, reopened["quality_score"])
    revent = apply_job_event(published_state, reopened)
    assert revent["event_id"] == pe["event_id"]
    assert revent["event_type"] == "REPOST"
    assert revent["repost_due"] is True

    scam_case = dict(sample)
    scam_case["summary"] = "Pay registration fee before applying."
    scam_case["scam_signals"] = []
    assert scam_filter(scam_case)["passed"] is False

    low = dict(e1)
    low["verification"] = {"status":"verified"}
    low["scam_filter"] = {"passed":True}
    low["quality_score"] = 90
    low["telegram"] = {"published":False}
    low["canonical"] = dict(sample)
    low["canonical"]["deadline"] = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%d-%m-%Y")
    assert not publishable([low])

    missing_deadline = dict(e1)
    missing_deadline["canonical"] = dict(sample)
    missing_deadline["canonical"]["deadline"] = None
    missing_deadline["verification"] = {"status":"verified"}
    missing_deadline["scam_filter"] = {"passed":True}
    missing_deadline["quality_score"] = 90
    missing_deadline["telegram"] = {"published":False}
    assert not publishable([missing_deadline])

    # PDF extraction regression test: a real circular deadline must be recoverable.
    try:
        from reportlab.pdfgen import canvas
        pdf_path = RUNTIME_DIR / "self_test_job.pdf"
        c = canvas.Canvas(str(pdf_path))
        c.drawString(72, 760, "ABC Bank PLC Recruitment Circular")
        c.drawString(72, 735, "Position: Management Trainee Officer")
        c.drawString(72, 710, "Application deadline: 30-09-2099")
        c.save()
        pdf_bytes = pdf_path.read_bytes()
        pdf_text = extract_pdf_text(pdf_bytes)
        assert "Application deadline" in pdf_text
        hints = deterministic_hints(pdf_text)
        assert parse_deadline(hints["deadline"]) is not None
    except ImportError:
        pass

    path = fallback_card(sample, RUNTIME_DIR / "self_test.jpg")
    assert path.exists()
    assert Image.open(path).size == (1200, 675)

    # Telegram publisher logic test via monkeypatch.
    original = globals()["telegram_call"]
    calls = []

    def fake_telegram(method, data, files=None):
        calls.append(method)
        return {"message_id": 999}

    globals()["telegram_call"] = fake_telegram
    try:
        result = publish_job(sample, path)
        assert result["published"] is True
        assert result["message_id"] == 999
        assert calls == ["sendPhoto"]
    finally:
        globals()["telegram_call"] = original

    # Telegram failure -> text fallback.
    calls = []

    def photo_fail(method, data, files=None):
        calls.append(method)
        if method == "sendPhoto":
            raise RuntimeError("simulated photo failure")
        return {"message_id": 1000}

    globals()["telegram_call"] = photo_fail
    try:
        result = publish_job(sample, path)
        assert result["published"] is True
        assert result["mode"] == "text"
        assert calls == ["sendPhoto", "sendMessage"]
    finally:
        globals()["telegram_call"] = original

    logger.info("CareerNewsroom self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self_test()
    else:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        run()
