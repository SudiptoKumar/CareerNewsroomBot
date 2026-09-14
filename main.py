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
MAX_EXA_QUERIES = 32
MAX_EXA_RESULTS_PER_QUERY = 6
EXA_FRESHNESS_DAYS = 45
POST_DELAY_SECONDS = 1.5
HTTP_TIMEOUT_SECONDS = 10
CEREBRAS_CALLS_PER_MINUTE = 3
CEREBRAS_RETRY_AFTER_SECONDS = 61

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
    source_url=canonical_url(source.get("url",""))
    r=http_get(source_url)
    if not r: raise RuntimeError("source request failed")
    final_url=canonical_url(r.url)
    ctype=r.headers.get("content-type","").lower()
    base={"source_id":source["id"],"source_name":source["name"],"source_priority":source.get("priority","P3"),"source_official":bool(source.get("official"))}
    if "pdf" in ctype or final_url.lower().endswith(".pdf"):
        text=extract_pdf_text(r.content)
        if not text: raise RuntimeError("PDF extraction returned no text")
        return [{**base,"url":final_url,"title_hint":"","text_hint":text[:30000],"image":"","meta":{},"published_date":"","discovery":"direct_pdf"}]
    if "xml" in ctype or final_url.lower().endswith((".rss",".xml")):
        feed=feedparser.parse(r.content); out=[]
        for e in feed.entries:
            link=canonical_url(getattr(e,"link","")); title=safe_text(getattr(e,"title","")); summary=BeautifulSoup(safe_text(getattr(e,"summary","")),"html.parser").get_text(" ",strip=True)
            if not link or not looks_like_job_document(title,summary,link,source.get("priority","P3")): continue
            out.append({**base,"url":link,"title_hint":title,"text_hint":summary[:12000],"image":"","meta":{},"published_date":safe_text(getattr(e,"published","") or getattr(e,"updated","")),"discovery":"rss"})
        return out
    text=extract_html_text(r.content,final_url); meta=extract_meta(r.content,final_url); candidates=[]
    if looks_like_job_document(meta.get("title",""),text,final_url,source.get("priority","P3")):
        candidates.append({**base,"url":final_url,"title_hint":meta.get("title",""),"text_hint":text[:30000],"image":meta.get("image",""),"meta":meta,"published_date":meta.get("datePublished") or meta.get("datePosted") or "","discovery":"direct_page"})
    soup=BeautifulSoup(r.content,"html.parser"); seen={final_url}
    for node in soup.find_all(["a","iframe","embed","object"]):
        href=node.get("href") or node.get("src") or node.get("data") or ""; link=canonical_url(urljoin(final_url,href)); label=safe_text(node.get_text(" ",strip=True))
        if not link or link in seen: continue
        seen.add(link)
        is_pdf=".pdf" in urlparse(link).path.lower() or ".pdf" in link.lower()
        if not (is_pdf or looks_like_job_link(label,link)): continue
        candidates.append({**base,"url":link,"title_hint":label,"text_hint":"","image":"","meta":{},"published_date":"","discovery":"direct_pdf_link" if is_pdf else "direct_link"})
        if MAX_DIRECT_LINKS_PER_SOURCE > 0 and len(candidates) >= MAX_DIRECT_LINKS_PER_SOURCE: break
    raw_html=r.content.decode("utf-8",errors="ignore")
    for match in re.findall(r'https?://[^\s"\']+?\.pdf(?:\?[^\s"\']*)?',raw_html,flags=re.I):
        link=canonical_url(match.rstrip(",;)]}"))
        if not link or link in seen: continue
        seen.add(link); candidates.append({**base,"url":link,"title_hint":"","text_hint":"","image":"","meta":{},"published_date":"","discovery":"direct_pdf_link"})
        if MAX_DIRECT_LINKS_PER_SOURCE > 0 and len(candidates) >= MAX_DIRECT_LINKS_PER_SOURCE: break
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
    candidates=[]; crawl_ok=0; crawl_failed=0; pdf_candidates=0
    def crawl_one(source): return source, direct_discover(source)
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map={pool.submit(crawl_one,source): source for source in all_sources}
        for future in as_completed(future_map):
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

def _render_pdf_preview(pdf_bytes: bytes, target: Path) -> str:
    """Render page 1 of a job circular at print-like resolution for Telegram."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count < 1:
            return ""
        page = doc.load_page(0)
        matrix = fitz.Matrix(2.4, 2.4)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(target))
        doc.close()
        return str(target)
    except Exception as exc:
        logger.debug("PDF preview render failed: %s", exc)
        return ""


def retrieve(candidate: dict[str, Any]) -> dict[str, Any] | None:
    url=canonical_url(candidate.get("url","")); text_hint=safe_text(candidate.get("text_hint","")); meta=candidate.get("meta") or {}
    if len(text_hint)>=250 and looks_like_job_document(candidate.get("title_hint",""),text_hint,url,candidate.get("source_priority","P3")):
        return {**candidate,"url":url,"text":text_hint[:24000],"hints":deterministic_hints(text_hint),"meta":meta}
    r=http_get(url)
    if not r: return None
    final_url=canonical_url(r.url); ctype=r.headers.get("content-type","").lower()
    if "pdf" in ctype or final_url.lower().endswith(".pdf") or ".pdf" in final_url.lower():
        text=extract_pdf_text(r.content)
        if not text: return None
        meta={"pdf_preview_path": _render_pdf_preview(r.content, RUNTIME_DIR / f"pdf_{hash_text(final_url)[:12]}.jpg")}
    else:
        text=extract_html_text(r.content,final_url); meta=extract_meta(r.content,final_url)
    title=candidate.get("title_hint","") or meta.get("title","")
    if not looks_like_job_document(title,text,final_url,candidate.get("source_priority","P3")): return None
    if not (is_bangladesh(text,final_url) or candidate.get("source_priority") in ("P0","P1")): return None
    return {**candidate,"url":final_url,"text":text[:24000],"hints":deterministic_hints(text),"meta":meta,
            "published_date":candidate.get("published_date") or meta.get("datePublished") or meta.get("datePosted") or ""}



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


def rule_extract(document: dict[str, Any]) -> dict[str, Any]:
    meta=document.get("meta") or {}; hints=document.get("hints") or {}; structured=jsonld_job(meta) or {}; inferred=infer_from_text(document.get("text",""))
    title=structured.get("title") or inferred.get("title") or safe_text(document.get("title_hint")) or safe_text(meta.get("title"))
    company=structured.get("company") or inferred.get("company") or ""
    if not company and document.get("source_official") and document.get("source_priority")=="P1": company=safe_text(document.get("source_name"))
    return {
        "is_job":True,"company":company or None,"title":title or None,"organization_type":None,"sector":None,"job_function":None,"job_level":None,"job_type":None,
        "employment_type":structured.get("employment_type") or None,"location":structured.get("location") or None,"vacancy":hints.get("vacancy"),
        "salary":structured.get("salary") or hints.get("salary"),"education":structured.get("education") or [],"experience":structured.get("experience") or None,"skills":structured.get("skills") or [],
        "age_limit":None,"gender":None,"published_date":structured.get("date_posted") or document.get("published_date") or safe_text(meta.get("datePublished")) or safe_text(meta.get("datePosted")) or None,
        "application_start":None,"deadline":structured.get("deadline") or hints.get("deadline"),"application_method":None,
        "application_url":structured.get("application_url") or infer_application_url(document.get("text", ""), document.get("url", "")) or document.get("url"),"requirements":[],"responsibilities":[],"summary":safe_text(meta.get("description")),
        "confidence":0.88 if structured else (0.68 if company and title else 0.52),"source_id":document.get("source_id"),"source_name":document.get("source_name"),
        "source_priority":document.get("source_priority","P3"),"official_source":bool(document.get("source_official")),"source_url":document.get("url"),"discovery":document.get("discovery"),"meta":meta,
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
    text = "\n".join(lines)
    if len(text) <= MAX_CAPTION:
        return text
    # Never cut an HTML tag/entity in half. Keep complete lines only.
    kept=[]; total=0
    for line in lines:
        add = len(line) + (1 if kept else 0)
        if total + add > MAX_CAPTION - 24:
            break
        kept.append(line); total += add
    return "\n".join(kept) + "\n…"


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
    """Three-level image strategy:
    1) real job/circular image, high quality + username only;
    2) high-quality source logo + username only;
    3) centered bold source name, no username.
    Never use page og:image/social banners as a job photo.
    """
    RUNTIME_DIR.mkdir(exist_ok=True)
    meta = job.get("meta") or {}

    # LEVEL 1: actual job/circular image. PDF page preview is strongest because it is the job circular itself.
    preview = safe_text(meta.get("pdf_preview_path"))
    if preview and Path(preview).exists():
        try:
            image = Image.open(preview).convert("RGB")
            if _image_has_usable_size(image, 900, 500):
                out = fit_with_padding(image)
                out = overlay_username(out)
                path = RUNTIME_DIR / f"job_{index}_circular.jpg"
                out.save(path, "JPEG", quality=95, optimize=True)
                logger.info("IMAGE | level=1 job_circular source=pdf title=%s", safe_text(job.get("title"))[:100])
                return path
        except Exception:
            pass

    candidates = meta.get("job_image_candidates") or []
    normalized=[]
    for item in candidates:
        if isinstance(item, dict):
            url=canonical_url(item.get("url","")); score=int(item.get("score",0))
        else:
            url=canonical_url(item); score=0
        if url: normalized.append((score,url))
    normalized.sort(key=lambda x:x[0], reverse=True)

    for score, url in normalized[:8]:
        image = download_image(url, job.get("source_url", ""))
        if not image or not _image_has_usable_size(image, MIN_JOB_IMAGE_SHORT, MIN_JOB_IMAGE_LONG):
            continue
        out = fit_with_padding(image)
        out = overlay_username(out)
        path = RUNTIME_DIR / f"job_{index}_matched.jpg"
        out.save(path, "JPEG", quality=95, optimize=True)
        logger.info("IMAGE | level=1 job_match score=%s url=%s", score, url[:160])
        return path

    # LEVEL 2: source logo only. Prefer semantic/logo candidates; reject tiny favicons.
    logos = meta.get("source_logo_candidates") or []
    # If the HTML collector did not expose a logo, try the source homepage once.
    if not logos:
        source_url = canonical_url(job.get("source_homepage") or job.get("source_url") or "")
        if source_url:
            try:
                r=http_get(source_url)
                if r:
                    homepage_meta=extract_meta(r.content, canonical_url(r.url))
                    logos=list(homepage_meta.get("source_logo_candidates") or [])
            except Exception:
                pass
    for i, url in enumerate(dict.fromkeys(canonical_url(x) for x in logos if canonical_url(x))):
        image = download_image(url, job.get("source_url", ""))
        if not image or not _logo_has_usable_size(image):
            continue
        out = fit_with_padding(image)
        out = overlay_username(out)
        path = RUNTIME_DIR / f"job_{index}_source_logo_{i}.jpg"
        out.save(path, "JPEG", quality=95, optimize=True)
        logger.info("IMAGE | level=2 source_logo url=%s", url[:160])
        return path

    # LEVEL 3: source name only, centered.
    path = RUNTIME_DIR / f"job_{index}_source_name.jpg"
    logger.info("IMAGE | level=3 source_name title=%s", safe_text(job.get("title"))[:100])
    return source_name_fallback(job, path)


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
# Importance is ranking metadata only. It is NOT a publication gate.
MIN_QUALITY_FOR_PUBLISH = 60
MIN_DAYS_TO_DEADLINE = 7

def publishable(events: list[dict[str,Any]])->list[dict[str,Any]]:
    out=[]; rejects={"verification":0,"scam":0,"missing_deadline":0,"expired":0,"deadline_under_7_days":0,"quality":0,"generic_title":0,"already_published":0,"no_update_pending":0,"no_repost_due":0,"invalid_event":0}
    now=datetime.now(timezone.utc)
    for event in events:
        kind=event.get("event_type","NEW")
        if kind not in ("NEW","UPDATE","REPOST"): rejects["invalid_event"]+=1; continue
        if event.get("verification",{}).get("status")!="verified": rejects["verification"]+=1; continue
        if not event.get("scam_filter",{}).get("passed",False): rejects["scam"]+=1; continue
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

def run()->None:
    registry=load_registry(); state=load_state(); posted=load_posted(); sources=registry_map(registry)
    logger.info("CAREER NEWSROOM V1 | channel=%s | model=%s",TELEGRAM_CHANNEL,CEREBRAS_MODEL)
    telegram_preflight()
    candidates=build_candidates(registry,state); logger.info("DISCOVERY CANDIDATES: %d",len(candidates))
    jobs=[]; seen_hashes=set(); retrieved=0; rejected_nonjob=0; deadline_ready=0
    for candidate in candidates:
        document=retrieve(candidate)
        if not document: continue
        retrieved+=1
        text_hash=document.get("hints",{}).get("text_hash")
        if text_hash and text_hash in seen_hashes: continue
        if text_hash: seen_hashes.add(text_hash)
        job=rule_extract(document)
        source=sources.get(job.get("source_id", "")) or source_for_url(job.get("source_url",""),sources)
        if source:
            job["source_id"]=source.get("id",job.get("source_id")); job["source_name"]=source.get("name"); job["source_priority"]=source.get("priority","P3"); job["official_source"]=bool(source.get("official"))
        if not is_bangladesh(f"{job.get('company','')} {job.get('title','')} {document.get('text','')}",document.get('url','')) and job.get('source_priority') not in ("P0","P1"):
            rejected_nonjob+=1; continue
        if is_generic_job_title(job) or not looks_like_job_document(job.get("title",""),document.get("text",""),document.get("url",""),job.get("source_priority","P3")):
            rejected_nonjob+=1; continue
        jobs.append(job)
        if parse_deadline(safe_text(job.get("deadline"))): deadline_ready+=1
    logger.info("DOCUMENT RETRIEVAL | retrieved=%d accepted_job_docs=%d rejected_nonjob=%d deadline_ready=%d",retrieved,len(jobs),rejected_nonjob,deadline_ready)
    jobs=ai_enrich(jobs); logger.info("AI ENRICHMENT COMPLETE")
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
        job=dict(event["canonical"]); job["event_id"]=event["event_id"]; job["source_name"]=job.get("source_name") or "Career Newsroom"
        try:
            image_path=prepare_image(job,index); result=publish_job(job,image_path)
            event["telegram"]["published"]=True; event["telegram"]["message_id"]=result.get("message_id"); event["telegram"]["published_at"]=now_iso(); event["telegram"]["last_event_type"]=event.get("event_type"); event["last_published_fingerprint"]=event.get("fingerprint"); event["repost_due"]=False; event["update_pending"]=False; event["event_type"]="PUBLISHED"
            for u in (canonical_url(job.get("source_url","")),canonical_url(job.get("application_url",""))):
                if u: posted.add(u)
            published_count+=1; logger.info("PUBLISHED #%d | score=%s | quality=%s | mode=%s | title=%s",published_count,event.get("importance_score"),event.get("quality_score"),result.get("mode"),safe_text(job.get("title"))); time.sleep(POST_DELAY_SECONDS)
        except Exception as exc:
            logger.exception("Publication failed: %s",event["event_id"]); admin_alert(f"Publication failed\nevent={event['event_id']}\ntitle={safe_text(job.get('title'))}\nerror={exc}")
    prune_state(state); state["updated_at"]=now_iso(); state["runs"].append({"timestamp":now_iso(),"sources":len(due_sources(registry,state)),"candidates":len(candidates),"eligible_jobs":len(eligible),"events":len(events),"published":published_count,"rejected_deadline":rejected_deadline}); state["runs"]=state["runs"][-30:]; save_state(state); save_posted(posted)
    logger.info("FINISHED | sources=%d candidates=%d eligible_jobs=%d events=%d published=%d",len(due_sources(registry,state)),len(candidates),len(eligible),len(events),published_count)



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
