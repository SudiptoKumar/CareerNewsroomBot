import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
import shutil
import threading
import atexit
from collections import deque

try:
    import curl_cffi
except ImportError:
    curl_cffi = None
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin, quote, unquote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import requests
try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:
    StealthyFetcher = None

SCRAPLING_BROWSER_ENABLED = os.environ.get("SCRAPLING_BROWSER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
SCRAPLING_BROWSER_TIMEOUT = int(os.environ.get("SCRAPLING_BROWSER_TIMEOUT", "15000"))
SCRAPLING_BROWSER_WAIT_MS = int(os.environ.get("SCRAPLING_BROWSER_WAIT_MS", "1200"))
SCRAPLING_BROWSER_DETAIL_LIMIT = int(os.environ.get("SCRAPLING_BROWSER_DETAIL_LIMIT", "60"))
SCRAPLING_BROWSER_LOCK = threading.Lock()
SCRAPLING_BROWSER_FETCH_COUNT = 0
from bs4 import BeautifulSoup, NavigableString

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:
    Cerebras = None


# ============================================================
# CONFIGURATION
# ============================================================

CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
PIPELINE_VERSION = "Career News V1"
STATE_FORMAT_VERSION = 5
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

TARGET_STORIES_PER_RUN = int(os.environ.get("TARGET_STORIES_PER_RUN", "15"))
MAX_STORIES_PER_RUN = int(os.environ.get("MAX_STORIES_PER_RUN", "20"))
MIN_PRIVATE_POSTS_PER_RUN = int(os.environ.get("MIN_PRIVATE_POSTS_PER_RUN", "10"))
MIN_GOVERNMENT_POSTS_PER_RUN = int(os.environ.get("MIN_GOVERNMENT_POSTS_PER_RUN", "3"))
QUALITY_FLOOR = float(os.environ.get("QUALITY_FLOOR", "65"))
PRIVATE_MIN_FILL_SCORE = float(os.environ.get("PRIVATE_MIN_FILL_SCORE", "58"))
PRIVATE_HARD_FILL_SCORE = float(os.environ.get("PRIVATE_HARD_FILL_SCORE", "55"))
PRIVATE_MIN_INFORMATION_QUALITY = int(os.environ.get("PRIVATE_MIN_INFORMATION_QUALITY", "3"))
PRIVATE_HARD_MIN_INFORMATION_QUALITY = int(os.environ.get("PRIVATE_HARD_MIN_INFORMATION_QUALITY", "3"))
MAX_GOVERNMENT_POSTS_PER_RUN = int(os.environ.get("MAX_GOVERNMENT_POSTS_PER_RUN", "5"))
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "1.0"))

MAX_POST_AGE_DAYS = int(os.environ.get("MAX_POST_AGE_DAYS", "5"))
PRIVATE_DISCOVERY_TARGET = int(os.environ.get("PRIVATE_DISCOVERY_TARGET", "120"))
PRIVATE_DISCOVERY_MAX = int(os.environ.get("PRIVATE_DISCOVERY_MAX", "200"))
PRIVATE_FAST_RANK_TARGET = int(os.environ.get("PRIVATE_FAST_RANK_TARGET", "60"))
PRIVATE_DETAIL_TARGET = int(os.environ.get("PRIVATE_DETAIL_TARGET", "60"))
INTERNSHIP_DETAIL_TARGET = int(os.environ.get("INTERNSHIP_DETAIL_TARGET", "10"))
INTERNSHIP_AI_TARGET = int(os.environ.get("INTERNSHIP_AI_TARGET", "8"))
MIN_INTERNSHIP_POSTS_PER_RUN = int(os.environ.get("MIN_INTERNSHIP_POSTS_PER_RUN", "2"))
PRIVATE_SNAPSHOT_MIN_FIELDS = int(os.environ.get("PRIVATE_SNAPSHOT_MIN_FIELDS", "4"))
GOVERNMENT_SNAPSHOT_MIN_FIELDS = int(os.environ.get("GOVERNMENT_SNAPSHOT_MIN_FIELDS", "3"))
AI_REVIEW_TARGET = int(os.environ.get("AI_REVIEW_TARGET", "50"))
AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "8"))
AI_RETRY_COUNT = int(os.environ.get("AI_RETRY_COUNT", "1"))
GOVERNMENT_DISCOVERY_TARGET = int(os.environ.get("GOVERNMENT_DISCOVERY_TARGET", "20"))
CATEGORY_PAGE_LIMIT = int(os.environ.get("CATEGORY_PAGE_LIMIT", "4"))
CATEGORY_P1_CAP = int(os.environ.get("CATEGORY_P1_CAP", "20"))
CATEGORY_P2_CAP = int(os.environ.get("CATEGORY_P2_CAP", "12"))
DETAIL_WORKERS = int(os.environ.get("DETAIL_WORKERS", "8"))
DISCOVERY_TIMEOUT = int(os.environ.get("DISCOVERY_TIMEOUT", "18"))
DETAIL_TIMEOUT = int(os.environ.get("DETAIL_TIMEOUT", "14"))
LEGACY_DETAIL_TIMEOUT = int(os.environ.get("LEGACY_DETAIL_TIMEOUT", "8"))
TELETALK_API_TIMEOUT = int(os.environ.get("TELETALK_API_TIMEOUT", "15"))
MAX_PRIVATE_EXPERIENCE_YEARS = int(os.environ.get("MAX_PRIVATE_EXPERIENCE_YEARS", "3"))
ACTIVE_JOB_RETENTION_DAYS = int(os.environ.get("ACTIVE_JOB_RETENTION_DAYS", "60"))
FUTURE_TOLERANCE_MINUTES = int(os.environ.get("FUTURE_TOLERANCE_MINUTES", "20"))
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000
DETAIL_MIN_TEXT_CHARS = int(os.environ.get("DETAIL_MIN_TEXT_CHARS", "220"))
DETAIL_CACHE = {}
DETAIL_CACHE_TTL_SECONDS = int(os.environ.get("DETAIL_CACHE_TTL_SECONDS", "900"))

BDJOBS_LISTING_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
BDJOBS_LEGACY_LISTING_URL = "https://jobs.bdjobs.com/jobsearch.asp"
BDJOBS_DETAIL_BASE = "https://jobs.bdjobs.com/jobdetails.asp?id="
BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]
TELETALK_API_URL = "https://alljobs.teletalk.com.bd/api/v1/published-jobs/search"
TELETALK_HOME_URL = "https://alljobs.teletalk.com.bd/"
TELETALK_DOMAIN = "alljobs.teletalk.com.bd"
JINA_PREFIX = "https://r.jina.ai/"

CURL_IMPERSONATES = tuple(x.strip() for x in os.environ.get(
    "CURL_IMPERSONATES", "safari18_0_ios,safari184_ios,safari260_ios,safari_ios"
).split(",") if x.strip()) or ("safari18_0_ios",)
CURL_MAX_FINGERPRINT_ATTEMPTS = max(1, int(os.environ.get("CURL_MAX_FINGERPRINT_ATTEMPTS", "4")))
CURL_VERIFY_SSL = os.environ.get("CURL_VERIFY_SSL", "1").strip().lower() not in {"0", "false", "no", "off"}
JINA_ENABLED = os.environ.get("JINA_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
JINA_TIMEOUT = int(os.environ.get("JINA_TIMEOUT", "10"))
JINA_RPM_LIMIT = max(1, int(os.environ.get("JINA_RPM_LIMIT", "24")))
JINA_WINDOW_SECONDS = int(os.environ.get("JINA_WINDOW_SECONDS", "60"))
JINA_REQUEST_TIMES = deque()
JINA_RATE_LOCK = threading.Lock()

BDBJOBS_CATEGORIES = {
    1:  {"name": "Accounting / Finance", "priority": 1},
    2:  {"name": "Bank / Non-Bank Financial Institution", "priority": 1},
    3:  {"name": "Commercial / Supply Chain", "priority": 1},
    9:  {"name": "Marketing / Sales", "priority": 1},
    17: {"name": "HR / Organization Development", "priority": 1},
    7:  {"name": "General Management / Admin", "priority": 1},
    16: {"name": "Customer Service / Call Centre", "priority": 2},
    10: {"name": "Media / Advertisement / Event Management", "priority": 2},
    13: {"name": "Research / Consultancy", "priority": 1},
    12: {"name": "NGO / Development", "priority": 2},
    20: {"name": "Hospitality / Travel / Tourism", "priority": 2},
    6:  {"name": "Garments / Textile", "priority": 2},
    8:  {"name": "IT / Telecom", "priority": 2},
    4:  {"name": "Education / Training", "priority": 2},
}

# BBA/MBA business-role vocabulary used for deterministic classification and ranking.
TARGET_FUNCTION_TERMS = (
    "account", "finance", "bank", "credit", "audit", "tax", "treasury",
    "marketing", "sales", "brand", "digital marketing", "business development",
    "partnership", "growth", "client acquisition", "human resource", "hr",
    "recruitment", "talent acquisition", "hr operations", "procurement", "purchase",
    "sourcing", "inventory", "logistics", "supply chain", "commercial", "import",
    "export", "trade operation", "management trainee", "management executive",
    "admin", "executive assistant", "customer service", "client service",
    "customer experience", "relationship management", "e-commerce", "marketplace",
    "online business", "digital operations", "operations", "process", "business analyst",
    "account manager", "account management", "customer success", "merchandising",
    "research", "consultancy", "ngo", "development", "hospitality", "travel", "tourism",
    "event", "media", "advertisement", "corporate affairs",
)
BUSINESS_ROLE_TERMS = TARGET_FUNCTION_TERMS
EARLY_CAREER_TERMS = ("fresher", "fresh graduate", "fresh graduates", "no experience", "entry-level", "entry level", "intern", "internship", "trainee", "0-1 year", "0–1 year", "0 to 1 year")
NON_BUSINESS_ROLE_TERMS = (
    "software engineer", "software developer", "web developer", "frontend developer",
    "backend developer", "full stack developer", "mobile developer", "app developer",
    "devops", "data engineer", "machine learning engineer", "civil engineer",
    "electrical engineer", "mechanical engineer", "doctor", "medical officer",
    "pharmacist", "nurse", "architect", "laboratory specialist", "designer",
)
SENIOR_TERMS = ("ceo", "cfo", "chief executive", "chief financial", "director", "head of", "general manager", "deputy general manager", "assistant general manager", "dgm", "agm")
NOISE_TITLE_TERMS = ("our valuable partners", "valuable partners", "job search", "find jobs", "active filters", "sign in", "login", "register", "cookie policy", "privacy policy")

HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BDJOBS_LISTING_URL,
    "Cache-Control": "no-cache",
}
SOURCE_NAMES = {
    "bdjobs.com": "Bdjobs",
    "jobs.bdjobs.com": "Bdjobs",
    "alljobs.teletalk.com.bd": "Teletalk",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("career-news-v1")

session = requests.Session()
session.headers.update(HEADERS)
retry_policy = Retry(
    total=4, connect=4, read=4, backoff_factor=1.2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"], respect_retry_after_header=True,
)
session.mount("https://", HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20))
session.mount("http://", HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20))

# ============================================================
# HELPERS
# ============================================================

def safe_text(value):
    return "" if value is None else str(value).strip()


def canonical_url(url):
    raw = safe_text(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    # Normalize percent-encoded/unencoded Unicode paths to the same canonical value.
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=%")
    path = path.rstrip("/")
    query = parsed.query
    # Preserve meaningful query parameters for application URLs; discard common tracking noise.
    if query:
        kept = []
        for part in query.split("&"):
            key = part.split("=", 1)[0].lower()
            if key not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}:
                kept.append(part)
        query = "&".join(kept)
    return f"{host}{path}" + (f"?{query}" if query else "")


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    return {x for x in normalize_title(text).split() if len(x) >= 3}


def title_similarity(a, b):
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    aa, bb = title_tokens(na), title_tokens(nb)
    jac = len(aa & bb) / max(1, len(aa | bb))
    return max(seq, jac)


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BD_TZ)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def now_iso():
    return datetime.now(BD_TZ).isoformat()


def source_name(url):
    domain = urlparse(safe_text(url)).netloc.lower().removeprefix("www.")
    return SOURCE_NAMES.get(domain, domain or "Source")


def normalized_domain(url):
    raw = safe_text(url).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")


def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)


def trim_source_text(text, limit):
    text = safe_text(text)
    if len(text) <= limit:
        return text
    text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,:;-/—")
    return text


def clean_generated_text(text):
    text = safe_text(text)
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.replace("\u2026", "").strip()


def clean_reader_markdown(text):
    """Convert Jina Reader Markdown into parser-safe plain text."""
    raw = html.unescape(safe_text(text))
    if not raw:
        return ""
    raw = raw.replace("\r", "")
    raw = re.sub(r"```(?:[A-Za-z0-9_+-]+)?", "", raw)
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
    raw = re.sub(r"\[\s*\]\([^)]*\)", "", raw)
    raw = re.sub(r"^\s*!?\[[^\]]*\]:\s*\S+\s*$", "", raw, flags=re.M)
    raw = re.sub(r"^\s*>+\s?", "", raw, flags=re.M)
    raw = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw, flags=re.M)
    raw = re.sub(r"^\s*[-*+]\s+", "", raw, flags=re.M)
    raw = re.sub(r"^\s*---+\s*$", "", raw, flags=re.M)
    raw = re.sub(r"<https?://[^>]+>", "", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\b[\w.-]+\.(?:gif|png|jpe?g|webp|svg)\b", "", raw, flags=re.I)
    raw = re.sub(r"^\s*\|\s*", "", raw, flags=re.M)
    raw = re.sub(r"\s*\|\s*", " | ", raw)
    raw = re.sub(r"^\s*:?[-]{3,}:?\s*(?:\|\s*:?[-]{3,}:?\s*)+$", "", raw, flags=re.M)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def is_noise_title(title, url=""):
    normalized_noise = re.sub(r"\s+", " ", safe_text(title)).strip().lower()
    if normalized_noise in {
        "our valuable partners", "valuable partners", "our partners",
        "job search", "find jobs", "active filters", "sign in", "login",
        "register", "home", "quick links",
    }:
        return True
    blob = f"{safe_text(title)} {safe_text(url)}".lower()
    return any(term in blob for term in NOISE_TITLE_TERMS)


def is_index_url(url):
    path = urlparse(safe_text(url)).path.lower().rstrip("/")
    if "jobsearch" in path or path in {"", "/", "/h/jobs"}:
        return True
    return "/jobdetails" not in path


BDJOBS_JOB_RE = re.compile(
    r"^https?://(?:www\.)?jobs\.bdjobs\.com/(?:jobdetails(?:\.asp)?)(?:/)?(?:\?|#)?[^#]*\bid=\d+", re.I
)

def is_bdjobs_job_url(url):
    raw = safe_text(url)
    if not is_domain_allowed(raw, BDJOBS_DOMAINS):
        return False
    parsed = urlparse(raw)
    path = parsed.path.lower().rstrip("/")
    if "/jobdetails" in path and bool(re.search(r"(?:^|[?&])id=\d+", parsed.query, re.I)):
        return True
    return path.startswith("/h/jobs/") and len(path.split("/")) >= 4


def is_teletalk_url(url):
    return is_domain_allowed(url, [TELETALK_DOMAIN])


def is_vacancy_url(url):
    return is_bdjobs_job_url(url) or is_teletalk_url(url)


def job_family_score(title, text):
    blob = f"{title} {text}".lower()
    title_blob = safe_text(title).lower()
    score = 0

    explicit_degree = sum(1 for x in ("bba", "mba", "bbs", "mbs", "business administration", "business studies", "commerce") if x in blob)
    business_function = sum(1 for x in TARGET_FUNCTION_TERMS if x in blob)
    business_role = sum(1 for x in BUSINESS_ROLE_TERMS if x in title_blob)
    early = sum(1 for x in EARLY_CAREER_TERMS if x in blob)
    non_business = sum(1 for x in NON_BUSINESS_ROLE_TERMS if x in title_blob)

    score += min(50, explicit_degree * 14)
    score += min(28, business_function * 4)
    score += min(20, business_role * 7)
    score += min(15, early * 4)
    if business_role and any(x in blob for x in ("bachelor", "honours", "graduate", "degree", "business")):
        score += 12
    if any(x in title_blob for x in SENIOR_TERMS):
        score -= 18
    score -= min(35, non_business * 20)
    return max(0, min(100, score))


def bba_mba_candidate_score(job):
    title = safe_text(job.get("title"))
    education = safe_text(job.get("education"))
    text = safe_text(job.get("raw_text"))
    blob = f"{title} {education} {text}".lower()
    title_blob = title.lower()

    degree_hits = sum(1 for x in ("bba", "mba", "bbs", "mbs", "business administration", "business studies", "commerce") if x in blob)
    role_hits = sum(1 for x in BUSINESS_ROLE_TERMS if x in title_blob)
    function_hits = sum(1 for x in TARGET_FUNCTION_TERMS if x in blob)
    early_hits = sum(1 for x in EARLY_CAREER_TERMS if x in blob)
    generic_degree = any(x in blob for x in ("bachelor", "honours", "honors", "graduate", "master"))

    score = min(60, degree_hits * 20)
    score += min(25, role_hits * 6)
    score += min(20, function_hits * 3)
    score += min(15, early_hits * 4)
    if generic_degree and (role_hits or function_hits):
        score += 10
    if any(x in title_blob for x in NON_BUSINESS_ROLE_TERMS):
        score -= 45
    if any(x in title_blob for x in SENIOR_TERMS):
        score -= 20
    return max(0, min(100, score))


def _normalized_company(text):
    return normalize_title(text).replace("limited", "").replace("ltd", "").strip()


def _job_identity_text(job):
    return "|".join([
        normalize_title(job.get("title", "")),
        _normalized_company(job.get("company", "")),
        normalize_title(job.get("location", "")),
    ])


def job_event_key(job):
    # Prefer a source-native immutable job ID when the source provides one.
    # Cross-source mirror detection still happens in likely_same_job/build_unique_job_pool.
    source_id = safe_text(job.get("source_job_id"))
    source = safe_text(job.get("source")).lower()
    if source_id and source:
        return hashlib.sha1(f"{source}:{source_id}".encode("utf-8")).hexdigest()[:24]
    # Cross-source content identity. This intentionally ignores the source domain so
    # the same vacancy mirrored on source/Bdjobs can still collapse into one event.
    return hashlib.sha1(_job_identity_text(job).encode("utf-8")).hexdigest()[:24]


def _same_application_target(a, b):
    ua = canonical_url(a.get("apply_url", ""))
    ub = canonical_url(b.get("apply_url", ""))
    return bool(ua and ub and ua == ub)


def likely_same_job(a, b):
    # Source-native IDs are authoritative for same-source vacancies. This check must
    # happen before URL equality because government boards can expose several jobs
    # through one shared board/application URL.
    source_a = safe_text(a.get("source")).lower()
    source_b = safe_text(b.get("source")).lower()
    id_a = safe_text(a.get("source_job_id"))
    id_b = safe_text(b.get("source_job_id"))
    if source_a and source_a == source_b and id_a and id_b and id_a != id_b:
        return False

    if canonical_url(a.get("source_url", "")) and canonical_url(a.get("source_url", "")) == canonical_url(b.get("source_url", "")):
        return True
    if _same_application_target(a, b):
        return True
    ta = title_similarity(a.get("title", ""), b.get("title", ""))
    ca = SequenceMatcher(None, _normalized_company(a.get("company", "")), _normalized_company(b.get("company", ""))).ratio()
    la = title_similarity(a.get("location", ""), b.get("location", "")) if a.get("location") and b.get("location") else 0.0
    pa = parse_datetime(a.get("posted_date"))
    pb = parse_datetime(b.get("posted_date"))
    date_close = True if not pa or not pb else abs((pa-pb).total_seconds()) <= 21*86400
    if normalize_title(a.get("title", "")) == normalize_title(b.get("title", "")) and ca >= 0.82 and date_close:
        if not a.get("location") or not b.get("location") or la >= 0.80:
            return True
    if ta >= 0.94 and ca >= 0.85 and date_close:
        return True
    if a.get("source") == b.get("source") and ta >= 0.88 and ca >= 0.80 and (not a.get("location") or not b.get("location") or la >= 0.70):
        return True
    return False


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "format_version": STATE_FORMAT_VERSION,
        "queue": {},
        "events": {},
        "recent_titles": [],
        "last_run": "",
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = default_state()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_posted_urls():
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return {canonical_url(x) for x in f if safe_text(x)}
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(canonical + "\n")


STATE = load_state()
POSTED_URLS = load_posted_urls()
# V1 state uses a fresh queue schema while preserving only current-run compatible data.
# Keep posted history, but do not reuse legacy pending queue records.
if int(STATE.get("format_version", 0) or 0) < STATE_FORMAT_VERSION:
    for _key, _item in STATE.get("queue", {}).items():
        if isinstance(_item, dict) and _item.get("status") in {"pending", "selected"}:
            _item["status"] = "legacy_ignored"
    STATE["format_version"] = STATE_FORMAT_VERSION


def prune_state():
    cutoff = datetime.now(BD_TZ) - timedelta(days=ACTIVE_JOB_RETENTION_DAYS)
    queue = {}
    for key, item in STATE.get("queue", {}).items():
        dt = parse_datetime(item.get("last_seen") or item.get("posted_at") or item.get("discovered_at"))
        if dt and dt >= cutoff and item.get("status") != "expired":
            queue[key] = item
    STATE["queue"] = queue
    events = {}
    for key, item in STATE.get("events", {}).items():
        dt = parse_datetime(item.get("published_at") or item.get("selected_at"))
        if dt and dt >= cutoff:
            events[key] = item
    STATE["events"] = events
    STATE["recent_titles"] = STATE.get("recent_titles", [])[-400:]


# ============================================================
# CLIENTS
# ============================================================

cerebras = None


def get_cerebras():
    global cerebras
    if Cerebras is None or not CEREBRAS_API_KEY:
        return None
    if cerebras is None:
        cerebras = Cerebras(api_key=CEREBRAS_API_KEY)
    return cerebras



# ============================================================
# SOURCE DISCOVERY
# ============================================================

BDJOBS_NATIVE_CATEGORY_HINTS = (
    "account", "finance", "bank", "commercial", "management", "admin", "hr",
    "human resource", "marketing", "sales", "business development", "supply chain",
    "procurement", "operation", "relationship", "credit", "customer service",
    "audit", "tax", "treasury", "merchandising", "corporate affairs", "analyst",
    "trainee", "intern", "executive", "officer",
)

def _listing_date_from_text(card_text):
    return normalize_date_text(_first_match(card_text, [
        r"(?:Posted|Published|Date Posted|Publication Date|প্রকাশিত|পোস্টেড)\s*[:：-]?\s*([^|]+?)(?=\s+(?:Deadline|শেষ তারিখ|View|দেখুন)\b|$)",
    ]))

def _absolute_category_url(category_id, legacy=False):
    base = BDJOBS_LEGACY_LISTING_URL if legacy else BDJOBS_LISTING_URL
    return f"{base}?fcatId={int(category_id)}"

def _bdjobs_pagination_links(page_html, base_url, max_pages=CATEGORY_PAGE_LIMIT):
    soup = BeautifulSoup(page_html or "", "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        label = _clean_one_line(a.get_text(" ", strip=True))
        href = urljoin(base_url, safe_text(a.get("href")))
        if not href or not is_domain_allowed(href, BDJOBS_DOMAINS):
            continue
        if label.isdigit():
            n = int(label)
            if 1 <= n <= max_pages:
                links[n] = href
    return sorted(links.items())

def _bdjobs_card_container(anchor):
    """Choose a compact ancestor containing the useful Bdjobs card metadata.

    Current Bdjobs cards may split title, company, and metadata across nested divs.
    Selecting the first matching parent can return a title-only wrapper.
    """
    title = _clean_one_line(anchor.get_text(" ", strip=True))
    markers = (
        "job location", "location", "experience required", "experience",
        "deadline", "education required", "education", "vacancy",
        "job work place", "workplace", "salary", "apply", "age",
    )
    best = None
    for depth, parent in enumerate(anchor.parents, start=1):
        name = getattr(parent, "name", "")
        if name in {"body", "html"}:
            break
        if not hasattr(parent, "get_text"):
            continue
        text_value = _clean_one_line(parent.get_text(" ", strip=True))
        if not title or title not in text_value or not (30 <= len(text_value) <= 3500):
            continue
        lowered = text_value.lower()
        marker_count = sum(1 for marker in markers if marker in lowered)
        if marker_count == 0:
            continue
        size_penalty = max(0, len(text_value) - 1400) / 500
        score = marker_count * 10 - depth * 0.5 - size_penalty
        candidate = (score, marker_count, -depth, parent)
        if best is None or candidate[:3] > best[:3]:
            best = candidate
    return best[3] if best else anchor.parent


def _listing_context_window(anchor, next_job_anchor=None, max_chars=5000):
    """Collect the visible text between this job link and the next job link.

    Bdjobs currently renders several card values as siblings of the title anchor,
    so relying only on an anchor's immediate/ancestor container can lose location,
    experience, deadline, and education. This linear DOM window preserves the
    actual card sequence without requiring brittle class names.
    """
    pieces = []
    anchor_text_nodes = set(anchor.find_all(string=True))
    next_text_nodes = set(next_job_anchor.find_all(string=True)) if next_job_anchor is not None else set()
    for node in anchor.next_elements:
        if next_job_anchor is not None:
            if node is next_job_anchor:
                break
            if isinstance(node, NavigableString) and node in next_text_nodes:
                break
        if isinstance(node, NavigableString):
            if node in anchor_text_nodes:
                continue
            value = _clean_one_line(str(node))
            if not value:
                continue
            pieces.append(value)
        elif getattr(node, "name", "") == "img":
            # The live Bdjobs search cards use icon images whose ALT text is the
            # actual metadata label ("Job Location", "Experience required",
            # "Deadline for apply the job", "Education required"). BeautifulSoup
            # get_text() drops ALT attributes, so explicitly preserve them.
            value = _clean_one_line(node.get("alt") or node.get("aria-label") or node.get("title") or "")
            if value and value.lower() not in {"logo", "image"}:
                pieces.append(value)
        if sum(len(x) + 1 for x in pieces) >= max_chars:
            break
    return _clean_one_line(" ".join(pieces))[:max_chars]


def _infer_listing_company_from_context(title, context_text):
    """Infer the unlabeled company directly after the job title."""
    title = _clean_one_line(title)
    text = _clean_one_line(context_text)
    if not title or not text:
        return ""
    markers = (
        "Job Location", "Location", "Work Location",
        "Experience required", "Experience", "Deadline",
        "Education required", "Education", "Vacancy", "Salary",
        "Age", "Employment Status", "Employment", "Workplace",
        "Published", "Posted",
    )
    boundary = r"(?=\s+(?:" + "|".join(re.escape(x) for x in markers) + r")\b)"
    match = re.search(rf"^(.+?){boundary}", text, flags=re.I)
    if match:
        candidate = _clean_one_line(match.group(1))
        candidate = re.sub(
            r"^(?:Image:\s*)+",
            "",
            candidate,
            flags=re.I,
        )
        candidate = re.sub(
            r"\s+(?:Dhaka|Chattogram|Chittagong|Khulna|Rajshahi|Sylhet|Barishal|"
            r"Rangpur|Mymensingh|Anywhere(?: in Bangladesh)?)$",
            "",
            candidate,
            flags=re.I,
        )
        candidate = _clean_one_line(candidate)
        if candidate and normalize_title(candidate) != normalize_title(title):
            if not re.search(r"\.(?:gif|png|jpe?g|webp|svg)$", candidate, re.I):
                return candidate
    return ""


def _infer_listing_company(anchor, parent, title, card_text):
    """Recover company names from current and legacy Bdjobs card layouts."""
    title = _clean_one_line(title)
    if not title or parent is None:
        return ""

    try:
        raw_lines = []
        for value in parent.get_text("\n", strip=True).splitlines():
            value = _clean_one_line(value)
            if value:
                raw_lines.append(value)

        norm_title = re.sub(r"\s+", " ", title).casefold()
        for idx, line in enumerate(raw_lines):
            if re.sub(r"\s+", " ", line).casefold() != norm_title:
                continue

            for nxt in raw_lines[idx + 1:idx + 8]:
                low = nxt.lower().strip()
                if low.startswith((
                    "job location", "location", "experience", "deadline",
                    "education", "vacancy", "salary", "age", "employment",
                    "job work place", "workplace", "posted", "published",
                )):
                    break
                if len(nxt) <= 180 and not re.search(r"\.(?:gif|png|jpe?g|webp|svg)\b", nxt, re.I):
                    candidate = re.split(
                        r"\s+(?:Job Location|Location|Experience required|Experience|Deadline|"
                        r"Education required|Education|Vacancy|Salary|Age|Employment Status|Posted|Published)\b",
                        nxt, maxsplit=1, flags=re.I,
                    )[0]
                    candidate = re.sub(
                        r"\s+(?:Dhaka|Chattogram|Chittagong|Khulna|Rajshahi|Sylhet|Barishal|"
                        r"Rangpur|Mymensingh|Anywhere(?: in Bangladesh)?)$",
                        "", candidate, flags=re.I,
                    )
                    candidate = _clean_one_line(candidate)
                    if candidate and normalize_title(candidate) != normalize_title(title):
                        return candidate

        compact = _clean_one_line(card_text)
        boundary = (
            r"(?=\s+(?:Job Location|Location|Experience required|Experience|Deadline|"
            r"Education required|Education|Vacancy|Salary|Age|Employment Status|Posted|Published)\b)"
        )
        match = re.search(rf"^{re.escape(title)}\s+(.+?){boundary}", compact, flags=re.I)
        if match:
            candidate = _clean_one_line(match.group(1))
            candidate = re.sub(
                r"\s+(?:Dhaka|Chattogram|Chittagong|Khulna|Rajshahi|Sylhet|Barishal|"
                r"Rangpur|Mymensingh|Anywhere(?: in Bangladesh)?)$",
                "", candidate, flags=re.I,
            )
            if candidate and normalize_title(candidate) != normalize_title(title):
                return candidate
    except Exception:
        pass
    return ""


def _extract_listing_baseline(card_text):
    """Extract source-backed fields from current/legacy Bdjobs listing cards.

    Current search results often flatten labels without punctuation, e.g.
    ``Job LocationDhaka`` and ``Experience required 0 to 1 year(s)``.
    """
    text = _clean_one_line(card_text)
    text = re.sub(r"\bImage:\s*", " ", text, flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip()

    fields = {
        "company": _first_match(text, [r"(?:Company|Company Name|Organization|Employer)\s*[:：-]\s*(.+?)(?=\s+(?:Job Location|Location|Work Location|Experience|required|Deadline|Education|Vacancy|Salary|Posted|Published|Employment|Workplace)\b|$)"]),
        "experience": _first_match(text, [r"(?:Experience required|Experience|অভিজ্ঞতা)\s*[:：-]?\s*(.+?)(?=\s+(?:Deadline for apply the job|Deadline|Education required|Education|Vacancy|Age|Job Location|Location|Salary|Posted|Published|Employment|Workplace)\b|$)"]),
        "deadline": _first_match(text, [r"(?:Deadline for apply the job|Deadline|শেষ তারিখ)\s*[:：-]?\s*(?:Deadline\s*[:：-]?\s*)?(.+?)(?=\s+(?:Education required|Education|Experience required|Experience|Vacancy|Age|Job Location|Location|Salary|Posted|Published|Employment|Workplace)\b|$)"]),
        "education": _first_match(text, [r"(?:Education required|Education|Educational Requirements|শিক্ষাগত যোগ্যতা)\s*[:：-]?\s*(.+?)(?=\s+(?:Deadline|Experience required|Experience|Vacancy|Job Location|Location|Salary|Posted|Published|Employment|Workplace)\b|$)"]),
        "location": _first_match(text, [r"(?:Job Location|Location|Work Location)\s*[:：-]?\s*(.+?)(?=\s+(?:Deadline for apply the job|Deadline|Education required|Education|Experience required|Experience|Vacancy|Salary|Posted|Published|Employment|Workplace)\b|$)"]),
        "salary": _first_match(text, [r"(?:Salary|Salary Range|Compensation)\s*[:：-]?\s*(.+?)(?=\s+(?:Deadline|Education required|Education|Experience required|Experience|Vacancy|Job Location|Location|Posted|Published|Employment|Workplace)\b|$)"]),
        "vacancy": _first_match(text, [r"(?:Vacancy|No\.\s*of\s*Vacancy|Number of Vacancy|Positions)\s*[:：-]?\s*(\d{1,5})\b"]),
        "employment_type": _first_match(text, [r"(?:Employment Status|Employment Type|Job Type|চাকরির ধরন)\s*[:：-]?\s*(.+?)(?=\s+(?:Workplace|Job Work Place|Job Location|Location|Deadline|Posted|Published)\b|$)"]),
        "workplace": _first_match(text, [r"(?:Job Work Place|Workplace|Work Place|কর্মক্ষেত্র)\s*[:：-]?\s*(.+?)(?=\s+(?:Employment Status|Job Location|Location|Deadline|Posted|Published)\b|$)"]),
        "age": _first_match(text, [r"(?:Age|Age Limit|Age Requirements|বয়স|বয়স|বয়সসীমা|বয়সসীমা)\s*[:：-]?\s*(.+?)(?=\s+(?:Salary|Vacancy|Deadline|Education|required|Experience required|Experience|Job Location|Location|Posted|Published)\b|$)"]),
        "posted": _first_match(text, [r"(?:Published|Posted|Date Posted|Publication Date|প্রকাশিত|প্রকাশ তারিখ|প্রকাশের তারিখ)\s*[:：-]?\s*(.+?)(?=\s+(?:Deadline|Education|Experience|Vacancy|Job Location|Location|Salary|Employment|Workplace)\b|$)"]),
        "application_method": _first_match(text, [r"(?:Application|Application Process|Application Procedure|How to Apply|আবেদন প্রক্রিয়া|আবেদন প্রক্রিয়া|আবেদনের নিয়ম|আবেদনের নিয়ম)\s*[:：-]?\s*(.+?)(?=\s+(?:Selection Process|Deadline|Posted|Published|Salary)\b|$)"]),
    }

    inline = {
        "location": r"(?:Job Location|Location|Work Location)(?!\s*required)\s*(.+?)(?=\s+(?:Experience required|Experience|Deadline for apply the job|Deadline|Education required|Education|Salary|Vacancy|$))",
        "experience": r"(?:Experience required|Experience|অভিজ্ঞতা)\s+(.+?)(?=\s+(?:Deadline for apply the job|Deadline|Education required|Education|Salary|Vacancy|Job Location|Location|$))",
        "deadline": r"(?:Deadline for apply the job|Deadline|শেষ তারিখ)\s*(?:Deadline\s*[:：-]?\s*)?(.+?)(?=\s+(?:Education required|Education|Experience required|Experience|Job Location|Location|Salary|Vacancy|$))",
        "education": r"(?:Education required|Education|শিক্ষাগত যোগ্যতা)\s+(.+?)(?=\s+(?:Deadline|Experience required|Experience|Job Location|Location|Salary|Vacancy|$))",
        "salary": r"(?:Salary|Salary Range|Compensation)\s+(.+?)(?=\s+(?:Deadline|Experience required|Experience|Job Location|Location|Education|Vacancy|$))",
        "vacancy": r"(?:Vacancy|No\.\s*of\s*Vacancy|Number of Vacancy|Positions)\s*[:：-]?\s*(\d{1,5})\b",
        "posted": r"(?:Published|Posted|Date Posted|Publication Date|প্রকাশিত|প্রকাশ তারিখ|প্রকাশের তারিখ)\s+(.+?)(?=\s+(?:Deadline|Experience required|Experience|Job Location|Location|Salary|Education|Vacancy|$))",
    }
    for key, pattern in inline.items():
        if not fields.get(key):
            fields[key] = _first_match(text, [pattern])

    return {
        "company": _clean_one_line(fields["company"]),
        "experience": compact_experience(fields["experience"]),
        "education": compact_education(fields["education"]),
        "deadline": normalize_date_text(fields["deadline"]),
        "location": compact_location(fields["location"]),
        "salary": compact_salary(fields["salary"]),
        "vacancy": compact_vacancy(fields["vacancy"]),
        "employment_type": compact_employment(fields["employment_type"]),
        "workplace": compact_workplace(fields["workplace"]),
        "age": compact_age(fields["age"]),
        "posted_date": normalize_date_text(fields["posted"]),
        "application_method": compact_application(fields["application_method"]),
    }


def _bdjobs_listing_candidates(page_html, page_url, category_id=None, category_name=""):
    """Extract genuine Bdjobs jobs from current/legacy card layouts.

    The current Bdjobs page places the company and metadata in sibling nodes
    after the title link. We therefore use the text window up to the next job
    link as a structural fallback, while retaining the compact ancestor parser.
    """
    soup = BeautifulSoup(page_html or "", "html.parser")
    job_anchors = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, safe_text(a.get("href")))
        if not is_bdjobs_job_url(href):
            continue
        title = _clean_one_line(a.get_text(" ", strip=True))
        if not title or is_noise_title(title, href):
            continue
        job_anchors.append(a)

    found, seen = [], set()
    for idx, a in enumerate(job_anchors):
        href = urljoin(page_url, safe_text(a.get("href")))
        canonical = canonical_url(href)
        if not canonical or canonical in seen:
            continue
        title = _clean_one_line(a.get_text(" ", strip=True))

        parent = _bdjobs_card_container(a)
        ancestor_text = _clean_one_line(parent.get_text(" ", strip=True)) if parent else title
        window_text = _listing_context_window(
            a,
            job_anchors[idx + 1] if idx + 1 < len(job_anchors) else None,
            max_chars=5000,
        )

        # Prefer the compact card when it contains real metadata; otherwise use
        # the DOM window. In either case, supplement missing fields from the
        # window instead of replacing populated values.
        baseline = _extract_listing_baseline(ancestor_text)
        window_baseline = _extract_listing_baseline(window_text)
        for key, value in window_baseline.items():
            if value and not baseline.get(key):
                baseline[key] = value

        if not baseline.get("company") and parent:
            inferred_company = _infer_listing_company(a, parent, title, ancestor_text)
            if inferred_company:
                baseline["company"] = _clean_one_line(inferred_company)
        if not baseline.get("company"):
            inferred_company = _infer_listing_company_from_context(title, window_text)
            if inferred_company:
                baseline["company"] = inferred_company

        # If the ancestor is title-only, the linear window is the actual card
        # context. Keep the richer one as the excerpt used by fallback research.
        card_text = window_text if window_text and len(window_text) >= len(ancestor_text) * 0.35 else ancestor_text
        if not card_text:
            card_text = title

        # Re-run baseline extraction on the chosen excerpt and merge any fields
        # missing from the first pass.
        chosen_baseline = _extract_listing_baseline(card_text)
        for key, value in chosen_baseline.items():
            if value and not baseline.get(key):
                baseline[key] = value

        if not baseline.get("company"):
            inferred_company = _infer_listing_company_from_context(title, card_text)
            if inferred_company:
                baseline["company"] = inferred_company

        posted = _listing_date_from_text(card_text)
        parsed_href = urlparse(href)
        id_match = re.search(r"(?:^|[?&])id=(\d+)", parsed_href.query, re.I)
        if not id_match:
            id_match = re.search(r"/h/jobs/(\d+)(?:/|$)", parsed_href.path, re.I)

        seen.add(canonical)
        found.append({
            "title": title,
            "company": baseline.get("company", ""),
            "url": href,
            "canonical": canonical,
            "source": "Bdjobs",
            "source_url": href,
            "source_job_id": id_match.group(1) if id_match else "",
            "discovery": "bdjobs_category_html",
            "category_id": category_id,
            "category_name": category_name,
            "excerpt": trim_source_text(card_text, 3500),
            "listing_posted": posted,
            "listing_deadline": baseline.get("deadline") or normalize_date_text(
                _first_match(card_text, [r"(?:Deadline(?: for apply the job)?|শেষ তারিখ)\s*[:：-]?\s*(?:Deadline\s*[:：-]?\s*)?([^|]+)"])
            ),
            "listing_fields": baseline,
            "discovered_at": now_iso(),
        })
    return found


def is_cloudflare_response(status, headers, body):
    h = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()
    b = safe_text(body).lower()
    markers = (
        "just a moment...", "performing security verification", "enable javascript and cookies",
        "checking your browser", "cf-chl-", "challenge-platform", "cloudflare ray id",
        "attention required! | cloudflare", "sorry, you have been blocked",
    )
    return "cf-mitigated" in h or status in {401, 403, 429, 503} or any(x in b for x in markers)

def _fetch_with_curl(url, *, timeout=None, referer=None, max_attempts=None):
    if curl_cffi is None:
        return None
    targets = list(CURL_IMPERSONATES)[:(max_attempts or CURL_MAX_FINGERPRINT_ATTEMPTS)]
    for target in targets:
        try:
            headers = dict(HEADERS)
            if referer:
                headers["Referer"] = referer
            response = curl_cffi.get(
                request_safe_url(url), headers=headers, timeout=timeout or DISCOVERY_TIMEOUT,
                allow_redirects=True, impersonate=target, verify=CURL_VERIFY_SSL,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            body = safe_text(getattr(response, "text", ""))
            cf = is_cloudflare_response(status, getattr(response, "headers", {}), body)
            result = {
                "ok": 200 <= status < 400 and bool(body) and not cf, "status": status, "text": body,
                "url": safe_text(getattr(response, "url", "")) or url,
                "backend": f"curl_cffi:{target}", "impersonate": target,
                "cloudflare": cf,
            }
            if cf:
                logger.info("CLOUDFLARE CHALLENGE | fingerprint=%s | status=%s | rotating", target, status)
                continue
            return result
        except Exception as exc:
            logger.debug("curl_cffi target failed | %s | %s | %s", target, url, exc)
    return None

def _acquire_jina_slot():
    if not JINA_ENABLED:
        return False
    while True:
        now = time.monotonic()
        with JINA_RATE_LOCK:
            while JINA_REQUEST_TIMES and now - JINA_REQUEST_TIMES[0] >= JINA_WINDOW_SECONDS:
                JINA_REQUEST_TIMES.popleft()
            if len(JINA_REQUEST_TIMES) < JINA_RPM_LIMIT:
                JINA_REQUEST_TIMES.append(now)
                return True
            wait = max(0.05, JINA_WINDOW_SECONDS - (now - JINA_REQUEST_TIMES[0]))
        time.sleep(min(wait, 2.0))


def _fetch_jina(url, *, timeout=None):
    if not JINA_ENABLED:
        return None
    _acquire_jina_slot()
    try:
        response = requests.get(
            JINA_PREFIX + request_safe_url(url),
            headers={
                "Accept": "text/plain, text/markdown",
                "User-Agent": "Career News V1/1.0",
                "X-Base": "true",
            },
            timeout=timeout or JINA_TIMEOUT, allow_redirects=True,
        )
        body = safe_text(response.text)
        if response.status_code >= 400 or not body:
            return None
        return {
            "ok": True, "status": response.status_code, "text": body, "url": url,
            "backend": "jina_reader", "cloudflare": False,
        }
    except Exception as exc:
        logger.info("JINA READER FAILED | url=%s | error=%s", url, exc)
        return None


def _fetch_source_document(url, *, timeout=None, referer=None):
    direct = _fetch_with_curl(url, timeout=timeout, referer=referer)
    if direct and direct.get("cloudflare"):
        logger.info("CLOUDFLARE DETECTED | %s | %s", url, direct.get("backend"))
    elif direct and direct.get("ok"):
        return direct
    if direct and not direct.get("ok"):
        logger.info("DIRECT FETCH FAILED | %s | status=%s", url, direct.get("status"))
    fallback = _fetch_jina(url, timeout=max(JINA_TIMEOUT, timeout or DISCOVERY_TIMEOUT))
    return fallback

def discover_bdjobs_category(category_id, config):
    category_name = config["name"]
    cap = CATEGORY_P1_CAP if int(config.get("priority", 2)) == 1 else CATEGORY_P2_CAP
    cutoff = datetime.now(BD_TZ) - timedelta(days=MAX_POST_AGE_DAYS)
    collected, seen = [], set()

    for base_url in (_absolute_category_url(category_id), _absolute_category_url(category_id, legacy=True)):
        if len(collected) >= cap:
            break
        next_url = base_url
        seen_pages = set()
        for page_index in range(1, CATEGORY_PAGE_LIMIT + 1):
            if next_url in seen_pages:
                break
            seen_pages.add(next_url)
            fetched = _fetch_source_document(next_url, timeout=DISCOVERY_TIMEOUT, referer=BDJOBS_LISTING_URL)
            if not fetched:
                break
            page_url = fetched.get("url") or next_url
            candidates = _bdjobs_listing_candidates(fetched.get("text", ""), page_url, category_id, category_name)
            page_dates = []
            new_count = 0
            for item in candidates:
                if item["canonical"] in seen or item["canonical"] in POSTED_URLS:
                    continue
                pdt = parse_datetime(item.get("listing_posted"))
                if pdt:
                    page_dates.append(pdt)
                    if pdt < cutoff:
                        continue
                seen.add(item["canonical"])
                item["discovery_backend"] = fetched.get("backend", "")
                collected.append(item)
                new_count += 1
                if len(collected) >= cap:
                    break
            if len(collected) >= cap:
                break
            links = dict(_bdjobs_pagination_links(fetched.get("text", ""), page_url, CATEGORY_PAGE_LIMIT + 2))
            next_url = links.get(page_index + 1, "")
            if not next_url:
                parsed = urlparse(next_url or page_url)
                if page_index == 1 and page_dates and max(page_dates) >= cutoff:
                    next_url = parsed._replace(query=(parsed.query + "&" if parsed.query else "") + "page=2").geturl()
                else:
                    break
            if page_dates and max(page_dates) < cutoff:
                break
            if new_count == 0:
                break
    logger.info("BDJOBS CATEGORY | %s | %d", category_name, len(collected))
    return collected

def discover_bdjobs():
    categories = sorted(BDBJOBS_CATEGORIES.items(), key=lambda kv: (int(kv[1].get("priority", 2)), kv[0]))
    all_items, seen = [], set()
    for priority in (1, 2):
        group = [(cid, cfg) for cid, cfg in categories if int(cfg.get("priority", 2)) == priority]
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(group)))) as pool:
            futures = [pool.submit(discover_bdjobs_category, cid, cfg) for cid, cfg in group]
            for future in as_completed(futures):
                try:
                    items = future.result()
                except Exception as exc:
                    logger.warning("Bdjobs category worker failed: %s", exc)
                    items = []
                for item in items:
                    canonical = item.get("canonical") or canonical_url(item.get("url", ""))
                    if not canonical or canonical in seen or canonical in POSTED_URLS:
                        continue
                    seen.add(canonical)
                    all_items.append(item)
                    if len(all_items) >= PRIVATE_DISCOVERY_MAX:
                        break
                if len(all_items) >= PRIVATE_DISCOVERY_MAX:
                    break
        if len(all_items) >= PRIVATE_DISCOVERY_MAX:
            break
    enriched = sum(
        1 for item in all_items
        if item.get("company")
        and sum(1 for k in ("location","education","experience","deadline") if (item.get("listing_fields") or {}).get(k))
    )
    logger.info(
        "BDJOBS CATEGORY-FIRST | raw=%d target=%d max=%d | categories=%d | listing_rich=%d",
        len(all_items), PRIVATE_DISCOVERY_TARGET, PRIVATE_DISCOVERY_MAX, len(BDBJOBS_CATEGORIES), enriched,
    )
    return all_items[:PRIVATE_DISCOVERY_MAX]

def _teletalk_record_fields(record):
    if not isinstance(record, dict):
        return None
    source_id = safe_text(record.get("job_primary_id") or record.get("jobPrimaryId") or record.get("id"))
    title = _clean_one_line(record.get("job_title") or record.get("jobTitle") or record.get("title"))
    if not source_id or not title:
        return None
    company = _clean_one_line(record.get("org_name") or record.get("orgName") or record.get("organization") or record.get("company"))
    vacancy = compact_vacancy(record.get("vacancy"))
    deadline = normalize_date_text(record.get("deadline_date") or record.get("deadlineDate") or record.get("deadline"))
    posted = normalize_date_text(record.get("published_date") or record.get("publish_date") or record.get("posted_date") or record.get("postedDate"))
    apply_url = safe_text(record.get("application_site_url") or record.get("applicationSiteUrl") or record.get("apply_url") or record.get("applyUrl"))
    education = _strip_html_fragment(record.get("education") or record.get("education_qualification") or "")
    location = _clean_one_line(record.get("location") or record.get("job_location") or "")
    employment = compact_employment(record.get("employment_type") or record.get("job_type") or record.get("jobType"))
    source_url = f"https://alljobs.teletalk.com.bd/?job_primary_id={quote(source_id)}"
    return {
        "title": title, "company": company, "location": location, "salary": "", "experience": "",
        "education": compact_education(education), "vacancy": vacancy, "employment_type": employment,
        "workplace": "", "age": "", "category": "", "application_method": "Online" if apply_url else "",
        "selection_process": "", "application_period": "", "application_start": "", "application_end": "",
        "posted_date": posted, "deadline": deadline, "source": "Teletalk", "source_url": source_url,
        "url": source_url, "apply_url": apply_url, "source_job_id": source_id,
        "discovery": "teletalk_api", "is_government": True,
        "raw_text": " | ".join(x for x in [title, company, location, education] if x),
    }

def _teletalk_records(payload):
    if not isinstance(payload, dict): return []
    for key in ("govtJobs", "data", "jobs", "results"):
        if isinstance(payload.get(key), list): return payload[key]
    return []

def _discover_teletalk_api():
    discovered, seen = [], set()
    try:
        response = session.get(TELETALK_API_URL, params={"searchKeyword": ""}, headers=HEADERS, timeout=TELETALK_API_TIMEOUT)
        response.raise_for_status(); payload = response.json()
        for record in _teletalk_records(payload):
            fields = _teletalk_record_fields(record)
            if not fields: continue
            canonical = canonical_url(fields["source_url"])
            if not canonical or canonical in seen or canonical in POSTED_URLS: continue
            seen.add(canonical)
            discovered.append({
                "title": fields["title"], "url": fields["url"], "canonical": canonical, "source": "Teletalk",
                "source_url": fields["source_url"], "source_job_id": fields["source_job_id"], "discovery": "teletalk_api",
                "excerpt": trim_source_text(fields["raw_text"], 1800), "listing_posted": fields["posted_date"],
                "listing_deadline": fields["deadline"], "discovered_at": now_iso(), "api_fields": fields, "is_government": True,
            })
            if len(discovered) >= GOVERNMENT_DISCOVERY_TARGET: break
        logger.info("TELETALK API DISCOVERY: %d", len(discovered))
    except Exception as exc:
        logger.warning("Teletalk API discovery failed: %s", exc)
    return discovered

def discover_all():
    with ThreadPoolExecutor(max_workers=2) as pool:
        ft = pool.submit(_discover_teletalk_api)
        fb = pool.submit(discover_bdjobs)
        try: government = ft.result()
        except Exception as exc: logger.warning("Teletalk worker failed: %s", exc); government = []
        try: bdjobs = fb.result()
        except Exception as exc: logger.warning("Bdjobs worker failed: %s", exc); bdjobs = []
    merged, seen = [], set()
    for item in government + bdjobs:
        canonical = item.get("canonical") or canonical_url(item.get("source_url", ""))
        if canonical and canonical not in seen:
            seen.add(canonical); merged.append(item)
    logger.info("DISCOVERED | Teletalk=%d | Bdjobs=%d | merged=%d", len(government), len(bdjobs), len(merged))
    return merged


# ============================================================
# RETRIEVAL / EXTRACTION
# ============================================================

def cache_key(url):
    return hashlib.sha256((canonical_url(url) + "|job").encode()).hexdigest()[:24]


def _text_from_html(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def _strip_html_fragment(value):
    """Bdjobs API text fields (eduRec/jobContext/jobDescription) carry inline
    HTML rather than plain text. Strip tags without touching non-HTML values."""
    raw = safe_text(value)
    if not raw or "<" not in raw:
        return raw
    try:
        return _clean_one_line(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))
    except Exception:
        return _clean_one_line(re.sub(r"<[^>]+>", " ", raw))


def _jsonld_objects(page_html):
    objects = []
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        objects.extend(payload if isinstance(payload, list) else [payload])
    return [x for x in objects if isinstance(x, dict)]


def _jobposting_jsonld(page_html):
    for obj in _jsonld_objects(page_html):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if any(safe_text(x).lower() == "jobposting" for x in types):
            return obj
    return {}


def _extract_links(page_html, base_url):
    links = []
    soup = BeautifulSoup(page_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, safe_text(anchor.get("href")))
        if urlparse(href).scheme not in {"http", "https"}:
            continue
        text = safe_text(anchor.get_text(" ", strip=True))
        links.append({"url": href, "text": text})
    return links


def _apply_link_score(link, source_domain):
    url = safe_text(link.get("url"))
    text = safe_text(link.get("text")).lower()
    blob = f"{text} {url.lower()}"
    score = 0
    exact = [
        "apply now", "apply online", "submit application", "application form",
        "apply", "আবেদন করুন", "আবেদন", "apply for this job", "career apply",
    ]
    for term in exact:
        if term in text:
            score += 80 if term in {"apply now", "apply online", "submit application", "application form"} else 55
            break
    for term in ("/apply", "apply?", "application", "/career", "careers.", "jobs.lever", "greenhouse", "workday", "smartrecruiters"):
        if term in blob:
            score += 35
    domain = normalized_domain(url)
    if domain and domain != source_domain and not domain.endswith("." + source_domain):
        score += 30
    if any(x in domain for x in ("facebook.com", "youtube.com", "instagram.com", "linkedin.com", "t.me")):
        score -= 100
    if "/job-details/" in urlparse(url).path.lower():
        score -= 100
    return score


def extract_apply_url(page_html, page_url, source):
    source_domain = normalized_domain(page_url)
    candidates = _extract_links(page_html, page_url)
    scored = sorted(
        ((
            _apply_link_score(link, source_domain),
            link,
        ) for link in candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    for score, link in scored:
        if score < 70:
            continue
        target = safe_text(link["url"])
        if target and target != page_url:
            return target
    return ""


JOB_LABELS = (
    "Title", "Job Title", "Position", "Post Name",
    "Company Name", "Company", "Organization Name", "Employer",
    "Job Location", "Location", "Job Location(s)", "Work Location",
    "Salary", "Salary Range", "Minimum Salary", "Compensation",
    "Experience", "Experience Requirements", "Experience Requirement",
    "Education", "Educational Requirements", "Educational Qualification", "Education Requirements",
    "Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions",
    "Employment Status", "Job Type", "Employment Type",
    "Job Work Place", "Workplace", "Work Place",
    "Age", "Age Limit", "Age Requirements",
    "Category", "Job Category",
    "Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply",
    "Selection Process", "Recruitment Process", "Selection Procedure", "Hiring Process", "Interview Process",
    "Application Period", "Application Date", "Interview Date", "Walk-in Date",
    "Published", "Posted", "Date Posted", "Publication Date",
    "প্রতিষ্ঠানের নাম", "চাকুরি স্থান", "চাকরি স্থান", "চাকরির সারসংক্ষেপ", "কর্মস্থল", "কর্মস্হল", "কর্মক্ষেত্র", "বয়স", "বয়স", "বয়সসীমা", "বয়সসীমা", "বেতন",
    "চাকরির ধরন", "প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ", "শেষ তারিখ", "অভিজ্ঞতা", "শিক্ষাগত যোগ্যতা", "পদ সংখ্যা", "পদসংখ্যা", "খালি পদ",
    "আবেদন প্রক্রিয়া", "আবেদন প্রক্রিয়া", "আবেদনের নিয়ম", "আবেদনের নিয়ম", "নিয়োগ প্রক্রিয়া", "নিয়োগ প্রক্রিয়া",
    "Application Deadline", "Deadline", "Last Date", "Apply Before",
    "Responsibilities & Context", "Responsibilities", "Job Description", "Requirements", "Additional Requirements",
    "Skills", "Skills Required", "Job Other Benifits", "Benefits", "Company Information",
    "Gender", "Job Summary", "Apply by BDJOBS",
)
JOB_LABEL_NORMALIZED = {re.sub(r"\s+", " ", x.lower().rstrip(":")).strip() for x in JOB_LABELS}


def _normalized_line_label(line):
    value = re.sub(r"\s+", " ", safe_text(line)).strip()
    return value.lower().rstrip(":- ").strip()


def _label_value(text, labels):
    """Extract one labelled field without swallowing the next labelled field."""
    raw = safe_text(text)
    if not raw:
        return ""

    wanted = {re.sub(r"\s+", " ", safe_text(label).lower().rstrip(":")).strip() for label in labels}
    lines = [re.sub(r"\s+", " ", x).strip() for x in raw.splitlines() if x.strip()]

    # First pass: line-oriented extraction, including optional whitespace before ':'.
    for i, line in enumerate(lines):
        lower_line = line.lower()
        for label in sorted(wanted, key=len, reverse=True):
            if re.match(rf"^{re.escape(label)}\s*:", lower_line):
                value = re.sub(rf"^{re.escape(label)}\s*:\s*", "", line, count=1, flags=re.I).strip()
                if value:
                    return value

        normalized = _normalized_line_label(line)
        if normalized not in wanted:
            continue

        collected = []
        for nxt in lines[i + 1:]:
            nxt_norm = _normalized_line_label(nxt)
            if nxt_norm in JOB_LABEL_NORMALIZED:
                break
            if re.match(r"^(responsibilities|requirements|additional requirements|benefits|company information)\s*:?$", nxt, re.I):
                break
            collected.append(nxt)
        return " ".join(collected).strip()

    # Second pass: flattened/HTML-text extraction. Unlike the old implementation,
    # the target label does not need to be at the start of a line. This matters for
    # trafilatura/full-page text where several summary fields may be flattened.
    all_labels = "|".join(
        re.escape(x) for x in sorted(JOB_LABELS, key=len, reverse=True)
    )
    for label in sorted(wanted, key=len, reverse=True):
        pattern = (
            rf"(?<![\w]){re.escape(label)}\s*[:\-]\s*(.*?)"
            rf"(?=(?:\s+|^)(?:{all_labels})\s*[:\-]|\Z)"
        )
        match = re.search(pattern, raw, flags=re.I | re.S)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" \t|-:")
            if value:
                return value
    return ""


def _clean_one_line(value):
    value = clean_generated_text(value)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" -:;,|")


def clean_source_field(value):
    """Remove reader/page chrome without altering legitimate source-backed text."""
    value = clean_reader_markdown(value)
    value = re.sub(r"\s*\|\|\s*(?:bdjobs(?:\.com)?|source\s*:\s*bdjobs(?:\.com)?)\s*$", "", value, flags=re.I)
    value = re.sub(r"\bname-share-details\.gif\b", "", value, flags=re.I)
    value = re.sub(r"\bmatching_lock_en(?:_res)?\.(?:webp|svg|png|gif)\b", "", value, flags=re.I)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" -:;,|#")


def clean_job_title(value, fallback=""):
    primary = clean_source_field(value)
    backup = clean_source_field(fallback)
    # Bdjobs page chrome occasionally gets attached to the H1/Markdown heading.
    if backup and (
        "bdjobs.com" in primary.lower()
        or "name-share-details" in primary.lower()
        or len(primary) > max(180, len(backup) + 55)
    ):
        return backup
    return primary or backup


def _first_match(value, patterns):
    value = _clean_one_line(value)
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if match:
            return _clean_one_line(match.group(1) if match.lastindex else match.group(0))
    return ""


def compact_experience(value, raw_text=""):
    # Only publish actual experience duration/fresher status. Never transform
    # technical skills, responsibilities or "area of experience" into a fake duration.
    blob = _clean_one_line(value)
    # source sometimes renders the Experience label/value as a separate summary block.
    # If the first label extraction missed it, recover only the label-specific value.
    if not blob and raw_text:
        recovered = _label_value(raw_text, [
            "Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"
        ])
        blob = _clean_one_line(recovered)
    if not blob:
        return ""
    blob = blob.translate(BENGALI_DIGIT_MAP).replace("বছরের", "years").replace("বছর", "years").replace("থেকে", "to")
    patterns = [
        r"\b(fresh(?:er|ers)|fresher|no\s+experience|entry[- ]level|entry level)\b",
        r"\b(at\s+least\s+\d+\s+years?)\b",
        r"\b(\d+\s*[-–]\s*\d+\s*years?)\b",
        r"\b(\d+\s+to\s+\d+\s+years?)\b",
        r"\b(\d+\+\s*years?)\b",
        r"\b(\d+\s+years?)\b",
    ]
    found = _first_match(blob, patterns)
    if not found:
        return ""
    found = found.replace("–", "-")
    cleaned = _clean_one_line(found)
    if re.fullmatch(r"fresh(?:er|ers)", cleaned, flags=re.I):
        return "Freshers"
    if re.fullmatch(r"no\s+experience", cleaned, flags=re.I):
        return "No Experience"
    if re.fullmatch(r"entry[- ]level", cleaned, flags=re.I):
        return "Entry Level"
    return cleaned


def compact_education(value, raw_text=""):
    blob = _clean_one_line(value)
    search_blob = blob
    found = []
    degree_patterns = [
        (r"\bbachelor(?:'s)?\s+of\s+business\s+administration\b", "BBA"),
        (r"\bmaster(?:'s)?\s+of\s+business\s+administration\b", "MBA"),
        (r"\bBBA\b", "BBA"), (r"\bMBA\b", "MBA"),
        (r"\bBBS\b", "BBS"), (r"\bMBS\b", "MBS"),
        (r"\bBachelor(?:'s)?(?:\s+degree)?\b", "Bachelor's"),
        (r"\bMaster(?:'s)?(?:\s+degree)?\b", "Master's"),
        (r"\bHonou?rs?\b", "Honours"),
    ]
    for pattern, label in degree_patterns:
        if re.search(pattern, search_blob, flags=re.I) and label not in found:
            found.append(label)
    if found:
        if "BBA" in found and "MBA" in found:
            return "BBA/MBA"
        return "/".join(found[:3])
    if re.search(r"\b(bachelor|master|degree|honours|honors|graduate)\b", search_blob, flags=re.I):
        return "Degree required"
    return ""


def compact_salary(value):
    blob = _clean_one_line(value)
    if not blob:
        return ""
    blob = re.sub(r"\s*\(monthly\)\s*", "/month", blob, flags=re.I)
    blob = re.sub(r"\s+", " ", blob)
    return trim_source_text(blob, 90)


def compact_location(value):
    blob = _clean_one_line(value)
    if not blob:
        return ""
    # A pure logo ALT value is never a location.
    if re.match(r"^(?:image\s*:?[ \t]*)?logo\s+of\b", blob, flags=re.I):
        return ""
    # Logo/image ALT text from Bdjobs can be attached to the location string.
    blob = re.sub(r"\s+(?:image\s*:?[ \t]*)?logo\s+of\b.*$", "", blob, flags=re.I)
    blob = re.sub(r"\s+(?:image\s*:?[ \t]*)?(?:matching_lock_en(?:_res)?\.(?:webp|svg|png|gif))\b.*$", "", blob, flags=re.I)
    blob = re.sub(r"^(?:image\s*:?[ \t]*)?logo\s+of\s+", "", blob, flags=re.I)
    return trim_source_text(_clean_one_line(blob), 80) if blob else ""


def compact_vacancy(value):
    blob = _clean_one_line(value)
    if not blob:
        return ""
    # Require vacancy-specific language instead of blindly taking the first number
    # from a paragraph. This prevents age/salary/experience numbers from becoming vacancy.
    patterns = [
        r"(?:vacanc(?:y|ies)|no\.?\s*of\s*vacanc(?:y|ies)|number\s+of\s+vacanc(?:y|ies)|positions?)\s*[:=-]?\s*(\d{1,5})\b",
        r"\btotal\s+(\d{1,5})\s+(?:people|persons|vacanc(?:y|ies)|positions?)\b",
        r"মোট\s+(\d{1,5})\s*(?:জন|টি|পদ)",
        r"পদসংখ্যা\s*[:=-]?\s*(\d{1,5})",
        r"পদ\s+সংখ্যা\s*[:=-]?\s*(\d{1,5})",
    ]
    for pattern in patterns:
        m=re.search(pattern, blob, flags=re.I)
        if m:
            return m.group(1)
    # A plain numeric value is accepted only when the field itself is already
    # a clean source label value.
    if re.fullmatch(r"\d{1,5}", blob):
        return blob
    return ""


def compact_age(value):
    """Normalize an explicitly age-labelled source value.

    Plausibility bounds prevent experience values such as "At least 3 years" from
    becoming an age field when an upstream parser accidentally passes the wrong text.
    """
    blob=_clean_one_line(value).translate(BENGALI_DIGIT_MAP)
    blob=blob.replace("থেকে","to").replace("বছর","years").replace("বছরের","years").replace("ন্যূনতম","minimum")
    if not blob:
        return ""
    def valid_age(n):
        return 16 <= int(n) <= 80
    m=re.search(r"\b(\d{1,2})\s*(?:to|[-–])\s*(\d{1,2})\s*(?:years?|year)?\b",blob,flags=re.I)
    if m and valid_age(m.group(1)) and valid_age(m.group(2)) and int(m.group(1)) <= int(m.group(2)):
        return f"{m.group(1)}-{m.group(2)} Years"
    m=re.search(r"\b(?:at\s+least|minimum(?:\s+age)?|not\s+less\s+than|minimum\s+of)\s*:?\s*(\d{1,2})\s*(?:years?|year)\b",blob,flags=re.I)
    if m and valid_age(m.group(1)):
        prefix = "At least" if re.search(r"at\s+least|minimum|not\s+less", blob, flags=re.I) else ""
        return f"{prefix} {m.group(1)} Years".strip()
    m=re.search(r"\b(?:at\s+most|maximum(?:\s+age)?|not\s+more\s+than)\s*:?\s*(\d{1,2})\s*(?:years?|year)\b",blob,flags=re.I)
    if m and valid_age(m.group(1)):
        return f"At most {m.group(1)} Years"
    m=re.search(r"\b(\d{1,2})\s*(?:\+|plus|years?|year)\b",blob,flags=re.I)
    if m and valid_age(m.group(1)):
        return f"{m.group(1)} Years"
    return ""


def compact_employment(value):
    blob = _clean_one_line(value).lower()
    if any(x in blob for x in ("সরকারি", "সরকারী")):
        return "Government Job"
    if not blob:
        return ""
    if "full time" in blob or "full-time" in blob:
        return "Full Time"
    if "part time" in blob or "part-time" in blob:
        return "Part Time"
    if "intern" in blob:
        return "Internship"
    if "contract" in blob:
        return "Contract"
    if "freelance" in blob:
        return "Freelance"
    if "ফুল টাইম" in blob or "ফুলটাইম" in blob:
        return "Full Time"
    if "পার্ট টাইম" in blob or "পার্টটাইম" in blob:
        return "Part Time"
    if "চুক্তিভিত্তিক" in blob:
        return "Contract"
    return ""


def compact_workplace(value):
    blob = _clean_one_line(value).lower()
    if not blob:
        return ""
    if "work from home" in blob or "remote" in blob:
        return "Remote"
    if "hybrid" in blob:
        return "Hybrid"
    if "office" in blob or "on-site" in blob or "onsite" in blob or "অফিসে" in blob:
        return "On-site"
    return ""


def compact_application(value, apply_url=""):
    blob = _clean_one_line(value).lower()
    if "অনলাইন" in blob:
        return "Online"
    if "ইমেইল" in blob or "ই-মেইল" in blob or "ইমেল" in blob:
        return "Email"
    if "walk-in" in blob or "walk in" in blob:
        if any(x in blob for x in ("online", "career website", "website")):
            return "Online + Walk-in"
        return "Walk-in"
    if any(x in blob for x in ("online", "career website", "website", "apply")) or apply_url:
        return "Online"
    if "email" in blob or "e-mail" in blob:
        return "Email"
    return ""


def compact_selection(value):
    blob = _clean_one_line(value).lower()
    if not blob:
        return ""
    written = "written" in blob or "exam" in blob or "test" in blob
    viva = "viva" in blob or "interview" in blob
    if written and viva:
        return "Written + Viva"
    if written:
        return "Written"
    if viva:
        return "Viva/Interview"
    return ""


def _regex_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.M)
        if match:
            return safe_text(match.group(1))
    return ""


BENGALI_DIGIT_MAP = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
BENGALI_MONTHS = {
    "জানুয়ারি": 1, "জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারি": 2,
    "মার্চ": 3, "এপ্রিল": 4, "মে": 5, "জুন": 6, "জুলাই": 7,
    "আগস্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12,
}
EN_MONTHS = {name.lower(): i for i, name in enumerate((
    "January February March April May June July August September October November December"
).split(), 1)}
EN_MONTHS.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
})

def normalize_date_text(value):
    raw = safe_text(value)
    if not raw:
        return ""
    raw = raw.translate(BENGALI_DIGIT_MAP)
    raw = re.sub(r"(\d{1,2})[ইঈয়]?\b", r"\1", raw)
    raw = re.sub(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", "", raw, flags=re.I)
    raw = re.sub(r"(?:তারিখ|সকাল|বিকাল|সন্ধ্যা|রোজ|বার)\b", " ", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip(" ,:-")
    parsed = parse_datetime(raw)
    if parsed:
        return parsed.date().isoformat()

    # English month/date forms, including ordinal suffixes.
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?[\s/-]+([A-Za-z]+)[,\s/-]+(\d{4})\b", raw, re.I)
    if not m:
        m = re.search(r"\b([A-Za-z]+)[\s/-]+(\d{1,2})(?:st|nd|rd|th)?[,\s/-]+(\d{4})\b", raw, re.I)
    if m:
        a,b,c = m.groups()
        if a.isalpha():
            month = EN_MONTHS.get(a.lower())
            day, year = int(b), int(c)
        else:
            day, month_name, year = int(a), b, int(c)
            month = EN_MONTHS.get(month_name.lower())
        if month:
            try:
                return datetime(int(year), int(month), int(day), tzinfo=BD_TZ).date().isoformat()
            except ValueError:
                pass

    # Bengali month-name forms.
    m = re.search(r"(?<!\d)(\d{1,2})\s+([\u0980-\u09ff]+)\s+(\d{4})(?!\d)", raw)
    if m:
        day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = BENGALI_MONTHS.get(month_name)
        if month:
            try:
                return datetime(year, month, day, tzinfo=BD_TZ).date().isoformat()
            except ValueError:
                pass

    for pattern in (r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"):
        m = re.search(pattern, raw)
        if m:
            parts = list(map(int, m.groups()))
            try:
                if len(str(parts[0])) == 4:
                    return datetime(parts[0], parts[1], parts[2], tzinfo=BD_TZ).date().isoformat()
                return datetime(parts[2], parts[1], parts[0], tzinfo=BD_TZ).date().isoformat()
            except ValueError:
                pass
    return ""


def format_date_display(value):
    """Display every publishable date consistently as DD-MM-YYYY."""
    raw = safe_text(value)
    if not raw:
        return ""
    iso = normalize_date_text(raw)
    if iso:
        try:
            return datetime.fromisoformat(iso).strftime("%d-%m-%Y")
        except Exception:
            pass
    return raw


def extract_date_tokens(value):
    """Extract up to two date tokens from a mixed English/Bengali date range."""
    raw = safe_text(value).translate(BENGALI_DIGIT_MAP)
    patterns = [
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b",
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}\b",
        r"\b[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?[,\s]+(?:19|20)\d{2}\b",
        r"(?<!\d)\d{1,2}\s+[\u0980-\u09ff]+\s+\d{4}(?!\d)",
    ]
    matches=[]
    for pattern in patterns:
        matches.extend(re.findall(pattern, raw, flags=re.I))
    ordered=[]; seen=set()
    for token in sorted(matches, key=lambda x: raw.find(x)):
        key=token.strip().lower()
        normalized=normalize_date_text(token)
        if key in seen or not normalized:
            continue
        seen.add(key); ordered.append(normalized)
        if len(ordered)>=2:
            break
    return ordered


def _format_mixed_dates(value):
    raw=safe_text(value)
    if not raw:
        return ""
    pattern=(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|"
             r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b|"
             r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}\b|"
             r"\b[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?[,\s]+(?:19|20)\d{2}\b|"
             r"(?<!\d)\d{1,2}\s+[\u0980-\u09ff]+\s+\d{4}(?!\d)")
    for token in sorted(set(re.findall(pattern, raw, flags=re.I)), key=len, reverse=True):
        formatted=format_date_display(token)
        if formatted != token:
            raw=raw.replace(token, formatted)
    return raw


def smart_title_case(value):
    """Readable display casing while preserving acronyms and common abbreviations."""
    text=_clean_one_line(value)
    if not text or not any(ch.isalpha() for ch in text):
        return text
    if "@" in text or "://" in text:
        return text
    stop={"and","or","of","in","to","for","the","a","an","at","on","by","from","with"}
    suffixes={"ltd":"Ltd.","ltd.":"Ltd.","limited":"Limited","llc":"LLC","plc":"PLC","inc":"Inc.","inc.":"Inc.","co":"Co.","co.":"Co.","phd":"PhD"}
    parts=re.split(r"(\s+|/|-|–|—|\(|\)|,|:)",text); out=[]; word_index=0
    for part in parts:
        if not part or re.fullmatch(r"\s+|/|-|–|—|\(|\)|,|:",part):
            out.append(part); continue
        low=part.lower()
        if low in suffixes:
            out.append(suffixes[low])
        elif part.upper()==part and len(part)<=8 and any(ch.isalpha() for ch in part):
            out.append(part)
        elif part.islower() or part.isupper():
            out.append(low if low in stop and word_index>0 else part[:1].upper()+part[1:].lower())
        else:
            out.append(part)
        word_index += 1
    return "".join(out).strip()


def format_table_value(label,value):
    raw=_clean_one_line(value)
    if not raw:
        return "—"
    if label in {"Deadline","Posted","Application Start","Application End"}:
        return format_date_display(raw)
    if label=="Age":
        return compact_age(raw) or smart_title_case(raw)
    if label=="Salary":
        cleaned=re.sub(r"\btk\b\.?","Tk.",raw,flags=re.I)
        cleaned=re.sub(r"\b(টাকা|taka)\b","Tk.",cleaned,flags=re.I)
        if cleaned.lower()=="negotiable":
            return "Negotiable"
        return re.sub(r"\s+"," ",cleaned)[:48].rstrip()
    if label=="Vacancy":
        return raw
    return smart_title_case(_format_mixed_dates(raw))[:52].rstrip()


def _contains_bengali(value):
    return bool(re.search(r"[\u0980-\u09ff]",safe_text(value)))


def infer_government_organization(title):
    text=_clean_one_line(title)
    if not text:
        return ""
    lower=text.lower()
    markers=("office","commission","commissioner","directorate","department","ministry","authority","tax zone","division","board","corporation","অফিস","কার্যালয়","কার্যালয়","কমিশন","অধিদপ্তর","বিভাগ","মন্ত্রণালয়","মন্ত্রণালয়","কর্তৃপক্ষ","কর অঞ্চল","বোর্ড")
    if not any(m in lower for m in markers):
        return ""
    text=re.sub(r"(?i)\b(?:job|recruitment|employment|appointment)\s+(?:circular|notice)\b.*$","",text)
    text=re.sub(r"(?i)\b(?:19|20)\d{2}\b","",text)
    text=re.sub(r"\b202[0-9]\b","",text)
    translated=fallback_government_translate(text)
    translated=re.sub(r"(?i)\b(?:recruitment|job|employment|appointment)\s+(?:circular|notice)\b.*$","",translated)
    return _clean_one_line(translated)


BANGLA_PHRASE_MAP={
    "নিয়োগ বিজ্ঞপ্তি":"Recruitment Circular","নিয়োগ বিজ্ঞপ্তি":"Recruitment Circular","নিয়োগ":"Recruitment","নিয়োগ":"Recruitment",
    "সরকারি চাকরি":"Government Job","সরকারী চাকরি":"Government Job","বিভাগীয় কমিশনার":"Divisional Commissioner","বিভাগীয় কমিশনার":"Divisional Commissioner",
    "কার্যালয়":"Office","কার্যালয়":"Office","অফিস":"Office","অধিদপ্তর":"Directorate","পরিদপ্তর":"Directorate",
    "মন্ত্রণালয়":"Ministry","মন্ত্রণালয়":"Ministry","বিভাগ":"Department","দপ্তর":"Department","কর্তৃপক্ষ":"Authority","কমিশন":"Commission","বোর্ড":"Board",
    "কর অঞ্চল":"Tax Zone","ঢাকা":"Dhaka","ময়মনসিংহ":"Mymensingh","ময়মনসিংহ":"Mymensingh","চট্টগ্রাম":"Chattogram","খুলনা":"Khulna","রাজশাহী":"Rajshahi","সিলেট":"Sylhet","বরিশাল":"Barishal","রংপুর":"Rangpur","কক্সবাজার":"Cox's Bazar",
    "আবেদন":"Application","আবেদনের":"Application","অনলাইন":"Online","লিখিত":"Written","মৌখিক":"Viva","পরীক্ষা":"Exam",
    "বেতন":"Salary","বয়সসীমা":"Age Limit","বয়সসীমা":"Age Limit","অভিজ্ঞতা":"Experience","শিক্ষাগত যোগ্যতা":"Educational Qualification",
    "পদ সংখ্যা":"Vacancy","পদসংখ্যা":"Vacancy","পদ":"Post","সংখ্যা":"Number","জন":"People","জনকে":"People","টি":"","প্রকাশিত":"Published","শেষ তারিখ":"Deadline","চাকরির ধরন":"Employment Type",
    "চাকুরি স্থান":"Job Location","চাকরি স্থান":"Job Location", "আবেদন শুরুর সময়":"Application Start Time","আবেদন শুরুর সময়":"Application Start Time",
    "আবেদনের শেষ সময়":"Application End Time","আবেদনের শেষ সময়":"Application End Time",
    "থেকে":"to","বছরের":"Years","বছর":"Years","এসএসসি":"SSC","এইচএসসি":"HSC",
    "স্নাতকোত্তর":"Master's","স্নাতক":"Bachelor's","ডিপ্লোমা":"Diploma","উচ্চ মাধ্যমিক":"HSC",
}
BN_CHAR_MAP=str.maketrans({"অ":"o","আ":"a","ই":"i","ঈ":"i","উ":"u","ঊ":"u","ঋ":"ri","এ":"e","ঐ":"oi","ও":"o","ঔ":"ou","ক":"k","খ":"kh","গ":"g","ঘ":"gh","ঙ":"ng","চ":"ch","ছ":"chh","জ":"j","ঝ":"jh","ঞ":"n","ট":"t","ঠ":"th","ড":"d","ঢ":"dh","ণ":"n","ত":"t","থ":"th","দ":"d","ধ":"dh","ন":"n","প":"p","ফ":"ph","ব":"b","ভ":"bh","ম":"m","য":"y","র":"r","ল":"l","শ":"sh","ষ":"sh","স":"s","হ":"h","ড়":"r","ঢ়":"rh","য়":"y","ৎ":"t","ং":"ng","ঃ":"h","ঁ":"n","্":"","া":"a","ি":"i","ী":"i","ু":"u","ূ":"u","ৃ":"ri","ে":"e","ৈ":"oi","ো":"o","ৌ":"ou"})

def fallback_government_translate(value):
    text=safe_text(value)
    if not text or not _contains_bengali(text):
        return smart_title_case(text)
    out=text
    for src,dst in sorted(BANGLA_PHRASE_MAP.items(),key=lambda x:len(x[0]),reverse=True):
        out=out.replace(src,dst)
    if _contains_bengali(out):
        out=out.translate(BN_CHAR_MAP)
    out=out.translate(BENGALI_DIGIT_MAP)
    out=re.sub(r"\s+"," ",out)
    return smart_title_case(out)

GOV_TRANSLATE_SCHEMA={"type":"object","properties":{"results":{"type":"array","items":{"type":"object","properties":{k:{"type":"string"} for k in ("id","title","company","location","salary","experience","education","vacancy","employment_type","workplace","age","application_method","selection_process","category")},"required":["id","title","company","location","salary","experience","education","vacancy","employment_type","workplace","age","application_method","selection_process","category"],"additionalProperties":False}}},"required":["results"],"additionalProperties":False}

def translate_government_jobs(jobs):
    """Translate government fields once, with local fallback so no Bangla is published."""
    gov=[j for j in jobs if j.get("is_government")]
    if not gov:
        return jobs
    source_snapshots=[]
    for job in gov:
        # Preserve the authoritative source identity. Government jobs are now
        # sourced directly from Teletalk, so translation must never relabel them.
        job["source"] = job.get("source") or "Teletalk"
        if not job.get("company"):
            job["company"]=infer_government_organization(job.get("title","")) or "Government Organization"
        source_fields={k:safe_text(job.get(k,"")) for k in ("title","company","location","salary","experience","education","employment_type","workplace","age","vacancy","application_method","selection_process","category")}
        if any(_contains_bengali(v) for v in source_fields.values()):
            source_snapshots.append((job,source_fields))
        for key,value in source_fields.items():
            if value:
                job[key]=fallback_government_translate(value)
        # Re-run field compaction after translation/fallback for stable display forms.
        job["age"]=compact_age(job.get("age")) or job.get("age","")
        job["application_method"]=compact_application(job.get("application_method"),job.get("apply_url","")) or job.get("application_method","")
        job["employment_type"]=compact_employment(job.get("employment_type")) or job.get("employment_type","")
    client=get_cerebras()
    if not client or not source_snapshots:
        return jobs
    payload=[]
    for idx,(job,source_fields) in enumerate(source_snapshots,1):
        payload.append("\n".join([
            f"ID: {idx}",
            "Translate these government-job fields from Bangla to concise natural English. Preserve names, numbers and facts. Do not invent.",
            *[f"{k}: {source_fields.get(k,'')}" for k in source_fields],
        ]))
    try:
        response=client.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role":"system","content":"Return only the supplied fields translated into natural English suitable for a Telegram job post."},
                {"role":"user","content":"\n\n".join(payload)},
            ],
            response_format={"type":"json_schema","json_schema":{"name":"government_job_translation","strict":True,"schema":GOV_TRANSLATE_SCHEMA}},
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=1800,
        )
        rows=json.loads(safe_text(response.choices[0].message.content)).get("results",[])
        for row in rows:
            try: idx=int(row.get("id"))
            except Exception: continue
            if 1<=idx<=len(source_snapshots):
                job,_=source_snapshots[idx-1]
                for key in ("title","company","location","salary","experience","education","employment_type","workplace","age","vacancy","application_method","selection_process","category"):
                    value=safe_text(row.get(key))
                    if value:
                        job[key]=smart_title_case(value)
    except Exception as exc:
        logger.warning("Government translation AI unavailable; local English fallback kept: %s",exc)
    return jobs


def _html_h1(page_html):
    if not page_html:
        return ""
    try:
        soup = BeautifulSoup(page_html, "html.parser")
        h1 = soup.find("h1")
        return _clean_one_line(h1.get_text(" ", strip=True)) if h1 else ""
    except Exception:
        return ""


def _summary_lines(text):
    """Return the most likely source Job Summary block, not an arbitrary page section."""
    lines = [re.sub(r"\s+", " ", x).strip() for x in safe_text(text).splitlines() if safe_text(x)]
    start_indexes = [i for i, line in enumerate(lines)
                     if line.lower() in {"job summary", "চাকরির সারসংক্ষেপ"}]
    if not start_indexes:
        return lines

    summary_markers = {
        "company name", "company", "organization name", "employer",
        "job location", "location", "salary", "vacancy", "age",
        "job type", "employment status", "published", "deadline",
        "প্রতিষ্ঠানের নাম", "চাকুরি স্থান", "চাকরি স্থান", "বেতন",
        "পদ সংখ্যা", "পদসংখ্যা", "বয়সসীমা", "বয়সসীমা", "প্রকাশিত", "শেষ তারিখ",
        "চাকরির ধরন",
    }
    stop_terms = {
        "for candidates", "চাকরির খবর ইউটিউবে", "copyright",
        "apply by bdjobs",
    }

    candidates = []
    for start_idx in start_indexes:
        out = []
        for line in lines[start_idx + 1:start_idx + 100]:
            if line.lower() in stop_terms:
                break
            out.append(line)
        normalized = {_normalized_line_label(x) for x in out}
        score = sum(1 for marker in summary_markers if marker in normalized)
        candidates.append((score, len(out), start_idx, out))

    # Prefer the block with the strongest concentration of actual summary labels.
    # Break ties toward the later block, matching current source page structure.
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return candidates[0][3] if candidates else lines


def _summary_value(text, labels):
    """Extract a summary field while preserving the source value's casing."""
    wanted = {re.sub(r"\s+", " ", safe_text(x).lower().rstrip(":-")).strip() for x in labels}
    lines = _summary_lines(text)
    for i, line in enumerate(lines):
        raw_line = _clean_one_line(line)
        norm = _normalized_line_label(raw_line)
        if norm in wanted and i + 1 < len(lines):
            return _clean_one_line(lines[i + 1])
        for label in sorted(wanted, key=len, reverse=True):
            match = re.match(rf"^{re.escape(label)}\s*[:\-]\s*(.*?)$", raw_line, flags=re.I)
            if match:
                return _clean_one_line(match.group(1))
    return ""


def _meta_or_time_published(page_html):
    if not page_html:
        return ""
    try:
        soup=BeautifulSoup(page_html,"html.parser")
        selectors=[
            ("meta", {"property":"article:published_time"}),
            ("meta", {"property":"article:modified_time"}),
            ("meta", {"name":"date"}),
        ]
        for tag_name, attrs in selectors:
            tag=soup.find(tag_name, attrs=attrs)
            if tag and safe_text(tag.get("content")):
                return safe_text(tag.get("content"))
        for tag in soup.find_all("time"):
            dt=safe_text(tag.get("datetime"))
            if dt:
                return dt
    except Exception:
        pass
    return ""



def _bdjobs_detail_summary_fields(text, expected_title=""):
    """Parse high-value fields from the current/legacy Bdjobs detail summary.

    The current page exposes a compact summary such as Application Deadline, Vacancy,
    Age, Location, Salary, Experience, Published, followed by Requirements, Workplace,
    and Employment Status. Parse by explicit labels so fields cannot borrow neighboring
    values (especially Age <- Experience).
    """
    raw = safe_text(text)
    if not raw:
        return {}
    lines = [_clean_one_line(x) for x in raw.splitlines() if _clean_one_line(x)]
    label_aliases = {
        "company": ("Company Name", "Company", "Organization Name", "Employer", "প্রতিষ্ঠানের নাম", "অফিসের নাম", "দপ্তরের নাম", "মন্ত্রণালয়ের নাম", "মন্ত্রণালয়ের নাম", "অধিদপ্তরের নাম", "কার্যালয়ের নাম", "কার্যালয়ের নাম"),
        "deadline": ("Application Deadline", "Deadline", "শেষ তারিখ"),
        "vacancy": ("Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "খালি পদ", "পদসংখ্যা", "পদ সংখ্যা"),
        "age": ("Age", "Age Limit", "Age Requirements", "বয়স", "বয়স", "বয়সসীমা", "বয়সসীমা"),
        "location": ("Location", "Job Location", "Job Location(s)", "Work Location", "চাকরি স্থান", "চাকুরি স্থান", "কর্মস্থল", "কর্মস্হল"),
        "salary": ("Salary", "Salary Range", "Compensation", "বেতন"),
        "experience": ("Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"),
        "posted_date": ("Published", "Posted", "Date Posted", "Publication Date", "প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ"),
        "employment_type": ("Employment Status", "Employment Type", "Job Type", "চাকরির ধরন"),
        "workplace": ("Workplace", "Job Work Place", "Work Place", "কর্মক্ষেত্র"),
        "application_method": ("Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply", "আবেদন প্রক্রিয়া", "আবেদন প্রক্রিয়া", "আবেদনের নিয়ম", "আবেদনের নিয়ম"),
    }
    normalized_aliases = {
        key: {re.sub(r"\s+", " ", safe_text(v).lower().rstrip(":- ")).strip() for v in aliases}
        for key, aliases in label_aliases.items()
    }
    normalized_known = set().union(*normalized_aliases.values())
    company_labels = normalized_aliases["company"]
    result = {}

    for i, line in enumerate(lines):
        norm = _normalized_line_label(line)
        for key, aliases in normalized_aliases.items():
            value = ""
            if norm in aliases:
                # A label on its own line: the next non-label line is the value.
                if i + 1 < len(lines):
                    nxt = lines[i + 1]
                    if _normalized_line_label(nxt) not in normalized_known:
                        value = nxt
            else:
                for alias in sorted(aliases, key=len, reverse=True):
                    m = re.match(rf"^{re.escape(alias)}\s*[:：-]\s*(.*?)$", line, flags=re.I)
                    if m:
                        value = _clean_one_line(m.group(1))
                        break
            if value and key not in {"company"}:
                # Explicit label semantics win. A value like `--` is cleaned later.
                result_value = _clean_one_line(value)
                # Store only the first explicit occurrence for each field.
                if result_value and key not in result:
                    result[key] = result_value
            elif value and key == "company" and key not in result:
                result[key] = _clean_one_line(value)

    # Flattened fallback. Use source labels followed by a different known label as a boundary.
    flat = re.sub(r"\s+", " ", raw)
    for key, aliases in normalized_aliases.items():
        if result.get(key):
            continue
        label_pattern = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        stop_labels = normalized_known - aliases
        stop = "|".join(re.escape(a) for a in sorted(stop_labels, key=len, reverse=True)) or r"(?!)"
        pattern = rf"(?:{label_pattern})\s*[:：-]\s*(.+?)(?=\s+(?:{stop})\s*[:：-]|$)"
        m = re.search(pattern, flat, flags=re.I)
        if m:
            result[key] = _clean_one_line(m.group(1))

    # Current detail pages often expose company on the line immediately before the expected title.
    # Some localized templates place a company label/value immediately after the title.
    if not result.get("company") and lines:
        expected = _clean_one_line(expected_title)
        title_idx = -1
        if expected:
            for i, line in enumerate(lines[:30]):
                if normalize_title(line) == normalize_title(expected):
                    title_idx = i
                    break
        candidates=[]
        if title_idx > 0:
            prev=lines[title_idx-1]
            if _normalized_line_label(prev) not in company_labels:
                candidates.append(prev)
        if title_idx >= 0 and title_idx + 1 < len(lines):
            nxt=lines[title_idx+1]
            if _normalized_line_label(nxt) in company_labels and title_idx + 2 < len(lines):
                candidates.insert(0, lines[title_idx+2])
            elif _normalized_line_label(nxt) not in normalized_known:
                candidates.append(nxt)
        if not candidates and len(lines)>=2:
            candidates.append(lines[0])
        for candidate in candidates:
            candidate=_clean_one_line(candidate)
            if (candidate and normalize_title(candidate) != normalize_title(expected or "__none__")
                and not re.search(r"\.(?:gif|png|jpe?g|webp|svg)\b", candidate, flags=re.I)
                and not any(x in candidate.lower() for x in ("job list", "create", "sign in", "unlock", "application deadline"))):
                result["company"]=candidate
                break
    return result

def extract_job_fields(text, page_html, source_url, discovery_item):
    text = safe_text(text)
    jsonld = _jobposting_jsonld(page_html) if page_html else {}
    is_gov = bool(discovery_item.get("is_government")) or "/gov-job/" in urlparse(source_url).path.lower()
    bd_summary = _bdjobs_detail_summary_fields(text, discovery_item.get("title", "")) if not is_gov else {}

    title = safe_text(jsonld.get("title")) or _html_h1(page_html) or _label_value(text, ["Title", "Job Title", "Position", "Post Name"])
    company = ""
    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict):
        company = safe_text(hiring.get("name"))
    company = (company or bd_summary.get("company") or _summary_value(text, [
        "প্রতিষ্ঠানের নাম", "অফিসের নাম", "দপ্তরের নাম", "মন্ত্রণালয়ের নাম", "মন্ত্রণালয়ের নাম",
        "অধিদপ্তরের নাম", "কার্যালয়ের নাম", "কার্যালয়ের নাম", "Company Name", "Company",
        "Organization Name", "Employer", "Department", "Ministry", "Office", "Directorate",
        "Authority", "Commission", "Board"
    ]) or _label_value(text, [
        "Company Name", "Company", "Organization Name", "Employer", "Department", "Ministry",
        "Office", "Directorate", "Authority", "Commission", "Board", "প্রতিষ্ঠানের নাম",
        "অফিসের নাম", "দপ্তরের নাম", "মন্ত্রণালয়ের নাম", "মন্ত্রণালয়ের নাম", "অধিদপ্তরের নাম",
        "কার্যালয়ের নাম", "কার্যালয়ের নাম"
    ]))

    location = bd_summary.get("location") or _summary_value(text, ["চাকুরি স্থান", "চাকরি স্থান", "কর্মস্থল", "কর্মস্হল", "কর্মক্ষেত্র", "Job Location", "Location", "Job Location(s)", "Work Location"]) or _label_value(text, ["Job Location", "Location", "Job Location(s)", "Work Location", "চাকুরি স্থান", "চাকরি স্থান", "কর্মস্থল", "কর্মস্হল", "কর্মক্ষেত্র"])
    salary = bd_summary.get("salary") or _summary_value(text, ["বেতন", "Salary", "Salary Range", "Minimum Salary", "Compensation"]) or _label_value(text, ["Salary", "Salary Range", "Minimum Salary", "Compensation", "বেতন"])
    # Age is intentionally extracted only from explicit age-labelled summary content.
    # Never use a generic fallback that can reinterpret the Experience value as Age.
    age = bd_summary.get("age") or _summary_value(text, ["বয়স", "বয়স", "বয়সসীমা", "বয়সসীমা", "Age", "Age Limit", "Age Requirements"])
    employment = bd_summary.get("employment_type") or _summary_value(text, ["চাকরির ধরন", "Employment Status", "Job Type", "Employment Type"]) or _label_value(text, ["Employment Status", "Job Type", "Employment Type", "চাকরির ধরন"])
    published = bd_summary.get("posted_date") or _summary_value(text, ["প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ", "Published", "Posted", "Date Posted", "Publication Date"]) or _label_value(text, ["Published", "Posted", "Date Posted", "Publication Date", "প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ"])
    deadline = bd_summary.get("deadline") or _summary_value(text, ["শেষ তারিখ", "Application Deadline", "Deadline", "Last Date", "Apply Before"]) or _label_value(text, ["Application Deadline", "Deadline", "Last Date", "Apply Before", "শেষ তারিখ"])

    # Detail-section fields. Use the same source text as a fallback because some
    # source templates expose the value outside the Job Summary card.
    experience = bd_summary.get("experience") or _summary_value(text, ["Experience", "অভিজ্ঞতা"]) or _label_value(text, ["Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"])
    education = bd_summary.get("education") or _summary_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"]) or _label_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"])
    vacancy = bd_summary.get("vacancy") or _summary_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা", "খালি পদ"]) or _label_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা", "খালি পদ"])
    if not vacancy and isinstance(jsonld, dict) and jsonld.get("totalJobOpenings") is not None:
        vacancy = safe_text(jsonld.get("totalJobOpenings"))
    workplace = bd_summary.get("workplace") or _summary_value(text, ["Job Work Place", "Workplace", "Work Place", "কর্মক্ষেত্র"]) or _label_value(text, ["Job Work Place", "Workplace", "Work Place", "কর্মক্ষেত্র"])
    category = _label_value(text, ["Category", "Job Category"])
    application_method = bd_summary.get("application_method") or _label_value(text, ["Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply", "আবেদন প্রক্রিয়া", "আবেদন প্রক্রিয়া", "আবেদনের নিয়ম", "আবেদনের নিয়ম"])
    selection_process = _label_value(text, ["Selection Process", "Recruitment Process", "Selection Procedure", "Hiring Process", "Interview Process"])
    application_period = _label_value(text, ["Application Period", "Application Date", "Interview Date", "Walk-in Date"])

    # Government circulars frequently expose application start/end dates in prose.
    start_date = _regex_value(text, [
        r"আবেদন শুরুর সময়\s*[:：-]?\s*([^\n]+)",
        r"আবেদন শুরুের সময়\s*[:：-]?\s*([^\n]+)",
        r"আবেদন শুরুের সময়\s*[:：-]?\s*([^\n]+)",
        r"আবেদন শুরুর সময়\s*[:：-]?\s*([^\n]+)",
        r"Application Start(?:ing)?(?: Date)?\s*[:：-]?\s*([^\n]+)",
    ])
    end_date = _regex_value(text, [
        r"আবেদনের শেষ সময়\s*[:：-]?\s*([^\n]+)",
        r"আবেদনের শেষ সময়\s*[:：-]?\s*([^\n]+)",
        r"Application End(?:ing)?(?: Date)?\s*[:：-]?\s*([^\n]+)",
    ])
    start_iso = normalize_date_text(start_date)
    end_iso = normalize_date_text(end_date)
    if not (start_iso and end_iso) and application_period:
        dates=extract_date_tokens(application_period)
        if len(dates)>=2:
            start_iso,end_iso=dates[:2]
    if end_iso and not deadline:
        deadline = end_iso
    if start_iso:
        application_period = f"{start_iso} to {end_iso}" if end_iso else start_iso

    if jsonld:
        if not published and jsonld.get("datePosted"):
            published = safe_text(jsonld.get("datePosted"))
        if not deadline and jsonld.get("validThrough"):
            deadline = safe_text(jsonld.get("validThrough"))
        if not employment and jsonld.get("employmentType"):
            employment = ", ".join(jsonld.get("employmentType")) if isinstance(jsonld.get("employmentType"), list) else safe_text(jsonld.get("employmentType"))
        if not salary and isinstance(jsonld.get("baseSalary"), dict):
            base = jsonld["baseSalary"]
            value = base.get("value") if isinstance(base.get("value"), dict) else base.get("value")
            currency = safe_text(base.get("currency"))
            if value:
                salary = f"{currency} {value}".strip()
        jl=jsonld.get("jobLocation")
        if not location and isinstance(jl, dict):
            addr=jl.get("address")
            if isinstance(addr, dict):
                location=", ".join(safe_text(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")) if safe_text(x))
            elif isinstance(addr,str):
                location=addr

    if not published:
        published = _meta_or_time_published(page_html)
    if not deadline and discovery_item.get("listing_deadline"):
        deadline = discovery_item.get("listing_deadline")

    title = clean_job_title(title, discovery_item.get("title", ""))
    listing_fields = discovery_item.get("listing_fields") or {}
    company = clean_source_field(company) or clean_source_field(listing_fields.get("company")) or clean_source_field(discovery_item.get("company", ""))
    location = compact_location(clean_source_field(location))
    salary = compact_salary(clean_source_field(salary))
    experience = compact_experience(clean_source_field(experience), text)
    education = compact_education(clean_source_field(education), text)
    vacancy = compact_vacancy(clean_source_field(vacancy))
    employment = compact_employment(clean_source_field(employment))
    workplace = compact_workplace(clean_source_field(workplace))
    age = compact_age(clean_source_field(age))
    application_method = compact_application(clean_source_field(application_method))
    selection_process = compact_selection(clean_source_field(selection_process))
    # Government summaries often state the total number of people recruited in prose.
    if not vacancy and is_gov:
        vacancy = compact_vacancy(_first_match(text, [
            r"মোট\s+(\d{1,5})\s*জন(?:কে)?\s*নিয়োগ",
            r"total\s+(\d{1,5})\s+(?:people|persons|posts|positions)",
        ]))
    application_period = _clean_one_line(application_period)

    published_iso = normalize_date_text(published)
    deadline_iso = normalize_date_text(deadline)
    period_dates=extract_date_tokens(application_period)
    application_start=period_dates[0] if period_dates else (start_iso or "")
    application_end=period_dates[1] if len(period_dates)>1 else (end_iso or "")
    if is_gov and not company:
        company=infer_government_organization(title) or "Government Organization"

    return {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
        "experience": experience,
        "education": education,
        "vacancy": vacancy,
        "employment_type": employment,
        "workplace": workplace,
        "age": age,
        "category": _clean_one_line(category),
        "application_method": application_method,
        "selection_process": selection_process,
        "application_period": application_period,
        "application_start": application_start,
        "application_end": application_end,
        "posted_date": published_iso,
        "deadline": deadline_iso,
        "source": source_name(source_url),
        "source_url": source_url,
        "url": source_url,
        "discovery": discovery_item.get("discovery", ""),
        "source_category_name": discovery_item.get("category_name", ""),
        "is_government": is_gov,
    }


def request_safe_url(url):
    raw = safe_text(url)
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=%")
    return parsed._replace(path=path).geturl()

def _extract_apply_url_from_text(text, page_url):
    candidates = []
    for value in re.findall(r"https?://[^\s<>\]\[\)\"']+", safe_text(text)):
        candidates.append(value.rstrip(".,;"))
    source_domain = normalized_domain(page_url)
    scored = []
    for candidate in candidates:
        score = _apply_link_score({"url": candidate, "text": candidate}, source_domain)
        if score >= 35:
            scored.append((score, candidate))
    return max(scored, key=lambda x: x[0])[1] if scored else ""

def _detail_url_variants(item):
    """Return deterministic Bdjobs detail routes from current to legacy.

    Current /h/details pages may serve an Angular shell to non-browser HTTP clients.
    The /hn/details server-rendered route and the legacy jobs.bdjobs.com route are
    intentionally tried before Jina so a real source document can supply the full
    Job Snapshot.
    """
    variants = []
    raw = safe_text(item.get("url") or item.get("source_url"))
    job_id = safe_text(item.get("source_job_id"))
    if job_id:
        qid = quote(job_id)
        variants.extend([
            f"https://bdjobs.com/h/details/{qid}?ln=1",
            f"https://bdjobs.com/hn/details/{qid}?ln=1",
            f"https://jobs.bdjobs.com/jobdetails.asp?id={qid}&ln=1",
            f"https://jobs.bdjobs.com/bn/jobdetailsbn.asp?id={qid}&ln=1",
            f"https://bdjobs.com/h/jobs/{qid}",
        ])
    if raw:
        variants.append(request_safe_url(raw))
    out, seen = [], set()
    for value in variants:
        c = canonical_url(value)
        if c and c not in seen:
            seen.add(c)
            out.append(value)
    return out


def _looks_like_job_document(body, *, url="", detail=True):
    """Validate visible job content, never raw HTML/CSS/JS length.

    Some Bdjobs detail URLs return an application shell with HTTP 200. The shell
    can be large because of CSS/JS while containing almost no visible job data.
    """
    raw = safe_text(body)
    if not raw:
        return False, "empty_body"

    is_html = "<html" in raw.lower() or "<body" in raw.lower() or "</" in raw
    visible = _text_from_html(raw) if is_html else raw
    visible = safe_text(visible)
    if not visible:
        return False, "empty_visible_text"

    if is_cloudflare_response(200, {}, raw):
        return False, "cloudflare_challenge"

    lowered = visible.lower()
    if detail:
        markers = (
            "job summary", "experience", "education", "deadline",
            "apply by bdjobs", "company name", "job location", "salary",
            "vacancy", "employment status", "job work place",
        )
        marker_hits = sum(1 for x in markers if x in lowered)

        shell_markers = (
            "find jobs in the no. 1 job site",
            "active filters",
            "job search",
            "my bdjobs",
            "career resources",
            "accessibility adjustments",
        )
        if any(x in lowered for x in shell_markers) and marker_hits < 3:
            return False, "bdjobs_application_shell"

        if marker_hits < 2 and len(visible) < DETAIL_MIN_TEXT_CHARS:
            return False, f"thin_or_non_job_page:{len(visible)}"
        if len(visible) < DETAIL_MIN_TEXT_CHARS and marker_hits < 5:
            return False, f"thin_detail:{len(visible)}"
    return True, "ok"


def _detail_payload_from_fetch(fetched):
    """Normalize direct/Jina acquisition into parser-safe text plus optional HTML."""
    if not fetched:
        return None
    raw = safe_text(fetched.get("text", ""))
    backend = safe_text(fetched.get("backend", ""))
    page_html = raw if backend.startswith("curl_cffi:") and "<" in raw else ""
    if page_html:
        visible = _text_from_html(page_html)
    elif backend == "jina_reader":
        visible = clean_reader_markdown(raw)
    else:
        visible = raw
    result = dict(fetched)
    result["html"] = page_html
    result["text"] = visible
    result["raw_text"] = raw
    return result


def _scrapling_visible_text(page):
    """Extract rendered body text from a Scrapling Response with API-tolerant fallbacks."""
    if page is None:
        return ""
    try:
        body_nodes = page.css("body")
        if body_nodes:
            first = body_nodes[0]
            text = first.get_all_text(strip=True)
            if text:
                return _clean_one_line(text)
    except Exception:
        pass
    for attr in ("text", "body_text"):
        try:
            value = getattr(page, attr, "")
            if callable(value):
                value = value()
            if value:
                return _clean_one_line(value)
        except Exception:
            pass
    try:
        value = page.get_all_text(strip=True)
        if value:
            return _clean_one_line(value)
    except Exception:
        pass
    return ""


def _fetch_bdjobs_with_scrapling(item):
    """Render the Bdjobs application when HTTP clients receive only its Angular shell.

    This is a bounded last-mile browser fallback. It runs only after the lightweight
    curl_cffi routes are rejected, and uses a single request per candidate.
    """
    global SCRAPLING_BROWSER_FETCH_COUNT
    if not SCRAPLING_BROWSER_ENABLED or StealthyFetcher is None:
        return None
    if SCRAPLING_BROWSER_FETCH_COUNT >= SCRAPLING_BROWSER_DETAIL_LIMIT:
        return None
    url = safe_text(item.get("url") or item.get("source_url"))
    if not url:
        return None
    with SCRAPLING_BROWSER_LOCK:
        if SCRAPLING_BROWSER_FETCH_COUNT >= SCRAPLING_BROWSER_DETAIL_LIMIT:
            return None
        SCRAPLING_BROWSER_FETCH_COUNT += 1
        ordinal = SCRAPLING_BROWSER_FETCH_COUNT
        try:
            page = StealthyFetcher.fetch(
                request_safe_url(url),
                headless=True,
                disable_resources=True,
                load_dom=True,
                network_idle=False,
                wait=SCRAPLING_BROWSER_WAIT_MS,
                timeout=SCRAPLING_BROWSER_TIMEOUT,
                google_search=True,
                solve_cloudflare=False,
                block_webrtc=True,
                hide_canvas=True,
                retries=1,
                retry_delay=0.5,
            )
            text = _scrapling_visible_text(page)
            ok, reason = _looks_like_job_document(text, url=url, detail=True)
            if not ok:
                logger.info(
                    "BDJOBS BROWSER REJECT | id=%s | attempt=%d | reason=%s | chars=%d",
                    item.get("source_job_id", ""), ordinal, reason, len(text),
                )
                return None
            logger.info(
                "BDJOBS BROWSER SUCCESS | id=%s | attempt=%d | chars=%d",
                item.get("source_job_id", ""), ordinal, len(text),
            )
            return {
                "ok": True, "status": 200, "text": text, "html": "",
                "url": url, "backend": "scrapling_stealthy",
                "cloudflare": False, "detail_quality": "browser_valid",
                "detail_route": url,
            }
        except Exception as exc:
            logger.info(
                "BDJOBS BROWSER FAILED | id=%s | attempt=%d | error=%s",
                item.get("source_job_id", ""), ordinal, exc,
            )
            return None


def _fetch_bdjobs_detail(item):
    """Acquire one real Bdjobs detail document with bounded fallbacks.

    Flow:
      1. current /h/details
      2. current /hn/details when the first route is an application shell
      3. legacy jobs.bdjobs.com server-rendered detail
      4. one Jina Reader request
      5. listing preservation handled by research_job/retrieve_job_content

    Legacy routes are tried on *content failure* as well as transport failure,
    because HTTP 200 Angular shells are a known current-site behavior.
    """
    key=cache_key(item.get("url") or item.get("source_url") or item.get("source_job_id"))
    cached=DETAIL_CACHE.get(key)
    if cached and time.monotonic()-cached.get("ts",0) < DETAIL_CACHE_TTL_SECONDS:
        return cached.get("result")

    variants=_detail_url_variants(item)
    if not variants:
        DETAIL_CACHE[key]={"ts":time.monotonic(),"result":None}
        return None

    last_reason="no_url"
    attempted=[]
    primary=variants[0]
    for idx, variant in enumerate(variants[:4]):
        timeout = DETAIL_TIMEOUT if idx == 0 else LEGACY_DETAIL_TIMEOUT
        referer = item.get("url") or BDJOBS_LISTING_URL
        direct=_fetch_with_curl(variant, timeout=timeout, referer=referer, max_attempts=(CURL_MAX_FINGERPRINT_ATTEMPTS if idx == 0 else 2))
        attempted.append(variant)
        if not direct:
            last_reason="transport_failure"
            continue
        body=safe_text(direct.get("text"))
        ok,reason=_looks_like_job_document(body, url=direct.get("url") or variant, detail=True)
        if ok:
            direct=_detail_payload_from_fetch(direct)
            direct["detail_quality"]="direct_valid"
            direct["detail_route"] = variant
            DETAIL_CACHE[key]={"ts":time.monotonic(),"result":direct}
            logger.info("BDJOBS DETAIL SUCCESS | id=%s | route=%s | backend=%s", item.get("source_job_id",""), urlparse(variant).path, direct.get("backend",""))
            return direct
        last_reason=reason
        logger.info(
            "BDJOBS DETAIL REJECT | id=%s | route=%s | backend=%s | status=%s | reason=%s",
            item.get("source_job_id",""), urlparse(variant).path, direct.get("backend",""), direct.get("status",""), reason,
        )

        # Once a valid-looking source document has been found, stop. Only a rejected
        # 200 shell/thin page triggers the next route.
        if idx == 0:
            continue

    # The current Bdjobs /h/details and /hn/details endpoints can return an Angular
    # shell to non-browser clients even with HTTP 200. At that point the correct
    # fallback is a real JS-capable browser render, not repeated Jina requests.
    browser = _fetch_bdjobs_with_scrapling(item)
    if browser:
        DETAIL_CACHE[key]={"ts":time.monotonic(),"result":browser}
        return browser

    # One Jina attempt as a final text-only fallback.
    if JINA_ENABLED:
        jina_target = variants[2] if len(variants) >= 3 else primary
        fallback=_fetch_jina(jina_target, timeout=min(10, max(5, int(JINA_TIMEOUT))))
        if fallback:
            ok,reason=_looks_like_job_document(fallback.get("text", ""), url=jina_target, detail=True)
            if ok:
                fallback=_detail_payload_from_fetch(fallback)
                fallback["detail_quality"]="jina_valid"
                fallback["detail_route"] = jina_target
                DETAIL_CACHE[key]={"ts":time.monotonic(),"result":fallback}
                logger.info("BDJOBS DETAIL SUCCESS | id=%s | route=jina:%s", item.get("source_job_id",""), urlparse(jina_target).path)
                return fallback
            last_reason=reason
            logger.info("BDJOBS DETAIL JINA REJECT | id=%s | reason=%s", item.get("source_job_id",""), reason)

    logger.info("BDJOBS DETAIL UNAVAILABLE | id=%s | reason=%s | routes=%d", item.get("source_job_id",""), last_reason, len(attempted))
    DETAIL_CACHE[key]={"ts":time.monotonic(),"result":None}
    return None


def _listing_fallback_content(item):
    baseline=item.get("listing_fields") or {}
    text_parts=[
        item.get("title",""), baseline.get("company",""), baseline.get("location",""),
        baseline.get("employment_type",""), baseline.get("workplace",""), baseline.get("education",""),
        baseline.get("experience",""), baseline.get("salary",""), baseline.get("vacancy",""),
        baseline.get("age",""), baseline.get("application_method",""), baseline.get("deadline",""),
        baseline.get("posted_date",""), item.get("listing_posted",""), item.get("listing_deadline",""), item.get("excerpt","")
    ]
    text=" | ".join(_clean_one_line(x) for x in text_parts if _clean_one_line(x))
    if not text or len(text) < 80:
        return None
    return {
        "text": trim_source_text(text, MAX_JOB_CONTENT_CHARS),
        "html": "", "final_url": item.get("url", ""),
        "apply_url": item.get("apply_url", ""),
        "backend": "bdjobs_listing_fallback", "cloudflare": False, "detail_quality": "listing_fallback",
    }

def retrieve_job_content(item):
    source=item.get("source", "Bdjobs")
    if source == "Bdjobs":
        fetched=_fetch_bdjobs_detail(item)
    else:
        fetched=_fetch_source_document(item["url"], timeout=DETAIL_TIMEOUT, referer=BDJOBS_LISTING_URL)

    if not fetched:
        fallback=_listing_fallback_content(item)
        if fallback:
            logger.info("DETAIL FALLBACK | source=Bdjobs | id=%s | backend=bdjobs_listing_fallback", item.get("source_job_id", ""))
            return fallback
        logger.warning("DETAIL retrieval failed | source=%s | id=%s | url=%s | reason=no_detail_or_listing_data", source, item.get("source_job_id", ""), item.get("url", ""))
        return None

    backend=fetched.get("backend", "")
    page_html=fetched.get("html", "")
    full_text=safe_text(fetched.get("text", ""))
    article_text=None
    if page_html and trafilatura:
        try:
            article_text=trafilatura.extract(page_html, include_comments=False, include_tables=True, favor_precision=True)
        except Exception:
            article_text=None
    raw_text=full_text or article_text or safe_text(fetched.get("text", ""))
    if article_text and len(article_text) > len(raw_text)*0.35 and article_text not in raw_text:
        raw_text += "\n" + article_text
    if not raw_text or len(raw_text) < DETAIL_MIN_TEXT_CHARS:
        fallback=_listing_fallback_content(item)
        if fallback:
            logger.info("DETAIL THIN -> LISTING FALLBACK | id=%s | backend=%s | chars=%d", item.get("source_job_id",""), backend, len(raw_text))
            return fallback
        logger.info("DETAIL PARSE EMPTY | id=%s | backend=%s | chars=%d", item.get("source_job_id",""), backend, len(raw_text))
        return None

    apply_url=extract_apply_url(page_html, fetched.get("url") or item.get("url"), source) if page_html else ""
    if not apply_url:
        apply_url=_extract_apply_url_from_text(raw_text, fetched.get("url") or item.get("url"))
    return {
        "text": raw_text[:MAX_JOB_CONTENT_CHARS], "html": page_html,
        "final_url": fetched.get("url") or item.get("url"), "apply_url": apply_url,
        "backend": backend, "cloudflare": bool(fetched.get("cloudflare")),
        "detail_quality": fetched.get("detail_quality", "direct_valid"),
    }

def _research_teletalk_job(item):
    fields = dict(item.get("api_fields") or {})
    fields.update({
        "canonical": item.get("canonical", canonical_url(item.get("source_url", ""))),
        "source": "Teletalk", "source_url": item.get("source_url", fields.get("source_url", "")),
        "apply_url": fields.get("apply_url", item.get("apply_url", "")),
        "retrieval_backend": "teletalk_api", "listing_posted": item.get("listing_posted", ""),
        "listing_deadline": item.get("listing_deadline", ""), "is_government": True,
        "source_job_id": fields.get("source_job_id", item.get("source_job_id", "")),
    })
    fields["application_method"] = compact_application(fields.get("application_method", ""), fields.get("apply_url", ""))
    fields["audience_pre_score"] = job_family_score(fields.get("title", ""), fields.get("raw_text", ""))
    fields["bba_mba_target_score"] = bba_mba_candidate_score(fields)
    fields["event_id"] = job_event_key(fields)
    return fields

def _valid_merged_field(key, value, *, title="", company=""):
    value=_clean_one_line(value)
    if not value:
        return False
    low=value.lower()
    if low in {"none","null","n/a","na","not specified","not available","--","-"}:
        return False
    if key in {"title","company"}:
        if len(value)>180 or "bdjobs.com" in low or "name-share-details" in low:
            return False
        if re.search(r"\.(?:gif|png|jpe?g|webp|svg)\b", value, re.I):
            return False
        if key=="company" and title and normalize_title(value)==normalize_title(title):
            return False
    if key=="location" and ("bdjobs.com" in low or len(value)>200):
        return False
    if key=="education" and len(value)>500:
        return False
    if key in {"experience","age"} and len(value)>120:
        return False
    if key in {"salary","vacancy","employment_type","workplace","application_method","selection_process"} and len(value)>220:
        return False
    return True


def merge_job_fields(detail_fields, listing_fields, item):
    """Merge detail enrichment without ever erasing a trustworthy listing value."""
    detail=detail_fields or {}
    listing=listing_fields or {}
    merged={}
    keys=(
        "title","company","location","employment_type","workplace","education","experience",
        "salary","vacancy","age","application_method","selection_process","application_period",
        "application_start","application_end","deadline","posted_date","category",
    )
    base_title=safe_text(item.get("title") or listing.get("title") or detail.get("title"))
    base_company=safe_text(item.get("company") or listing.get("company") or detail.get("company"))
    for key in keys:
        for candidate in (detail.get(key), listing.get(key), item.get(key)):
            if _valid_merged_field(key,candidate,title=base_title,company=base_company):
                merged[key]=candidate
                break
    merged["title"]=merged.get("title") or base_title
    merged["company"]=merged.get("company") or base_company
    merged["source_url"]=safe_text(item.get("source_url") or item.get("url"))
    merged["source"]=safe_text(item.get("source")) or "Bdjobs"
    merged["source_job_id"]=safe_text(item.get("source_job_id"))
    if not merged.get("posted_date"):
        merged["posted_date"]=safe_text(item.get("listing_posted"))
    if not merged.get("deadline"):
        merged["deadline"]=safe_text(item.get("listing_deadline"))
    return merged


def research_job(item):
    if item.get("source") == "Teletalk" and item.get("api_fields"):
        return _research_teletalk_job(item)

    retrieved=retrieve_job_content(item)
    if not retrieved:
        # Listing data remains the source of truth when enrichment completely fails.
        fallback=_listing_fallback_content(item)
        if not fallback:
            return None
        retrieved=fallback

    # A listing fallback is deliberately built from already-normalized listing data.
    # Treating the fallback's re-parsed text as higher-precedence "detail" data can
    # resurrect stale excerpt values and overwrite richer listing fields. Only a
    # genuinely fetched detail/Jina document is allowed to override the listing.
    fields=extract_job_fields(retrieved["text"], retrieved.get("html", ""), item["url"], item)
    listing=item.get("listing_fields") or {}
    detail_fields = {} if retrieved.get("detail_quality") == "listing_fallback" else fields
    fields=merge_job_fields(detail_fields, listing, item)

    apply_url=retrieved.get("apply_url", "") or item.get("apply_url", "")
    if not apply_url and fields.get("application_method"):
        apply_url=_extract_apply_url_from_text(fields["application_method"], item["url"])
    if not fields.get("application_method") and apply_url:
        fields["application_method"] = "Online"
    if item.get("source") == "Bdjobs" and not fields.get("application_method") and not fields.get("selection_process"):
        # The current Bdjobs detail page exposes a first-class Apply Now action.
        # Use Online only when we can actually point to the source/application URL.
        if apply_url:
            fields["application_method"] = "Online"
    fields.update({
        "canonical": item["canonical"], "apply_url": apply_url,
        "source_url": item.get("source_url") or item.get("url", ""),
        "retrieval_backend": retrieved.get("backend", ""),
        "detail_quality": retrieved.get("detail_quality", ""),
        "raw_text": retrieved.get("text", ""),
        "listing_posted": item.get("listing_posted", ""),
        "listing_deadline": item.get("listing_deadline", ""),
        "source_job_id": item.get("source_job_id", ""),
        "source_category_id": item.get("category_id", ""),
        "source_category_name": item.get("category_name", ""),
        "is_government": bool(item.get("is_government")),
    })
    if not fields.get("posted_date") and item.get("listing_posted"):
        fields["posted_date"]=item["listing_posted"]
    if not fields.get("deadline") and item.get("listing_deadline"):
        fields["deadline"]=item["listing_deadline"]
    fields["source"]=item.get("source") or source_name(item.get("url", ""))
    fields["title"]=fields.get("title") or item.get("title", "")
    fields["company"]=fields.get("company") or listing.get("company") or item.get("company", "")
    fields["application_method"]=compact_application(fields.get("application_method", ""), apply_url)
    fields["audience_pre_score"]=job_family_score(fields.get("title", ""), retrieved.get("text", "")[:9000])
    fields["bba_mba_target_score"]=bba_mba_candidate_score(fields)
    fields["event_id"]=job_event_key(fields)
    return fields


# ============================================================
# FRESHNESS / DEADLINE / RANKING
# ============================================================

def deadline_status(job):
    raw = safe_text(job.get("deadline"))
    if not raw: return "unknown"
    dt = parse_datetime(raw)
    if not dt: return "unknown"
    return "expired" if dt < datetime.now(BD_TZ) else "active"

def posted_age_days(job):
    dt = parse_datetime(job.get("posted_date") or job.get("listing_posted"))
    if not dt: return None
    return max(0.0, (datetime.now(BD_TZ)-dt).total_seconds()/86400)

def posted_freshness_score(job):
    age = posted_age_days(job)
    if age is None: return 5
    if age <= 1: return 15
    if age <= 2: return 13
    if age <= 3: return 11
    if age <= 5: return 9
    return 0

def education_priority_score(job):
    blob = f"{job.get('education','')} {job.get('raw_text','')}".lower()
    has_bba = bool(re.search(r"\bbba\b|bachelor\s+of\s+business\s+administration|business\s+administration", blob))
    has_mba = bool(re.search(r"\bmba\b|master\s+of\s+business\s+administration", blob))
    if has_bba and has_mba: return 25
    if has_bba or has_mba: return 24
    if any(x in blob for x in ("bbs", "mbs", "business studies", "commerce", "management degree")): return 21
    if any(x in blob for x in ("bachelor", "honours", "honors", "graduate", "degree")): return 17
    if "master" in blob: return 15
    return 5

def experience_upper_bound(value):
    blob = _clean_one_line(value).lower()
    if not blob: return None
    if any(x in blob for x in ("fresh", "no experience", "entry-level", "entry level", "intern")): return 0
    m = re.search(r"(\d+)\s*(?:to|[-–])\s*(\d+)\s*years?", blob)
    if m: return int(m.group(2))
    m = re.search(r"(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s*(\d+)\s*years?", blob)
    if m: return int(m.group(1))
    m = re.search(r"(\d+)\s*\+\s*years?", blob)
    if m: return int(m.group(1))
    m = re.search(r"(\d+)\s*years?", blob)
    return int(m.group(1)) if m else None

def _experience_years(value):
    blob = _clean_one_line(value).lower()
    if any(x in blob for x in ("fresh", "no experience", "entry-level", "entry level", "intern")): return 0
    m = re.search(r"(\d+)\s*(?:to|[-–])\s*(\d+)\s*years?", blob)
    if m: return int(m.group(1))
    upper = experience_upper_bound(value)
    return upper

def experience_priority_score(job):
    years = _experience_years(job.get("experience", ""))
    if years is None: return 10
    if years <= 0: return 15
    if years <= 1: return 14
    if years <= 2: return 12
    if years <= 3: return 10
    if years <= 5: return 7
    if years <= 7: return 4
    return 1

def deadline_urgency_score(job):
    dt = parse_datetime(job.get("deadline"))
    if not dt: return 2
    days = (dt-datetime.now(BD_TZ)).total_seconds()/86400
    if days < 0: return 0
    if days <= 1: return 10
    if days <= 3: return 9
    if days <= 7: return 8
    if days <= 14: return 6
    if days <= 30: return 4
    return 2

def salary_attractiveness_score(job):
    text = safe_text(job.get("salary", "")).lower()
    nums = [int(x.replace(",", "")) for x in re.findall(r"\b\d{2,6}(?:,\d{3})?\b", text)]
    nums = [x for x in nums if 1000 <= x <= 2000000]
    if len(nums) >= 2:
        midpoint = (nums[0]+nums[1])/2
        return 5 if midpoint >= 60000 else 4 if midpoint >= 45000 else 3 if midpoint >= 30000 else 2
    if nums:
        return 5 if nums[0] >= 50000 else 4 if nums[0] >= 30000 else 3 if nums[0] >= 20000 else 2
    return 1 if any(x in text for x in ("competitive", "negotiable")) else 0

def vacancy_score(job):
    m = re.search(r"\d+", safe_text(job.get("vacancy", "")).replace(",", ""))
    if not m: return 0
    n = int(m.group(0))
    return 5 if n >= 10 else 4 if n >= 5 else 3 if n >= 2 else 1

def information_quality_score(job):
    fields = ("company", "education", "experience", "salary", "vacancy", "deadline", "location", "posted_date", "age", "employment_type", "workplace", "apply_url")
    return min(5, round(sum(1 for k in fields if safe_text(job.get(k)))*5/len(fields)))

def role_fit_score(job):
    title = safe_text(job.get("title", "")).lower()
    raw = f"{title} {safe_text(job.get('raw_text',''))}".lower()
    title_hits = sum(1 for t in TARGET_FUNCTION_TERMS if t in title)
    any_hits = sum(1 for t in TARGET_FUNCTION_TERMS if t in raw)
    score = min(20, title_hits*6 + min(8, any_hits*2))
    if any(t in title for t in NON_BUSINESS_ROLE_TERMS): return 0
    return score

def job_quality_score(job):
    return information_quality_score(job)

def private_experience_too_high(job):
    upper = experience_upper_bound(job.get("experience", ""))
    return upper is not None and upper > MAX_PRIVATE_EXPERIENCE_YEARS

def deterministic_job_gate(job):
    if not job.get("title"): return False, "missing_title"
    if is_noise_title(job["title"], job.get("source_url", "")): return False, "noise_title"
    if job.get("is_government"):
        if job.get("source") != "Teletalk" or not is_teletalk_url(job.get("source_url", "")): return False, "government_source_not_allowed"
        if deadline_status(job) == "expired": return False, "expired"
        return True, "ok_government"
    if not job.get("company"): return False, "missing_company"
    if not is_domain_allowed(job.get("source_url", ""), BDJOBS_DOMAINS): return False, "source_not_allowed"
    if deadline_status(job) == "expired": return False, "expired"
    age = posted_age_days(job)
    if age is not None and age > MAX_POST_AGE_DAYS: return False, f"posted_older_than_{MAX_POST_AGE_DAYS}_days"
    if private_experience_too_high(job): return False, f"experience_above_{MAX_PRIVATE_EXPERIENCE_YEARS}_years"
    score = bba_mba_candidate_score(job)
    job["bba_mba_target_score"] = score
    if score < 25: return False, "not_bba_mba_business_candidate_relevant"
    return True, "ok_bba_mba_target"

def private_rank_score(job):
    components = {
        "education": min(25, education_priority_score(job)),
        "role": min(20, role_fit_score(job)),
        "career_stage": min(15, experience_priority_score(job)),
        "freshness": min(15, posted_freshness_score(job)),
        "deadline": min(10, deadline_urgency_score(job)),
        "salary": min(5, salary_attractiveness_score(job)),
        "vacancy": min(5, vacancy_score(job)),
        "information": min(5, information_quality_score(job)),
    }
    return round(sum(components.values()), 3), components

def government_rank_score(job):
    return round(min(35, posted_freshness_score(job)*(35/15)) + min(30, deadline_urgency_score(job)*3) + min(15, vacancy_score(job)*3) + min(20, information_quality_score(job)*4), 3)

# ============================================================
# CEREBRAS EDITORIAL JUDGE
# ============================================================

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "publish": {"type": "boolean"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "bba_mba_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "early_career_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "role_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["id", "publish", "score", "bba_mba_fit", "early_career_fit", "role_fit", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _judge_prompt():
    return """
You are the semantic audit layer for Career News V1.
Audience: Bangladesh BBA/MBA students, graduates, freshers and early-career business candidates.
Use only supplied source-backed facts. Never invent missing fields.
For private jobs, audit education match, business-role fit, career-stage fit, semantic contradictions, specialist-degree requirements and seniority.
For government Teletalk jobs, do not apply the private BBA/MBA gate. Audit only source coherence and contradictions.
Return every input candidate.
"""

def _extract_json_objects(raw):
    """Recover complete JSON objects from fenced/partially malformed model output."""
    text = safe_text(raw)
    if not text:
        return []
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return payload["results"]
        if isinstance(payload, list):
            return payload
    except Exception:
        pass

    # Salvage object-by-object from a broken results array.
    start_idx = cleaned.find("[")
    if start_idx < 0:
        start_idx = cleaned.find("{")
    if start_idx < 0:
        return []

    objects = []
    depth = 0
    in_string = False
    escaped = False
    obj_start = None
    for idx in range(start_idx, len(cleaned)):
        ch = cleaned[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                obj_start = idx
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    chunk = cleaned[obj_start:idx + 1]
                    try:
                        objects.append(json.loads(chunk))
                    except Exception:
                        pass
                    obj_start = None
    if objects:
        return objects

    # Last-resort field extraction. This handles one bad `reason` string without
    # discarding otherwise useful numeric/boolean audit fields.
    recovered = []
    for match in re.finditer(r'"id"\s*:\s*(\d+)(?P<body>.*?)(?=(?:"id"\s*:)|\}\s*,?\s*\}|$)', cleaned, flags=re.S):
        body = match.group("body")
        def pick(pattern, default=None):
            m = re.search(pattern, body, flags=re.I | re.S)
            return m.group(1) if m else default
        row = {
            "id": int(match.group(1)),
            "publish": str(pick(r'"publish"\s*:\s*(true|false)', "true")).lower() == "true",
            "score": int(float(pick(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', "0"))),
            "bba_mba_fit": int(float(pick(r'"bba_mba_fit"\s*:\s*(-?\d+(?:\.\d+)?)', "0"))),
            "early_career_fit": int(float(pick(r'"early_career_fit"\s*:\s*(-?\d+(?:\.\d+)?)', "0"))),
            "role_fit": int(float(pick(r'"role_fit"\s*:\s*(-?\d+(?:\.\d+)?)', "0"))),
            "reason": safe_text(pick(r'"reason"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', "")),
        }
        recovered.append(row)
    return recovered


def _validate_judge_rows(rows, batch_len):
    valid = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("id"))
        except Exception:
            continue
        if not 1 <= idx <= batch_len:
            continue
        try:
            score = max(0, min(100, int(float(row.get("score", 0)))))
            bba_fit = max(0, min(100, int(float(row.get("bba_mba_fit", 0)))))
            early_fit = max(0, min(100, int(float(row.get("early_career_fit", 0)))))
            role_fit = max(0, min(100, int(float(row.get("role_fit", 0)))))
        except Exception:
            continue
        valid.append({
            "id": idx,
            "publish": bool(row.get("publish", True)),
            "score": score,
            "bba_mba_fit": bba_fit,
            "early_career_fit": early_fit,
            "role_fit": role_fit,
            "reason": safe_text(row.get("reason", "")),
        })
    return valid


def _call_judge_once(batch, batch_no, retry=False):
    payload = []
    for idx, job in enumerate(batch, 1):
        payload.append("\n".join([
            f"ID: {idx}", f"Source: {job.get('source','')}", f"Title: {job.get('title','')}",
            f"Company: {job.get('company','')}", f"Career category: {job.get('career_category','')}",
            f"Education: {trim_source_text(job.get('education',''),180)}",
            f"Experience: {trim_source_text(job.get('experience',''),100)}",
            f"Salary: {trim_source_text(job.get('salary',''),80)}", f"Vacancy: {job.get('vacancy','')}",
            f"Posted: {job.get('posted_date','')}", f"Deadline: {job.get('deadline','')}",
            f"Location: {job.get('location','')}",
            f"Description: {trim_source_text(job.get('raw_text',''),700)}",
        ]))
    prompt = (
        "Audit every candidate. Return ONLY one JSON object matching the schema. "
        "Do not use markdown fences. Keep reason under 140 characters. "
        "Use only supplied facts. "
        + ("Retry: ensure the JSON is syntactically valid and contains every candidate ID." if retry else "")
        + "\n\n" + "\n\n".join(payload)
    )
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role":"system","content":_judge_prompt()},{"role":"user","content":prompt}],
            response_format={"type":"json_schema","json_schema":{"name":f"career_job_audit_{batch_no}","strict":True,"schema":JUDGE_SCHEMA}},
            temperature=0.0,
            max_completion_tokens=max(2400, 700 * max(1, len(batch))),
        )
        content = safe_text(response.choices[0].message.content)
        rows = _validate_judge_rows(_extract_json_objects(content), len(batch))
        if rows:
            return rows
        raise ValueError("no_valid_judge_rows")
    except Exception as exc:
        logger.warning("CEREBRAS audit batch %d failed%s: %s", batch_no, " on retry" if retry else "", exc)
        return []


def judge_batch(batch, batch_no):
    if not get_cerebras() or not batch:
        return []
    # Smaller batches materially reduce malformed/truncated structured-output risk.
    chunks = [batch[i:i + max(1, AI_BATCH_SIZE)] for i in range(0, len(batch), max(1, AI_BATCH_SIZE))]
    all_rows = []
    offset = 0
    for chunk_no, chunk in enumerate(chunks, 1):
        rows = _call_judge_once(chunk, batch_no * 100 + chunk_no, retry=False)
        if not rows and AI_RETRY_COUNT > 0:
            rows = _call_judge_once(chunk, batch_no * 100 + chunk_no, retry=True)
        # If a multi-candidate call still fails, split it once more rather than
        # losing the entire private audit population.
        if not rows and len(chunk) > 1:
            midpoint = max(1, len(chunk) // 2)
            for sub_no, sub in enumerate((chunk[:midpoint], chunk[midpoint:]), 1):
                if not sub:
                    continue
                sub_rows = _call_judge_once(sub, batch_no * 1000 + chunk_no * 10 + sub_no, retry=True)
                all_rows.extend(sub_rows)
        else:
            all_rows.extend(rows)
        offset += len(chunk)
    return all_rows


def rank_jobs(jobs):
    if not jobs:
        return []
    pre = []
    for job in jobs:
        if private_experience_too_high(job):
            continue
        score, components = private_rank_score(job)
        candidate = dict(job)
        candidate["deterministic_score"] = score
        candidate["deterministic_components"] = components
        candidate["career_category"] = candidate.get("career_category") or _career_family(candidate)
        pre.append(candidate)

    pre.sort(key=lambda j: (
        -j["deterministic_score"],
        -role_fit_score(j),
        -posted_freshness_score(j),
        j.get("canonical", ""),
    ))

    interns = [j for j in pre if is_internship_job(j)]
    general = [j for j in pre if not is_internship_job(j)]
    forced_interns = interns[:min(INTERNSHIP_AI_TARGET, len(interns))]
    forced_keys = {j.get("canonical") for j in forced_interns}
    ai_candidates = forced_interns + [j for j in general if j.get("canonical") not in forced_keys]
    ai_candidates = ai_candidates[:min(AI_REVIEW_TARGET, len(ai_candidates))]
    judged = {}
    if ai_candidates:
        rows = judge_batch(ai_candidates, 1)
        logger.info("PRIVATE AI AUDITED | %d / %d", len(rows), len(ai_candidates))
        for row in rows:
            try:
                idx = int(row.get("id"))
            except Exception:
                continue
            if 1 <= idx <= len(ai_candidates):
                judged[ai_candidates[idx - 1].get("canonical")] = row

    ranked = []
    for job in pre:
        row = judged.get(job.get("canonical"), {})
        ai_available = bool(row)
        ai_score = max(0, min(100, int(row.get("score", 0)))) if ai_available else None
        candidate = dict(job)
        candidate.update({
            "ai_score": ai_score if ai_available else None,
            "judge_publish": bool(row.get("publish", True)) if ai_available else True,
            "bba_mba_fit": int(row.get("bba_mba_fit", min(100, education_priority_score(job) * 4))) if ai_available else None,
            "early_career_fit": int(row.get("early_career_fit", min(100, experience_priority_score(job) * 6))) if ai_available else None,
            "role_fit": int(row.get("role_fit", min(100, role_fit_score(job) * 5))) if ai_available else None,
            "judge_reason": safe_text(row.get("reason", "Deterministic source-first ranking.")) if ai_available else "AI unavailable; deterministic score used.",
        })
        if not candidate["judge_publish"]:
            continue
        candidate["final_score"] = (
            round(candidate["deterministic_score"] * 0.85 + ai_score * 0.15, 3)
            if ai_available else candidate["deterministic_score"]
        )
        floor = (45 if is_internship_job(candidate) else PRIVATE_HARD_FILL_SCORE)
        if candidate["final_score"] >= floor and deadline_status(candidate) != "expired":
            ranked.append(candidate)

    ranked.sort(key=lambda j: (
        -j.get("final_score", 0),
        -j.get("deterministic_score", 0),
        j.get("canonical", ""),
    ))
    return ranked


# ============================================================
# STATE / DEDUP / SELECTION
# ============================================================

def save_job_to_queue(job):
    key = job["canonical"]
    existing = STATE["queue"].get(key, {})
    existing.update(job)
    existing["record_format"] = 4
    existing.setdefault("status", "pending")
    existing.setdefault("first_seen", now_iso())
    existing["last_seen"] = now_iso()
    STATE["queue"][key] = existing


def candidate_already_posted(job):
    canonical = canonical_url(job.get("source_url", ""))
    if canonical in POSTED_URLS:
        return True
    source_id = safe_text(job.get("source_job_id"))
    source = safe_text(job.get("source")).lower()
    if source_id and source:
        for published in STATE.get("events", {}).values():
            if published.get("status") == "published" and safe_text(published.get("source")).lower() == source and safe_text(published.get("source_job_id")) == source_id:
                return True
    event_id = job.get("event_id") or job_event_key(job)
    event = STATE.get("events", {}).get(event_id, {})
    if event.get("status") == "published":
        return True
    # Cross-source persistent mirror check. A source/Bdjobs/Teletalk copy can have different
    # URLs, so compare it against the small persistent published-event set as well.
    for published in STATE.get("events", {}).values():
        if published.get("status") != "published":
            continue
        if likely_same_job(job, published):
            return True
    return False


def build_unique_job_pool(jobs):
    unique = []
    event_keys = set()
    identity_buckets = {}
    for job in jobs:
        if candidate_already_posted(job):
            continue
        event_key = job_event_key(job)
        if event_key in event_keys:
            continue
        # First-pass bucket by normalized company/title family. This catches category copies
        # cheaply before the stronger fuzzy mirror comparison.
        company_key = _normalized_company(job.get("company", ""))
        bucket = company_key if company_key else f"__no_company__:{normalize_title(job.get('title',''))[:60]}"
        maybe_same = identity_buckets.get(bucket, [])
        if any(likely_same_job(job, previous) for previous in maybe_same):
            continue
        unique.append(job)
        event_keys.add(event_key)
        identity_buckets.setdefault(bucket, []).append(job)
    return unique


def is_internship_job(job):
    """Detect true internship opportunities without treating generic trainee roles as internships."""
    title = safe_text(job.get("title")).lower()
    employment = safe_text(job.get("employment_type")).lower()
    raw = safe_text(job.get("raw_text")).lower()
    blob = f"{title} {employment} {raw}"
    strong = (
        "internship" in title
        or re.search(r"\bintern\b", title)
        or "internship" in employment
        or "internship program" in raw
        or re.search(r"\bintern\b", employment)
    )
    return bool(strong)

def _career_family(job):
    blob = f"{job.get('title','')} {job.get('raw_text','')}".lower()
    families = [
        ("Finance & Accounting", ("finance","account","audit","tax","treasury")),
        ("Banking & Financial Services", ("bank","credit","relationship officer","branch","financial institution")),
        ("Marketing & Sales", ("marketing","sales","brand","trade marketing")),
        ("Business Development", ("business development","partnership","growth","client acquisition")),
        ("Human Resources", ("human resource","hr executive","recruitment","talent acquisition")),
        ("Supply Chain & Procurement", ("supply chain","procurement","purchase","sourcing","logistics")),
        ("Commercial", ("commercial","import","export","trade operations")),
        ("Management & Administration", ("management","admin","administration","executive assistant")),
        ("Customer Experience", ("customer service","client service","customer experience","relationship management")),
        ("E-commerce & Digital Business", ("e-commerce","ecommerce","marketplace","digital operations","online business")),
        ("Operations", ("operations","process executive","business operations")),
        ("Media / Advertisement / Events", ("media","advertisement","event management")),
        ("Research & Consultancy", ("research","consultancy","consultant","analyst")),
        ("NGO / Development", ("ngo","development organization","development program")),
        ("Hospitality / Travel / Tourism", ("hospitality","travel","tourism","hotel")),
    ]
    for family, terms in families:
        if any(term in blob for term in terms): return family
    return "Business / General"

def _private_selection_pool(ranked):
    base = [
        j for j in ranked
        if j.get("final_score", 0) >= QUALITY_FLOOR
        and j.get("judge_publish", True)
        and deadline_status(j) != "expired"
        and j.get("bba_mba_target_score", 0) >= 25
        and not private_experience_too_high(j)
        and j.get("company")
        and information_quality_score(j) >= PRIVATE_MIN_INFORMATION_QUALITY
        and not candidate_already_posted(j)
    ]
    return base


def _private_minimum_fill_pool(ranked, minimum_score=PRIVATE_MIN_FILL_SCORE, minimum_info=PRIVATE_MIN_INFORMATION_QUALITY):
    return [
        j for j in ranked
        if j.get("final_score", 0) >= minimum_score
        and j.get("judge_publish", True)
        and deadline_status(j) != "expired"
        and j.get("bba_mba_target_score", 0) >= 40
        and not private_experience_too_high(j)
        and j.get("company")
        and information_quality_score(j) >= minimum_info
        and not candidate_already_posted(j)
    ]


def _private_hard_fill_pool(ranked):
    return [
        j for j in ranked
        if j.get("final_score", 0) >= PRIVATE_HARD_FILL_SCORE
        and j.get("judge_publish", True)
        and deadline_status(j) != "expired"
        and j.get("bba_mba_target_score", 0) >= 45
        and not private_experience_too_high(j)
        and j.get("company")
        and information_quality_score(j) >= PRIVATE_HARD_MIN_INFORMATION_QUALITY
        and not candidate_already_posted(j)
    ]


def _select_diverse_private(pool, limit):
    selected, company_counts, family_counts = [], {}, {}
    remaining = [dict(j, career_category=_career_family(j)) for j in pool]
    while remaining and len(selected) < max(0, int(limit)):
        best_idx, best = 0, -1e9
        for idx, job in enumerate(remaining):
            score = float(job.get("final_score", 0))
            company = _normalized_company(job.get("company", ""))
            family = job["career_category"]
            if company_counts.get(company, 0) >= 2:
                score -= 8
            if family_counts.get(family, 0) >= 4:
                score -= 5
            if score > best:
                best, best_idx = score, idx
        chosen = remaining.pop(best_idx)
        selected.append(chosen)
        company = _normalized_company(chosen.get("company", ""))
        family = chosen["career_category"]
        company_counts[company] = company_counts.get(company, 0) + 1
        family_counts[family] = family_counts.get(family, 0) + 1
    return selected


def select_private_jobs_by_category(ranked, limit, minimum_required=MIN_PRIVATE_POSTS_PER_RUN):
    """Select private jobs with a quality-first path and a controlled minimum-quota expansion.

    The expansion is only used to reach the requested minimum when enough fresh,
    non-specialist, source-backed jobs exist. It never accepts obviously weak
    or senior/specialist roles merely to pad the feed.
    """
    limit = max(0, int(limit))
    if not limit:
        return []

    strict = _select_diverse_private(_private_selection_pool(ranked), limit)
    if len(strict) >= min(minimum_required, limit):
        return strict

    selected_keys = {j.get("canonical") for j in strict}
    relaxed_pool = [
        j for j in _private_minimum_fill_pool(ranked)
        if j.get("canonical") not in selected_keys
    ]
    relaxed = _select_diverse_private(strict + relaxed_pool, min(limit, max(minimum_required, len(strict))))
    if len(relaxed) >= min(minimum_required, limit):
        return relaxed

    selected_keys = {j.get("canonical") for j in relaxed}
    hard_pool = [
        j for j in _private_hard_fill_pool(ranked)
        if j.get("canonical") not in selected_keys
    ]
    return _select_diverse_private(relaxed + hard_pool, limit)


def select_government_jobs(government_jobs):
    eligible, seen = [], set()
    for job in government_jobs:
        if candidate_already_posted(job) or deadline_status(job) == "expired":
            continue
        key = job.get("canonical") or job_event_key(job)
        if key in seen:
            continue
        seen.add(key)
        candidate = dict(job)
        candidate["government_rank_score"] = government_rank_score(candidate)
        eligible.append(candidate)
    eligible.sort(key=lambda j: (
        -j["government_rank_score"],
        -posted_freshness_score(j),
        -deadline_urgency_score(j),
        j.get("canonical", ""),
    ))
    return eligible[:min(MAX_GOVERNMENT_POSTS_PER_RUN, len(eligible))]


def select_final_jobs(private_ranked, government_jobs):
    """Select up to 20 posts while guaranteeing government/private/internship minima when qualifying pools exist."""
    gov_pool = select_government_jobs(government_jobs)
    gov_required = min(MIN_GOVERNMENT_POSTS_PER_RUN, len(gov_pool), MAX_STORIES_PER_RUN)
    gov = gov_pool[:gov_required]

    # Internship is a subset of private. Reserve the required number first so
    # diversity/ranking cannot consume their slots.
    internship_pool = [j for j in private_ranked if is_internship_job(j)]
    internship_selected = _select_diverse_private(internship_pool, min(MIN_INTERNSHIP_POSTS_PER_RUN, len(internship_pool)))
    internship_keys = {j.get("canonical") for j in internship_selected}

    remaining_private_ranked = [j for j in private_ranked if j.get("canonical") not in internship_keys]
    private_room = max(0, MAX_STORIES_PER_RUN - len(gov))
    private_required = min(MIN_PRIVATE_POSTS_PER_RUN, private_room)
    private_general_needed = max(0, private_required - len(internship_selected))
    private_general = select_private_jobs_by_category(
        remaining_private_ranked,
        max(private_general_needed, 0),
        minimum_required=private_general_needed,
    ) if private_general_needed else []

    private = internship_selected + private_general

    # After minima are satisfied, use the remaining room for the strongest available jobs.
    used = {j.get("canonical") for j in private + gov}
    remaining_slots = MAX_STORIES_PER_RUN - len(private) - len(gov)
    if remaining_slots > 0:
        extra_private_pool = [j for j in private_ranked if j.get("canonical") not in used]
        selected_extra = _select_diverse_private(extra_private_pool, remaining_slots)
        private.extend(selected_extra[:remaining_slots])
        used.update(j.get("canonical") for j in selected_extra)

    room = MAX_STORIES_PER_RUN - len(private) - len(gov)
    if room > 0:
        extra_gov = [j for j in gov_pool if j.get("canonical") not in used]
        private.extend([])
        gov.extend(extra_gov[:min(room, MAX_GOVERNMENT_POSTS_PER_RUN - len(gov))])

    selected = gov + private
    return selected[:MAX_STORIES_PER_RUN]


def store_selected_event(job, published=False, message_id=None):
    event_id = job.get("event_id") or job_event_key(job)
    STATE["events"][event_id] = {
        "event_id": event_id,
        "canonical_url": job.get("canonical", ""),
        "source_url": job.get("source_url", ""),
        "apply_url": job.get("apply_url", ""),
        "source": job.get("source", ""),
        "source_job_id": job.get("source_job_id", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "posted_date": job.get("posted_date", ""),
        "deadline": job.get("deadline", ""),
        "score": job.get("judge_score", 0),
        "status": "published" if published else "selected",
        "selected_at": now_iso(),
        "published_at": now_iso() if published else "",
        "message_id": message_id,
    }
    return event_id


# ============================================================
# PHOTO FEATURE
# ============================================================
# Polish deliberately has NO photo pipeline. Job posts are always text-only Rich Messages.
# This avoids source placeholders, black cards and mismatched media entirely.

# ============================================================
# TELEGRAM HTTP LAYER: SAME RELIABLE RETRY PATTERN
# ============================================================

def telegram_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last = {"ok": False, "description": "Unknown error"}
    for attempt in range(1, 4):
        try:
            response = session.post(url, data=data or {}, files=files, timeout=90)
            result = response.json()
            if result.get("ok"):
                return result
            last = result
            if response.status_code == 429:
                retry_after = min(5, int(result.get("parameters", {}).get("retry_after", 2)))
                logger.warning("Telegram 429; waiting %ss", retry_after)
                time.sleep(max(1, retry_after))
                continue
            if response.status_code >= 500:
                time.sleep(2 * attempt)
                continue
            break
        except Exception as exc:
            last = {"ok": False, "description": str(exc)}
            time.sleep(2 * attempt)
    return last


def _button_markup(job):
    url = safe_text(job.get("apply_url"))
    if url and url.startswith(("http://", "https://")):
        label = "APPLY NOW"
        target = url
    else:
        label = "APPLY NOW"
        target = safe_text(job.get("source_url"))
    return {
        "inline_keyboard": [[{"text": label, "url": target}]]
    }


def send_rich_text(blocks, job):
    payload={
        "chat_id":TELEGRAM_CHANNEL,
        "rich_message":json.dumps({"blocks":blocks},ensure_ascii=False,separators=(",",":")),
        "reply_markup":json.dumps(_button_markup(job),ensure_ascii=False,separators=(",",":")),
    }
    return telegram_call("sendRichMessage",data=payload)


def send_bot_api_text_fallback(job, plain_text):
    text = plain_text[:3800] if len(plain_text) > 3800 else plain_text
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    return telegram_call("sendMessage", data=data)


# ============================================================
# JOB POST RENDERING
# ============================================================

def display_value(value):
    return english_display_text(clean_generated_text(safe_text(value)))


def job_hashtags(job):
    """Generate explicit source-aware, role-aware hashtags."""
    if job.get("is_government"):
        return ["#GovtJob"]
    tags=[]
    if is_internship_job(job):
        tags.append("#Internship")
    family = safe_text(job.get("career_category") or _career_family(job))
    family_tags = {
        "Finance & Accounting": "#Finance",
        "Banking & Financial Services": "#Banking",
        "Marketing & Sales": "#Marketing",
        "Business Development": "#BusinessDevelopment",
        "Human Resources": "#HR",
        "Supply Chain & Procurement": "#SupplyChain",
        "Commercial": "#Commercial",
        "Management & Administration": "#Management",
        "Customer Experience": "#CustomerService",
        "E-commerce & Digital Business": "#Ecommerce",
        "Operations": "#Operations",
        "Media / Advertisement / Events": "#Media",
        "Research & Consultancy": "#Research",
        "NGO / Development": "#NGO",
        "Hospitality / Travel / Tourism": "#Hospitality",
    }
    tag=family_tags.get(family)
    if tag:
        tags.append(tag)
    blob=f"{safe_text(job.get('title'))} {safe_text(job.get('raw_text'))}".lower()
    extra = (
        ("#Sales", ("sales", "territory sales", "key account")),
        ("#Marketing", ("marketing", "brand", "trade marketing")),
        ("#Finance", ("finance", "account", "audit", "tax")),
        ("#Banking", ("bank", "credit", "branch")),
        ("#HR", ("human resource", "recruitment", "talent acquisition", "hr ")),
        ("#SupplyChain", ("supply chain", "procurement", "logistics", "sourcing")),
    )
    for tag_name, terms in extra:
        if any(term in blob for term in terms):
            tags.append(tag_name)
    if any(x in blob for x in EARLY_CAREER_TERMS):
        tags.append("#EarlyCareer")
    if not tags:
        tags.append("#Career")
    return list(dict.fromkeys(tags))[:4]


def _field_icon(label):
    return {
        "Location": "📍",
        "Employment": "💼",
        "Workplace": "🏢",
        "Education": "🎓",
        "Experience": "🧑‍💼",
        "Salary": "💰",
        "Vacancy": "👥",
        "Age": "🎂",
        "Application": "📝",
        "Application Period": "🗓️",
        "Selection": "🧪",
        "Deadline": "📅",
        "Posted": "🕒",
    }.get(label, "•")


def english_display_text(value):
    """Guarantee that user-visible job text contains no Bengali script."""
    text=_clean_one_line(value)
    if not text:
        return ""
    if _contains_bengali(text):
        text=fallback_government_translate(text)
    # A final Bengali-digit pass is useful for values such as government vacancy counts.
    text=text.translate(BENGALI_DIGIT_MAP)
    return _clean_one_line(text)


def snapshot_field_quality(job):
    """Count useful source-backed fields that can actually be displayed."""
    fields=("location","employment_type","workplace","education","experience","salary","vacancy","age","application_method","deadline","posted_date")
    return sum(1 for key in fields if english_display_text(job.get(key)))


def snapshot_integrity(job):
    """Reject sparse, contaminated, or internally inconsistent records before Telegram."""
    if not safe_text(job.get("title")) or not safe_text(job.get("company")):
        return False, "missing_identity"
    if not safe_text(job.get("source_url")):
        return False, "missing_source_url"
    for key in ("title","company","location","education","experience","salary","vacancy","age","employment_type","workplace","application_method"):
        value=safe_text(job.get(key)).lower()
        if any(token in value for token in ("name-share-details", "matching_lock_en", "![image", "bdjobs.com/h/images", "logo of ")):
            return False, "source_chrome_in_field"
    # Age must be explicitly plausible and independent from the experience field.
    age=safe_text(job.get("age"))
    exp=safe_text(job.get("experience"))
    if age and exp and age.casefold() == compact_age(exp).casefold() and re.search(r"\byears?\b", exp, flags=re.I):
        return False, "age_matches_experience_suspect"
    if job.get("is_government"):
        minimum=GOVERNMENT_SNAPSHOT_MIN_FIELDS
    else:
        minimum=PRIVATE_SNAPSHOT_MIN_FIELDS
        for key in ("location", "experience", "deadline"):
            if not safe_text(job.get(key)):
                return False, f"private_missing_core_{key}"
        if not any(safe_text(job.get(k)) for k in ("education", "salary", "vacancy", "employment_type", "workplace", "age", "application_method")):
            return False, "private_missing_secondary_field"
    count=snapshot_field_quality(job)
    if count < minimum:
        return False, f"snapshot_too_sparse:{count}"
    return True, "ok"


def split_snapshot_eligible(jobs):
    """Partition jobs before final selection so snapshot failures can never consume quota slots."""
    private, government = [], []
    rejected = 0
    for job in jobs:
        ok, reason = snapshot_integrity(job)
        if ok:
            (government if job.get("is_government") else private).append(job)
            continue
        rejected += 1
        job["pipeline_status"] = "rejected"
        job["rejection_reason"] = reason
    return private, government, rejected


def job_snapshot_rows(job):
    """Return only source-backed available fields; dates shown are Deadline and Posted only."""
    mapping=[
        ("Location","location"),
        ("Employment","employment_type"),
        ("Workplace","workplace"),
        ("Education","education"),
        ("Experience","experience"),
        ("Salary","salary"),
        ("Vacancy","vacancy"),
        ("Age","age"),
        ("Application","application_method"),
        ("Deadline","deadline"),
        ("Posted","posted_date"),
    ]
    rows=[]
    for label,key in mapping:
        raw=english_display_text(job.get(key))
        if not raw or raw.lower() in {"—","--","n/a","na","not available","not specified","none","null"}:
            continue
        value=format_table_value(label,raw)
        value=english_display_text(value)
        if not value or value=="—":
            continue
        rows.append((label,value))
    return rows


def _rich_bold(text):
    return {"type":"bold","text":safe_text(text)}


def _rich_url(text,url):
    return {"type":"url","text":safe_text(text),"url":safe_text(url)}


def rich_message_blocks(job):
    """Native Telegram Rich Message. Media is intentionally disabled."""
    blocks=[
        {"type":"heading","size":1,"text":"📣 "+smart_title_case(display_value(job.get("title")))},
    ]
    company=smart_title_case(display_value(job.get("company"))) or ("Government Organization" if job.get("is_government") else "")
    if company:
        blocks.append({"type":"paragraph","text":_rich_bold("🏢 "+company)})
    blocks.append({"type":"heading","size":2,"text":"JOB SNAPSHOT"})

    # Native bordered table. is_striped improves row separation on clients where border
    # lines are subtle; is_bordered remains explicitly enabled per Bot API 10.3.
    cells=[[
        {"text":"FIELD","is_header":True,"align":"center","valign":"middle"},
        {"text":"DETAILS","is_header":True,"align":"center","valign":"middle"},
    ]]
    for label,value in job_snapshot_rows(job):
        cells.append([
            {"text":_rich_bold(_field_icon(label)+" "+label),"align":"left","valign":"middle"},
            {"text":value,"align":"left","valign":"middle"},
        ])
    blocks.append({
        "type":"table",
        "cells":cells,
        "is_bordered":True,
        "is_striped":True,
        "is_compact":False,
    })

    # Telegram RichBlockTable has no width/min-width property. Keep the table
    # non-compact and use a centered pull-quote as the consistent editorial
    # signature. The short 21-character dividers are intentional.
    divider="─────────────────────"
    blocks.append({"type":"paragraph","text":divider})
    blocks.append({"type":"pullquote","text":"Your next opportunity starts here. 💼"})
    blocks.append({"type":"paragraph","text":divider})
    tags=" ".join(job_hashtags(job))
    if tags:
        blocks.append({"type":"paragraph","text":tags})
    # Channel identity is displayed as a username while the actual channel URL
    # stays behind the link, so no raw URL is exposed in the post.
    blocks.append({"type":"paragraph","text":{"type":"bold","text":_rich_url("Career News","https://t.me/CareerNewsroom")}})

    source=safe_text(job.get("source","Source"))
    source_url=safe_text(job.get("source_url"))
    footer=["Source: "]
    footer.append(_rich_url(source,source_url) if source_url else source)
    blocks.append({"type":"footer","text":footer})
    return blocks


def rich_blocks_visible_length(blocks):
    def text_len(value):
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(text_len(x) for x in value)
        if isinstance(value, dict):
            return text_len(value.get("text", ""))
        return 0
    total = 0
    for block in blocks:
        total += text_len(block)
        for row in block.get("cells", []) if isinstance(block, dict) else []:
            for cell in row:
                total += text_len(cell)
    return total


def dynamic_rich_html(job):
    lines=[
        html.escape("📣 "+display_value(job.get("title")),quote=False),
        html.escape("🏢 "+display_value(job.get("company")),quote=False),
        "JOB SNAPSHOT",
    ]
    for label,value in job_snapshot_rows(job):
        lines.append(f"{_field_icon(label)} {label}: {value}")
    lines.append(" ".join(job_hashtags(job)))
    lines.append(f"Source: {html.escape(job.get('source','Source'),quote=False)}")
    return "\n".join(lines)


def plain_job_text(job):
    lines=["📣 "+smart_title_case(display_value(job.get("title"))),"🏢 "+(smart_title_case(display_value(job.get("company"))) or ("Government Organization" if job.get("is_government") else "")),"","JOB SNAPSHOT"]
    for label,value in job_snapshot_rows(job):
        lines.append(f"{_field_icon(label)} {label}: {value}")
    tags=" ".join(job_hashtags(job))
    if tags: lines.extend(["",tags])
    source=job.get("source","Source")
    lines.append(f"Source: {source}")
    return "\n".join(lines)


def fit_rich_blocks(job):
    blocks=rich_message_blocks(job)
    if rich_blocks_visible_length(blocks)<=MAX_RICH_CHARACTERS:
        return blocks
    candidate=dict(job)
    for key,limit in (("title",120),("company",80),("location",42),("education",42),("experience",28),("salary",44),("application_start",20),("application_end",20)):
        if candidate.get(key): candidate[key]=trim_source_text(candidate[key],limit)
    return rich_message_blocks(candidate)


# ============================================================
# SOURCE DIAGNOSTICS
# ============================================================

def _probe_get(url, params=None, timeout=10, json_expected=False):
    started=time.monotonic(); probe=requests.Session(); probe.headers.update(HEADERS)
    try:
        response=probe.get(url,params=params,timeout=timeout,allow_redirects=True); payload=None
        if json_expected:
            try: payload=response.json()
            except Exception: payload=None
        return {"ok":response.ok,"status":response.status_code,"elapsed":round(time.monotonic()-started,2),"payload":payload,"bytes":len(response.content or b"")}
    except Exception as exc:
        return {"ok":False,"status":0,"elapsed":round(time.monotonic()-started,2),"error":str(exc)}

def source_test():
    print("CAREER NEWS V1 SOURCE TEST")
    print(f"curl_cffi: {'available' if curl_cffi else 'MISSING'}")
    print(f"Jina fallback: {'enabled' if JINA_ENABLED else 'disabled'}")
    print(f"Fingerprints: {', '.join(CURL_IMPERSONATES)}")
    tel=_probe_get(TELETALK_API_URL,params={"searchKeyword":""},timeout=TELETALK_API_TIMEOUT,json_expected=True)
    if tel.get("ok"):
        print(f"Teletalk API: OK | jobs={len(_teletalk_records(tel.get('payload') or {}))} | time={tel['elapsed']}s")
    else:
        print(f"Teletalk API: FAIL | status={tel.get('status')} | error={tel.get('error','')}")

    cid=next(iter(BDBJOBS_CATEGORIES)); url=_absolute_category_url(cid); started=time.monotonic()
    fetched=_fetch_source_document(url,timeout=DISCOVERY_TIMEOUT,referer=BDJOBS_LISTING_URL); elapsed=round(time.monotonic()-started,2)
    if not fetched:
        print(f"Bdjobs category {cid}: FAIL | time={elapsed}s")
        return
    candidates=_bdjobs_listing_candidates(fetched.get("text",""),fetched.get("url") or url,cid,BDBJOBS_CATEGORIES[cid]["name"])
    rich_candidates=sum(
        1 for c in candidates
        if c.get("company")
        and sum(1 for k in ("location","education","experience","deadline") if (c.get("listing_fields") or {}).get(k)) >= 3
    )
    print(
        f"Bdjobs category {cid}: OK | backend={fetched.get('backend')} | status={fetched.get('status')} | "
        f"candidates={len(candidates)} | listing_rich={rich_candidates} | time={elapsed}s"
    )
    if fetched.get("cloudflare"): print("  Cloudflare: detected")
    if not candidates:
        print("Bdjobs detail: SKIPPED | no job candidate on category page")
        raise RuntimeError("Bdjobs category returned no job candidates")
    if rich_candidates == 0:
        print("Bdjobs listing: INVALID | candidates found but listing metadata is sparse")
        raise RuntimeError("Bdjobs listing parser returned no rich candidates")
    else:
        sample=candidates[0]
        detail=_fetch_bdjobs_detail(sample)
        if detail:
            parsed=extract_job_fields(
                detail.get("text",""),
                detail.get("html",""),
                sample["url"],
                sample,
            )
            sample_company = sample.get("company") or (sample.get("listing_fields") or {}).get("company", "")
            sane_title = bool(parsed.get("title")) and len(parsed.get("title","")) <= 180 and "--tw-" not in parsed.get("title","")
            sane_company = bool(parsed.get("company") or sample_company) and len(parsed.get("company") or sample_company) <= 180 and "--tw-" not in (parsed.get("company") or sample_company)
            status = "OK" if sane_title and sane_company else "INVALID_PARSE"
            print(
                f"Bdjobs detail: {status} | id={sample.get('source_job_id','')} | "
                f"backend={detail.get('backend')} | quality={detail.get('detail_quality')} | "
                f"chars={len(detail.get('text',''))} | title={parsed.get('title') or sample.get('title')} | "
                f"company={parsed.get('company') or sample_company}"
            )
            if status != "OK":
                fallback=_listing_fallback_content(sample)
                print(f"Bdjobs detail fallback: {'AVAILABLE' if fallback else 'UNAVAILABLE'}")
        else:
            fallback=_listing_fallback_content(sample)
            print(f"Bdjobs detail: FALLBACK | id={sample.get('source_job_id','')} | listing_chars={len((fallback or {}).get('text',''))}")
    print("Production: Teletalk government + Bdjobs private")
    print("Discovery: category-first; no global-first 100-job path")
    print("Fallback: curl_cffi -> fingerprint rotation -> Jina -> listing preservation")

# ============================================================
# MAIN
# ============================================================

def _research_items_parallel(items):
    results=[]
    metrics={"attempted":len(items),"success":0,"detail_success":0,"detail_fallback":0,"failed":0,"government":0,"private":0,"private_failed":0,"government_failed":0}
    if not items:
        return results,metrics
    with ThreadPoolExecutor(max_workers=max(1, DETAIL_WORKERS)) as pool:
        futures={pool.submit(research_job,item):item for item in items}
        for future in as_completed(futures):
            item=futures[future]
            try:
                researched=future.result()
            except Exception as exc:
                logger.warning("RESEARCH worker failed | source=%s | id=%s | title=%s | error=%s", item.get("source",""), item.get("source_job_id",""), item.get("title",""), exc)
                researched=None
            if not researched:
                metrics["failed"]+=1
                if item.get("is_government"):
                    metrics["government_failed"]+=1
                else:
                    metrics["private_failed"]+=1
                continue
            metrics["success"]+=1
            if item.get("is_government"):
                metrics["government"]+=1
            else:
                metrics["private"]+=1
                quality=researched.get("detail_quality", "")
                if quality == "listing_fallback": metrics["detail_fallback"]+=1
                else: metrics["detail_success"]+=1
            researched["source_url"]=item.get("source_url") or item.get("url")
            researched["canonical"]=item.get("canonical") or canonical_url(item.get("url",""))
            researched["source"]=item.get("source") or source_name(item.get("url",""))
            researched["is_government"]=bool(item.get("is_government"))
            results.append(researched)
    return results,metrics

def _prepare_shortlists(discovered):
    unique=build_unique_job_pool(discovered)
    gov=[x for x in unique if x.get("is_government")]
    private=[x for x in unique if not x.get("is_government")]
    private.sort(key=lambda j:(
        -bba_mba_candidate_score(j),
        -posted_freshness_score({"posted_date":j.get("listing_posted")}),
        j.get("canonical","")
    ))
    gov.sort(key=lambda j:(
        -posted_freshness_score({"posted_date":j.get("listing_posted")}),
        -deadline_urgency_score({"deadline":j.get("listing_deadline")}),
        j.get("canonical","")
    ))

    # Reserve internship candidates before the ordinary private top-N cutoff so
    # internships cannot disappear simply because their listing text has less BBA detail.
    internships=[x for x in private if is_internship_job(x)]
    internships.sort(key=lambda j:(
        -posted_freshness_score({"posted_date":j.get("listing_posted")}),
        -bba_mba_candidate_score(j),
        j.get("canonical","")
    ))
    reserved_internships=internships[:min(INTERNSHIP_DETAIL_TARGET, len(internships))]
    reserved_keys={x.get("canonical") for x in reserved_internships}
    ordinary=[x for x in private if x.get("canonical") not in reserved_keys]
    private_short=(reserved_internships + ordinary)[:PRIVATE_DETAIL_TARGET]
    logger.info("INTERNSHIP DISCOVERED | candidates=%d reserved_for_detail=%d minimum=%d", len(internships), len(reserved_internships), MIN_INTERNSHIP_POSTS_PER_RUN)
    return gov[:GOVERNMENT_DISCOVERY_TARGET], private_short


def run(*, dry_run=False, print_ranking=False):
    started=time.monotonic()
    logger.info("CAREER NEWS V1 | category-first | target=%d max=%d", TARGET_STORIES_PER_RUN, MAX_STORIES_PER_RUN)
    prune_state()
    discovered=discover_all()
    gov_items,private_items=_prepare_shortlists(discovered)
    logger.info("===== PRIVATE FUNNEL =====")
    logger.info("PRIVATE DISCOVERY | %d", sum(1 for x in discovered if not x.get("is_government")))
    logger.info("PRIVATE TOP DETAIL | %d", len(private_items))
    logger.info("GOVERNMENT SHORTLIST | %d", len(gov_items))

    researched,research_metrics=_research_items_parallel(gov_items+private_items)
    logger.info("PRIVATE DETAIL SUCCESS | %d", research_metrics["detail_success"])
    logger.info("PRIVATE DETAIL FALLBACK | %d", research_metrics["detail_fallback"])
    logger.info("PRIVATE DETAIL FAILED | %d", research_metrics["private_failed"])

    verified=[]
    gate_counts={}
    for job in researched:
        ok,reason=deterministic_job_gate(job)
        gate_counts[reason]=gate_counts.get(reason,0)+1
        if not ok:
            logger.info("DROP gate | reason=%s | source=%s | id=%s | title=%s", reason, job.get("source",""), job.get("source_job_id",""), job.get("title",""))
            job["pipeline_status"]="rejected"
            job["rejection_reason"]=reason
            continue
        save_job_to_queue(job)
        if not candidate_already_posted(job):
            verified.append(job)
        else:
            job["pipeline_status"]="already_posted"
    save_state(STATE)

    unique=build_unique_job_pool(verified)
    government_jobs=[j for j in unique if j.get("is_government")]
    private_jobs=[j for j in unique if not j.get("is_government")]

    # Snapshot integrity is an eligibility gate, not a post-selection cleanup step.
    # Otherwise a sparse selected record could be removed after the source-mix
    # selector has already consumed the 10 private / 3 government slots, leaving
    # the final run below quota with no chance to refill.
    snapshot_private, snapshot_government, snapshot_rejected = split_snapshot_eligible(private_jobs + government_jobs)
    for job in private_jobs + government_jobs:
        if job.get("pipeline_status") == "rejected":
            logger.info(
                "DROP snapshot | reason=%s | source=%s | id=%s | title=%s",
                job.get("rejection_reason", "unknown"), job.get("source", ""), job.get("source_job_id", ""), job.get("title", ""),
            )

    logger.info(
        "SNAPSHOT ELIGIBILITY | private=%d government=%d rejected=%d",
        len(snapshot_private), len(snapshot_government), snapshot_rejected,
    )
    logger.info("PRIVATE GATE PASSED | %d", len(snapshot_private))
    ranked_private=rank_jobs(snapshot_private)
    logger.info("PRIVATE SCORED | %d", len(ranked_private))
    eligible_government=snapshot_government
    selected=select_final_jobs(ranked_private, eligible_government)
    internship_final=sum(1 for j in selected if not j.get("is_government") and is_internship_job(j))
    if internship_final < MIN_INTERNSHIP_POSTS_PER_RUN:
        logger.error("INTERNSHIP QUOTA SHORTFALL | selected=%d required=%d | qualifying_internship_pool=%d", internship_final, MIN_INTERNSHIP_POSTS_PER_RUN, sum(1 for j in ranked_private if is_internship_job(j)))
    else:
        logger.info("INTERNSHIP QUOTA | selected=%d/%d minimum", internship_final, MIN_INTERNSHIP_POSTS_PER_RUN)
    selected=translate_government_jobs(selected)
    selected=[j for j in selected if j.get("is_government") or not private_experience_too_high(j)][:MAX_STORIES_PER_RUN]
    checked=[]
    for job in selected:
        ok_snapshot, snapshot_reason=snapshot_integrity(job)
        if not ok_snapshot:
            # Defensive second check after translation. This should be rare because
            # eligibility was already enforced before selection.
            logger.warning("PUBLISH CANDIDATE REJECTED | snapshot=%s | source=%s | id=%s | title=%s", snapshot_reason, job.get("source",""), job.get("source_job_id",""), job.get("title",""))
            job["pipeline_status"]="rejected"
            job["rejection_reason"]=snapshot_reason
            continue
        checked.append(job)
    selected=checked
    for job in selected:
        for key in ("title","company","location","salary","experience","education","vacancy","employment_type","workplace","age","application_method","selection_process","category"):
            if _contains_bengali(job.get(key,"")):
                job[key]=fallback_government_translate(job.get(key,""))
        job["title"]=english_display_text(job.get("title","")); job["company"]=english_display_text(job.get("company",""))

    private_final=sum(1 for j in selected if not j.get("is_government"))
    gov_final=sum(1 for j in selected if j.get("is_government"))
    logger.info("FINAL SELECTED=%d | gov=%d private=%d | discovered=%d | pre_publish=%.1fs", len(selected), gov_final, private_final, len(discovered), time.monotonic()-started)
    logger.info(
        "PUBLICATION QUOTA | private=%d/%d minimum | government=%d/%d minimum | total=%d/%d maximum",
        private_final, MIN_PRIVATE_POSTS_PER_RUN, gov_final, MIN_GOVERNMENT_POSTS_PER_RUN,
        len(selected), MAX_STORIES_PER_RUN,
    )
    if private_final < MIN_PRIVATE_POSTS_PER_RUN:
        logger.error(
            "PRIVATE QUOTA SHORTFALL | selected=%d required=%d | qualifying private pool=%d",
            private_final, MIN_PRIVATE_POSTS_PER_RUN, len(ranked_private),
        )
    if gov_final < MIN_GOVERNMENT_POSTS_PER_RUN:
        logger.error(
            "GOVERNMENT QUOTA SHORTFALL | selected=%d required=%d | eligible government pool=%d",
            gov_final, MIN_GOVERNMENT_POSTS_PER_RUN, len(select_government_jobs(government_jobs)),
        )
    logger.info("FUNNEL | discovered=%d researched=%d gate_passed=%d snapshot_eligible_private=%d snapshot_eligible_gov=%d private_ranked=%d final_private=%d", len(discovered), len(researched), len(verified), len(snapshot_private), len(snapshot_government), len(ranked_private), private_final)
    logger.info("FUNNEL DETAIL | private_attempted=%d private_success=%d private_fallback=%d private_failed=%d", len(private_items), research_metrics["private"], research_metrics["detail_fallback"], research_metrics["private_failed"])
    snapshot_counts=[snapshot_field_quality(j) for j in selected]
    logger.info("SNAPSHOT QUALITY | selected=%d | >=5_fields=%d | avg_fields=%.1f", len(snapshot_counts), sum(1 for n in snapshot_counts if n>=5), (sum(snapshot_counts)/len(snapshot_counts) if snapshot_counts else 0.0))

    if print_ranking:
        print("=== CAREER NEWS V1 PRIVATE RANKING ===")
        for i,job in enumerate(ranked_private[:25],1):
            print(f"{i}. {job.get('final_score',0):.1f} | {job.get('title','')} | {job.get('company','')} | {job.get('career_category','')}")

    if private_items and research_metrics["private"] == 0 and research_metrics["private_failed"] > 0:
        logger.error("PRIVATE PIPELINE HEALTH | no private research records survived. Government lane may continue, but private lane is unhealthy.")

    if dry_run:
        logger.info("DRY RUN | selected=%d | Telegram not contacted",len(selected))
        STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION; save_state(STATE)
        return {"selected":selected,"published":0,"metrics":{**research_metrics,"gate_counts":gate_counts}}

    published=0
    for index,job in enumerate(selected,1):
        blocks=fit_rich_blocks(job)
        if rich_blocks_visible_length(blocks)>MAX_RICH_CHARACTERS:
            logger.warning("PUBLISH skipped | too_long | id=%s | title=%s",job.get("source_job_id",""),job.get("title","")); continue
        store_selected_event(job,published=False)
        result=send_rich_text(blocks,job)
        if not result.get("ok"):
            result=send_bot_api_text_fallback(job,plain_job_text(job))
        if result.get("ok"):
            published+=1
            message=result.get("result",{}); message_id=message.get("message_id") if isinstance(message,dict) else None
            canonical=canonical_url(job.get("source_url","")); POSTED_URLS.add(canonical); save_posted_url(canonical)
            item=STATE["queue"].get(job.get("canonical"))
            if item:
                item.update({"status":"posted","posted_at":now_iso(),"pipeline_version":PIPELINE_VERSION,"deterministic_score":job.get("deterministic_score",0),"ai_score":job.get("ai_score",0),"final_score":job.get("final_score",job.get("government_rank_score",0))})
            store_selected_event(job,published=True,message_id=message_id)
            STATE["recent_titles"].append(normalize_title(job.get("title","")))
        else:
            logger.error("PUBLISH failed | source=%s | id=%s | title=%s",job.get("source",""),job.get("source_job_id",""),job.get("title",""))
        save_state(STATE)
        if POST_DELAY_SECONDS>0 and index<len(selected): time.sleep(POST_DELAY_SECONDS)
    STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION; save_state(STATE)
    logger.info("Finished Career News V1. Published=%d | elapsed=%.1fs",published,time.monotonic()-started)
    return {"selected":selected,"published":published,"metrics":{**research_metrics,"gate_counts":gate_counts}}


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    assert PIPELINE_VERSION == "Career News V1"
    assert STATE_FORMAT_VERSION == 5
    assert MAX_STORIES_PER_RUN == 20
    assert TARGET_STORIES_PER_RUN == 15
    assert MIN_PRIVATE_POSTS_PER_RUN == 10
    assert MIN_GOVERNMENT_POSTS_PER_RUN == 3
    assert MIN_PRIVATE_POSTS_PER_RUN + MIN_GOVERNMENT_POSTS_PER_RUN <= MAX_STORIES_PER_RUN
    assert QUALITY_FLOOR == 65
    assert MAX_POST_AGE_DAYS == 5
    assert len(BDBJOBS_CATEGORIES) == 14
    assert is_bdjobs_job_url("https://jobs.bdjobs.com/jobdetails.asp?id=1534666")
    assert is_bdjobs_job_url("https://bdjobs.com/h/jobs/1534666")
    assert not is_bdjobs_job_url("https://bdjobs.com/h/jobs")
    route_fixture = '<html><body><a href="https://bdjobs.com/h/jobs/987654/management-trainee">Management Trainee</a></body></html>'
    route_candidates = _bdjobs_listing_candidates(route_fixture, "https://bdjobs.com/h/jobs/?fcatId=1", 1, "Accounting / Finance")
    assert route_candidates and route_candidates[0]["source_job_id"] == "987654"
    assert is_teletalk_url("https://alljobs.teletalk.com.bd/")
    assert is_cloudflare_response(403,{"cf-mitigated":"challenge"},"")
    assert is_cloudflare_response(200,{},"<title>Just a moment...</title>")
    assert not is_cloudflare_response(200,{},"<html>normal</html>")
    assert (JINA_PREFIX+request_safe_url("https://example.com/job?id=1")).startswith("https://r.jina.ai/https://example.com")
    jina_fixture = "## Customer Support Executive E-Commerce\\n🏢 name-share-details.gif\\n[![Image 18](https://bdjobs.com/h/images/matching_lock_en.webp)](https://bdjobs.com/h/)\\nJob Summary\\nCompany Name\\nChino Carts\\nExperience\\nAt Least 1 Year\\nVacancy\\n5"
    cleaned_jina = clean_reader_markdown(jina_fixture)
    assert "name-share-details.gif" not in cleaned_jina and "matching_lock_en.webp" not in cleaned_jina and "[](" not in cleaned_jina

    shell_fixture = """<html><head><style>.foo{--tw-gradient-to-position:}</style></head>
    <body><app-root></app-root><script>console.log("app shell")</script></body></html>"""
    ok, reason = _looks_like_job_document(shell_fixture, detail=True)
    assert not ok and reason in {"empty_visible_text", "bdjobs_application_shell", "thin_or_non_job_page:0"}

    fixture='''<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee","datePosted":"2026-09-18","validThrough":"2026-10-18","hiringOrganization":{"name":"Example Bank"},"jobLocation":{"address":{"addressLocality":"Dhaka","addressCountry":"Bangladesh"}},"employmentType":"FULL_TIME"}</script></head><body><h1>Management Trainee</h1><p>Company Name: Example Bank</p><p>Vacancy: 10</p><p>Education: Bachelor of Business Administration (BBA) or MBA</p><p>Experience: Freshers are encouraged to apply.</p><p>Salary: Tk. 35000 - 45000</p><p>Employment Status: Full Time</p><p>Job Work Place: Work at Office</p><p>Age: 18 to 30 years</p><p>Application: Online</p><p>Application Deadline: 18 Oct 2026</p></body></html>'''
    fake={"title":"Management Trainee","url":"https://jobs.bdjobs.com/jobdetails.asp?id=123","canonical":canonical_url("https://jobs.bdjobs.com/jobdetails.asp?id=123"),"source":"Bdjobs","discovery":"self_test"}
    text_source=_text_from_html(fixture); fields=extract_job_fields(text_source,fixture,fake["url"],fake); fields.update({"raw_text":text_source,"apply_url":"","canonical":fake["canonical"],"audience_pre_score":job_family_score(fields["title"],text_source)})
    fields["bba_mba_target_score"]=bba_mba_candidate_score(fields)
    assert fields["title"] == "Management Trainee" and fields["company"] == "Example Bank"
    assert "BBA" in fields["education"] and "MBA" in fields["education"]
    assert fields["experience"] == "Freshers" and fields["vacancy"] == "10" and fields["deadline"] == "2026-10-18"
    score,components=private_rank_score(fields); assert 0 <= score <= 100 and sum(components.values()) == score
    too_high=dict(fields,experience="5 to 8 years"); ok,reason=deterministic_job_gate(too_high); assert not ok and reason=="experience_above_3_years"
    allowed=dict(fields,experience="2 to 3 years"); ok,reason=deterministic_job_gate(allowed); assert ok and reason=="ok_bba_mba_target"

    listing='''<html><body><div><a href="/jobdetails.asp?id=101">Accounts Executive</a><span>Published: 2026-09-19 Deadline: 2026-10-01 Dhaka</span></div><div><a href="/jobdetails.asp?id=102">Software Engineer</a><span>Published: 2026-09-19 Deadline: 2026-10-01 Dhaka</span></div></body></html>'''
    candidates=_bdjobs_listing_candidates(listing,"https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=1",1,"Accounting / Finance"); assert {x["source_job_id"] for x in candidates} == {"101","102"}
    tel=_teletalk_record_fields({"job_primary_id":"TL-1001","job_title":"Accounts Assistant","org_name":"Example Government Department","vacancy":"12","deadline_date":"2026-10-10","application_site_url":"https://example.teletalk.com.bd/"}); assert tel["source"]=="Teletalk" and tel["source_job_id"]=="TL-1001"
    a={"source":"Bdjobs","source_job_id":"1","title":"Marketing Executive","company":"Example Ltd.","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=1","posted_date":"2026-09-19"}; b={"source":"Bdjobs","source_job_id":"","title":"Executive - Marketing","company":"Example Limited","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=2","posted_date":"2026-09-19"}; assert likely_same_job(a,b)
    c={"source":"Bdjobs","source_job_id":"10","title":"Assistant Manager","company":"Example Ltd.","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=10","posted_date":"2026-09-19"}; d=dict(c,source_job_id="11",source_url="https://jobs.bdjobs.com/jobdetails.asp?id=11"); assert not likely_same_job(c,d)
    listing_item={
        "title":"Accounts Executive", "url":"https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1",
        "canonical":canonical_url("https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1"), "source":"Bdjobs",
        "source_job_id":"777001", "category_id":1, "category_name":"Accounting / Finance",
        "listing_posted":"2026-09-19", "listing_deadline":"2026-10-01",
        "listing_fields":{"company":"Example Finance Ltd.","experience":"1 to 2 years","education":"BBA","deadline":"2026-10-01","location":"Dhaka","salary":"Tk. 30,000","vacancy":"3"},
        "excerpt":"Accounts Executive Example Finance Ltd. Dhaka Experience required: 1 to 2 year(s) Deadline: Oct 1, 2026 Education required: BBA Vacancy: 3",
    }
    original_retrieve=retrieve_job_content
    try:
        globals()["retrieve_job_content"] = lambda item: _listing_fallback_content(item)
        fallback_job=research_job(listing_item)
    finally:
        globals()["retrieve_job_content"] = original_retrieve
    assert fallback_job["company"] == "Example Finance Ltd." and fallback_job["education"] == "BBA"
    assert fallback_job["experience"] == "1 to 2 years" and fallback_job["vacancy"] == "3"
    assert fallback_job["detail_quality"] == "listing_fallback"
    assert deterministic_job_gate(fallback_job)[0]

    original_detail= _fetch_bdjobs_detail
    try:
        globals()["_fetch_bdjobs_detail"] = lambda item: None
        assert retrieve_job_content(listing_item)["detail_quality"] == "listing_fallback"
    finally:
        globals()["_fetch_bdjobs_detail"] = original_detail

    logger.info("Career News V1 self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Career News V1")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--source-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-ranking", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.source_test:
        source_test()
    else:
        run(dry_run=args.dry_run, print_ranking=args.print_ranking)
