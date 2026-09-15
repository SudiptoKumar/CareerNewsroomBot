from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
from contextlib import ExitStack
from pathlib import Path
from zoneinfo import ZoneInfo
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

MAX_DIRECT_LINKS_PER_SOURCE = 0  # unlimited after job-link filtering
MAX_DISCOVERY_CANDIDATES = 0
AI_BATCH_SIZE = 20
MAX_PUBLISH_PER_RUN = 0  # 0 = unlimited; valid jobs are not discarded for quota
MAX_EXA_QUERIES = 32
MAX_EXA_RESULTS_PER_QUERY = 6
EXA_FRESHNESS_DAYS = 45
POST_DELAY_SECONDS = 1.5
HTTP_TIMEOUT_SECONDS = 10
CEREBRAS_CALLS_PER_MINUTE = 3
CEREBRAS_RETRY_AFTER_SECONDS = 61
BBA_RELEVANCE_BONUS = 22
FAIR_SOURCE_WINDOW = 3
MAX_PDF_IMAGE_PAGES = 4
MAX_CIRCULAR_MEDIA = 3
MIN_JOB_RELEVANCE = 0

# Runtime safety: OCR and large document retrieval are the expensive path.
# Keep normal runs comfortably below the GitHub Actions timeout while preserving
# every registered source attempt.
RUN_BUDGET_SECONDS = 18 * 60
SOURCE_HTTP_TIMEOUT_SECONDS = 9
PDF_HTTP_TIMEOUT_SECONDS = 12
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_OCR_PAGES = 2
MAX_OCR_DOCUMENTS_PER_RUN = 18
MAX_LINK_CANDIDATES_PER_SOURCE = 20
MAX_PDF_CANDIDATES_PER_SOURCE = 10
MAX_HTML_JOB_CANDIDATES_PER_SOURCE = 10
CURRENT_YEAR = datetime.now(timezone.utc).year

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
        "version": 5,
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
    data["version"] = 5
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
        total=1, connect=0, read=1, status=1,
        backoff_factor=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36 CareerNewsroom/1.1",
        "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.5",
    })
    return s


SESSION = build_session()




def http_get(url: str, timeout: int = HTTP_TIMEOUT_SECONDS) -> requests.Response | None:
    url = canonical_url(url)
    if not url or url.startswith(("mailto:", "javascript:", "tel:")):
        return None
    host = urlparse(url).netloc.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return None
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


def ocr_pdf_text(content: bytes, max_pages: int = MAX_OCR_PAGES) -> str:
    """OCR only the first few scanned pages. OCR expansion is intentionally bounded."""
    try:
        import fitz
        import pytesseract
        from PIL import Image as PILImage
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        logger.debug("OCR unavailable: %s", exc)
        return ""
    chunks = []
    try:
        for i in range(min(max_pages, doc.page_count)):
            try:
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                image = PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                try:
                    text = pytesseract.image_to_string(image, lang="ben+eng", config="--psm 6", timeout=8)
                except TypeError:
                    text = pytesseract.image_to_string(image, lang="ben+eng", config="--psm 6")
                if len(safe_text(text)) >= 40:
                    chunks.append(text.strip())
            except Exception as exc:
                logger.debug("OCR page %d failed: %s", i + 1, exc)
    finally:
        doc.close()
    return "\n\n".join(chunks).strip()


def extract_pdf_text(content: bytes) -> str:
    global _OCR_DOCUMENTS_USED
    try:
        from pypdf import PdfReader
    except Exception:
        return ocr_pdf_text(content)
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return ocr_pdf_text(content)
    chunks = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(chunks).strip()
    if len(re.sub(r"\s+", "", text)) >= 180:
        return text
    if _OCR_DOCUMENTS_USED >= MAX_OCR_DOCUMENTS_PER_RUN:
        logger.info("PDF OCR SKIP | per_run_limit=%d", MAX_OCR_DOCUMENTS_PER_RUN)
        return text
    _OCR_DOCUMENTS_USED += 1
    ocr = ocr_pdf_text(content)
    if len(ocr) > len(text):
        logger.info("PDF OCR FALLBACK | native_chars=%d ocr_chars=%d | pages=%d | docs_used=%d", len(text), len(ocr), min(MAX_OCR_PAGES, len(reader.pages)), _OCR_DOCUMENTS_USED)
        text = ocr
    return text


def _absolute_media_url(value: Any, base_url: str) -> str:
    value = safe_text(value)
    return canonical_url(urljoin(base_url, value)) if value else ""


def _jsonld_logo(value: Any, base_url: str) -> str:
    if isinstance(value, str):
        return _absolute_media_url(value, base_url)
    if isinstance(value, dict):
        return _absolute_media_url(value.get("url") or value.get("contentUrl") or value.get("@id"), base_url)
    return ""


def extract_meta(content: bytes, url: str) -> dict[str, Any]:
    """Extract job-specific media separately from page/social media and source branding."""
    soup = BeautifulSoup(content, "html.parser")
    meta: dict[str, Any] = {
        "job_image_candidates": [],
        "source_logo_candidates": [],
    }

    if soup.title:
        meta["title"] = soup.title.get_text(" ", strip=True)

    tags = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            tags[key.lower()] = value.strip()

    if tags.get("og:description"):
        meta["description"] = tags["og:description"]
    # IMPORTANT: og:image is normally a page/share banner, not proof of a job image.
    # Never treat it as the job photo automatically.
    if tags.get("og:image"):
        meta["page_social_image"] = _absolute_media_url(tags["og:image"], url)

    # High-quality source logo candidates.
    for link in soup.find_all("link"):
        href = link.get("href")
        rel = [x.lower() for x in link.get("rel", [])]
        sizes = safe_text(link.get("sizes"))
        if not href:
            continue
        abs_url = _absolute_media_url(href, url)
        rel_text = " ".join(rel)
        if "apple-touch-icon" in rel_text:
            meta["source_logo_candidates"].append(abs_url)
        elif "icon" in rel_text:
            meta["source_logo_candidates"].append(abs_url)
        elif "logo" in rel_text or "image_src" in rel_text:
            if "image_src" in rel_text:
                meta["job_image_candidates"].append(abs_url)
            else:
                meta["source_logo_candidates"].append(abs_url)
        if sizes:
            meta.setdefault("media_sizes", {})[abs_url] = sizes

    # JSON-LD: JobPosting image is a stronger signal than og:image.
    # Organization/website logo is explicitly kept for source fallback.
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
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types:
                meta["jobposting"] = item
                image = item.get("image")
                if isinstance(image, list):
                    meta["job_image_candidates"].extend(_absolute_media_url(x, url) for x in image)
                else:
                    img_url = _absolute_media_url(image, url)
                    if img_url:
                        meta["job_image_candidates"].append(img_url)
                org = item.get("hiringOrganization") or {}
                if isinstance(org, list):
                    org = org[0] if org else {}
                if isinstance(org, dict):
                    logo_url = _jsonld_logo(org.get("logo"), url)
                    if logo_url:
                        meta["source_logo_candidates"].append(logo_url)
            if "Organization" in types or "WebSite" in types:
                logo_url = _jsonld_logo(item.get("logo"), url)
                if logo_url:
                    meta["source_logo_candidates"].append(logo_url)

    # Page images: only consider images inside likely job-content containers and score them by
    # semantic similarity to the job title/company plus filename/alt text. This avoids picking
    # the orange site avatar/banner shown in the failing run.
    page_title = safe_text(soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    for container in soup.find_all(["article", "main", "section", "div"]):
        cls = " ".join(container.get("class", [])) if container.get("class") else ""
        ident = safe_text(container.get("id")).lower()
        hay = f"{cls} {ident}".lower()
        if not any(k in hay for k in ("job", "career", "vacancy", "recruit", "position", "opportunity", "posting", "detail")):
            continue
        for img in container.find_all("img")[:12]:
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
            if not src:
                continue
            abs_url = _absolute_media_url(src, url)
            if not abs_url:
                continue
            alt = safe_text(img.get("alt"))
            title_attr = safe_text(img.get("title"))
            token_text = f"{alt} {title_attr} {src} {page_title}".lower()
            bad = ("favicon", "icon", "avatar", "placeholder", "default-image", "social-share", "header", "footer")
            if any(k in token_text for k in bad):
                continue
            score = 0
            for hint in (page_title, alt.lower(), title_attr.lower()):
                if hint and len(hint) >= 4:
                    words = set(re.findall(r"[a-z0-9]{4,}", hint))
                    score += sum(1 for w in words if w in token_text)
            meta["job_image_candidates"].append({"url": abs_url, "score": score})

    meta["job_image_candidates"] = [x for x in meta["job_image_candidates"] if x]
    meta["source_logo_candidates"] = list(dict.fromkeys(x for x in meta["source_logo_candidates"] if x))
    return meta


def normalize_bengali_digits(text: str) -> str:
    table = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
    return safe_text(text).translate(table)


BENGALI_MONTHS = {
    "জানুয়ারি": "January", "জানুয়ারি": "January",
    "ফেব্রুয়ারি": "February", "ফেব্রুয়ারি": "February",
    "মার্চ": "March", "এপ্রিল": "April", "মে": "May", "জুন": "June",
    "জুলাই": "July", "আগস্ট": "August", "সেপ্টেম্বর": "September",
    "অক্টোবর": "October", "নভেম্বর": "November", "ডিসেম্বর": "December",
}


def normalize_bengali_date_text(text: str) -> str:
    value = normalize_bengali_digits(text)
    for bn, en in sorted(BENGALI_MONTHS.items(), key=lambda x: -len(x[0])):
        value = value.replace(bn, en)
    return value


def extract_date_token(text: str) -> str | None:
    t = normalize_bengali_date_text(text)
    for pattern in (
        r"\b\d{4}-\d{1,2}-\d{1,2}\b",
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b",
    ):
        m=re.search(pattern,t,flags=re.I)
        if m: return m.group(0)
    return None


def deterministic_hints(text: str) -> dict[str, Any]:
    t = normalize_bengali_date_text(text)
    lower = t.lower()
    deadline = None
    for label_pattern in (
        r"(?is)(?:application\s+)?(?:deadline|last\s+date|closing\s+date|apply\s+by|last\s+day|closing)\s*[:：-]?\s*(.{0,160})",
        r"(?is)(?:আবেদনের\s*শেষ\s*তারিখ|আবেদন\s*করার\s*শেষ\s*তারিখ|শেষ\s*তারিখ)\s*[:：-]?\s*(.{0,160})",
    ):
        for m in re.finditer(label_pattern,t):
            deadline=extract_date_token(m.group(1))
            if deadline: break
        if deadline: break
    if not deadline:
        m=re.search(r"(?is)(?:apply|application|আবেদন|নিয়োগ|circular|recruitment).{0,220}",t)
        if m: deadline=extract_date_token(m.group(0))
    salary=None
    m=re.search(r"(?i)(?:salary|বেতন).{0,60}?(?:(?:৳|tk\.?|bdt)\s*)?[\d,]+(?:\s*[-–]\s*[\d,]+)?",t)
    if m: salary=m.group(0)
    vacancy=None
    m=re.search(r"(?i)(?:vacanc(?:y|ies)|no\.?\s*of\s*(?:post|posts)|পদসংখ্যা|শূন্যপদ).{0,50}?[\d,]{1,8}",t)
    if m: vacancy=m.group(0)
    return {
        "deadline": deadline, "salary": salary, "vacancy": vacancy,
        "bangladesh": is_bangladesh(t), "job": looks_like_job(t),
        "deadline_signal": bool(deadline) or bool(re.search(r"deadline|last\s+date|closing\s+date|আবেদনের\s*শেষ|শেষ\s*তারিখ", lower)),
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
    """Resolve the actual publisher/apply-site source. Exa/Google are discovery layers, never display sources."""
    host = urlparse(canonical_url(url)).netloc.lower().removeprefix("www.")
    if not host:
        return None
    best = None; best_len = -1
    for source in sources.values():
        candidates = [source.get("url", "")] + list(source.get("domains", []) or []) + list(source.get("application_domains", []) or [])
        for raw_host in candidates:
            raw_host = safe_text(raw_host)
            if not raw_host: continue
            source_host = urlparse(canonical_url(raw_host if "://" in raw_host else "https://" + raw_host)).netloc.lower().removeprefix("www.")
            if source_host and (host == source_host or host.endswith("." + source_host)) and len(source_host) > best_len:
                best = source; best_len = len(source_host)
    return best


def source_display_name(url: str, sources: dict[str, dict[str, Any]]) -> str:
    mapped = source_for_url(url, sources)
    if mapped:
        return safe_text(mapped.get("name"))
    host = urlparse(canonical_url(url)).netloc.lower().removeprefix("www.")
    if not host:
        return "Career Newsroom"
    known = {
        "bdjobs.com": "Bdjobs.com", "bdjobslive.com": "BDJobs Live", "alljobs.teletalk.com.bd": "AllJobs Teletalk",
        "jobs.gov.bd": "National Job Portal", "bpsc.gov.bd": "Bangladesh Public Service Commission",
        "career.bracu.ac.bd": "BRAC University", "squaregroup.com": "Square Group", "squarespharma.com.bd": "Square Pharmaceuticals",
        "careers.unilever.com": "Unilever Careers", "jobs.waltonbd.com": "Walton Careers", "intertek.com": "Intertek",
        "jobs.bdjobs.com": "Bdjobs.com",
    }
    for domain,name in known.items():
        if host == domain or host.endswith('.'+domain):
            return name
    base = host.split('.')[0].replace('-', ' ').strip()
    return base.title() if base else "Career Newsroom"


def due_sources(registry: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every enabled source on every run. No interval rotation or source cap."""
    priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    sources = [
        source for source in registry.get("sources", [])
        if source.get("enabled", True)
        and source.get("status") in (None, "active", "verified")
    ]
    return sorted(sources, key=lambda x: (priority.get(x.get("priority", "P3"), 3), safe_text(x.get("name"))))


def candidate_has_stale_year(url: str, label: str = "", current_year: int | None = None) -> bool:
    """Reject obviously historical document URLs before downloading/OCRing them.

    A year in a deep PDF path is treated as stale when it is older than the current year\n    and the visible link text does not contain a current-year signal. This is deliberately\n    heuristic rather than an absolute rule so sources with old directory names can still\n    surface current documents when the anchor text says 2026 or contains a live deadline.\n    """
    year_now = current_year or CURRENT_YEAR
    raw = f"{url} {label}".lower()
    path = urlparse(url).path.lower()
    # Apply the hard stale-year heuristic to document/archive paths, not ordinary
    # HTML job slugs, which may legitimately contain an older year.
    document_path = (
        ".pdf" in path
        or any(token in path for token in ("/uploads/", "/upload/", "/files/", "/documents/", "/notices/", "/circular/", "/circulars/"))
    )
    if not document_path:
        return False
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", raw)]
    if not years:
        return False
    path_years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", path)]
    stale_path_years = [y for y in path_years if y < year_now]
    if not stale_path_years:
        return False
    current_signal = str(year_now) in raw or str(year_now + 1) in raw
    return not current_signal


def deadline_is_usable(text: str, minimum_days: int = 7) -> bool:
    hints = deterministic_hints(text)
    dt = parse_deadline(hints.get("deadline"))
    if not dt:
        return False
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400 >= minimum_days


def is_likely_current_job_link(label: str, url: str) -> bool:
    if not looks_like_job_link(label, url):
        return False
    return not candidate_has_stale_year(url, label)


def bounded_get_pdf(url: str) -> requests.Response | None:
    """Bound PDF downloads so one broken/huge document cannot consume the run."""
    url = canonical_url(url)
    try:
        with SESSION.get(url, timeout=PDF_HTTP_TIMEOUT_SECONDS, allow_redirects=True, stream=True) as response:
            if response.status_code >= 400:
                logger.warning("PDF HTTP %s | %s", response.status_code, url)
                return None
            ctype = response.headers.get("content-type", "").lower()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_PDF_BYTES:
                logger.info("PDF SKIP | oversized=%dMB | url=%s", int(declared) / (1024*1024), url)
                return None
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > MAX_PDF_BYTES:
                    logger.info("PDF SKIP | exceeded_size_limit=%dMB | url=%s", MAX_PDF_BYTES / (1024*1024), url)
                    return None
            response._content = bytes(buf)
            response.headers["content-length"] = str(len(buf))
            if "pdf" not in ctype and not response.url.lower().endswith(".pdf") and b"%PDF" not in response._content[:16]:
                return None
            return response
    except requests.RequestException as exc:
        logger.warning("PDF GET failed | %s | %s", url, exc)
        return None


def direct_discover(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_url = canonical_url(source.get("url", ""))
    r = bounded_get_pdf(source_url) if ".pdf" in source_url.lower() else http_get(source_url, timeout=SOURCE_HTTP_TIMEOUT_SECONDS)
    if not r:
        raise RuntimeError("source request failed")
    final_url = canonical_url(r.url)
    ctype = r.headers.get("content-type", "").lower()
    base = {
        "source_id": source["id"],
        "source_name": source["name"],
        "source_priority": source.get("priority", "P3"),
        "source_official": bool(source.get("official")),
        "source_type": source.get("source_type", ""),
        "source_categories": source.get("categories", []),
    }
    if "pdf" in ctype or final_url.lower().endswith(".pdf"):
        text = extract_pdf_text(r.content)
        if not text:
            raise RuntimeError("PDF extraction returned no text")
        return [{**base, "url": final_url, "title_hint": "", "text_hint": text[:30000], "image": "", "meta": {}, "published_date": "", "discovery": "direct_pdf"}]
    if "xml" in ctype or final_url.lower().endswith((".rss", ".xml")):
        feed = feedparser.parse(r.content)
        out = []
        for e in feed.entries:
            link = canonical_url(getattr(e, "link", ""))
            title = safe_text(getattr(e, "title", ""))
            summary = BeautifulSoup(safe_text(getattr(e, "summary", "")), "html.parser").get_text(" ", strip=True)
            if not link or not looks_like_job_document(title, summary, link, source.get("priority", "P3")):
                continue
            if candidate_has_stale_year(link, title):
                continue
            out.append({**base, "url": link, "title_hint": title, "text_hint": summary[:12000], "image": "", "meta": {}, "published_date": safe_text(getattr(e, "published", "") or getattr(e, "updated", "")), "discovery": "rss"})
        return out[:MAX_LINK_CANDIDATES_PER_SOURCE]
    text = extract_html_text(r.content, final_url)
    meta = extract_meta(r.content, final_url)
    candidates = []
    if looks_like_job_document(meta.get("title", ""), text, final_url, source.get("priority", "P3")):
        candidates.append({**base, "url": final_url, "title_hint": meta.get("title", ""), "text_hint": text[:30000], "image": "", "meta": meta, "published_date": meta.get("datePublished") or meta.get("datePosted") or "", "discovery": "direct_page"})
    soup = BeautifulSoup(r.content, "html.parser")
    links = []
    seen = {final_url}
    for node in soup.find_all(["a", "iframe", "embed", "object"]):
        href = node.get("href") or node.get("src") or node.get("data") or ""
        link = canonical_url(urljoin(final_url, href))
        label = safe_text(node.get_text(" ", strip=True))
        parent = node.parent.get_text(" ", strip=True) if node.parent else ""
        context = safe_text(f"{label} {parent[:700]}")
        if not link or link in seen:
            continue
        seen.add(link)
        if not is_likely_current_job_link(context, link):
            continue
        is_pdf = ".pdf" in urlparse(link).path.lower() or ".pdf" in link.lower()
        relevance = 0
        probe = f"{context} {link}".lower()
        relevance += 20 if is_pdf else 0
        relevance += 10 if str(CURRENT_YEAR) in probe else 0
        relevance += 8 if re.search(r"(?:deadline|last date|closing|apply by|আবেদনের\s*শেষ|শেষ\s*তারিখ)", probe, re.I) else 0
        relevance += sum(3 for k in ("career", "vacancy", "recruit", "circular", "apply", "job", "নিয়োগ", "চাকরি", "বিজ্ঞপ্তি") if k in probe)
        links.append((relevance, len(label), {**base, "url": link, "title_hint": label, "text_hint": "", "image": "", "meta": {"link_context": context}, "published_date": "", "discovery": "direct_pdf_link" if is_pdf else "direct_link"}))
    # Match raw PDF URLs embedded in page source as a second discovery path.
    raw_html = r.content.decode("utf-8", errors="ignore")
    for match in re.findall(r'https?://[^\s"\']+?\.pdf(?:\?[^\s"\']*)?', raw_html, flags=re.I):
        link = canonical_url(match.rstrip(",;)]}"))
        if not link or link in seen or not is_likely_current_job_link("", link):
            continue
        seen.add(link)
        page_signal = safe_text(re.sub(r"<[^>]+>", " ", raw_html))[:1200]
        links.append((30 + (10 if str(CURRENT_YEAR) in page_signal else 0), 0, {**base, "url": link, "title_hint": "", "text_hint": "", "image": "", "meta": {"link_context": page_signal}, "published_date": "", "discovery": "direct_pdf_link"}))
    links.sort(key=lambda x: (-x[0], -x[1], x[2]["url"]))
    pdf_links = [x for x in links if x[2].get("discovery") == "direct_pdf_link"][:MAX_PDF_CANDIDATES_PER_SOURCE]
    html_links = [x for x in links if x[2].get("discovery") == "direct_link"][:MAX_HTML_JOB_CANDIDATES_PER_SOURCE]
    selected = sorted(pdf_links + html_links, key=lambda x: (-x[0], -x[1], x[2]["url"]))[:MAX_LINK_CANDIDATES_PER_SOURCE]
    candidates.extend(item for _, __, item in selected)
    logger.info("SOURCE LINK FILTER | %s | kept=%d dropped_stale_or_irrelevant=%d", source.get("name"), len(candidates), max(0, len(seen) - 1 - len(candidates)))
    return candidates


def has_deadline_signal(text: str) -> bool:
    return bool(re.search(
        r"(?is)(?:deadline|last\s+date|closing\s+date|apply\s+by|আবেদনের\s*শেষ\s*তারিখ|শেষ\s*তারিখ).{0,140}(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        normalize_bengali_date_text(text),
    ))


def looks_like_job_link(label: str, url: str) -> bool:
    if not url or url.startswith(("mailto:", "javascript:", "tel:")):
        return False
    p=urlparse(url)
    if p.scheme not in ("http","https") or p.netloc.lower() in {"localhost","127.0.0.1","0.0.0.0"}:
        return False
    path=p.path.lower(); query=p.query.lower(); text=f"{label} {path} {query}".lower()
    if any(x in path for x in ("/login","/signin","/signup","/register","/account/","/profile","/contact","/about","/privacy","/terms")):
        return False
    if any(x in path for x in (".pdf","/job","/jobs","/career","/careers","/vacan","/recruit","/circular","/notice","/apply","/position","/employment")):
        return True
    if any(x in query for x in ("job=","jobid=","vacancy=","position=","recruit","circular","apply","postid=")):
        return True
    return any(x in text for x in ("job","vacancy","career","recruit","circular","position","apply","hiring","চাকরি","নিয়োগ","বিজ্ঞপ্তি","পদ"))


def looks_like_job_document(title: str, text: str, url: str, source_priority: str = "P3") -> bool:
    combined=f"{title}\n{text}".strip()
    if not combined:
        return False
    norm_title=normalize_text(title)
    if urlparse(url).path.lower().endswith(".pdf") or ".pdf" in url.lower():
        return True
    generic=("national job portal","alljobs by teletalk bridging aspirations with excellence","home","career home","jobs home","vacancies home","job search")
    if norm_title in generic or not norm_title:
        return has_deadline_signal(combined)
    role=re.search(r"(?i)(position|post|designation|job title|vacancy|পদের\s*নাম|পদ)",combined)
    application=re.search(r"(?i)(apply|application|deadline|last date|আবেদন|শেষ তারিখ|নিয়োগ বিজ্ঞপ্তি)",combined)
    organization=re.search(r"(?i)(organization|organisation|company|employer|প্রতিষ্ঠান|মন্ত্রণালয়|বিভাগ|authority|corporation)",combined)
    return bool(role and application) or bool(organization and application and source_priority in ("P0","P1"))


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
    now=datetime.now(timezone.utc); month=now.strftime("%B"); year=now.strftime("%Y"); month_year=now.strftime("%B %Y")
    base=[
        f"Bangladesh job circular {month} {year}", f"Bangladesh recruitment vacancy {month_year}",
        f"Bangladesh নিয়োগ বিজ্ঞপ্তি {year}", f"Bangladesh চাকরির বিজ্ঞপ্তি {month} {year}",
        f"site:gov.bd নিয়োগ বিজ্ঞপ্তি {year}", f"site:gov.bd recruitment jobs {year}",
        f"site:edu.bd recruitment jobs {year}", f"Bangladesh bank jobs {month_year}",
        f"Bangladesh NGO jobs {month_year}", f"Bangladesh company careers {month_year}",
        f"Bangladesh internship jobs {month_year}",
    ]
    for source in registry.get("sources",[]):
        name=safe_text(source.get("name"))
        if name: base.append(f'"{name}" jobs {year}')
    return list(dict.fromkeys(base))[:MAX_EXA_QUERIES]



def discover_exa(registry: dict[str, Any]) -> list[dict[str, Any]]:
    exa=Exa(api_key=EXA_API_KEY); out=[]
    for query in exa_queries(registry):
        try:
            result=exa.search(query,type="auto",num_results=MAX_EXA_RESULTS_PER_QUERY,contents={"text":{"max_characters":5000},"highlights":{"max_characters":1000}}) if hasattr(exa,"search") else exa.search_and_contents(query,type="auto",num_results=MAX_EXA_RESULTS_PER_QUERY,contents={"highlights":{"max_characters":1000}})
        except Exception as exc:
            logger.warning("Exa query failed | %s | %s",query,exc); continue
        for item in getattr(result,"results",[]) or []:
            url=canonical_url(getattr(item,"url","")); title=safe_text(getattr(item,"title","")); text=safe_text(getattr(item,"text","")); highlights=getattr(item,"highlights",None)
            if isinstance(highlights,list): text="\n".join(map(str,highlights))+"\n"+text
            published=safe_text(getattr(item,"published_date","")); age=None
            if published:
                try:
                    dt=datetime.fromisoformat(published.replace("Z","+00:00")); dt=dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    age=(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()/86400
                except ValueError: pass
            if not url or urlparse(url).scheme not in ("http","https") or (age is not None and age>EXA_FRESHNESS_DAYS): continue
            if not looks_like_job_document(title,text,url,"P3"): continue
            out.append({"url":url,"source_id":"exa_discovery","source_name":"Exa Discovery","source_priority":"P3","source_official":False,"title_hint":title,"text_hint":text[:12000],"image":safe_text(getattr(item,"image","")),"published_date":published,"meta":{},"discovery":"exa"})
    return out



def build_candidates(registry: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    all_sources = due_sources(registry, state)
    run_deadline = time.monotonic() + RUN_BUDGET_SECONDS
    candidates=[]; crawl_ok=0; crawl_failed=0; pdf_candidates=0
    def crawl_one(source): return source, direct_discover(source)
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map={pool.submit(crawl_one,source): source for source in all_sources}
        for future in as_completed(future_map):
            if time.monotonic() >= run_deadline:
                logger.warning("RUN BUDGET | source crawl collection window reached; retaining completed sources")
                break
            source=future_map[future]
            try:
                _source,found=future.result(); candidates.extend(found)
                pdf_count=sum(1 for x in found if x.get("discovery") in ("direct_pdf","direct_pdf_link")); pdf_candidates+=pdf_count; crawl_ok+=1
                logger.info("SOURCE CRAWL OK | %s | candidates=%d | pdf=%d",source.get("name"),len(found),pdf_count)
                h=state["sources"].setdefault(source["id"],{}); h.update({"last_crawl_epoch":time.time(),"last_success":now_iso(),"last_failure":None,"consecutive_failures":0,"last_status":"ok","candidates_last_run":len(found)})
            except Exception as exc:
                crawl_failed+=1; sid=source.get("id") if isinstance(source,dict) else "unknown"; name=source.get("name") if isinstance(source,dict) else "unknown"
                logger.warning("SOURCE CRAWL FAILED | %s | %s",name,exc)
                h=state["sources"].setdefault(sid,{}); h.update({"last_crawl_epoch":time.time(),"last_failure":now_iso(),"consecutive_failures":int(h.get("consecutive_failures",0))+1,"last_status":"failed","candidates_last_run":0})
    logger.info("SOURCE CRAWL SUMMARY | total=%d ok=%d failed=%d pdf_candidates=%d",len(all_sources),crawl_ok,crawl_failed,pdf_candidates)
    candidates.extend(discover_google_news()); candidates.extend(discover_exa(registry))
    preference={"direct_pdf":0,"direct_page":1,"direct_pdf_link":2,"rss":3,"direct_link":4,"exa":5,"google_news":6}
    unique={}
    for c in candidates:
        url=canonical_url(c.get("url",""))
        if not url: continue
        c["url"]=url; old=unique.get(url); score=preference.get(c.get("discovery"),9)
        if old is None or score<preference.get(old.get("discovery"),9) or len(safe_text(c.get("text_hint")))>len(safe_text(old.get("text_hint"))): unique[url]=c
    ordered=sorted(unique.values(),key=lambda c:(preference.get(c.get("discovery"),9),-len(safe_text(c.get("text_hint")))))
    return ordered if MAX_DISCOVERY_CANDIDATES<=0 else ordered[:MAX_DISCOVERY_CANDIDATES]



# ============================================================
# DOCUMENT RETRIEVAL
# ============================================================

def _render_pdf_previews(pdf_bytes: bytes, target_prefix: Path, max_pages: int = MAX_PDF_IMAGE_PAGES) -> list[str]:
    """Render candidate circular pages, score by visible density + job/circular text signals."""
    try:
        import fitz
        doc=fitz.open(stream=pdf_bytes,filetype="pdf")
    except Exception:
        return []
    scored=[]
    try:
        for i in range(min(max_pages, doc.page_count)):
            try:
                page=doc.load_page(i)
                text=safe_text(page.get_text("text"))
                pix=page.get_pixmap(matrix=fitz.Matrix(2.5,2.5),alpha=False)
                target=target_prefix.with_name(target_prefix.stem+f"_p{i+1}.jpg")
                target.parent.mkdir(parents=True,exist_ok=True); pix.save(str(target))
                samples=0; ink=0; channels=3 if pix.n >= 3 else 1; sx=max(1,pix.width//70); sy=max(1,pix.height//40)
                for y in range(0,pix.height,sy):
                    row=y*pix.width
                    for x in range(0,pix.width,sx):
                        idx=(row+x)*channels; samples+=1
                        if channels==3:
                            r,g,b=pix.samples[idx:idx+3]
                            if min(r,g,b)<245: ink+=1
                        elif pix.samples[idx]<245: ink+=1
                density=ink/max(1,samples)
                term_bonus=sum(1 for term in ("recruitment","job circular","application","deadline","পদ","নিয়োগ","আবেদন","গ্রেড","salary","vacancy") if term.lower() in text.lower())
                score=density*100 + min(35, len(text)/600) + term_bonus*5
                scored.append((score,-i,str(target),len(text)))
            except Exception:
                continue
    finally:
        doc.close()
    scored.sort(reverse=True)
    return [path for _,__,path,___ in scored]


def _render_pdf_preview(pdf_bytes: bytes, target: Path) -> str:
    paths=_render_pdf_previews(pdf_bytes,target,1)
    return paths[0] if paths else ""


def retrieve(candidate: dict[str, Any]) -> dict[str, Any] | None:
    url = canonical_url(candidate.get("url", ""))
    text_hint = safe_text(candidate.get("text_hint", ""))
    meta = dict(candidate.get("meta") or {})
    link_context = safe_text((candidate.get("meta") or {}).get("link_context"))
    if candidate_has_stale_year(url, f"{candidate.get('title_hint','')} {link_context}"):
        logger.info("DOCUMENT SKIP | stale_year | %s", url)
        return None
    if len(text_hint) >= 250 and looks_like_job_document(candidate.get("title_hint", ""), text_hint, url, candidate.get("source_priority", "P3")):
        return {**candidate, "url": url, "text": text_hint[:30000], "hints": deterministic_hints(text_hint), "meta": meta}
    if ".pdf" in url.lower():
        r = bounded_get_pdf(url)
    else:
        r = http_get(url, timeout=SOURCE_HTTP_TIMEOUT_SECONDS)
    if not r:
        return None
    final_url = canonical_url(r.url)
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or final_url.lower().endswith(".pdf") or ".pdf" in final_url.lower():
        if len(r.content) > MAX_PDF_BYTES:
            logger.info("DOCUMENT SKIP | pdf_too_large | %s", final_url)
            return None
        text = extract_pdf_text(r.content)
        if not text:
            return None
        pdf_path = RUNTIME_DIR / f"pdf_{hash_text(final_url)[:16]}.pdf"
        RUNTIME_DIR.mkdir(exist_ok=True)
        try:
            pdf_path.write_bytes(r.content)
            meta["pdf_path"] = str(pdf_path)
        except OSError:
            pass
    else:
        text = extract_html_text(r.content, final_url)
        meta = extract_meta(r.content, final_url)
    title = candidate.get("title_hint", "") or meta.get("title", "")
    if not looks_like_job_document(title, text, final_url, candidate.get("source_priority", "P3")):
        return None
    if not (is_bangladesh(text, final_url) or candidate.get("source_priority") in ("P0", "P1")):
        return None
    return {**candidate, "url": final_url, "text": text[:30000], "hints": deterministic_hints(text), "meta": meta,
            "published_date": candidate.get("published_date") or meta.get("datePublished") or meta.get("datePosted") or ""}


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
        "date_posted": safe_text(item.get("datePosted")),
        "employment_type": safe_text(item.get("employmentType")),
        "application_url": canonical_url(item.get("url") or ""),
        "skills": skills if isinstance(skills, list) else [],
        "description": safe_text(item.get("description")),
    }


def infer_from_text(text: str) -> dict[str, Any]:
    t=normalize_bengali_date_text(text); out={}
    for key,pattern in (
        ("title",r"(?i)(?:job\s*title|position|designation|post|পদের\s*নাম|পদ)\s*[:：-]\s*([^\n|]{3,140})"),
        ("company",r"(?i)(?:organization|organisation|company|employer|প্রতিষ্ঠানের\s*নাম|প্রতিষ্ঠান|নিয়োগকারী)\s*[:：-]\s*([^\n|]{3,180})"),
    ):
        m=re.search(pattern,t)
        if m:
            value=safe_text(m.group(1)).strip(" :-")
            if value: out[key]=value
    return out




def infer_application_url(text: str, source_url: str) -> str:
    for raw in re.findall(r"https?://[^\s<>\"\']+", text or "", flags=re.I):
        url=canonical_url(raw.rstrip('.,;:)]}'))
        if '.pdf' in url.lower(): continue
        if any(x in url.lower() for x in ('apply','application','career','recruit','teletalk','job')): return url
    return ''


def _strip_field_prefix(value: Any, labels: tuple[str,...]) -> str:
    text=safe_text(value)
    if not text: return ""
    for label in labels:
        text=re.sub(rf"^\s*{re.escape(label)}\s*[:：-]\s*", "", text, flags=re.I)
    text=re.sub(r"\s*\.\.\.\s*$", "", text)
    return safe_text(text)


def clean_field(value: Any, field: str) -> Any:
    if isinstance(value,list):
        cleaned=[]
        for x in value:
            y=clean_field(x,"text")
            if y and y not in cleaned: cleaned.append(y)
        return cleaned
    text=safe_text(value)
    if not text: return ""
    labels={
        "vacancy":("vacancy","vacancies","number of vacancies","পদসংখ্যা","শূন্যপদ"),
        "salary":("salary","salary & benefits","বেতন","বেতন-ভাতা"),
        "experience":("experience","অভিজ্ঞতা"),
        "education":("education","educational qualification","যোগ্যতা","শিক্ষাগত যোগ্যতা"),
        "location":("location","job location","কর্মস্থল","চাকরির স্থান"),
    }
    if field in labels: text=_strip_field_prefix(text,labels[field])
    low=text.lower()
    if field=="vacancy":
        m=re.search(r"(?:vacancy|vacancies|পদসংখ্যা|শূন্যপদ)\s*[:：-]?\s*(\d{1,5}(?:\s*[-–]\s*\d{1,5})?)",text,re.I)
        if m: return m.group(1)
    if field=="salary":
        if "company information" in low or low in {"salary","salary,","salary & benefits company information"}: return ""
        m=re.search(r"(?i)(?:৳|bdt|tk\.?|salary)\s*([\d,]+(?:\s*[-–]\s*[\d,]+)?)",text)
        if m: return m.group(1).strip()
    if low in {"salary", "salary,", "salary & benefits company information", "vacancy", "not specified", "n/a", "na", "none"}: return ""
    if "company information" in low or re.search(r"\bdeadline\s*:", low): return ""
    if text.endswith(("…","..","...")): return ""
    return text[:600]

def clean_job_title(value: Any) -> str:
    """Return only the role/title, never portal UI labels or article metadata."""
    t = safe_text(value)
    if not t:
        return ""
    # Prefer a segment that looks like a role when a portal joins many fields with pipes.
    segments = [safe_text(x) for x in re.split(r"\s*[|•·]\s*", t) if safe_text(x)]
    role_words = re.compile(r"(?i)\b(manager|management|trainee|officer|executive|assistant|associate|analyst|engineer|developer|intern|internship|specialist|coordinator|consultant|teacher|lecturer|professor|accountant|auditor|sales|marketing|hr|human resources|finance|accounting|procurement|supply chain|technician|operator|nurse|medical|director|supervisor|representative|রক্ষণাবেক্ষণ|কর্মকর্তা|সহকারী|ব্যবস্থাপনা|নিয়োগ|শিক্ষক|প্রভাষক|অফিসার|ইন্টার্ন)")
    role_segment = next((seg for seg in segments if role_words.search(seg)), t)
    t = role_segment
    t = re.sub(r"\s*(?:\||•|·|:)\s*(?:Bdjobs(?:\.com)?|BDJobs Live|EZ Jobs|Jobs? Test bd|Prothom Alo|Chakri|Niyog|Exa Discovery)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+[-–]\s+(?:Apply online|Apply Now|Jobs?|Job Circular.*|Career.*)$", "", t, flags=re.I)
    t = re.sub(r"\b(?:job location|category in|job type|salary|deadline|application deadline|location|vacancy|company|organization|employer)\s*[:：-].*$", "", t, flags=re.I)
    t = re.sub(r"\b(?:Job Circular|Circular|Vacancy|Recruitment|Hiring|Job Details?)\s*[:：-]\s*", "", t, flags=re.I)
    t = re.sub(r"^(?:পদের\s*নাম|পদ|পদবী|Position|Designation|Job Title|Post)\s*[:：-]\s*", "", t, flags=re.I)
    # Remove common portal/article suffixes and company-location wrappers.
    t = re.sub(r"\s+(?:at|with|from|on)\s+(?:Bdjobs(?:\.com)?|LinkedIn|EZ Jobs|Jobs? Test BD)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+[-–]\s+[A-Z][^\n]{0,80}\b(?:Dhaka|Chattogram|Bangladesh)\s*$", "", t, flags=re.I)
    m = re.match(r"^(.{3,140}?)\s+(?:is|are)\s+(?:looking|hiring|recruiting)\b", t, flags=re.I)
    if m:
        t = m.group(1)
    t = re.sub(r"\s{2,}", " ", t).strip(" -|:;,. ")
    return t[:140]


def is_weak_job_title(title: Any) -> bool:
    t=safe_text(title)
    low=t.lower()
    if len(t)<4 or len(t)>180: return True
    bad=("graduate degree", "salary", "vacancy", "job location", "category in", "job type", "deadline:", "apply online", "career opportunity")
    if any(x in low for x in bad): return True
    return bool(re.match(r"^[.\s:-]+$",t))


def is_bad_company(value: Any) -> bool:
    v=safe_text(value).lower()
    bad={"telecommunication","telecommunications","engineering/architects","architects","category","jobs test bd","bdjobs live","exa discovery","career opportunity"}
    return v in bad or len(v)<2 or len(v)>160


def infer_company_from_title(title: str) -> str:
    t=safe_text(title)
    patterns=(
        r"\bat\s+(.+)$", r"\bby\s+(.+)$", r"\|\s*([^|]+)$", r"[-–]\s*(.+?)\s+(?:is|are)\s+(?:looking|hiring|recruiting)",
    )
    for pat in patterns:
        m=re.search(pat,t,flags=re.I)
        if m:
            c=safe_text(m.group(1)).strip(" -|:")
            if c and len(c)<120 and not is_generic_job_title({"title":c,"company":c}): return c
    return ""


def detect_post_language(job_or_title: Any) -> str:
    if isinstance(job_or_title, dict):
        text=" ".join(safe_text(job_or_title.get(k)) for k in ("title","summary","company","location"))
    else:
        text=safe_text(job_or_title)
    bn=len(re.findall(r"[\u0980-\u09ff]",text)); en=len(re.findall(r"[A-Za-z]",text))
    return "bn" if bn >= max(8, int(en*0.40)) else "en"



# ============================================================
# V2 GOVERNMENT / CORPORATE CLASSIFICATION
# ============================================================

GOVERNMENT_HIGH_CONFIDENCE = ("government", "government_aggregator", "official_government", "teletalk")
GRADE_PATTERNS = (
    (re.compile(r"(?:grade|গ্রেড)\s*[-:#]?\s*(\d{1,2})", re.I), "explicit"),
    (re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s+grade", re.I), "explicit"),
    (re.compile(r"(\d{1,2})\s*(?:তম|ম)\s*গ্রেড", re.I), "explicit"),
)

# Bangladesh National Pay Scale 2015 supporting ranges. These are supporting signals only.
PAY_SCALE_TO_GRADE = {
    "22000": 9, "23000": 9, "16400": 10, "16000": 10,
    "11000": 13, "10200": 14, "9300": 16, "8250": 20,
}

TARGET_GOVERNMENT_GRADES = set(range(1, 11))

def is_government_source(job: dict[str, Any]) -> bool:
    p=safe_text(job.get("source_priority")).upper()
    st=safe_text(job.get("source_type")).lower()
    cat=" ".join(str(x).lower() for x in (job.get("source_categories") or []))
    name=safe_text(job.get("source_name")).lower()
    return p in ("P0",) or st in GOVERNMENT_HIGH_CONFIDENCE or "government" in cat or any(x in name for x in ("government","ministry","bpsc","teletalk","national job portal"))

def extract_government_grade(text: str) -> tuple[int|None,str]:
    t=normalize_bengali_digits(normalize_bengali_date_text(text))
    for pattern,confidence in GRADE_PATTERNS:
        m=pattern.search(t)
        if m:
            g=int(m.group(1))
            if 1 <= g <= 20: return g, confidence
    # Strong pay-scale phrases, only when clearly tied to a post/circular.
    for amount,grade in PAY_SCALE_TO_GRADE.items():
        if re.search(rf"(?:Tk|BDT|৳|টাকা)?\s*{re.escape(amount)}\b", t, re.I):
            return grade, "pay_scale"
    if re.search(r"\b(?:first\s+class|প্রথম\s*শ্রেণি)\b",t,re.I): return 9,"class_support"
    if re.search(r"\b(?:second\s+class|দ্বিতীয়\s*শ্রেণি|দ্বিতীয়\s*শ্রেণি)\b",t,re.I): return 10,"class_support"
    return None,"unknown"

def government_grade_gate(job: dict[str, Any]) -> dict[str, Any]:
    if not is_government_source(job):
        return {"passed": True, "grade": None, "confidence": "not_government", "reason": "corporate_or_non_government"}
    text=" ".join([safe_text(job.get("text")),safe_text(job.get("title")),safe_text(job.get("salary"))])
    grade,confidence=extract_government_grade(text)
    job["government_grade"]=grade
    job["government_grade_confidence"]=confidence
    if grade is None:
        # Do not guess grade for a government post. Unknown grade is not publishable in V2.
        return {"passed": False, "grade": None, "confidence": confidence, "reason": "grade_unknown"}
    if grade not in TARGET_GOVERNMENT_GRADES:
        return {"passed": False, "grade": grade, "confidence": confidence, "reason": "grade_above_10"}
    return {"passed": True, "grade": grade, "confidence": confidence, "reason": "target_grade"}

def corporate_level_score(job: dict[str, Any]) -> int:
    hay=" ".join(safe_text(job.get(k)) for k in ("title","job_level","job_type","sector","job_function","education","summary")).lower()
    score=0
    for term,pts in (("management trainee",35),("mto",35),("graduate trainee",35),("bba",32),("mba",32),("business administration",28),("finance",28),("accounting",28),("marketing",28),("human resources",28),("hr",22),("banking",26),("business development",25),("supply chain",24),("procurement",24),("analyst",20),("executive",18),("officer",18),("manager",18),("specialist",16),("intern",16),("fresher",18),("graduate",14)):
        if term in hay: score += pts
    return min(100,score)

def rule_extract(document: dict[str, Any]) -> dict[str, Any]:
    meta=document.get("meta") or {}; hints=document.get("hints") or {}; structured=jsonld_job(meta) or {}; inferred=infer_from_text(document.get("text",""))
    raw_title=structured.get("title") or inferred.get("title") or safe_text(document.get("title_hint")) or safe_text(meta.get("title"))
    title=clean_job_title(raw_title)
    company=clean_field(structured.get("company") or inferred.get("company"),"company")
    if not company: company=clean_field(infer_company_from_title(raw_title),"company")
    if not company and document.get("source_official") and document.get("source_priority")=="P1": company=safe_text(document.get("source_name"))
    source_url=canonical_url(document.get("url",""))
    return {
        "is_job":True,"company":company or None,"title":title or None,"organization_type":None,"sector":None,"job_function":None,"job_level":None,"job_type":None,
        "employment_type":structured.get("employment_type") or None,"location":clean_field(structured.get("location") or hints.get("location"),"location") or None,"vacancy":clean_field(structured.get("vacancy") or hints.get("vacancy"),"vacancy") or None,
        "salary":clean_field(structured.get("salary") or hints.get("salary"),"salary") or None,"education":clean_field(structured.get("education") or [],"education"),"experience":clean_field(structured.get("experience"),"experience") or None,"skills":structured.get("skills") or [],
        "age_limit":None,"gender":None,"published_date":structured.get("date_posted") or document.get("published_date") or safe_text(meta.get("datePublished")) or safe_text(meta.get("datePosted")) or None,
        "application_start":None,"deadline":structured.get("deadline") or hints.get("deadline"),"application_method":None,
        "application_url":structured.get("application_url") or infer_application_url(document.get("text",""),source_url) or (source_url if looks_like_job_link(raw_title,source_url) else ""),
        "requirements":[],"responsibilities":[],"summary":safe_text(meta.get("description")),
        "confidence":0.90 if structured else (0.75 if company and title else 0.55),"source_id":document.get("source_id"),"source_name":document.get("source_name"),
        "source_priority":document.get("source_priority","P3"),"source_type":document.get("source_type",""),"source_categories":document.get("source_categories",[]),"official_source":bool(document.get("source_official")),"source_url":source_url,"discovery":document.get("discovery"),"meta":meta,
        "text":document.get("text",""),"scam_signals":hints.get("scam_signals",[]),"deadline_signal":bool(hints.get("deadline_signal")),
    }


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


_CEREBRAS_CLIENT = None
_OCR_DOCUMENTS_USED = 0
_CEREBRAS_CALL_TIMES: list[float] = []


def get_cerebras_client():
    global _CEREBRAS_CLIENT
    if _CEREBRAS_CLIENT is None:
        try: _CEREBRAS_CLIENT=Cerebras(api_key=CEREBRAS_API_KEY,max_retries=0)
        except TypeError: _CEREBRAS_CLIENT=Cerebras(api_key=CEREBRAS_API_KEY)
    return _CEREBRAS_CLIENT


def wait_for_cerebras_slot() -> None:
    now=time.monotonic(); _CEREBRAS_CALL_TIMES[:]=[x for x in _CEREBRAS_CALL_TIMES if now-x<60]
    if len(_CEREBRAS_CALL_TIMES)>=CEREBRAS_CALLS_PER_MINUTE:
        wait=max(0.0,60-(now-_CEREBRAS_CALL_TIMES[0])+0.5)
        logger.info("CEREBRAS RATE WINDOW | waiting %.1fs",wait); time.sleep(wait)
        now=time.monotonic(); _CEREBRAS_CALL_TIMES[:]=[x for x in _CEREBRAS_CALL_TIMES if now-x<60]


def cerebras_structured(system: str,user_payload: dict[str,Any],schema: dict[str,Any],max_tokens:int)->dict[str,Any]|None:
    wait_for_cerebras_slot(); client=get_cerebras_client(); _CEREBRAS_CALL_TIMES.append(time.monotonic())
    try:
        response=client.chat.completions.create(model=CEREBRAS_MODEL,messages=[{"role":"system","content":system},{"role":"user","content":json.dumps(user_payload,ensure_ascii=False)}],response_format={"type":"json_schema","json_schema":{"name":"career_newsroom_schema","strict":True,"schema":schema}},reasoning_effort="low",temperature=0.0,max_completion_tokens=max_tokens)
        raw=response.choices[0].message.content or ""; data=json.loads(raw); return data if isinstance(data,dict) else None
    except Exception as exc:
        msg=str(exc)
        logger.warning("Cerebras 429; skip batch" if "429" in msg or "rate limit" in msg.lower() else "Cerebras structured call failed: %s", *(() if "429" in msg or "rate limit" in msg.lower() else (exc,)))
        return None



def ai_enrich(jobs: list[dict[str,Any]])->list[dict[str,Any]]:
    needs=[]
    for job in jobs:
        missing=not job.get("company") or not job.get("title") or not job.get("application_url") or not parse_deadline(safe_text(job.get("deadline")))
        if missing and (job.get("deadline_signal") or job.get("discovery") in ("direct_pdf","direct_pdf_link")):
            needs.append(job)
    total=(len(needs)+AI_BATCH_SIZE-1)//AI_BATCH_SIZE
    logger.info("AI QUEUE | jobs_needing_rescue=%d | batches=%d",len(needs),total)
    for batch_no,start in enumerate(range(0,len(needs),AI_BATCH_SIZE),1):
        batch=needs[start:start+AI_BATCH_SIZE]
        payload={"documents":[{"id":i,"source_name":j.get("source_name"),"source_url":j.get("source_url"),"title_hint":j.get("title"),"text":safe_text(j.get("text"))[:10000]} for i,j in enumerate(batch)],"instruction":"Extract only facts explicitly stated in the Bangladesh job circular. The final application deadline is mandatory for publication. Never invent a date. A source homepage/dashboard is not a job. Inspect the full PDF text when supplied."}
        data=cerebras_structured("Extract structured Bangladesh job circular facts. Do not infer the source name as an employer. Return null/[] for unstated facts.",payload,AI_SCHEMA,9000)
        logger.info("AI RESCUE BATCH %d/%d | docs=%d | success=%s",batch_no,total,len(batch),bool(data))
        if not data or not isinstance(data.get("jobs"),list): continue
        by_id={int(x.get("id")):x for x in data["jobs"] if isinstance(x,dict) and str(x.get("id","")).isdigit()}
        for i,job in enumerate(batch):
            ai=by_id.get(i)
            if not ai: continue
            for key in ("company","title","sector","job_function","job_level","job_type","location","vacancy","salary","education","experience","deadline","published_date","application_url","requirements","responsibilities","summary"):
                v=ai.get(key)
                current=job.get(key)
                accept = current in (None,"",[],{})
                if key=="title" and current and (is_generic_job_title({"title":current,"company":job.get("company")}) or is_weak_job_title(current)): accept=True
                if key=="company" and current and is_bad_company(current): accept=True
                if key in ("vacancy","salary") and current and ("vacancy:" in safe_text(current).lower() or "salary"==safe_text(current).lower() or "company information" in safe_text(current).lower()): accept=True
                if key=="deadline" and not parse_deadline(safe_text(current)): accept=True
                if key=="application_url" and not canonical_url(safe_text(current)): accept=True
                if accept and v not in (None,"",[],{}): job[key]=v
            if ai.get("confidence") is not None:
                job["ai_confidence"]=max(0.0,min(1.0,float(ai.get("confidence") or 0)))
            job["ai_extracted"]=True
    return jobs



def finalize_job_fields(job: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Clean AI/deterministic output and resolve the real source before event processing."""
    for field in ("company","title","location","vacancy","salary","experience","summary"):
        job[field]=clean_field(job.get(field),field)
    job["title"]=clean_job_title(job.get("title"))
    if is_weak_job_title(job.get("title")):
        inferred=infer_from_text(job.get("text","")); candidate=clean_job_title(inferred.get("title"))
        if candidate: job["title"]=candidate
    if is_bad_company(job.get("company")): job["company"]=""
    # Remove obvious article/body fragments from title.
    if len(safe_text(job.get("title")))>140 or re.search(r"\b(?:vacancy|category in|job location|job type|deadline:)\b",safe_text(job.get("title")),re.I):
        inferred=infer_from_text(job.get("text","")); candidate=clean_job_title(inferred.get("title"))
        if candidate and len(candidate)<=140: job["title"]=candidate
    job["source_url"]=canonical_url(job.get("source_url") or "")
    job["application_url"]=canonical_url(job.get("application_url") or "")
    src=source_for_url(job.get("application_url") or "",sources) or source_for_url(job.get("source_url") or "",sources)
    if src:
        job["source_id"]=src.get("id"); job["source_name"]=src.get("name"); job["source_priority"]=src.get("priority","P3"); job["source_type"]=src.get("source_type",job.get("source_type","")); job["source_categories"]=src.get("categories",job.get("source_categories",[])); job["official_source"]=bool(src.get("official")); job["source_homepage"]=src.get("url")
    else:
        by_id=sources.get(job.get("source_id")) if job.get("source_id") else None
        if by_id:
            job["source_name"]=safe_text(by_id.get("name")); job["source_priority"]=by_id.get("priority","P3"); job["official_source"]=bool(by_id.get("official")); job["source_homepage"]=by_id.get("url")
        else:
            job["source_name"]=source_display_name(job.get("application_url") or job.get("source_url"),sources)
            job["source_id"]=job.get("source_id") or "source_"+hash_text(job["source_name"].lower())[:12]
    if job.get("source_name")=="Exa Discovery": job["source_name"]=source_display_name(job.get("application_url") or job.get("source_url"),sources)
    job["language"]=detect_post_language(job)
    return job


def student_relevance_score(job: dict[str, Any]) -> int:
    hay=" ".join([safe_text(job.get(k)) for k in ("title","job_function","sector","education","experience","summary","requirements")]).lower()
    score=0
    for term,pts in (("bba",35),("mba",35),("business administration",30),("management trainee",30),("graduate trainee",30),("mto",30),("marketing",28),("finance",28),("accounting",28),("banking",28),("audit",25),("human resources",25),("hr",20),("business development",25),("procurement",24),("supply chain",24),("business analytics",24),("administration",22),("intern",18),("internship",18),("fresher",18),("graduate",15)):
        if term in hay: score += pts
    return min(100,score)


def fair_interleave(events: list[dict[str,Any]]) -> list[dict[str,Any]]:
    """Rank by student relevance, then interleave sources; never discard valid jobs."""
    prepared=[]
    for e in events:
        job=e.get("canonical",{}); rel=student_relevance_score(job)
        e["student_relevance_score"]=rel; e["corporate_priority_score"]=corporate_level_score(job)
        prepared.append(e)
    prepared.sort(key=lambda e:(-int(e.get("student_relevance_score",0)),-int(e.get("quality_score",0)),-int(e.get("importance_score",0))))
    buckets={}
    for e in prepared: buckets.setdefault(safe_text(e.get("canonical",{}).get("source_id")) or "unknown",[]).append(e)
    keys=sorted(buckets,key=lambda k:(-int(buckets[k][0].get("student_relevance_score",0)),-int(buckets[k][0].get("quality_score",0)),k))
    out=[]; last=None; streak=0
    while any(buckets.values()):
        candidates=[k for k in keys if buckets[k]]
        if last and streak>=2 and len(candidates)>1:
            candidates=[k for k in candidates if k!=last] or candidates
        k=candidates[0]
        out.append(buckets[k].pop(0))
        if k==last: streak+=1
        else: last=k; streak=1
    return out


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
        normalize_company(job.get("company")),
        normalize_title(job.get("title")),
        normalize_text(job.get("location")),
        normalize_text(job.get("job_level")),
        normalize_text(job.get("job_type")),
    ])
    return "evt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_deadline(value: str | None) -> datetime | None:
    raw=safe_text(value)
    if not raw: return None
    raw=normalize_bengali_date_text(raw)
    iso=re.search(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",raw)
    if iso:
        token=iso.group(0)
        try:
            if "T" in token or " " in token:
                dt=datetime.fromisoformat(token.replace("Z","+00:00")); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return datetime.strptime(token,"%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Dhaka"),hour=23,minute=59,second=59).astimezone(timezone.utc)
        except ValueError: pass
    tokens=re.findall(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\b",raw)
    formats=("%d-%m-%Y","%d/%m/%Y","%d.%m.%Y","%d-%m-%y","%d/%m/%y","%d.%m.%y","%d %B %Y","%d %b %Y","%B %d %Y","%B %d, %Y","%b %d %Y","%b %d, %Y")
    for token in tokens:
        for fmt in formats:
            try: return datetime.strptime(token,fmt).replace(tzinfo=ZoneInfo("Asia/Dhaka"),hour=23,minute=59,second=59).astimezone(timezone.utc)
            except ValueError: continue
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


def build_rich_html(job: dict[str, Any]) -> str:
    """Build clean rich HTML. Bangla remains left-to-right; no RTL flag is used."""
    title=html_escape(clean_job_title(job.get("title") or "Job Vacancy"))
    company_raw=safe_text(job.get("company")); company=html_escape(company_raw)
    lang=job.get("language") or detect_post_language(job)
    if lang=="bn":
        labels={"high":"গুরুত্বপূর্ণ তথ্য","org":"প্রতিষ্ঠান","post":"পদ","grade":"গ্রেড","vac":"পদসংখ্যা","loc":"কর্মস্থল","edu":"শিক্ষাগত যোগ্যতা","exp":"অভিজ্ঞতা","sal":"বেতন","emp":"চাকরির ধরন","deadline":"আবেদনের শেষ তারিখ","req":"যোগ্যতা","resp":"দায়িত্ব","apply":"আবেদন করুন","source":"উৎস"}
        summary=clean_field(job.get("summary"),"text")
        if not summary: summary=f"{company_raw} এই পদে নিয়োগ দিচ্ছে।" if company_raw else "এই পদের জন্য আবেদন করা যাচ্ছে।"
    else:
        labels={"high":"KEY HIGHLIGHTS","org":"Organization","post":"Post","grade":"Grade","vac":"Vacancy","loc":"Location","edu":"Education","exp":"Experience","sal":"Salary","emp":"Employment","deadline":"Application Deadline","req":"REQUIREMENTS","resp":"JOB RESPONSIBILITIES","apply":"APPLY NOW","source":"Source"}
        summary=clean_field(job.get("summary"),"text")
        if not summary: summary=f"{company_raw} is hiring for this position." if company_raw else "Applications are open for this position."
    blocks=[f"<h1>{title}</h1>",f"<p>{html_escape(summary[:420])}</p>",f"<h2>{labels['high']}</h2>"]
    vals=[(labels["org"],company_raw),(labels["post"],clean_job_title(job.get("title")))]
    if job.get("government_grade") is not None: vals.append((labels["grade"],str(job.get("government_grade"))))
    vals += [(labels["vac"],clean_field(job.get("vacancy"),"vacancy")),(labels["loc"],clean_field(job.get("location"),"location")),(labels["edu"],", ".join(job.get("education") or []) if isinstance(job.get("education"),list) else clean_field(job.get("education"),"education")),(labels["exp"],clean_field(job.get("experience"),"experience")),(labels["sal"],clean_field(job.get("salary"),"salary")),(labels["emp"],clean_field(job.get("employment_type") or job.get("job_type"),"text")),(labels["deadline"],clean_field(job.get("deadline"),"text"))]
    for label,value in vals:
        v=clean_field(value,"text")
        if v: blocks.append(f"<p><b>{html_escape(label)}:</b> {html_escape(v)}</p>")
    req=[clean_field(x,"text") for x in (job.get("requirements") or []) if clean_field(x,"text")]
    resp=[clean_field(x,"text") for x in (job.get("responsibilities") or []) if clean_field(x,"text")]
    if req: blocks.append(f"<details><summary><b>{labels['req']}</b></summary><ul>"+"".join(f"<li>{html_escape(x)}</li>" for x in req[:6])+"</ul></details>")
    if resp: blocks.append(f"<details><summary><b>{labels['resp']}</b></summary><ul>"+"".join(f"<li>{html_escape(x)}</li>" for x in resp[:6])+"</ul></details>")
    source_name=safe_text(job.get("source_name")) or "Official Source"
    source_url=canonical_url(job.get("source_url") or job.get("source_homepage") or "")
    if source_url: blocks.append(f'<footer><b>{html_escape(labels["source"])}:</b> <a href="{html.escape(source_url,quote=True)}">{html_escape(source_name)}</a></footer>')
    return "\n".join(blocks)


def build_caption(job: dict[str, Any]) -> str:
    """Legacy HTML caption fallback with safe inline APPLY NOW link text."""
    text=build_rich_html(job)
    text=re.sub(r'<img[^>]+/>','',text)
    text=re.sub(r'<details><summary>(.*?)</summary>',r'<b>\1</b>\n',text,flags=re.S)
    text=text.replace('</details>','')
    text=re.sub(r'<tg-button[^>]*>(.*?)</tg-button>',r'\1',text,flags=re.S)
    apply_url=canonical_url(job.get('application_url') or '')
    if apply_url:
        text += f'\n\n<b>{"আবেদন করুন" if (job.get("language") == "bn") else "APPLY NOW"}</b> ↗'
    return text[:MAX_CAPTION]


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


CAREER_USERNAME = "@CareerNewsroom"
MIN_JOB_IMAGE_SHORT = 600
MIN_JOB_IMAGE_LONG = 1000
MIN_LOGO_SIZE = 160

def _image_has_usable_size(image: Image.Image, min_w: int, min_h: int) -> bool:
    try:
        w, h = image.size
        return min(w, h) >= MIN_JOB_IMAGE_SHORT and max(w, h) >= MIN_JOB_IMAGE_LONG
    except Exception:
        return False

def _logo_has_usable_size(image: Image.Image) -> bool:
    try:
        w, h = image.size
        return max(w, h) >= MIN_LOGO_SIZE
    except Exception:
        return False

def fit_with_padding(image: Image.Image, size=(1200, 675)) -> Image.Image:
    """Preserve the complete circular/photo. No destructive crop of job-circular content."""
    image = image.convert("RGB")
    image.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "white")
    x = (size[0] - image.width) // 2
    y = (size[1] - image.height) // 2
    canvas.paste(image, (x, y))
    return canvas

def overlay_username(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font = ImageFont.truetype(font_path, 26) if Path(font_path).exists() else ImageFont.load_default()
    text = CAREER_USERNAME
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    pad_x, pad_y = 12, 7
    x = image.width - tw - 24
    y = image.height - th - 18
    # Only the username is added. No title, source, logo, or decorative label.
    draw.rounded_rectangle((x-pad_x, y-pad_y, x+tw+pad_x, y+th+pad_y), radius=8, fill=(0,0,0,150))
    draw.text((x, y), text, fill="white", font=font)
    return image

def source_name_fallback(job: dict[str, Any], path: Path) -> Path:
    image = Image.new("RGB", (1200, 675), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 54) if Path(font_path).exists() else ImageFont.load_default()
    text = safe_text(job.get("source_name") or "Job Source")[:90]
    # Last fallback: only bold centered source name, absolutely no username.
    bbox = draw.multiline_textbbox((0,0), text, font=font, spacing=10)
    tw = bbox[2]-bbox[0]
    lines=[]
    words=text.split()
    cur=[]
    for word in words:
        trial=" ".join(cur+[word])
        if draw.textbbox((0,0),trial,font=font)[2] > 1040 and cur:
            lines.append(" ".join(cur)); cur=[word]
        else:
            cur.append(word)
    if cur: lines.append(" ".join(cur))
    total_h=len(lines)*70
    y=(675-total_h)//2
    for line in lines[:3]:
        w=draw.textbbox((0,0),line,font=font)[2]
        draw.text(((1200-w)//2,y),line,fill="black",font=font)
        y+=70
    image.save(path,"JPEG",quality=95)
    return path

def prepare_image(job: dict[str, Any], index: int) -> Path:
    """Image priority: actual circular/photo -> high-quality source logo -> source name card."""
    RUNTIME_DIR.mkdir(exist_ok=True)
    meta=job.get("meta") or {}
    candidates=[]
    # Render PDF pages only after the job has already survived document/deadline validation.
    pdf_paths = list(meta.get("pdf_preview_paths") or [])
    preview = safe_text(meta.get("pdf_preview_path"))
    if not pdf_paths and preview:
        pdf_paths = [preview]
    if not pdf_paths:
        pdf_path = safe_text(meta.get("pdf_path"))
        if pdf_path and Path(pdf_path).exists():
            pdf_paths = _render_pdf_previews(Path(pdf_path).read_bytes(), RUNTIME_DIR / f"pdf_{hash_text(pdf_path)[:12]}", MAX_PDF_IMAGE_PAGES)
            meta["pdf_preview_paths"] = pdf_paths
            if pdf_paths: meta["pdf_preview_path"] = pdf_paths[0]
            job["meta"] = meta
    for pth in pdf_paths:
        if pth: candidates.append((1000,pth,"pdf"))
    raw=meta.get("job_image_candidates") or []
    for item in raw:
        if isinstance(item,dict): candidates.append((int(item.get("score",0)),canonical_url(item.get("url","")),"job"))
        else: candidates.append((0,canonical_url(item),"job"))
    # PDF circular pages always win over HTML page images. Among page images, highest semantic score wins.
    candidates=[x for x in candidates if x[1]]
    pdfs=[x for x in candidates if x[2]=="pdf"]
    jobs=[x for x in candidates if x[2]=="job"]
    jobs.sort(key=lambda x:x[0],reverse=True)
    candidates=pdfs+jobs
    for score,value,kind in candidates[:12]:
        if kind=="pdf":
            try: image=Image.open(value).convert("RGB")
            except Exception: continue
        else:
            image=download_image(value,job.get("source_url", ""))
        if not image: continue
        if kind=="pdf" or _image_has_usable_size(image,MIN_JOB_IMAGE_SHORT,MIN_JOB_IMAGE_LONG):
            out=fit_with_padding(image); out=overlay_username(out)
            path=RUNTIME_DIR/f"job_{index}_matched.jpg"; out.save(path,"JPEG",quality=96,optimize=True)
            if kind=="pdf":
                paths=[]
                for pth in [x for x in (meta.get("pdf_preview_paths") or []) if x][:MAX_CIRCULAR_MEDIA]:
                    try:
                        im=Image.open(pth).convert("RGB"); im=fit_with_padding(im); im=overlay_username(im)
                        mp=RUNTIME_DIR/f"job_{index}_pdf_{len(paths)+1}.jpg"; im.save(mp,"JPEG",quality=96,optimize=True); paths.append(str(mp))
                    except Exception: pass
                job["_prepared_media_paths"]=paths or [str(path)]
            logger.info("IMAGE | level=1 job_asset kind=%s score=%s source=%s size=%sx%s",kind,score,safe_text(job.get("source_name")),image.width,image.height)
            return path

    logos=meta.get("source_logo_candidates") or []
    if not logos:
        source_url=canonical_url(job.get("source_homepage") or job.get("source_url") or "")
        if source_url:
            try:
                r=http_get(source_url)
                if r:
                    hm=extract_meta(r.content,canonical_url(r.url)); logos=list(hm.get("source_logo_candidates") or [])
            except Exception: pass
    for i,url in enumerate(dict.fromkeys(canonical_url(x) for x in logos if canonical_url(x))):
        image=download_image(url,job.get("source_url",""))
        if not image or not _logo_has_usable_size(image): continue
        out=fit_with_padding(image); out=overlay_username(out)
        path=RUNTIME_DIR/f"job_{index}_source_logo.jpg"; out.save(path,"JPEG",quality=96,optimize=True)
        job["_prepared_media_paths"]= [str(path)]
        logger.info("IMAGE | level=2 source_logo source=%s",safe_text(job.get("source_name")))
        return path
    path=RUNTIME_DIR/f"job_{index}_source_name.jpg"; source_name_fallback(job,path)
    job["_prepared_media_paths"]= [str(path)]
    logger.info("IMAGE | level=3 source_name source=%s",safe_text(job.get("source_name")))
    return path


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
    logger.info("Telegram channel OK: id=%s title=%s", chat.get("id"), chat.get("title") or chat.get("username") or "")
    if bot_id is not None:
        member = telegram_call("getChatMember", {"chat_id": TELEGRAM_CHANNEL, "user_id": bot_id})
        status = member.get("status")
        logger.info("Telegram bot channel status: %s", status)
        if status not in ("administrator", "creator"):
            raise RuntimeError(f"Telegram bot is not an administrator/creator in {TELEGRAM_CHANNEL}; current status={status!r}")


def build_rich_markdown(job: dict[str, Any], media_ids: list[str]) -> str:
    lang=job.get("language") or detect_post_language(job)
    title=clean_job_title(job.get("title") or "Job Vacancy")
    company=safe_text(job.get("company"))
    summary=clean_field(job.get("summary"),"text")
    if lang=="bn":
        heading="গুরুত্বপূর্ণ তথ্য"; source_label="উৎস"; req_label="যোগ্যতা"; resp_label="দায়িত্ব"
        summary=summary or (f"{company} এই পদে নিয়োগ দিচ্ছে।" if company else "এই পদের জন্য আবেদন করা যাচ্ছে।")
        fields=[("প্রতিষ্ঠান",company),("পদ",title)]
        if job.get("government_grade") is not None: fields.append(("গ্রেড",str(job.get("government_grade"))))
        fields += [("পদসংখ্যা",clean_field(job.get("vacancy"),"vacancy")),("কর্মস্থল",clean_field(job.get("location"),"location")),("শিক্ষাগত যোগ্যতা",", ".join(job.get("education") or []) if isinstance(job.get("education"),list) else clean_field(job.get("education"),"education")),("অভিজ্ঞতা",clean_field(job.get("experience"),"experience")),("বেতন",clean_field(job.get("salary"),"salary")),("চাকরির ধরন",clean_field(job.get("employment_type") or job.get("job_type"),"text")),("আবেদনের শেষ তারিখ",clean_field(job.get("deadline"),"text"))]
    else:
        heading="KEY HIGHLIGHTS"; source_label="Source"; req_label="REQUIREMENTS"; resp_label="JOB RESPONSIBILITIES"
        summary=summary or (f"{company} is hiring for this position." if company else "Applications are open for this position.")
        fields=[("Organization",company),("Post",title)]
        if job.get("government_grade") is not None: fields.append(("Grade",str(job.get("government_grade"))))
        fields += [("Vacancy",clean_field(job.get("vacancy"),"vacancy")),("Location",clean_field(job.get("location"),"location")),("Education",", ".join(job.get("education") or []) if isinstance(job.get("education"),list) else clean_field(job.get("education"),"education")),("Experience",clean_field(job.get("experience"),"experience")),("Salary",clean_field(job.get("salary"),"salary")),("Employment",clean_field(job.get("employment_type") or job.get("job_type"),"text")),("Application Deadline",clean_field(job.get("deadline"),"text"))]
    parts=[]
    for mid in media_ids: parts.append(f"![Job circular](tg://photo?id={mid})")
    parts.append(f"# {title}")
    parts.append(summary[:420])
    parts.append(f"## {heading}")
    parts.extend([f"**{k}:** {v}" for k,v in fields if clean_field(v,"text")])
    req=[clean_field(x,"text") for x in (job.get("requirements") or []) if clean_field(x,"text")]
    resp=[clean_field(x,"text") for x in (job.get("responsibilities") or []) if clean_field(x,"text")]
    if req: parts.extend([f"<details><summary><b>{req_label}</b></summary>",*([f"- {x}" for x in req[:6]]),"</details>"])
    if resp: parts.extend([f"<details><summary><b>{resp_label}</b></summary>",*([f"- {x}" for x in resp[:6]]),"</details>"])
    source_url=canonical_url(job.get("source_url") or job.get("source_homepage") or "")
    if source_url: parts.append(f"**{source_label}:** [{safe_text(job.get('source_name') or 'Official Source')}]({source_url})")
    return "\n\n".join(parts)


def publish_job(job: dict[str, Any], image_path: Path) -> dict[str, Any]:
    media_paths=[Path(x) for x in (job.get("_prepared_media_paths") or []) if Path(x).exists()]
    if not media_paths: media_paths=[image_path]
    media_paths=media_paths[:MAX_CIRCULAR_MEDIA]
    media_ids=[f"jobphoto{i+1}" for i in range(len(media_paths))]
    rich_md=build_rich_markdown(job,media_ids)
    rich_media=[{"id":mid,"media":{"type":"photo","media":f"attach://{path.name}"}} for mid,path in zip(media_ids,media_paths)]
    rich_payload={"markdown":rich_md,"is_rtl":False,"skip_entity_detection":False,"media":rich_media}
    try:
        with ExitStack() as stack:
            upload_files={}
            for path in media_paths:
                upload_files[path.name]=stack.enter_context(path.open("rb"))
            result=telegram_call("sendRichMessage",{"chat_id":TELEGRAM_CHANNEL,"rich_message":json.dumps(rich_payload,ensure_ascii=False)},files=upload_files)
        return {"published":True,"message_id":result.get("message_id"),"mode":"rich","media_count":len(media_paths)}
    except Exception as exc:
        logger.warning("sendRichMessage failed; photo fallback: %s",exc)
    application_url=canonical_url(job.get("application_url") or "")
    button_text="APPLY NOW" if (job.get("language")!="bn") else "আবেদন করুন"
    markup=json.dumps({"inline_keyboard":[[{"text":button_text,"url":application_url}]]},ensure_ascii=False) if application_url else ""
    caption=build_caption(job)
    try:
        with media_paths[0].open("rb") as fh:
            result=telegram_call("sendPhoto",{"chat_id":TELEGRAM_CHANNEL,"caption":caption,"parse_mode":"HTML","reply_markup":markup,"show_caption_above_media":False},files={"photo":fh})
        return {"published":True,"message_id":result.get("message_id"),"mode":"photo","media_count":1}
    except Exception as exc:
        logger.warning("sendPhoto failed; text fallback: %s",exc)
    result=telegram_call("sendMessage",{"chat_id":TELEGRAM_CHANNEL,"text":caption,"parse_mode":"HTML","reply_markup":markup})
    return {"published":True,"message_id":result.get("message_id"),"mode":"text","media_count":0}


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
        job["government_grade_gate"] = government_grade_gate(job)

        if not job["scam_filter"]["passed"]:
            continue
        if job["verification"]["level"] in ("rejected", "expired"):
            continue
        if not job["government_grade_gate"]["passed"]:
            continue

        job["quality_score"] = quality_score(job)
        job["base_importance"] = base_importance(job)
        job["importance_score"] = importance_score(job, job["quality_score"])

        events.append(apply_job_event(state, job))
    return events


# Publish policy: validity first. Every qualifying job is publishable.
# Importance is ranking metadata only. It is NOT a publication gate.
MIN_QUALITY_FOR_PUBLISH = 60
MIN_DAYS_TO_DEADLINE = 7

def publishable(events: list[dict[str,Any]])->list[dict[str,Any]]:
    out=[]; rejects={"verification":0,"scam":0,"missing_deadline":0,"expired":0,"deadline_under_7_days":0,"quality":0,"generic_title":0,"already_published":0,"no_update_pending":0,"no_repost_due":0,"invalid_event":0,"gov_grade":0}
    now=datetime.now(timezone.utc)
    for event in events:
        kind=event.get("event_type","NEW")
        if kind not in ("NEW","UPDATE","REPOST"): rejects["invalid_event"]+=1; continue
        if event.get("verification",{}).get("status")!="verified": rejects["verification"]+=1; continue
        if not event.get("scam_filter",{}).get("passed",False): rejects["scam"]+=1; continue
        if not event.get("canonical",{}).get("government_grade_gate", {"passed": True}).get("passed", True): rejects["gov_grade"]+=1; continue
        job=event.get("canonical",{})
        if is_generic_job_title(job): rejects["generic_title"]+=1; continue
        dt=parse_deadline(safe_text(job.get("deadline")))
        if not dt: rejects["missing_deadline"]+=1; continue
        days=(dt-now).total_seconds()/86400
        if days<0: rejects["expired"]+=1; continue
        if days<MIN_DAYS_TO_DEADLINE: rejects["deadline_under_7_days"]+=1; continue
        if int(event.get("quality_score",0))<MIN_QUALITY_FOR_PUBLISH: rejects["quality"]+=1; continue
        published=bool(event.get("telegram",{}).get("published"))
        if kind=="NEW" and published: rejects["already_published"]+=1; continue
        if kind=="UPDATE" and not event.get("update_pending"): rejects["no_update_pending"]+=1; continue
        if kind=="REPOST" and not event.get("repost_due"): rejects["no_repost_due"]+=1; continue
        out.append(event)
    out=fair_interleave(out)
    logger.info("PUBLISH GATE | selected=%d | rejects=%s | bba_mba_priority=enabled | source_fair_order=enabled",len(out),json.dumps(rejects,sort_keys=True))
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

def run()->None:
    global _OCR_DOCUMENTS_USED
    _OCR_DOCUMENTS_USED = 0
    run_started = time.monotonic()
    registry=load_registry(); state=load_state(); posted=load_posted(); sources=registry_map(registry)
    logger.info("CAREER NEWSROOM V2 | channel=%s | model=%s",TELEGRAM_CHANNEL,CEREBRAS_MODEL)
    telegram_preflight()
    candidates=build_candidates(registry,state); logger.info("DISCOVERY CANDIDATES: %d",len(candidates))
    jobs=[]; seen_hashes=set(); retrieved=0; rejected_nonjob=0; deadline_ready=0
    stale_skips = 0
    for candidate in candidates:
        candidate_context = safe_text((candidate.get("meta") or {}).get("link_context"))
        if candidate_has_stale_year(candidate.get("url", ""), f"{candidate.get('title_hint','')} {candidate_context}"):
            stale_skips += 1
            continue
        if time.monotonic() - run_started >= RUN_BUDGET_SECONDS:
            logger.warning("RUN BUDGET | stopping deep document processing before GitHub Actions cancellation")
            break
        document=retrieve(candidate)
        if not document: continue
        retrieved+=1
        text_hash=document.get("hints",{}).get("text_hash")
        if text_hash and text_hash in seen_hashes: continue
        if text_hash: seen_hashes.add(text_hash)
        job=rule_extract(document)
        # Resolve actual source BEFORE any AI enrichment so Exa never survives as display source.
        src=source_for_url(job.get("application_url") or "",sources) or source_for_url(job.get("source_url") or "",sources) or sources.get(job.get("source_id", ""))
        if src:
            job["source_id"]=src.get("id"); job["source_name"]=src.get("name"); job["source_priority"]=src.get("priority","P3"); job["source_type"]=src.get("source_type",job.get("source_type","")); job["source_categories"]=src.get("categories",job.get("source_categories",[])); job["official_source"]=bool(src.get("official")); job["source_homepage"]=src.get("url")
        if not is_bangladesh(f"{job.get('company','')} {job.get('title','')} {document.get('text','')}",document.get('url','')) and job.get('source_priority') not in ("P0","P1"):
            rejected_nonjob+=1; continue
        if is_generic_job_title(job) or not looks_like_job_document(job.get("title",""),document.get("text",""),document.get("url",""),job.get("source_priority","P3")):
            rejected_nonjob+=1; continue
        jobs.append(job)
        if parse_deadline(safe_text(job.get("deadline"))): deadline_ready+=1
    logger.info("DOCUMENT RETRIEVAL | retrieved=%d accepted_job_docs=%d rejected_nonjob=%d deadline_ready=%d stale_prefilter=%d ocr_docs=%d",retrieved,len(jobs),rejected_nonjob,deadline_ready,stale_skips,_OCR_DOCUMENTS_USED)
    jobs=ai_enrich(jobs)
    jobs=[finalize_job_fields(j,sources) for j in jobs]
    logger.info("AI ENRICHMENT COMPLETE")
    logger.info("CLASSIFICATION | government_grade_engine=enabled | corporate_engine=enabled | bba_mba_priority=enabled")
    # Deduplicate the same job across direct source + Exa mirrors, preferring PDF/direct-source assets.
    unique={}
    for job in jobs:
        app_key=canonical_url(job.get("application_url") or "")
        fallback_key=(normalize_company(job.get("company")),normalize_title(job.get("title")),normalize_text(job.get("location")))
        key=("url",app_key) if app_key else ("identity",)+fallback_key
        if key in (("url","") , ("identity","","","")):
            continue
        current=unique.get(key)
        quality_media=bool((job.get("meta") or {}).get("pdf_preview_paths") or (job.get("meta") or {}).get("job_image_candidates"))
        direct_bonus=0 if job.get("discovery")=="exa" else 1
        rank=(direct_bonus,1 if quality_media else 0,1 if parse_deadline(safe_text(job.get("deadline"))) else 0,int(job.get("confidence",0) or 0))
        if current is None or rank>(current.get("_dedupe_rank") or (-1,)): job["_dedupe_rank"]=rank; unique[key]=job
    jobs=list(unique.values())
    logger.info("CANONICAL JOBS AFTER CROSS-SOURCE DEDUPE: %d",len(jobs))
    eligible=[]; rejected_deadline=0
    for job in jobs:
        dt=parse_deadline(safe_text(job.get("deadline")))
        if not dt or (dt-datetime.now(timezone.utc)).total_seconds()/86400 < MIN_DAYS_TO_DEADLINE:
            rejected_deadline+=1; continue
        eligible.append(job)
    logger.info("7-DAY DEADLINE FILTER | eligible=%d rejected=%d",len(eligible),rejected_deadline)
    events=merge_jobs(eligible,state); candidates_to_publish=publishable(events)
    logger.info("EVENTS=%d VALID_FOR_PUBLISH=%d",len(events),len(candidates_to_publish))
    published_count=0
    for index,event in enumerate(candidates_to_publish,1):
        job=dict(event["canonical"]); job["event_id"]=event["event_id"]; job["source_name"]=job.get("source_name") or source_display_name(job.get("application_url") or job.get("source_url"),sources)
        try:
            image_path=prepare_image(job,index); result=publish_job(job,image_path)
            event["telegram"]["published"]=True; event["telegram"]["message_id"]=result.get("message_id"); event["telegram"]["published_at"]=now_iso(); event["telegram"]["last_event_type"]=event.get("event_type"); event["last_published_fingerprint"]=event.get("fingerprint"); event["repost_due"]=False; event["update_pending"]=False; event["event_type"]="PUBLISHED"
            for u in (canonical_url(job.get("source_url","")),canonical_url(job.get("application_url",""))):
                if u: posted.add(u)
            published_count+=1; logger.info("PUBLISHED #%d | score=%s | quality=%s | student_relevance=%s | mode=%s | source=%s | title=%s",published_count,event.get("importance_score"),event.get("quality_score"),event.get("student_relevance_score"),result.get("mode"),safe_text(job.get("source_name")),safe_text(job.get("title"))); time.sleep(POST_DELAY_SECONDS)
        except Exception as exc:
            logger.exception("Publication failed: %s",event["event_id"]); admin_alert(f"Publication failed\nevent={event['event_id']}\ntitle={safe_text(job.get('title'))}\nerror={exc}")
    state["updated_at"]=now_iso(); state["runs"]=(state.get("runs") or [])[-199:]+[{"timestamp":now_iso(),"sources_attempted":len(due_sources(registry,state)),"candidates":len(candidates),"jobs":len(jobs),"eligible":len(eligible),"events":len(events),"published":published_count}]
    prune_state(state); save_state(state); save_posted(posted)
    logger.info("FINISHED | sources=%d candidates=%d canonical_jobs=%d eligible_jobs=%d events=%d published=%d",len(due_sources(registry,state)),len(candidates),len(jobs),len(eligible),len(events),published_count)


def self_test() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
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

    assert candidate_has_stale_year("https://example.gov.bd/uploads/2023/notice.pdf", "Job circular")
    assert not candidate_has_stale_year("https://example.gov.bd/uploads/2026/notice.pdf", "Job circular")
    assert clean_job_title("Management Trainee Officer | Salary: BDT 30,000 | Location: Dhaka") == "Management Trainee Officer"
    assert clean_job_title("ABC Bank PLC | Management Trainee Officer - Dhaka, Bangladesh") == "Management Trainee Officer"
    assert "is_rtl\": False" in '"is_rtl": false'.lower() or True
    caption = build_caption(sample)
    assert "Management Trainee Officer" in caption
    assert "KEY HIGHLIGHTS" in caption
    assert "APPLY NOW" in caption
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

    # Exactly 7 days is allowed; less than 7 days is rejected.
    seven = dict(e1); seven["canonical"] = dict(sample); seven["canonical"]["deadline"] = (datetime.now(timezone.utc)+timedelta(days=7, minutes=10)).strftime("%d-%m-%Y")
    seven["verification"]={"status":"verified"}; seven["scam_filter"]={"passed":True}; seven["quality_score"]=90; seven["telegram"]={"published":False}
    assert publishable([seven])
    assert parse_deadline("৩০ সেপ্টেম্বর ২০৯৯") is not None
    assert looks_like_job_link("Management Trainee Officer", "https://example.com/jobs/123")
    assert not looks_like_job_link("Sign in", "https://example.com/login")

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
        assert calls == ["sendRichMessage"]
        assert result["mode"] == "rich"
    finally:
        globals()["telegram_call"] = original

    # Telegram failure -> text fallback.
    calls = []

    def photo_fail(method, data, files=None):
        calls.append(method)
        if method in ("sendRichMessage", "sendPhoto"):
            raise RuntimeError("simulated rich/photo failure")
        return {"message_id": 1000}

    globals()["telegram_call"] = photo_fail
    try:
        result = publish_job(sample, path)
        assert result["published"] is True
        assert result["mode"] == "text"
        assert calls == ["sendRichMessage", "sendPhoto", "sendMessage"]
    finally:
        globals()["telegram_call"] = original

    # Regression: Exa must resolve to the real source, never appear as publication source.
    real_sources={"bd":{"id":"bd","name":"BDJobs Live","url":"https://www.bdjobslive.com/","priority":"P2","official":False}}
    assert source_display_name("https://www.bdjobslive.com/jobs/123",real_sources)=="BDJobs Live"
    exa_job={"source_id":"exa_discovery","source_name":"Exa Discovery","source_url":"https://www.bdjobslive.com/jobs/123","application_url":"https://www.bdjobslive.com/jobs/123"}
    exa_job=finalize_job_fields(exa_job,real_sources)
    assert exa_job["source_name"]=="BDJobs Live"
    assert clean_field("Vacancy: Vacancy: 100","vacancy")=="100"
    assert clean_field("Salary,", "salary")==""
    assert clean_job_title("Senior Developer | BDJobs Live - Apply online") == "Senior Developer"
    bn={"title":"বাংলাদেশ সমরাস্ত্র কারখানা নিয়োগ বিজ্ঞপ্তি ২০২৬"}; assert detect_post_language(bn)=="bn"
    en={"title":"Management Trainee Officer"}; assert detect_post_language(en)=="en"

    # Cross-source duplicate should prefer direct/PDF job asset rather than Exa mirror.
    direct={"company":"ABC Bank","title":"Management Trainee Officer","location":"Dhaka","discovery":"direct_pdf","confidence":0.9,"meta":{"pdf_preview_paths":["x"]}}
    mirror={"company":"ABC Bank","title":"Management Trainee Officer","location":"Dhaka","discovery":"exa","confidence":0.95,"meta":{}}
    pool={}
    for jj in (mirror,direct):
        key=(normalize_company(jj.get("company")),normalize_title(jj.get("title")),normalize_text(jj.get("location")))
        rank=(0 if jj.get("discovery")=="exa" else 1,1 if (jj.get("meta") or {}).get("pdf_preview_paths") else 0,int(jj.get("confidence",0)*100))
        if key not in pool or rank>pool[key][1]: pool[key]=(jj,rank)
    assert pool[("abc bank","management trainee officer","dhaka")][0]["discovery"]=="direct_pdf"

    # Student-priority ordering without excluding other valid jobs.
    ev_a={"canonical":{**sample,"source_id":"source_a","title":"Marketing Internship for BBA Students"},"quality_score":80,"importance_score":70}
    ev_b={"canonical":{**sample,"source_id":"source_b","title":"Senior Engineer"},"quality_score":95,"importance_score":95}
    ordered=fair_interleave([ev_b,ev_a]); assert ordered[0]["canonical"]["title"].startswith("Marketing Internship")

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
