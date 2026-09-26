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
import math
from collections import deque

try:
    import curl_cffi
except ImportError:
    curl_cffi = None
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin, urlunparse, quote, unquote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

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
SCRAPLING_BROWSER_TIMEOUT = int(os.environ.get("SCRAPLING_BROWSER_TIMEOUT", "60000"))
SCRAPLING_BROWSER_WAIT_MS = int(os.environ.get("SCRAPLING_BROWSER_WAIT_MS", "750"))
SCRAPLING_BROWSER_WAIT_SELECTOR = os.environ.get("SCRAPLING_BROWSER_WAIT_SELECTOR", "app-summary #allSection").strip()
SCRAPLING_BROWSER_NETWORK_IDLE = os.environ.get("SCRAPLING_BROWSER_NETWORK_IDLE", "1").strip().lower() not in {"0", "false", "no", "off"}
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
TELEGRAM_PROMO_URL = (os.environ.get("TELEGRAM_PROMO_URL") or "https://t.me/CareerNewsroom").strip()
DEADLINE_SWEEP_MAX_UPDATES_PER_RUN = max(1, int(os.environ.get("DEADLINE_SWEEP_MAX_UPDATES_PER_RUN", "100")))
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
PIPELINE_VERSION = "CareerNewsroom V1"
STATE_FORMAT_VERSION = 7
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

TARGET_STORIES_PER_RUN = int(os.environ.get("TARGET_STORIES_PER_RUN", "19"))
MAX_STORIES_PER_RUN = int(os.environ.get("MAX_STORIES_PER_RUN", "25"))
MIN_PRIVATE_POSTS_PER_RUN = int(os.environ.get("MIN_PRIVATE_POSTS_PER_RUN", "0"))
MIN_GOVERNMENT_POSTS_PER_RUN = int(os.environ.get("MIN_GOVERNMENT_POSTS_PER_RUN", "0"))
QUALITY_FLOOR = float(os.environ.get("QUALITY_FLOOR", "65"))
PRIVATE_MIN_FILL_SCORE = float(os.environ.get("PRIVATE_MIN_FILL_SCORE", "58"))
PRIVATE_HARD_FILL_SCORE = float(os.environ.get("PRIVATE_HARD_FILL_SCORE", "55"))
PRIVATE_MIN_INFORMATION_QUALITY = int(os.environ.get("PRIVATE_MIN_INFORMATION_QUALITY", "3"))
PRIVATE_HARD_MIN_INFORMATION_QUALITY = int(os.environ.get("PRIVATE_HARD_MIN_INFORMATION_QUALITY", "3"))
MAX_GOVERNMENT_POSTS_PER_RUN = int(os.environ.get("MAX_GOVERNMENT_POSTS_PER_RUN", "5"))
PRIVATE_TARGET_PER_RUN = int(os.environ.get("PRIVATE_TARGET_PER_RUN", "10"))
GOVERNMENT_TARGET_PER_RUN = int(os.environ.get("GOVERNMENT_TARGET_PER_RUN", "5"))
INTERNSHIP_TARGET_PER_RUN = int(os.environ.get("INTERNSHIP_TARGET_PER_RUN", "4"))
INTERNSHIP_BDJOBS_TARGET = int(os.environ.get("INTERNSHIP_BDJOBS_TARGET", "2"))
INTERNSHIP_BDJOBSLIVE_TARGET = int(os.environ.get("INTERNSHIP_BDJOBSLIVE_TARGET", "2"))
EXTRA_TARGET_PER_RUN = int(os.environ.get("EXTRA_TARGET_PER_RUN", "6"))
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "1.0"))

MAX_POST_AGE_DAYS = int(os.environ.get("MAX_POST_AGE_DAYS", "3"))
PRIVATE_DISCOVERY_TARGET = int(os.environ.get("PRIVATE_DISCOVERY_TARGET", "180"))
PRIVATE_DISCOVERY_MAX = int(os.environ.get("PRIVATE_DISCOVERY_MAX", "200"))
PRIVATE_FAST_RANK_TARGET = int(os.environ.get("PRIVATE_FAST_RANK_TARGET", "80"))
PRIVATE_DETAIL_TARGET = int(os.environ.get("PRIVATE_DETAIL_TARGET", "60"))
INTERNSHIP_DETAIL_TARGET = int(os.environ.get("INTERNSHIP_DETAIL_TARGET", "12"))
INTERNSHIP_AI_TARGET = int(os.environ.get("INTERNSHIP_AI_TARGET", "8"))
MIN_INTERNSHIP_POSTS_PER_RUN = int(os.environ.get("MIN_INTERNSHIP_POSTS_PER_RUN", "0"))
PRIVATE_SNAPSHOT_MIN_FIELDS = int(os.environ.get("PRIVATE_SNAPSHOT_MIN_FIELDS", "4"))
INTERNSHIP_SNAPSHOT_MIN_FIELDS = int(os.environ.get("INTERNSHIP_SNAPSHOT_MIN_FIELDS", "3"))
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
MIN_PRIVATE_AGE_YEARS = int(os.environ.get("MIN_PRIVATE_AGE_YEARS", "18"))
MAX_PRIVATE_AGE_YEARS = int(os.environ.get("MAX_PRIVATE_AGE_YEARS", "30"))
ACTIVE_JOB_RETENTION_DAYS = int(os.environ.get("ACTIVE_JOB_RETENTION_DAYS", "60"))
FUTURE_TOLERANCE_MINUTES = int(os.environ.get("FUTURE_TOLERANCE_MINUTES", "20"))
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000
DETAIL_MIN_TEXT_CHARS = int(os.environ.get("DETAIL_MIN_TEXT_CHARS", "220"))
DETAIL_CACHE = {}
DETAIL_CACHE_TTL_SECONDS = int(os.environ.get("DETAIL_CACHE_TTL_SECONDS", "900"))
LAST_DISCOVERY_HEALTH = {}

BDJOBS_LISTING_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
BDJOBS_LEGACY_LISTING_URL = "https://jobs.bdjobs.com/jobsearch.asp"
BDJOBS_DETAIL_BASE = "https://jobs.bdjobs.com/jobdetails.asp?id="
BDJOBS_INTERNSHIP_URL = os.environ.get("BDJOBS_INTERNSHIP_URL", "https://bdjobs.com/h/jobs?lang=en&JobType=intern")
BDJOBSLIVE_INTERNSHIP_URL = os.environ.get("BDJOBSLIVE_INTERNSHIP_URL", "https://www.bdjobslive.com/bdjobs-circular/internship-opportunity")
BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]
BDJOBSLIVE_DOMAIN = "bdjobslive.com"
PRIVATE_JOB_DOMAINS = [*BDJOBS_DOMAINS, BDJOBSLIVE_DOMAIN]
BDJOBSLIVE_ENABLED = os.environ.get("BDJOBSLIVE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
BDJOBSLIVE_CATEGORY_PAGE_LIMIT = max(1, int(os.environ.get("BDJOBSLIVE_CATEGORY_PAGE_LIMIT", "2")))
BDJOBSLIVE_CATEGORY_CAP = max(1, int(os.environ.get("BDJOBSLIVE_CATEGORY_CAP", "10")))
BDJOBSLIVE_DISCOVERY_MAX = max(1, int(os.environ.get("BDJOBSLIVE_DISCOVERY_MAX", "120")))
BDJOBSLIVE_TIMEOUT = int(os.environ.get("BDJOBSLIVE_TIMEOUT", "18"))
BDJOBSLIVE_BROWSER_TIMEOUT = int(os.environ.get("BDJOBSLIVE_BROWSER_TIMEOUT", "15000"))
BDJOBSLIVE_BROWSER_WAIT_MS = int(os.environ.get("BDJOBSLIVE_BROWSER_WAIT_MS", "2200"))
BDJOBSLIVE_BROWSER_DETAIL_LIMIT = int(os.environ.get("BDJOBSLIVE_BROWSER_DETAIL_LIMIT", "60"))
BDJOBSLIVE_BROWSER_LISTING_LIMIT = int(os.environ.get("BDJOBSLIVE_BROWSER_LISTING_LIMIT", "28"))
BDJOBSLIVE_INDEX_FALLBACK_URL = os.environ.get(
    "BDJOBSLIVE_INDEX_FALLBACK_URL", "https://www.bdjobslive.com/"
).strip()
BDJOBSLIVE_INDEX_FALLBACK_CAP = max(1, int(os.environ.get("BDJOBSLIVE_INDEX_FALLBACK_CAP", "60")))
# Detail research is adaptive, not a fixed BDJobs Live percentage. Reserve a small
# number of fresh candidates from each private source when both have supply, then
# fill the remaining budget from the combined freshness-ranked pool.
PRIVATE_DETAIL_SOURCE_RESERVE = max(0, int(os.environ.get("PRIVATE_DETAIL_SOURCE_RESERVE", "4")))
BDJOBSLIVE_BROWSER_LONG_RETRY_WAIT_MS = max(8000, int(os.environ.get("BDJOBSLIVE_BROWSER_LONG_RETRY_WAIT_MS", "12000")))
BDJOBSLIVE_BROWSER_LONG_RETRY_TIMEOUT = max(15000, int(os.environ.get("BDJOBSLIVE_BROWSER_LONG_RETRY_TIMEOUT", "30000")))
BDJOBSLIVE_BROWSER_FETCH_COUNT = 0
BDJOBSLIVE_BROWSER_LOCK = threading.Lock()
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
    "bdjobslive.com": "BDJobs Live",
    "alljobs.teletalk.com.bd": "Teletalk",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("career-news-v2")

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


def is_bdjobslive_job_url(url):
    parsed = urlparse(safe_text(url))
    if not is_domain_allowed(url, [BDJOBSLIVE_DOMAIN]):
        return False
    return bool(re.match(r"^/bdjobs-details/[^?#/]+(?:/)?$", parsed.path, flags=re.I))


def is_vacancy_url(url):
    return is_bdjobs_job_url(url) or is_bdjobslive_job_url(url) or is_teletalk_url(url)


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
    value = normalize_title(text)
    value = re.sub(r"\b(private limited|pvt limited|pvt ltd|private ltd|limited|ltd|plc|incorporated|inc|company|co)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalized_identity_title(text):
    value = normalize_title(text)
    value = re.sub(r"\bsr\b", "senior", value)
    value = re.sub(r"\bjr\b", "junior", value)
    value = re.sub(r"\basst\b", "assistant", value)
    return re.sub(r"\s+", " ", value).strip()


def _job_identity_text(job):
    return "|".join([
        _normalized_identity_title(job.get("title", "")),
        _normalized_company(job.get("company", "")),
        normalize_title(job.get("location", "")),
    ])


def job_event_key(job):
    # Prefer a source-native immutable job ID when the source provides one.
    # Cross-source content identity deliberately ignores the source domain so a
    # Bdjobs / BDJobs Live mirror can collapse into the same event.
    source_id = safe_text(job.get("source_job_id"))
    source = safe_text(job.get("source")).lower()
    if source_id and source:
        return hashlib.sha1(f"{source}:{source_id}".encode("utf-8")).hexdigest()[:24]
    return hashlib.sha1(_job_identity_text(job).encode("utf-8")).hexdigest()[:24]


def _same_application_target(a, b):
    ua = canonical_url(a.get("apply_url", ""))
    ub = canonical_url(b.get("apply_url", ""))
    return bool(ua and ub and ua == ub)


def _identity_date_close(a, b, days=45):
    pa = parse_datetime(a.get("posted_date") or a.get("listing_posted"))
    pb = parse_datetime(b.get("posted_date") or b.get("listing_posted"))
    if not pa or not pb:
        return True
    return abs((pa - pb).total_seconds()) <= days * 86400


def likely_same_job(a, b):
    # Source-native IDs remain authoritative within the same source. Different IDs
    # on the same source must not be collapsed merely from title/company similarity.
    source_a = safe_text(a.get("source")).lower()
    source_b = safe_text(b.get("source")).lower()
    id_a = safe_text(a.get("source_job_id"))
    id_b = safe_text(b.get("source_job_id"))
    same_source = source_a == source_b and bool(source_a)
    if same_source and id_a and id_b and id_a != id_b:
        return False

    source_url_a = canonical_url(a.get("source_url", ""))
    source_url_b = canonical_url(b.get("source_url", ""))
    if source_url_a and source_url_a == source_url_b:
        return True

    title_a = _normalized_identity_title(a.get("title", ""))
    title_b = _normalized_identity_title(b.get("title", ""))
    company_a = _normalized_company(a.get("company", ""))
    company_b = _normalized_company(b.get("company", ""))
    title_sim = title_similarity(a.get("title", ""), b.get("title", "")) if title_a and title_b else 0.0
    company_sim = SequenceMatcher(None, company_a, company_b).ratio() if company_a and company_b else 0.0
    location_sim = title_similarity(a.get("location", ""), b.get("location", "")) if a.get("location") and b.get("location") else 0.0
    close_date = _identity_date_close(a, b)

    # A shared application/ATS endpoint alone is NOT proof of duplication. A single
    # government circular can contain several distinct roles under one application URL.
    # Use the endpoint as a strong supporting signal only when role identity also agrees.
    if _same_application_target(a, b) and close_date:
        if title_a and title_b and title_sim >= 0.92:
            if not company_a or not company_b or company_sim >= 0.88:
                return True
        if title_a and title_b and company_a and company_b and title_sim >= 0.85 and company_sim >= 0.95:
            return True

    if not title_a or not title_b or not company_a or not company_b:
        return False

    # Strong cross-source mirror: same normalized company + same normalized title.
    # Require a reasonable recency window when dates are available so legitimate
    # reposts are not swallowed.
    if title_a == title_b and company_sim >= 0.92 and close_date:
        if (not same_source or not id_a or not id_b) and (
            not a.get("location") or not b.get("location") or location_sim >= 0.70
        ):
            return True

    # Near-identical cross-source mirrors with minor wording/punctuation differences.
    if title_sim >= 0.95 and company_sim >= 0.90 and close_date:
        if (not same_source or not id_a or not id_b) and (
            not a.get("location") or not b.get("location") or location_sim >= 0.65
        ):
            return True

    return False


# ============================================================
# STATE
# ============================================================

SCHEDULE_SESSION_MAP = {
    "5 10 * * *": "morning",
    "20 10 * * *": "morning",
    "35 10 * * *": "morning",
    "5 17 * * *": "afternoon",
    "20 17 * * *": "afternoon",
    "35 17 * * *": "afternoon",
}
SCHEDULE_GUARD_RETENTION_DAYS = 14
SCHEDULE_LOCK_MAX_MINUTES = max(20, int(os.environ.get("SCHEDULE_LOCK_MAX_MINUTES", "45")))
AI_DISABLED_FOR_RUN = False
AI_DISABLE_REASON = ""


def scheduled_session_context():
    """Return the scheduled session key for a GitHub scheduled trigger, else None."""
    if safe_text(os.getenv("GITHUB_EVENT_NAME")).lower() != "schedule":
        return None
    cron = safe_text(os.getenv("CAREER_SCHEDULE_CRON"))
    slot = SCHEDULE_SESSION_MAP.get(cron)
    if not slot:
        logger.warning("SCHEDULE GUARD | unknown schedule trigger=%r; continuing without a guard", cron)
        return None
    today = datetime.now(BD_TZ).date().isoformat()
    return {
        "key": f"{today}:{slot}",
        "slot": slot,
        "date": today,
        "cron": cron,
    }


def begin_scheduled_session():
    """Claim the current scheduled session. Return True when it should be skipped.

    The workflow can persist the claim before the expensive crawl. When that
    persisted claim is present in the same workflow (CAREER_SCHEDULE_CLAIMED=1),
    allow the actual run to proceed without re-claiming it. A recent running
    claim from another trigger is treated as a duplicate. Very old running
    claims are recoverable after an interrupted workflow.
    """
    ctx = scheduled_session_context()
    if not ctx:
        return False
    if safe_text(os.getenv("CAREER_SCHEDULE_CLAIMED")).lower() in {"1", "true", "yes"}:
        logger.info("SCHEDULE GUARD | session=%s | persisted claim already held by this workflow", ctx["key"])
        return False
    guard = STATE.setdefault("schedule_guard", {})
    existing = guard.get(ctx["key"]) if isinstance(guard.get(ctx["key"]), dict) else None
    if existing:
        status = safe_text(existing.get("status")).lower()
        if status == "completed":
            logger.info(
                "SCHEDULE GUARD | session=%s | already_completed_at=%s | skipping duplicate trigger",
                ctx["key"], existing.get("completed_at", ""),
            )
            return True
        if status == "running":
            started = parse_datetime(existing.get("started_at"))
            age_minutes = None if not started else max(0.0, (datetime.now(BD_TZ) - started).total_seconds() / 60.0)
            if age_minutes is None or age_minutes <= SCHEDULE_LOCK_MAX_MINUTES:
                logger.info(
                    "SCHEDULE GUARD | session=%s | already_running age=%.1fmin | skipping duplicate trigger",
                    ctx["key"], age_minutes or 0.0,
                )
                return True
            logger.warning(
                "SCHEDULE GUARD | session=%s | stale_running_lock age=%.1fmin | reclaiming",
                ctx["key"], age_minutes,
            )
    guard[ctx["key"]] = {
        "status": "running",
        "slot": ctx["slot"],
        "date": ctx["date"],
        "cron": ctx["cron"],
        "started_at": now_iso(),
    }
    STATE["pipeline_version"] = PIPELINE_VERSION
    save_state(STATE)
    logger.info("SCHEDULE GUARD | session=%s | claimed", ctx["key"])
    return False


def claim_scheduled_session_for_workflow():
    """Persist a scheduled-session claim and return True when this trigger should run.

    Exit code semantics are handled by the CLI: 0 means claimed, 2 means duplicate.
    The GitHub workflow retries the git push atomically; a push conflict is resolved
    by refreshing origin/main and checking the remote guard state again.
    """
    return not begin_scheduled_session()


def complete_scheduled_session():
    """Mark the current scheduled session complete after the bot has finished publishing/state updates."""
    ctx = scheduled_session_context()
    if not ctx:
        return
    guard = STATE.setdefault("schedule_guard", {})
    entry = guard.setdefault(ctx["key"], {"slot": ctx["slot"], "date": ctx["date"], "cron": ctx["cron"]})
    entry.update({
        "status": "completed",
        "slot": ctx["slot"],
        "date": ctx["date"],
        "cron": ctx["cron"],
        "completed_at": now_iso(),
    })
    logger.info("SCHEDULE GUARD | session=%s | completed", ctx["key"])


def prune_schedule_guard():
    guard = STATE.get("schedule_guard")
    if not isinstance(guard, dict):
        STATE["schedule_guard"] = {}
        return
    cutoff = datetime.now(BD_TZ).date() - timedelta(days=SCHEDULE_GUARD_RETENTION_DAYS)
    kept = {}
    for key, item in guard.items():
        if not isinstance(item, dict):
            continue
        date_text = safe_text(item.get("date")) or safe_text(key).split(":", 1)[0]
        try:
            item_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except Exception:
            continue
        if item_date >= cutoff:
            kept[key] = item
    STATE["schedule_guard"] = kept


def default_state():
    return {
        "format_version": STATE_FORMAT_VERSION,
        "queue": {},
        "events": {},
        "recent_titles": [],
        "last_run": "",
        "schedule_guard": {},
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
# V2 state uses the same queue/event model with explicit format migration.
# Keep posted history, but do not reuse legacy pending queue records.
if int(STATE.get("format_version", 0) or 0) < STATE_FORMAT_VERSION:
    for _key, _item in STATE.get("queue", {}).items():
        if isinstance(_item, dict) and _item.get("status") in {"pending", "selected"}:
            _item["status"] = "legacy_ignored"
    STATE["format_version"] = STATE_FORMAT_VERSION


def prune_state():
    prune_schedule_guard()
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


STATE_STATUS_PRECEDENCE = {
    "legacy_ignored": 0,
    "rejected": 0,
    "pending": 1,
    "selected": 2,
    "posted": 3,
    "published": 3,
    "expired": 4,
}


def _state_item_time(item):
    if not isinstance(item, dict):
        return None
    dates = []
    for key in ("expired_at", "published_at", "selected_at", "last_seen", "posted_at", "discovered_at"):
        dt = parse_datetime(item.get(key))
        if dt:
            dates.append(dt)
    return max(dates) if dates else None


def _state_item_rank(item):
    if not isinstance(item, dict):
        return (-1, datetime.min.replace(tzinfo=timezone.utc))
    status = safe_text(item.get("status")).lower()
    dt = _state_item_time(item) or datetime.min.replace(tzinfo=timezone.utc)
    return (STATE_STATUS_PRECEDENCE.get(status, 0), dt)


def _merge_state_item(remote_item, local_item):
    """Merge two records without allowing a stale local snapshot to erase remote state."""
    if not isinstance(remote_item, dict):
        return dict(local_item) if isinstance(local_item, dict) else remote_item
    if not isinstance(local_item, dict):
        return dict(remote_item)

    remote_rank = _state_item_rank(remote_item)
    local_rank = _state_item_rank(local_item)
    preferred, secondary = (local_item, remote_item) if local_rank >= remote_rank else (remote_item, local_item)

    merged = dict(preferred)
    for key, value in secondary.items():
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = value

    # Publication/expiry identifiers must survive any reconciliation.
    for key in ("message_id", "canonical_url", "source_url", "apply_url", "source_job_id", "event_id"):
        if not merged.get(key):
            merged[key] = remote_item.get(key) or local_item.get(key)
    return merged


def merge_state_data(remote_state, local_state):
    """Reconcile remote GitHub state with the local run snapshot using union semantics."""
    remote = remote_state if isinstance(remote_state, dict) else {}
    local = local_state if isinstance(local_state, dict) else {}
    merged = default_state()
    merged["format_version"] = max(
        int(remote.get("format_version", 0) or 0),
        int(local.get("format_version", 0) or 0),
        STATE_FORMAT_VERSION,
    )
    merged["pipeline_version"] = safe_text(local.get("pipeline_version")) or safe_text(remote.get("pipeline_version")) or PIPELINE_VERSION

    for mapping_name in ("queue", "events"):
        result = {}
        remote_map = remote.get(mapping_name) if isinstance(remote.get(mapping_name), dict) else {}
        local_map = local.get(mapping_name) if isinstance(local.get(mapping_name), dict) else {}
        for key in sorted(set(remote_map) | set(local_map)):
            result[key] = _merge_state_item(remote_map.get(key), local_map.get(key))
        merged[mapping_name] = result

    titles = []
    for value in list(remote.get("recent_titles") or []) + list(local.get("recent_titles") or []):
        title = safe_text(value)
        if title and title not in titles:
            titles.append(title)
    merged["recent_titles"] = titles[-400:]

    merged_guard = {}
    remote_guard = remote.get("schedule_guard") if isinstance(remote.get("schedule_guard"), dict) else {}
    local_guard = local.get("schedule_guard") if isinstance(local.get("schedule_guard"), dict) else {}
    for key in sorted(set(remote_guard) | set(local_guard)):
        remote_item = remote_guard.get(key) if isinstance(remote_guard.get(key), dict) else {}
        local_item = local_guard.get(key) if isinstance(local_guard.get(key), dict) else {}
        if safe_text(remote_item.get("status")).lower() == "completed" or safe_text(local_item.get("status")).lower() == "completed":
            preferred = remote_item if safe_text(remote_item.get("status")).lower() == "completed" else local_item
            secondary = local_item if preferred is remote_item else remote_item
        else:
            remote_dt = parse_datetime(remote_item.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
            local_dt = parse_datetime(local_item.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
            preferred, secondary = (local_item, remote_item) if local_dt >= remote_dt else (remote_item, local_item)
        item = dict(preferred)
        for field, value in secondary.items():
            if field not in item or item.get(field) in (None, "", [], {}):
                item[field] = value
        merged_guard[key] = item
    merged["schedule_guard"] = merged_guard

    remote_last = parse_datetime(remote.get("last_run"))
    local_last = parse_datetime(local.get("last_run"))
    if remote_last and local_last:
        merged["last_run"] = max(remote_last, local_last).astimezone(BD_TZ).isoformat()
    else:
        merged["last_run"] = safe_text(local.get("last_run")) or safe_text(remote.get("last_run"))
    return merged


def reconcile_state_files(local_snapshot_dir):
    """Merge a saved local run snapshot into the currently checked-out remote state."""
    snapshot = Path(local_snapshot_dir)
    local_state_path = snapshot / STATE_FILE
    local_posted_path = snapshot / POSTED_FILE
    if not local_state_path.is_file() or not local_posted_path.is_file():
        raise FileNotFoundError(f"State snapshot is incomplete: {snapshot}")

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        remote_state = json.load(f)
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)
    merged = merge_state_data(remote_state, local_state)
    save_state(merged)

    remote_urls = []
    if Path(POSTED_FILE).is_file():
        remote_urls = [canonical_url(x) for x in Path(POSTED_FILE).read_text(encoding="utf-8").splitlines() if safe_text(x)]
    local_urls = [canonical_url(x) for x in local_posted_path.read_text(encoding="utf-8").splitlines() if safe_text(x)]
    merged_urls = []
    seen = set()
    for value in remote_urls + local_urls:
        if value and value not in seen:
            seen.add(value)
            merged_urls.append(value)
    Path(POSTED_FILE).write_text(("\n".join(merged_urls) + "\n") if merged_urls else "", encoding="utf-8")
    logger.info("STATE RECONCILED | events=%d queue=%d posted_urls=%d", len(merged.get("events", {})), len(merged.get("queue", {})), len(merged_urls))


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
    return extract_posted_date_from_fragment(card_text)

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
    raw = clean_reader_markdown(card_text)
    fields = _extract_source_label_fields(raw)
    return {
        "company": clean_source_field(fields.get("company", "")),
        "experience": compact_experience(fields.get("experience", "")),
        "education": compact_education(fields.get("education", "")),
        "deadline": normalize_date_text(fields.get("deadline", "")),
        "location": compact_location(fields.get("location", "")),
        "salary": compact_salary(fields.get("salary", "")),
        "vacancy": compact_vacancy(fields.get("vacancy", "")),
        "employment_type": compact_employment(fields.get("employment_type", "")),
        "workplace": compact_workplace(fields.get("workplace", "")),
        "age": compact_age(fields.get("age", "")),
        "posted_date": normalize_date_text(fields.get("posted_date", "")),
        "application_method": compact_application(fields.get("application_method", "")),
        "gender": clean_source_field(fields.get("gender", "")),
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
        if not posted and parent is not None:
            try:
                posted = extract_posted_date_from_fragment(str(parent))
            except Exception:
                pass
        parsed_href = urlparse(href)
        id_match = re.search(r"(?:^|[?&])id=(\d+)", parsed_href.query, re.I)
        if not id_match:
            id_match = re.search(r"/h/jobs/(\d+)(?:/|$)", parsed_href.path, re.I)

        seen.add(canonical)
        raw_source_fields = dict(baseline)
        raw_source_fields.update({k: v for k, v in chosen_baseline.items() if v})
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
            "listing_fields": raw_source_fields,
            "raw_source_fields": raw_source_fields,
            "source_content": trim_source_text(card_text, 5000),
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

    for base_url in (_absolute_category_url(category_id),):
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
                eligible, reject_reason, _age = _strict_discovery_eligibility(item)
                if not eligible:
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


def discover_bdjobs_internships():
    """Discover internships from Bdjobs' dedicated internship feed."""
    cutoff = datetime.now(BD_TZ) - timedelta(days=MAX_POST_AGE_DAYS)
    collected, seen = [], set()
    base_urls = [BDJOBS_INTERNSHIP_URL]
    for base_url in base_urls:
        if len(collected) >= max(INTERNSHIP_DETAIL_TARGET * 3, 24):
            break
        fetched = _fetch_source_document(base_url, timeout=DISCOVERY_TIMEOUT, referer=BDJOBS_LISTING_URL)
        if not fetched:
            continue
        page_url = fetched.get("url") or base_url
        candidates = _bdjobs_listing_candidates(
            fetched.get("text", ""), page_url, "internship", "Internship"
        )
        for item in candidates:
            canonical = item.get("canonical") or canonical_url(item.get("url", ""))
            if not canonical or canonical in seen or canonical in POSTED_URLS:
                continue
            item["discovery"] = "bdjobs_internship"
            item["lane"] = "internship"
            item["category_name"] = "Internship"
            item["is_internship_source"] = True
            eligible, reject_reason, _age = _strict_discovery_eligibility(item)
            if not eligible:
                continue
            seen.add(canonical)
            collected.append(item)
    logger.info("BDJOBS INTERNSHIP DISCOVERY | candidates=%d url=%s", len(collected), BDJOBS_INTERNSHIP_URL)
    return collected[:max(INTERNSHIP_DETAIL_TARGET * 3, 24)]

BDJOBSLIVE_CATEGORIES = (
    ("Accounting / Finance", "accounting-finance-jobs", "accounting-finance"),
    ("Bank / Financial Institution", "bank-financial-institution-jobs", "bank-financial-institution"),
    ("Commercial", "commercial-jobs", "commercial"),
    ("Company Secretary / Regulatory Affairs", "company-secretary-regulatory-affairs", "company-secretary-regulatory-affairs"),
    ("Customer Service / Call Centre", "customer-service-call-centre-jobs", "customer-service-call-centre"),
    ("E-commerce / Digital Marketing", "e-commerce-digital-marketing-jobs", "e-commerce-digital-marketing"),
    ("General Management / Admin", "general-management-admin-jobs", "general-management-admin"),
    ("HR / Organizational Development", "hr-organizational-development-jobs", "hr-organizational-development"),
    ("Marketing / Sales", "marketing-sales-jobs", "marketing-sales"),
    ("Media / Advertising / Event Management", "media-advertising-event-management-jobs", "media-advertising-event-management"),
    ("NGO / Development", "ngo-development-jobs", "ngo-development"),
    ("Production / Operation", "production-operation-jobs", "production-operation"),
    ("Research / Consultancy", "research-consultancy-jobs", "research-consultancy"),
    ("Supply Chain / Procurement", "supply-chain-procurement-jobs", "supply-chain-procurement"),
)

BDJOBSLIVE_ALLOWED_CATEGORY_TERMS = tuple(
    re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    for name, _, _ in BDJOBSLIVE_CATEGORIES
)

def _bdjobslive_category_allowed(category):
    value = re.sub(r"[^a-z0-9]+", " ", safe_text(category).casefold()).strip()
    if not value:
        return True
    compact = value.replace(" ", "")
    if any(value == allowed or allowed in value or value in allowed or allowed.replace(" ", "") in compact for allowed in BDJOBSLIVE_ALLOWED_CATEGORY_TERMS):
        return True
    # Known site wording variants are kept explicit so unrelated categories such
    # as Software Development are not accepted merely because they contain the
    # generic word "development".
    aliases = (
        ("bank financial institution", "bank non bank financial institution", "non bank financial institution"),
        ("e commerce digital marketing", "e commerce f commerce", "ecommerce digital marketing"),
        ("hr organizational development", "hr organization development", "human resources organizational development"),
        ("production operation", "production operations"),
        ("supply chain procurement", "supply chain"),
        ("media advertising event management", "media ad event management"),
        ("customer service call centre", "customer service call center"),
        ("company secretary regulatory affairs", "company secretary regulatory"),
        ("ngo development", "ngo development organization"),
    )
    return any(any(alias in value or value == alias for alias in group) for group in aliases)


def _bdjobslive_category_urls(slug, current_slug):
    # The site's own functional-category navigation currently points to
    # /bdjobs-circular/<slug>-jobs. Keep /bdjobs/<slug> as a secondary
    # compatibility route only.
    return (
        f"https://www.bdjobslive.com/bdjobs-circular/{slug}",
        f"https://www.bdjobslive.com/bdjobs/{current_slug}",
    )


def _is_bdjobslive_detail_url(url):
    parsed = urlparse(safe_text(url))
    if not is_domain_allowed(url, [BDJOBSLIVE_DOMAIN]):
        return False
    return bool(re.match(r"^/bdjobs-details/[^?#/]+(?:/)?$", parsed.path, flags=re.I))


def _bdjobslive_pagination_links(page_html, base_url, max_pages):
    soup = BeautifulSoup(page_html or "", "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, safe_text(a.get("href")))
        if not is_domain_allowed(href, [BDJOBSLIVE_DOMAIN]):
            continue
        label = _clean_one_line(a.get_text(" ", strip=True))
        match = re.search(r"(?:page|পৃষ্ঠা)[\s:_-]*(\d+)", label, flags=re.I)
        parsed = urlparse(href)
        q_page = re.search(r"(?:^|&)page=(\d+)(?:&|$)", parsed.query, flags=re.I)
        n = int(match.group(1)) if match else (int(q_page.group(1)) if q_page else None)
        if n and 1 <= n <= max_pages:
            links[n] = href
    return sorted(links.items())


def _bdjobslive_card_company(anchor, title):
    # Current BDJobs Live cards commonly render the title anchor followed by
    # company and category siblings. Prefer that direct sibling relationship.
    for sibling in getattr(anchor, "next_siblings", []):
        text = _clean_one_line(getattr(sibling, "get_text", lambda *args, **kwargs: str(sibling))(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
        if not text or _normalized_identity_title(text) == _normalized_identity_title(title):
            continue
        low = text.casefold()
        if low in {"job list", "save", "share", "apply now"}:
            continue
        if _normalized_source_label(text) in {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}:
            continue
        if re.match(r"^(?:location|salary|experience|application deadline|deadline|published|posted|vacancy|age|gender|job type)\b", text, flags=re.I):
            break
        if len(text) <= 180 and not is_noise_title(text):
            return text

    parent = anchor
    for _ in range(6):
        parent = getattr(parent, "parent", None)
        if parent is None or not hasattr(parent, "get_text"):
            break
        text = parent.get_text("\n", strip=True)
        if title and len(text) <= 3500 and len(text) >= len(title):
            lines = [_clean_one_line(x) for x in text.splitlines() if _clean_one_line(x)]
            norm_title = _normalized_identity_title(title)
            for idx, line in enumerate(lines):
                if _normalized_identity_title(line) != norm_title:
                    continue
                # Most current BDJobs Live cards place Company -> Category/Industry -> Title.
                for candidate in reversed(lines[max(0, idx-5):idx]):
                    low = candidate.casefold()
                    if low in {"job list", "save", "share", "apply now"}:
                        continue
                    if _normalized_source_label(candidate) in {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}:
                        continue
                    if candidate == title or len(candidate) > 180 or is_noise_title(candidate):
                        continue
                    return candidate
    return ""


def _bdjobslive_listing_candidates(page_html, page_url, category_name):
    soup = BeautifulSoup(page_html or "", "html.parser")
    found, seen = [], set()
    anchors = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, safe_text(a.get("href")))
        if not _is_bdjobslive_detail_url(href):
            continue
        title = _clean_one_line(a.get_text(" ", strip=True))
        if not title or is_noise_title(title, href):
            continue
        anchors.append(a)

    for idx, anchor in enumerate(anchors):
        href = urljoin(page_url, safe_text(anchor.get("href")))
        canonical = canonical_url(href)
        if not canonical or canonical in seen:
            continue
        title = _clean_one_line(anchor.get_text(" ", strip=True))
        parent = anchor
        card_text = ""
        for _ in range(7):
            parent = getattr(parent, "parent", None)
            if parent is None or not hasattr(parent, "get_text"):
                break
            candidate_text = _clean_one_line(parent.get_text(" ", strip=True))
            if title in candidate_text and 40 <= len(candidate_text) <= 5000:
                card_text = candidate_text
                break
        card_html = ""
        try:
            card_html = str(parent) if parent is not None else ""
        except Exception:
            card_html = ""
        baseline = _extract_source_label_fields(card_text)
        posted = normalize_date_text(baseline.get("posted_date", "")) or extract_posted_date_from_fragment(card_html or card_text)
        deadline = normalize_date_text(baseline.get("deadline", ""))
        if not posted:
            m = re.search(r"(?:Published|Posted|Date Posted|Publication Date)\s*[:：-]?\s*([^|]+?)(?=\s+(?:Application Deadline|Deadline)\b|$)", card_text, flags=re.I)
            if m:
                posted = normalize_date_text(m.group(1)) or normalize_relative_date_text(m.group(1))
        if not deadline:
            m = re.search(r"(?:Application Deadline|Deadline)\s*[:：-]\s*([^|]+?)(?=\s+(?:Apply Now|Save|Share|Vacancy|Age|Location|Salary|Experience|Gender|Job Type)\b|$)", card_text, flags=re.I)
            if m:
                deadline = normalize_date_text(m.group(1))
        company = clean_source_field(baseline.get("company", "")) or _bdjobslive_card_company(anchor, title)
        found.append({
            "title": title,
            "url": href,
            "canonical": canonical,
            "source": "BDJobs Live",
            "source_url": href,
            "source_job_id": (re.search(r"-(\d+)(?:/)?$", urlparse(href).path) or ["", ""])[1],
            "category_name": category_name,
            "discovery": "bdjobslive_category",
            "listing_fields": {k: v for k, v in {
                "company": company,
                "location": compact_location(baseline.get("location", "")),
                "salary": compact_salary(baseline.get("salary", "")),
                "experience": compact_experience(baseline.get("experience", "")),
                "education": compact_education(baseline.get("education", "")),
                "vacancy": compact_vacancy(baseline.get("vacancy", "")),
                "age": compact_age(baseline.get("age", "")),
                "employment_type": compact_employment(baseline.get("employment_type", "")),
                "workplace": compact_workplace(baseline.get("workplace", "")),
                "application_method": compact_application(baseline.get("application_method", "")),
                "deadline": deadline,
                "posted_date": posted,
            }.items() if v},
            "raw_source_fields": baseline,
            "listing_posted": posted,
            "listing_deadline": deadline,
            "excerpt": trim_source_text(card_text, 2200),
            "discovered_at": now_iso(),
        })
        seen.add(canonical)

    # Jina Reader can return Markdown links instead of raw <a> elements. Preserve
    # those job-detail URLs as a discovery fallback; detail enrichment will provide
    # the authoritative company/field data later.
    if not found:
        markdown = safe_text(page_html or "")
        for match in re.finditer(r"\[([^\]]{2,180})\]\((https?://[^)]+/bdjobs-details/[^)]+)\)", markdown, flags=re.I):
            title = _clean_one_line(match.group(1))
            href = urljoin(page_url, safe_text(match.group(2)))
            if not title or not _is_bdjobslive_detail_url(href) or is_noise_title(title, href):
                continue
            canonical = canonical_url(href)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            found.append({
                "title": title,
                "url": href,
                "canonical": canonical,
                "source": "BDJobs Live",
                "source_url": href,
                "source_job_id": (re.search(r"-(\d+)(?:/)?$", urlparse(href).path) or ["", ""])[1],
                "category_name": category_name,
                "discovery": "bdjobslive_category",
                "listing_fields": {},
                "raw_source_fields": {},
                "listing_posted": "",
                "listing_deadline": "",
                "excerpt": title,
                "discovered_at": now_iso(),
            })
    return found


def _fetch_bdjobslive_browser_document(url, *, mode="listing"):
    """Render a BDJobs Live page with the same real-browser fallback used for Bdjobs.

    BDJobs Live category pages currently expose their job cards after client-side
    rendering. Therefore direct curl/Jina HTML can contain the page shell without
    any /bdjobs-details links. Unlike the Bdjobs detail renderer, resources stay
    enabled here because the listing data itself is hydrated by the page scripts.
    """
    global BDJOBSLIVE_BROWSER_FETCH_COUNT
    if not SCRAPLING_BROWSER_ENABLED or StealthyFetcher is None:
        return None

    limit = BDJOBSLIVE_BROWSER_LISTING_LIMIT if mode == "listing" else BDJOBSLIVE_BROWSER_DETAIL_LIMIT
    with BDJOBSLIVE_BROWSER_LOCK:
        if BDJOBSLIVE_BROWSER_FETCH_COUNT >= max(1, limit):
            return None
        BDJOBSLIVE_BROWSER_FETCH_COUNT += 1
        ordinal = BDJOBSLIVE_BROWSER_FETCH_COUNT

    target = request_safe_url(url)
    # Listing pages are client-rendered and several category templates hydrate
    # later than the default 2.2s. Wait for the actual job-detail anchor instead
    # of relying on a flat sleep, then retry once with a longer render window.
    # Detail pages keep their existing h1 readiness check and one bounded attempt.
    wait_selector = 'a[href*="/bdjobs-details/"]' if mode == "listing" else "h1"
    if mode == "listing":
        attempts = (
            {
                "wait": max(800, BDJOBSLIVE_BROWSER_WAIT_MS),
                "wait_selector": wait_selector,
                "network_idle": True,
                "timeout": max(8000, min(BDJOBSLIVE_BROWSER_TIMEOUT, 15000)),
            },
            {
                "wait": max(BDJOBSLIVE_BROWSER_WAIT_MS * 2, 5000),
                "wait_selector": wait_selector,
                "network_idle": True,
                "timeout": max(8000, min(BDJOBSLIVE_BROWSER_TIMEOUT, 15000)),
            },
            {
                "wait": BDJOBSLIVE_BROWSER_LONG_RETRY_WAIT_MS,
                "wait_selector": wait_selector,
                "network_idle": True,
                "timeout": BDJOBSLIVE_BROWSER_LONG_RETRY_TIMEOUT,
            },
        )
    else:
        attempts = (
            {
                "wait": max(800, BDJOBSLIVE_BROWSER_WAIT_MS),
                "wait_selector": wait_selector,
                "network_idle": False,
            },
        )
    last_reason = "no_render"
    for attempt, opts in enumerate(attempts, start=1):
        try:
            kwargs = dict(
                headless=True,
                disable_resources=False,
                load_dom=True,
                network_idle=opts["network_idle"],
                wait=opts["wait"],
                timeout=opts.get("timeout", max(8000, min(BDJOBSLIVE_BROWSER_TIMEOUT, 15000))),
                google_search=False,
                solve_cloudflare=False,
                block_webrtc=True,
                hide_canvas=True,
                retries=1,
                retry_delay=0,
            )
            # Both listing and detail rendering must wait for their real content
            # to attach. The old code accidentally applied this only to detail
            # mode, creating a race on slower category templates.
            if opts["wait_selector"]:
                kwargs.update(wait_selector=opts["wait_selector"], wait_selector_state="attached")
            page = StealthyFetcher.fetch(target, **kwargs)
            text = _scrapling_visible_text(page)
            rendered_html = safe_text(getattr(page, "html_content", ""))
            if not rendered_html:
                try:
                    body = getattr(page, "body", b"")
                    rendered_html = body.decode("utf-8", "ignore") if isinstance(body, bytes) else safe_text(body)
                except Exception:
                    rendered_html = ""
            final_url = safe_text(getattr(page, "url", "")) or url
            if not rendered_html and not text:
                last_reason = "empty_render"
                continue
            if mode == "listing":
                probe = _bdjobslive_listing_candidates(rendered_html or text, final_url, "")
                if not probe:
                    last_reason = "no_job_detail_links_after_render"
                    continue
            else:
                ok, reason = _looks_like_job_document(rendered_html or text, url=final_url, detail=True)
                if not ok:
                    last_reason = reason
                    continue
            return {
                "ok": True,
                "status": 200,
                "text": text,
                "html": rendered_html,
                "url": final_url,
                "backend": "scrapling_stealthy_bdjobslive",
                "cloudflare": False,
                "detail_quality": "browser_valid",
                "browser_attempt": attempt,
                "browser_mode": mode,
            }
        except Exception as exc:
            last_reason = str(exc)
            logger.info(
                "BDJOBSLIVE BROWSER FAILED | mode=%s | attempt=%d | url=%s | error=%s",
                mode, attempt, urlparse(url).path, exc,
            )
    logger.info(
        "BDJOBSLIVE BROWSER UNAVAILABLE | mode=%s | ordinal=%d | url=%s | attempts=%d | reason=%s",
        mode, ordinal, urlparse(url).path, len(attempts), last_reason,
    )
    return None


def _fetch_bdjobslive_detail(item):
    """Acquire a BDJobs Live detail page using Bdjobs-style fallback ordering."""
    key = cache_key(item.get("url") or item.get("source_url") or item.get("source_job_id"))
    cached = DETAIL_CACHE.get(key)
    if cached and time.monotonic() - cached.get("ts", 0) < DETAIL_CACHE_TTL_SECONDS:
        return cached.get("result")

    url = request_safe_url(item.get("url") or item.get("source_url"))
    if not url:
        return None

    # 1) Direct source retrieval first.
    direct = _fetch_with_curl(url, timeout=DETAIL_TIMEOUT, referer="https://bdjobslive.com/", max_attempts=CURL_MAX_FINGERPRINT_ATTEMPTS)
    if direct:
        body = safe_text(direct.get("text"))
        ok, reason = _looks_like_job_document(body, url=direct.get("url") or url, detail=True)
        if ok:
            direct = _detail_payload_from_fetch(direct)
            direct["detail_quality"] = "direct_valid"
            direct["detail_route"] = direct.get("url") or url
            DETAIL_CACHE[key] = {"ts": time.monotonic(), "result": direct}
            logger.info("BDJOBSLIVE DETAIL SUCCESS | id=%s | backend=%s", item.get("source_job_id", ""), direct.get("backend", ""))
            return direct
        logger.info("BDJOBSLIVE DETAIL REJECT | id=%s | reason=%s | backend=%s", item.get("source_job_id", ""), reason, direct.get("backend", ""))

    # 2) Jina Reader as the normal text fallback. BDJobs Live detail pages
    # expose the core facts directly in HTML, so we do not spend browser time
    # unless both direct and Jina acquisition fail.
    fallback = _fetch_jina(url, timeout=min(12, max(5, int(JINA_TIMEOUT))))
    if fallback:
        ok, reason = _looks_like_job_document(fallback.get("text", ""), url=url, detail=True)
        if ok:
            fallback = _detail_payload_from_fetch(fallback)
            fallback["detail_quality"] = "jina_valid"
            fallback["detail_route"] = url
            DETAIL_CACHE[key] = {"ts": time.monotonic(), "result": fallback}
            logger.info("BDJOBSLIVE DETAIL SUCCESS | id=%s | route=jina", item.get("source_job_id", ""))
            return fallback
        logger.info("BDJOBSLIVE DETAIL JINA REJECT | id=%s | reason=%s", item.get("source_job_id", ""), reason)

    # 3) Exceptional browser fallback for live/dynamic edge cases.
    browser = _fetch_bdjobslive_browser_document(url, mode="detail")
    if browser:
        DETAIL_CACHE[key] = {"ts": time.monotonic(), "result": browser}
        logger.info("BDJOBSLIVE DETAIL SUCCESS | id=%s | backend=scrapling_stealthy_bdjobslive", item.get("source_job_id", ""))
        return browser

    DETAIL_CACHE[key] = {"ts": time.monotonic(), "result": None}
    return None


def discover_bdjobslive_category(category_name, legacy_slug, current_slug):
    cutoff = datetime.now(BD_TZ) - timedelta(days=MAX_POST_AGE_DAYS)
    collected, seen = [], set()
    urls = (f"https://www.bdjobslive.com/bdjobs-circular/{legacy_slug}",)
    browser_attempted_for_category = False

    for base_index, base_url in enumerate(urls):
        if len(collected) >= BDJOBSLIVE_CATEGORY_CAP:
            break

        next_url = base_url
        seen_pages = set()
        for page_index in range(1, BDJOBSLIVE_CATEGORY_PAGE_LIMIT + 1):
            if not next_url or next_url in seen_pages:
                break
            seen_pages.add(next_url)

            fetched = _fetch_source_document(next_url, timeout=BDJOBSLIVE_TIMEOUT, referer="https://bdjobslive.com/")
            page_url = (fetched or {}).get("url") or next_url
            candidates = _bdjobslive_listing_candidates(
                (fetched or {}).get("text", ""), page_url, category_name
            ) if fetched else []

            # Match Bdjobs' proven acquisition strategy: one browser render when
            # normal HTTP returns only a JS shell. Do not repeat that expensive
            # browser render for every alias/legacy URL in the same category.
            if not candidates and not browser_attempted_for_category:
                browser_attempted_for_category = True
                browser = _fetch_bdjobslive_browser_document(next_url, mode="listing")
                if browser:
                    fetched = browser
                    page_url = browser.get("url") or next_url
                    candidates = _bdjobslive_listing_candidates(
                        browser.get("html", "") or browser.get("text", ""),
                        page_url, category_name
                    )

            page_dates = []
            new_count = 0
            for item in candidates:
                canonical = item.get("canonical") or canonical_url(item.get("source_url", ""))
                if not canonical or canonical in seen or canonical in POSTED_URLS:
                    continue
                pdt = parse_datetime(item.get("listing_posted"))
                if pdt:
                    page_dates.append(pdt)
                eligible, reject_reason, _age = _strict_discovery_eligibility(item)
                if not eligible:
                    continue
                seen.add(canonical)
                item["discovery_backend"] = (fetched or {}).get("backend", "")
                collected.append(item)
                new_count += 1
                if len(collected) >= BDJOBSLIVE_CATEGORY_CAP:
                    break

            if len(collected) >= BDJOBSLIVE_CATEGORY_CAP:
                break
            if not fetched:
                break

            links = dict(_bdjobslive_pagination_links(
                fetched.get("text", ""), page_url, BDJOBSLIVE_CATEGORY_PAGE_LIMIT + 2
            ))
            next_url = links.get(page_index + 1, "")
            if not next_url and page_index == 1:
                parsed = urlparse(page_url)
                next_url = urlunparse(
                    parsed._replace(query=(parsed.query + "&" if parsed.query else "") + "page=2")
                )

            if page_dates and max(page_dates) < cutoff:
                break
            if new_count == 0 and page_index > 1:
                break

    logger.info("BDJOBSLIVE CATEGORY | %s | %d", category_name, len(collected))
    return collected


def _bdjobslive_add_discovery_items(target, seen, items, *, discovery_name=None, cap=None):
    for item in items or []:
        canonical=item.get("canonical") or canonical_url(item.get("source_url", ""))
        if not canonical or canonical in seen or canonical in POSTED_URLS:
            continue
        if discovery_name:
            item["discovery"] = discovery_name
        seen.add(canonical)
        target.append(item)
        if cap and len(target) >= cap:
            break


def discover_bdjobslive():
    if not BDJOBSLIVE_ENABLED:
        return []
    categories=list(BDJOBSLIVE_CATEGORIES)
    all_items, seen = [], set()
    with ThreadPoolExecutor(max_workers=min(6, len(categories))) as pool:
        futures=[pool.submit(discover_bdjobslive_category, name, legacy, current) for name, legacy, current in categories]
        for future in as_completed(futures):
            try:
                items=future.result()
            except Exception as exc:
                logger.warning("BDJobs Live category worker failed: %s", exc)
                items=[]
            _bdjobslive_add_discovery_items(all_items, seen, items, discovery_name="bdjobslive_category")

    # Deterministic ordering for the next funnel stage: fresh listings first, then category/title.
    all_items.sort(key=lambda j: (
        -posted_freshness_score({"posted_date": j.get("listing_posted", "")}),
        j.get("category_name", ""),
        normalize_title(j.get("title", "")),
    ))
    all_items=all_items[:BDJOBSLIVE_DISCOVERY_MAX]
    logger.info("BDJOBSLIVE DISCOVERY | raw=%d categories=%d max=%d", len(all_items), len(categories), BDJOBSLIVE_DISCOVERY_MAX)
    return all_items


def discover_bdjobslive_internships():
    """Discover internships from the dedicated BDJobs Live internship feed."""
    if not BDJOBSLIVE_ENABLED:
        return []
    cutoff = datetime.now(BD_TZ) - timedelta(days=MAX_POST_AGE_DAYS)
    collected, seen = [], set()
    url = BDJOBSLIVE_INTERNSHIP_URL
    fetched = _fetch_source_document(url, timeout=min(BDJOBSLIVE_TIMEOUT, 15), referer="https://bdjobslive.com/")
    candidates = _bdjobslive_listing_candidates(
        (fetched or {}).get("text", ""), (fetched or {}).get("url") or url, "Internship"
    ) if fetched else []
    if not candidates:
        browser = _fetch_bdjobslive_browser_document(url, mode="listing")
        if browser:
            candidates = _bdjobslive_listing_candidates(
                browser.get("html", "") or browser.get("text", ""),
                browser.get("url") or url,
                "Internship",
            )
    for item in candidates:
        canonical = item.get("canonical") or canonical_url(item.get("url", ""))
        if not canonical or canonical in seen or canonical in POSTED_URLS:
            continue
        item["discovery"] = "bdjobslive_internship"
        item["lane"] = "internship"
        item["category_name"] = "Internship"
        item["is_internship_source"] = True
        eligible, reject_reason, _age = _strict_discovery_eligibility(item)
        if not eligible:
            continue
        seen.add(canonical)
        collected.append(item)
    logger.info("BDJOBSLIVE INTERNSHIP DISCOVERY | candidates=%d", len(collected))
    return collected[:max(INTERNSHIP_DETAIL_TARGET * 3, 24)]


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
    raw_records = invalid = already_posted = duplicates = 0
    try:
        response = session.get(TELETALK_API_URL, params={"searchKeyword": ""}, headers=HEADERS, timeout=TELETALK_API_TIMEOUT)
        response.raise_for_status(); payload = response.json()
        records = _teletalk_records(payload)
        raw_records = len(records)
        for record in records:
            fields = _teletalk_record_fields(record)
            if not fields:
                invalid += 1
                continue
            canonical = canonical_url(fields["source_url"])
            if not canonical:
                invalid += 1
                continue
            if canonical in seen:
                duplicates += 1
                continue
            if canonical in POSTED_URLS:
                already_posted += 1
                continue
            discovery_probe = {
                "listing_posted": fields["posted_date"],
                "listing_deadline": fields["deadline"],
            }
            eligible, reject_reason, _age = _strict_discovery_eligibility(discovery_probe)
            if not eligible:
                invalid += 1
                continue
            seen.add(canonical)
            discovered.append({
                "title": fields["title"], "url": fields["url"], "canonical": canonical, "source": "Teletalk",
                "source_url": fields["source_url"], "source_job_id": fields["source_job_id"], "discovery": "teletalk_api",
                "excerpt": trim_source_text(fields["raw_text"], 1800), "listing_posted": fields["posted_date"],
                "listing_deadline": fields["deadline"], "discovered_at": now_iso(), "api_fields": fields, "is_government": True,
            })
            if len(discovered) >= GOVERNMENT_DISCOVERY_TARGET: break
        logger.info(
            "TELETALK API DISCOVERY | raw=%d | new=%d | already_posted=%d | invalid=%d | duplicates=%d",
            raw_records, len(discovered), already_posted, invalid, duplicates,
        )
    except Exception as exc:
        logger.warning("Teletalk API discovery failed | raw=%d | error=%s", raw_records, exc)
    return discovered

def _strict_discovery_eligibility(item):
    """Return (eligible, reason, age_days) using only listing/discovery metadata."""
    posted_raw = item.get("listing_posted") or item.get("posted_date")
    posted_dt = parse_datetime(posted_raw)
    if not posted_dt:
        return False, "posted_date_unknown", None
    age_days = max(0.0, (datetime.now(BD_TZ) - posted_dt).total_seconds() / 86400)
    if age_days > MAX_POST_AGE_DAYS:
        return False, f"posted_older_than_{MAX_POST_AGE_DAYS}_days", age_days

    deadline_raw = item.get("listing_deadline") or item.get("deadline")
    deadline_dt = parse_datetime(deadline_raw)
    if not deadline_dt:
        return False, "deadline_unknown", age_days
    if deadline_dt < datetime.now(BD_TZ):
        return False, "deadline_expired", age_days
    return True, "ok", age_days


def _private_discovery_freshness_state(item):
    """Classify a private discovery record using only the listing-level posted date."""
    age = posted_age_days(item)
    if age is None:
        return "unknown", None
    if age > MAX_POST_AGE_DAYS:
        return "stale", age
    return "fresh", age


def _filter_private_discovery_by_freshness(jobs):
    """Strict pre-research eligibility: verified fresh posting + active deadline.

    A discovery candidate only enters the expensive detail/research funnel when:
      * a posting date is known and is within MAX_POST_AGE_DAYS; and
      * a deadline is known and currently active.

    Unknown posting dates and unknown/expired deadlines are dropped at discovery.
    This keeps the research budget focused only on verifiable current vacancies.
    """
    kept = []
    stats = {"fresh": 0, "unknown": 0, "stale": 0, "deadline_unknown": 0, "expired": 0}
    by_source = {}
    for job in jobs:
        source = safe_text(job.get("source")) or "unknown"
        bucket = by_source.setdefault(source, {
            "fresh": 0, "unknown": 0, "stale": 0, "deadline_unknown": 0, "expired": 0
        })

        state, age = _private_discovery_freshness_state(job)
        if state == "unknown":
            bucket["unknown"] += 1
            stats["unknown"] += 1
            job["discovery_freshness"] = "unknown"
            job["discovery_reject_reason"] = "posted_date_unknown"
            job["discovery_age_days"] = None
            continue
        if state == "stale":
            bucket["stale"] += 1
            stats["stale"] += 1
            job["discovery_freshness"] = "stale"
            job["discovery_reject_reason"] = f"posted_older_than_{MAX_POST_AGE_DAYS}_days"
            job["discovery_age_days"] = round(age, 2) if age is not None else None
            continue

        bucket["fresh"] += 1
        stats["fresh"] += 1
        job["discovery_freshness"] = "fresh"
        job["discovery_age_days"] = round(age, 2) if age is not None else None

        deadline_state = deadline_status(job)
        if deadline_state == "unknown":
            bucket["deadline_unknown"] += 1
            stats["deadline_unknown"] += 1
            job["discovery_reject_reason"] = "deadline_unknown"
            continue
        if deadline_state == "expired":
            bucket["expired"] += 1
            stats["expired"] += 1
            job["discovery_reject_reason"] = "deadline_expired"
            continue

        job["discovery_deadline_state"] = "active"
        kept.append(job)

    kept.sort(key=_research_priority_key)
    source_log = "; ".join(
        f"{source}:fresh={vals['fresh']},unknown={vals['unknown']},stale={vals['stale']},deadline_unknown={vals['deadline_unknown']},expired={vals['expired']}"
        for source, vals in sorted(by_source.items())
    ) or "none"
    logger.info(
        "DISCOVERY FRESHNESS | before=%d after=%d fresh=%d unknown=%d stale_pruned=%d deadline_unknown=%d expired=%d | %s",
        len(jobs), len(kept), stats["fresh"], stats["unknown"], stats["stale"],
        stats["deadline_unknown"], stats["expired"], source_log,
    )
    return kept


def discover_all():
    """Run every discovery lane independently. Empty results are valid; only exceptions are unhealthy."""
    global LAST_DISCOVERY_HEALTH
    with ThreadPoolExecutor(max_workers=5) as pool:
        ft = pool.submit(_discover_teletalk_api)
        fb = pool.submit(discover_bdjobs)
        fl = pool.submit(discover_bdjobslive)
        fib = pool.submit(discover_bdjobs_internships)
        fil = pool.submit(discover_bdjobslive_internships)

        results = {}
        futures = {
            "Teletalk": ft,
            "Bdjobs": fb,
            "BDJobs Live": fl,
            "Bdjobs Internships": fib,
            "BDJobs Live Internships": fil,
        }
        health = {}
        for source, future in futures.items():
            try:
                value = future.result()
                results[source] = value or []
                health[source] = {"status": "ok", "count": len(results[source])}
            except Exception as exc:
                logger.warning("%s worker failed: %s", source, exc)
                results[source] = []
                health[source] = {"status": "error", "count": 0, "error": safe_text(exc)[:240]}

    LAST_DISCOVERY_HEALTH = health
    error_sources = [name for name, info in health.items() if info.get("status") == "error"]
    healthy_sources = [name for name, info in health.items() if info.get("status") == "ok"]
    logger.info(
        "DISCOVERY HEALTH | healthy=%d/%d | errors=%s",
        len(healthy_sources), len(health), ", ".join(error_sources) if error_sources else "none",
    )
    # A genuine all-source outage is different from a valid zero-job result.
    # Fail loudly only when every discovery lane raised an exception.
    if health and not healthy_sources:
        raise RuntimeError("All discovery sources failed; no source produced a valid discovery result")

    government = results.get("Teletalk", [])
    bdjobs = results.get("Bdjobs", [])
    bdjobslive = results.get("BDJobs Live", [])
    internships_bdjobs = results.get("Bdjobs Internships", [])
    internships_live = results.get("BDJobs Live Internships", [])

    # Merge duplicate discovery lanes instead of using first-URL-wins. BDJobs Live
    # regular discovery and its internship fallback can surface the same vacancy;
    # the internship copy must be allowed to promote the retained record into the
    # internship lane so it remains eligible for the dedicated internship allocator.
    merged, seen = [], {}
    for item in government + bdjobs + bdjobslive + internships_bdjobs + internships_live:
        canonical = item.get("canonical") or canonical_url(item.get("source_url", ""))
        if not canonical or canonical in POSTED_URLS:
            continue
        if canonical in seen:
            _merge_duplicate_metadata(seen[canonical], item)
            continue
        item["canonical"] = canonical
        seen[canonical] = item
        merged.append(item)
    internship_count = sum(1 for x in merged if x.get("lane") == "internship" or x.get("is_internship_source"))
    logger.info(
        "DISCOVERED | Teletalk=%d | Bdjobs=%d | BDJobsLive=%d | BdjobsInternships=%d | BDJobsLiveInternships=%d | internships=%d | merged=%d",
        len(government), len(bdjobs), len(bdjobslive), len(internships_bdjobs), len(internships_live), internship_count, len(merged),
    )
    filtered = _filter_private_discovery_by_freshness(merged)
    logger.info(
        "DISCOVERED READY FOR RESEARCH | private=%d government=%d total=%d",
        sum(1 for x in filtered if not x.get("is_government")),
        sum(1 for x in filtered if x.get("is_government")),
        len(filtered),
    )
    return filtered


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


def age_bounds(value):
    """Return explicit (minimum_age, maximum_age) bounds from a source-backed age field.

    A missing/unspecified age is represented as (None, None) and is not rejected merely
    because the source omitted the field. Explicit limits are evaluated conservatively:
    single numeric ages are treated as an upper age cap; "at least N" supplies only a
    lower bound and "at most N" supplies only an upper bound.
    """
    blob = _clean_one_line(value).translate(BENGALI_DIGIT_MAP)
    if not blob:
        return None, None
    low = blob.lower().strip()
    if low in {
        "not specified", "not specified.", "not mentioned", "not disclosed",
        "no age limit", "any age", "age not specified", "n/a", "na", "none", "null", "--", "-",
    }:
        return None, None
    low = low.replace("years of age", "years").replace("year of age", "year")
    low = low.replace("থেকে", "to").replace("বছরের", "years").replace("বছর", "years")

    m = re.search(r"\b(\d{1,2})\s*(?:to|[-–])\s*(\d{1,2})\s*(?:years?|year)?\b", low, flags=re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and 10 <= a <= 90 and 10 <= b <= 90:
            return a, b

    m = re.search(r"\b(?:at\s+least|minimum(?:\s+age)?|not\s+less\s+than|minimum\s+of)\s*:?[ ]*(\d{1,2})\s*(?:years?|year)\b", low, flags=re.I)
    if m:
        return int(m.group(1)), None

    m = re.search(r"\b(?:at\s+most|maximum(?:\s+age)?|not\s+more\s+than|below|under)\s*:?[ ]*(\d{1,2})\s*(?:years?|year)?\b", low, flags=re.I)
    if m:
        return None, int(m.group(1))

    # Source pages often show a bare "30 Years" as the age ceiling. Treat it as
    # an upper bound rather than inventing an exact required applicant age.
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:years?|year)\s*", low, flags=re.I)
    if m:
        return None, int(m.group(1))
    return None, None


def private_age_out_of_bounds(job):
    """Reject only when an explicit source age rule falls outside 18-30."""
    raw = safe_text(job.get("age"))
    if not raw:
        return False
    minimum, maximum = age_bounds(raw)
    if minimum is not None and minimum > MAX_PRIVATE_AGE_YEARS:
        return True
    if maximum is not None and maximum < MIN_PRIVATE_AGE_YEARS:
        return True
    if maximum is not None and maximum > MAX_PRIVATE_AGE_YEARS:
        return True
    if minimum is not None and minimum < MIN_PRIVATE_AGE_YEARS:
        return True
    return False


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


def normalize_relative_date_text(value, *, now=None):
    """Normalize common relative posting-date phrases to an ISO date.

    Used only for discovery metadata. A relative phrase such as "2 days ago" is
    converted against the current Bangladesh local date. Time-of-day precision is
    intentionally discarded because the freshness rule is day-based.
    """
    raw = _clean_one_line(value).translate(BENGALI_DIGIT_MAP).lower()
    if not raw:
        return ""
    now = now or datetime.now(BD_TZ)
    if re.search(r"\b(today|just now|just posted|today only)\b", raw):
        return now.date().isoformat()
    if re.search(r"\b(yesterday)\b", raw):
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"\b(\d+)\s*(?:day|days)\s+ago\b", raw)
    if m:
        return (now - timedelta(days=int(m.group(1)))).date().isoformat()
    if re.search(r"\b\d+\s*(?:hour|hours|hr|hrs)\s+ago\b", raw):
        return now.date().isoformat()
    if re.search(r"\b(আজ|আজকে)\b", raw):
        return now.date().isoformat()
    if re.search(r"\b(গতকাল)\b", raw):
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"\b(\d+)\s*দিন\s*আগে\b", raw)
    if m:
        return (now - timedelta(days=int(m.group(1)))).date().isoformat()
    return ""


def extract_posted_date_from_fragment(fragment, *, now=None):
    """Extract a posting/publication date from rendered listing text or HTML.

    Preference order is deliberately source-semantic: explicit Published/Posted
    labels, time/meta attributes, then relative-date phrases. Deadline fields are
    never treated as posted dates.
    """
    raw = safe_text(fragment)
    if not raw:
        return ""
    now = now or datetime.now(BD_TZ)

    # HTML semantic hints: <time datetime>, meta/date attributes, and data-* values.
    if "<" in raw and ">" in raw:
        try:
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup.find_all("time"):
                candidate = safe_text(tag.get("datetime"))
                parent_text = _clean_one_line(tag.parent.get_text(" ", strip=True) if getattr(tag, "parent", None) else "")
                if candidate and re.search(r"(?:published|posted|publication|date\s+posted)", parent_text, re.I):
                    normalized = normalize_date_text(candidate) or normalize_relative_date_text(candidate, now=now)
                    if normalized:
                        return normalized
            for tag in soup.find_all(["meta", "input", "div", "span"], attrs=True):
                attrs = tag.attrs or {}
                blob = " ".join(str(attrs.get(k, "")) for k in ("itemprop", "name", "property", "data-date", "data-posted", "data-published", "datetime"))
                if not re.search(r"published|posted|datePublished|date-posted|date_published", blob, re.I):
                    continue
                candidate = safe_text(attrs.get("content") or attrs.get("value") or attrs.get("data-date") or attrs.get("data-posted") or attrs.get("data-published") or attrs.get("datetime"))
                normalized = normalize_date_text(candidate) or normalize_relative_date_text(candidate, now=now)
                if normalized:
                    return normalized
            raw = _text_from_html(raw)
        except Exception:
            raw = _text_from_html(raw) if "<" in raw else raw

    label_pattern = re.compile(
        r"(?:Published|Posted|Date Posted|Publication Date|Publication|Published On|প্রকাশিত|প্রকাশের তারিখ|প্রকাশ তারিখ|পোস্টেড)\s*[:：-]?\s*(.{1,100})",
        re.I,
    )
    for match in label_pattern.finditer(raw):
        value = _clean_one_line(match.group(1))
        value = re.split(r"\b(?:Application Deadline|Deadline|Last Date|শেষ তারিখ)\b", value, maxsplit=1, flags=re.I)[0].strip(" |-:")
        normalized = normalize_date_text(value) or normalize_relative_date_text(value, now=now)
        if normalized:
            return normalized

    for phrase_pattern in (
        r"\b(?:today|yesterday)\b",
        r"\b\d+\s*(?:day|days|hour|hours|hr|hrs)\s+ago\b",
        r"\b(?:আজ|আজকে|গতকাল)\b",
        r"\b\d+\s*দিন\s*আগে\b",
    ):
        m = re.search(phrase_pattern, raw, flags=re.I)
        if m:
            normalized = normalize_relative_date_text(m.group(0), now=now)
            if normalized:
                return normalized
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
    client=None if AI_DISABLED_FOR_RUN else get_cerebras()
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


BDJOBS_MISSING_VALUES = {"", "-", "--", "n/a", "na", "none", "null"}
BDJOBS_DOM_CHROME_TAGS = {"footer", "nav", "aside"}
BDJOBS_DOM_CHROME_HINT = re.compile(
    r"footer|partner|sidebar|navbar|breadcrumb|cookie|newsletter|site-footer", re.I
)
BDJOBS_DOM_NOISE_TITLES = {
    "our valuable partners", "bdjobs", "bdjobs.com", "job details", "home"
}
BDJOBS_DOM_SITE_SUFFIX = re.compile(r"\s*\|\|\s*Bdjobs\.com\s*$", re.I)


def _bdjobs_dom_text(el):
    if not el:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _bdjobs_dom_clean(value):
    value = _bdjobs_dom_text(value) if hasattr(value, "get_text") else _clean_one_line(value)
    return None if value.casefold() in BDJOBS_MISSING_VALUES else value


def _bdjobs_dom_in_chrome(el):
    for parent in [el, *getattr(el, "parents", [])]:
        if getattr(parent, "name", None) in BDJOBS_DOM_CHROME_TAGS:
            return True
        try:
            attrs = " ".join([*(parent.get("class") or []), parent.get("id") or ""])
        except Exception:
            attrs = ""
        if attrs and BDJOBS_DOM_CHROME_HINT.search(attrs):
            return True
    return False


def _bdjobs_dom_similarity(candidate, listing_title):
    candidate = _clean_one_line(candidate)
    listing_title = _clean_one_line(listing_title)
    if not candidate or not listing_title or len(candidate) > 180:
        return 0.0
    a = normalize_title(candidate)
    b = normalize_title(listing_title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    aa = set(title_tokens(a))
    bb = set(title_tokens(b))
    if not aa or not bb:
        return 0.0

    shared = len(aa & bb)
    seq = SequenceMatcher(None, a, b).ratio()

    # Identity matching must not accept a merely similar generic role.
    # Example: listing "Accounts Executive" must NOT match "Marketing Executive"
    # just because both contain the word "Executive". Require at least two shared
    # title tokens when the listing has two or more tokens, or very high sequence
    # similarity for longer titles.
    if len(bb) >= 2:
        min_shared = min(2, len(bb))
        if shared < min_shared:
            return 0.0
        cover = shared / len(bb)
        jaccard = shared / max(1, len(aa | bb))
        if cover >= 0.60 or seq >= 0.90:
            return max(cover, jaccard, seq)
        return 0.0

    # Single-token titles need an almost exact lexical match.
    return 1.0 if (bb & aa) or seq >= 0.94 else 0.0


def _bdjobs_dom_head_candidates(soup, company=""):
    candidates = []
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]', 'meta[name="title"]'):
        meta = soup.select_one(selector)
        if meta and meta.get("content"):
            candidates.append(("meta", _clean_one_line(meta.get("content"))))
    if soup.title and soup.title.get_text(strip=True):
        candidates.append(("title", _clean_one_line(soup.title.get_text(" ", strip=True))))

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objects = data if isinstance(data, list) else [data]
        for item in objects:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(safe_text(t).casefold() == "jobposting" for t in types) and item.get("title"):
                candidates.append(("jsonld", _clean_one_line(item.get("title"))))

    expanded = []
    for src, text in candidates:
        if not text:
            continue
        expanded.append((src, text))
        if src in {"title", "meta"}:
            clean = BDJOBS_DOM_SITE_SUFFIX.sub("", text).strip()
            expanded.append((src, clean))
            if company and clean.endswith(f": {company}"):
                expanded.append((src, clean[: -len(f": {company}")].strip()))
            elif " : " in clean:
                expanded.append((src, clean.rsplit(" : ", 1)[0].strip()))
            head = clean.split(" | ", 1)[0].strip()
            if head != clean:
                expanded.append((src, head))
                parts = head.split(" - ")
                for i in range(len(parts) - 1, 0, -1):
                    expanded.append((src, " - ".join(parts[:i]).strip()))
            # Current Bdjobs head format can be: "TITLE : COMPANY || Bdjobs.com".
            # Keep colon-separated prefixes as independent candidates too.
            if " : " in clean:
                prefix = clean.rsplit(" : ", 1)[0].strip()
                if prefix and prefix != clean:
                    expanded.append((src, prefix))
    return expanded


def _bdjobs_dom_title(soup, listing_title="", company=""):
    candidates = []
    main = soup.find("app-details-main")
    header_h2 = None
    header_company = None
    if main:
        btn_h2 = main.select_one("button > h2")
        if btn_h2:
            header_company = _bdjobs_dom_text(btn_h2)
            sib = btn_h2.parent.find_next_sibling("h2")
            if sib and not _bdjobs_dom_in_chrome(sib):
                header_h2 = _bdjobs_dom_text(sib)

    if header_h2:
        candidates.append(("header_h2", header_h2))
    candidates.extend(_bdjobs_dom_head_candidates(soup, header_company or company))

    for h in soup.find_all(["h1", "h2", "h3"]):
        if _bdjobs_dom_in_chrome(h):
            continue
        text = _bdjobs_dom_text(h)
        if text and text.casefold() not in BDJOBS_DOM_NOISE_TITLES:
            candidates.append((h.name, text))

    clean_candidates = []
    seen = set()
    for src, value in candidates:
        value = _clean_one_line(value)
        if not value or value.casefold() in BDJOBS_DOM_NOISE_TITLES:
            continue
        key = (src, normalize_title(value))
        if key in seen:
            continue
        seen.add(key)
        clean_candidates.append((src, value))

    if listing_title:
        scored = [
            (_bdjobs_dom_similarity(value, listing_title), 1 if src == "header_h2" else 0, -len(value), value, src)
            for src, value in clean_candidates
        ]
        if scored:
            score, _, _, value, src = max(scored)
            if score >= 0.60:
                return value, src, "matched"
            return _clean_one_line(listing_title), "listing_fallback", "mismatch"
        return _clean_one_line(listing_title), "listing_fallback", "missing"

    if clean_candidates:
        for preferred in ("header_h2", "jsonld", "title", "meta", "h2", "h3"):
            for src, value in clean_candidates:
                if src == preferred:
                    return value, src, "matched"
        return clean_candidates[0][1], clean_candidates[0][0], "matched"
    return "", "", "missing"


def _bdjobs_dom_next_text(el, tag="p"):
    sibling = el.find_next_sibling(tag) if el else None
    return _bdjobs_dom_clean(sibling) if sibling else None


def _bdjobs_dom_summary(soup):
    box = soup.select_one("app-summary #allSection") or soup.select_one("app-summary")
    result = {}
    if not box:
        return result
    spans = list(box.find_all("span"))
    for index, label in enumerate(spans):
        raw = _bdjobs_dom_text(label)
        if not raw.endswith(":") or len(raw) > 40:
            continue
        key = raw[:-1].strip().casefold()
        value = None
        # In the current Angular template the value immediately follows the
        # label. Stop before the next label so one field can never swallow all
        # subsequent summary values.
        for sibling in spans[index + 1:]:
            sibling_text = _bdjobs_dom_text(sibling)
            if sibling_text.endswith(":") and len(sibling_text) <= 40:
                break
            if sibling_text:
                value = _bdjobs_dom_clean(sibling_text)
                break
        if value is None:
            # Fallback for text-node or non-span value wrappers.
            for sibling in label.next_siblings:
                text = _bdjobs_dom_text(sibling) if hasattr(sibling, "get_text") else _clean_one_line(sibling)
                if not text:
                    continue
                if text.endswith(":") and len(text) <= 40:
                    break
                value = _bdjobs_dom_clean(text)
                break
        if value is not None:
            result[key] = value
    return result


def _bdjobs_dom_requirements(soup):
    req = soup.select_one("#requirements")
    result = {}
    if not req:
        return result
    for block in req.find_all("div", recursive=False):
        head = block.find("p", recursive=False)
        if not head:
            continue
        key = _bdjobs_dom_text(head).rstrip(":").casefold()
        items = [_bdjobs_dom_text(li) for li in block.find_all("li") if not li.find("li")]
        items = [x for x in items if x]
        if items:
            result[key] = items
    return result


def _bdjobs_dom_label_pairs(main):
    pairs = {}
    if not main:
        return pairs
    for div in main.find_all("div"):
        children = div.find_all(True, recursive=False)
        if len(children) == 2 and all(getattr(child, "name", "") == "p" for child in children):
            key = _bdjobs_dom_text(children[0]).rstrip().rstrip(":").strip().casefold()
            value = _bdjobs_dom_clean(_bdjobs_dom_text(children[1]))
            if key and value is not None:
                pairs[key] = value
    return pairs


def _bdjobs_dom_extract(page_html, url="", listing_title=""):
    """Parse the current Angular Bdjobs detail DOM using stable tags/IDs."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    main = soup.find("app-details-main")
    if not main:
        title, title_source, identity_status = _bdjobs_dom_title(soup, listing_title=listing_title)
        return {
            "dom_used": False, "title": title, "title_source": title_source,
            "identity_status": identity_status, "is_job_page": False,
        }

    btn_h2 = main.select_one("button > h2")
    company = _bdjobs_dom_text(btn_h2) if btn_h2 else ""
    title, title_source, identity_status = _bdjobs_dom_title(soup, listing_title=listing_title, company=company)

    summary = _bdjobs_dom_summary(soup)
    requirements = _bdjobs_dom_requirements(soup)
    pairs = _bdjobs_dom_label_pairs(main)
    skills_box = soup.select_one("#skills")
    skills = [_bdjobs_dom_text(b) for b in skills_box.find_all("button")] if skills_box else []
    skills = [x for x in skills if x]

    deadline_label = None
    for paragraph in main.find_all("p"):
        if re.search(r"Application\s+Deadline", _bdjobs_dom_text(paragraph), re.I):
            deadline_label = paragraph
            break
    deadline = _bdjobs_dom_next_text(deadline_label) if deadline_label else None

    card = soup.select_one("app-company-info-card")
    company_address = None
    company_size = None
    if card:
        address_label = card.find("p", string=re.compile(r"^\s*Address", re.I))
        company_address = _bdjobs_dom_next_text(address_label) if address_label else None
        m = re.search(r"([\d,+\-]+)\s*Employees", _bdjobs_dom_text(card), re.I)
        company_size = m.group(1) if m else None
        if not company:
            candidate_heading = card.find(["h2", "h3", "h4"])
            if candidate_heading:
                company = _bdjobs_dom_text(candidate_heading)

    body_label_fields = _extract_source_label_fields(main.get_text("\n", strip=True))
    summary_map = {
        "vacancy": summary.get("vacancy"),
        "age": summary.get("age"),
        "location": summary.get("location") or summary.get("job location") or pairs.get("job location"),
        "salary": summary.get("salary"),
        "experience": summary.get("experience"),
        "posted_date": summary.get("published") or summary.get("posted"),
    }
    for key in ("salary", "experience", "posted_date", "location", "vacancy", "age"):
        if not _valid_source_field_value(key, summary_map.get(key, "")) and _valid_source_field_value(key, body_label_fields.get(key, "")):
            summary_map[key] = _normalize_authoritative_field_value(key, body_label_fields.get(key))

    # Some Angular pages expose the summary heading/value separately from the
    # authoritative detail text. Recover explicit salary/experience phrases from
    # the same job DOM before leaving the field empty.
    main_text = main.get_text("\n", strip=True)
    if not _valid_source_field_value("salary", summary_map.get("salary", "")):
        candidate = _extract_salary_fallback(main_text)
        if candidate:
            summary_map["salary"] = candidate
    if not _valid_source_field_value("experience", summary_map.get("experience", "")):
        candidate = _normalize_authoritative_field_value("experience", _extract_source_label_fields(main_text).get("experience", ""))
        if candidate and _valid_source_field_value("experience", candidate):
            summary_map["experience"] = candidate

    education_items = requirements.get("education", [])
    req_experience = requirements.get("experience", [])
    additional_items = requirements.get("additional requirements", [])

    if not compact_experience(summary_map.get("experience", "")) and req_experience:
        summary_map["experience"] = "; ".join(req_experience)

    out = {
        "dom_used": True,
        "title": title, "title_source": title_source, "identity_status": identity_status,
        "company": company or None,
        "location": summary_map["location"],
        "salary": summary_map["salary"],
        "experience": summary_map["experience"],
        "education": "; ".join(education_items) if education_items else None,
        "experience_requirements": "; ".join(req_experience) if req_experience else None,
        "additional_requirements": "; ".join(additional_items) if additional_items else None,
        "skills": ", ".join(skills) if skills else None,
        "vacancy": summary_map["vacancy"],
        "age": summary_map["age"],
        "employment_type": pairs.get("employment status") or pairs.get("employment type"),
        "workplace": pairs.get("workplace") or pairs.get("job work place") or pairs.get("work place"),
        "gender": pairs.get("gender"),
        "job_location": pairs.get("job location"),
        "posted_date": summary_map["posted_date"],
        "deadline": deadline,
        "company_address": company_address,
        "company_size": company_size,
        "body_text": main.get_text("\n", strip=True),
    }
    core = [
        summary_map["vacancy"], summary_map["age"], summary_map["location"], summary_map["salary"],
        summary_map["experience"], summary_map["posted_date"], deadline, out["employment_type"],
        out["workplace"], out["job_location"], out["education"], out["skills"],
    ]
    out["fields_filled"] = sum(1 for x in core if x)
    out["is_job_page"] = bool(title and identity_status != "mismatch" and (summary or requirements or deadline))
    return out


def _html_h1(page_html):
    """Generic fallback retained for non-Bdjobs pages only."""
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




BDJOBS_SOURCE_FIELD_ALIASES = {
    "deadline": ("Application Deadline", "Deadline", "Last Date", "Apply Before"),
    "vacancy": ("Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions"),
    "age": ("Age", "Age Limit", "Age Requirements"),
    "location": ("Location", "Job Location", "Job Location(s)", "Work Location"),
    "salary": ("Monthly Salary", "Monthly salary", "Salary", "Salary Range", "Compensation"),
    "experience": ("Experience", "Experience Requirements", "Experience Requirement"),
    "posted_date": ("Published", "Posted", "Date Posted", "Publication Date"),
    "education": ("Education", "Education required", "Educational Requirements", "Educational Qualification", "Education Requirements"),
    "employment_type": ("Employment Status", "Employment Type", "Job Type"),
    "workplace": ("Workplace", "Job Work Place", "Work Place"),
    "gender": ("Gender",),
    "application_method": ("Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply"),
}
BDJOBS_ALL_SOURCE_LABELS = tuple(label for aliases in BDJOBS_SOURCE_FIELD_ALIASES.values() for label in aliases)


def _normalized_source_label(value):
    value = clean_source_field(value)
    return re.sub(r"\s+", " ", value).strip().rstrip(":-").casefold()


def _normalize_authoritative_field_value(field, value):
    """Normalize a source-labelled value without inventing data.

    Current Bdjobs/BDJobs Live templates can place headings such as
    `Salary & Other Benefits` or `Additional Requirements` directly beside the
    real value. This helper extracts only explicit source-backed patterns and
    otherwise leaves the value untouched.
    """
    value = _clean_one_line(value)
    if not value:
        return ""
    if field == "salary":
        # Preserve a real salary that is embedded after a salary heading.
        m = re.search(
            r"(?:monthly\s+salary|salary|salary\s+range|minimum\s+salary|compensation)\s*[:：-]\s*"
            r"((?:bdt|tk\.?|৳)\s*[0-9][0-9,]*(?:\s*(?:-|–|to)\s*(?:bdt|tk\.?|৳)?\s*[0-9][0-9,]*)?"
            r"(?:\s*\([^)]*\))?|negotiable|competitive|not\s+disclosed)",
            value, flags=re.I,
        )
        if m:
            return _clean_one_line(m.group(1))
        # A compact raw salary without a label is also valid.
        if re.search(r"(?:bdt|tk\.?|৳)\s*[0-9][0-9,]*|\bnegotiable\b|\bcompetitive\b", value, flags=re.I):
            return value
        if re.match(r"^(?:&|and)\s*(?:other\s+)?benefits?\b", value, flags=re.I):
            return ""
    elif field == "experience":
        # Keep explicit experience phrases when a heading was accidentally
        # concatenated with the following value.
        patterns = (
            r"\bfreshers?\b[^.;|]*",
            r"\b(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s*\d+(?:\s*[-–]\s*\d+)?\s*years?\b[^.;|]*",
            r"\b\d+(?:\s*[-–]\s*\d+)?\s*(?:years?|yrs?)\b[^.;|]*",
            r"\b\d+(?:\s*[-–]\s*\d+)?\s*year\(s\)\b[^.;|]*",
        )
        if re.search(r"additional\s+requirements", value, flags=re.I):
            for pattern in patterns:
                m=re.search(pattern, value, flags=re.I)
                if m:
                    return _clean_one_line(m.group(0))
            return ""
    return value


def _valid_source_field_value(field, value):
    value = _normalize_authoritative_field_value(field, value)
    if not value:
        return False
    norm = _normalized_source_label(value)
    if norm in {"none", "null", "n/a", "na", "not specified", "not available", "--", "-"}:
        return False
    if field == "salary":
        # Salary fields must carry a real salary/status signal. A heading or
        # neighbouring label such as `Experience` must never become salary.
        known_labels = {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}
        if norm in known_labels:
            return False
        if re.fullmatch(r"(?:&|and)\s*(?:other\s+)?benefits?", value, flags=re.I):
            return False
        if re.match(r"^(?:(?:&|and)\s*)?(?:(?:other)\s+)?benefits?\b", value, flags=re.I):
            return False
        if re.search(r"(?:^|\s)(?:salary\s*&\s*benefits|compensation\s*&\s*other\s+benefits)(?:$|\s)", value, flags=re.I):
            return False
        if re.fullmatch(r"(?:salary|salary\s*&\s*benefits|compensation\s*&\s*other\s+benefits|other\s+benefits)", value, flags=re.I):
            return False
        if not re.search(r"(?:\d|bdt|tk\.?|৳|negotiable|competitive|not disclosed)", norm, flags=re.I):
            return False
        if re.fullmatch(r"(?:salary|salary\s*&\s*benefits|compensation)", value, flags=re.I):
            return False
    elif field == "experience":
        if norm in {
            "additional requirements", "requirements", "education", "educational requirements",
            "responsibilities", "responsibilities context", "skills", "duties", "qualifications",
        }:
            return False
    elif field in {"deadline", "posted_date"}:
        if not normalize_date_text(value):
            return False
    elif field == "location":
        if norm in {"location", "job location", "work location"}:
            return False
    elif field == "application_method":
        if norm in {"application", "application process", "application procedure"}:
            return False
        if re.match(r"^(?:deadline|application\s+deadline)\b", norm, flags=re.I):
            return False
    return True


def _source_label_match(line, aliases, field=None):
    line = clean_source_field(line)
    if not line:
        return False, ""
    known = "|".join(re.escape(x) for x in sorted(BDJOBS_ALL_SOURCE_LABELS, key=len, reverse=True))
    for alias in sorted(aliases, key=len, reverse=True):
        # A benefit heading such as `Salary & Benefits` is not the Salary field.
        if field == "salary" and re.match(rf"^{re.escape(alias)}\s*&\s*(?:other\s+)?benefits?\s*[:：-]?\s*$", line, flags=re.I):
            continue
        # If another, longer known label starts with this alias, this alias is
        # not the label on the current line. This prevents `Application` from
        # matching the prefix of `Application Deadline`.
        longer_prefix = any(
            other.casefold() != alias.casefold()
            and re.match(rf"^{re.escape(other)}(?:\s|[:：-]|$)", line, flags=re.I)
            for other in BDJOBS_ALL_SOURCE_LABELS
            if len(other) > len(alias)
        )
        if longer_prefix:
            continue
        if re.fullmatch(re.escape(alias) + r"\s*[:：-]?", line, flags=re.I):
            return True, ""
        pattern = rf"^{re.escape(alias)}\s*(?:[:：-]\s*|\s+)(.*?)(?=\s+(?:{known})\s*(?:[:：-]|\s)|$)"
        m = re.match(pattern, line, flags=re.I)
        if m:
            value = _normalize_authoritative_field_value(field, _clean_one_line(m.group(1))) if field else _clean_one_line(m.group(1))
            if value and _normalized_source_label(value) in {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}:
                return False, ""
            if field and value and not _valid_source_field_value(field, value):
                return False, ""
            return True, value
    return False, ""


def _source_field_lines(text):
    cleaned = clean_reader_markdown(text)
    out=[]
    for raw_line in cleaned.splitlines():
        line=clean_source_field(raw_line)
        # Bdjobs listing/detail markup can expose icon placeholders such as
        # "Image:" between the label and its value. They are not source data.
        line=re.sub(r"\bImage\s*[:：]?\s*", " ", line, flags=re.I)
        line=_clean_one_line(line)
        if line:
            out.append(line)
    return out


def _extract_source_label_fields(text):
    """Extract summary fields only from their explicit source labels."""
    lines = _source_field_lines(text)
    if not lines:
        return {}
    result = {}
    normalized_known = {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}
    for i, line in enumerate(lines):
        for field, aliases in BDJOBS_SOURCE_FIELD_ALIASES.items():
            if field in result:
                continue
            matched, value = _source_label_match(line, aliases, field=field)
            if not matched:
                continue
            if not value and i + 1 < len(lines):
                nxt = lines[i + 1]
                if _normalized_source_label(nxt) not in normalized_known:
                    value = nxt
            value = clean_source_field(value)
            value = re.sub(r"\bImage\s*[:：]?\s*", " ", value, flags=re.I)
            value = _normalize_authoritative_field_value(field, _clean_one_line(value))
            if value and _valid_source_field_value(field, value):
                result[field] = value

    # Flattened Jina output fallback. It still uses known-label boundaries, never a
    # first-number/nearest-number heuristic.
    flat = re.sub(r"\s+", " ", clean_reader_markdown(text)).strip()
    known = "|".join(re.escape(x) for x in sorted(BDJOBS_ALL_SOURCE_LABELS, key=len, reverse=True))
    for field, aliases in BDJOBS_SOURCE_FIELD_ALIASES.items():
        if result.get(field):
            continue
        label = "|".join(re.escape(x) for x in sorted(aliases, key=len, reverse=True))
        pattern = rf"(?<![\w])(?:{label})\s*(?:[:：-]\s*)?(.*?)(?=\s+(?:{known})\s*(?:[:：-]|\s)|$)"
        m = re.search(pattern, flat, flags=re.I)
        if m:
            value = clean_source_field(m.group(1))
            value = re.sub(r"\bImage\s*[:：]?\s*", " ", value, flags=re.I)
            value = _normalize_authoritative_field_value(field, _clean_one_line(value))
            if value and _valid_source_field_value(field, value):
                result[field] = value

    # Some Bdjobs templates place the actual monthly salary in the
    # `Compensation & Other Benefits` section rather than the summary row.
    # Recover it explicitly without accepting the section heading itself.
    # Dedicated exact-label recovery. This is intentionally later than the
    # normal label parser so a section heading such as `Salary & Benefits` can
    # never lock the field to a bogus value.
    if not result.get("salary"):
        salary_patterns = (
            r"(?:^|\n)\s*(?:Monthly\s+salary|Monthly\s+Salary|Salary)\s*[:：-]\s*(?!&\s*(?:other\s+)?benefits?\b)([^\n|;]+)",
            r"(?:Monthly\s+salary|Monthly\s+Salary)\s*[:：-]\s*([^|;\n]+)",
        )
        for pattern in salary_patterns:
            for match in re.finditer(pattern, clean_reader_markdown(text), flags=re.I):
                candidate = _clean_one_line(match.group(1))
                if _valid_source_field_value("salary", candidate):
                    result["salary"] = candidate
                    break
            if result.get("salary"):
                break
    if not result.get("experience"):
        experience_patterns = (
            r"(?:^|\n)\s*(?:Experience|Experience\s+Requirement|Experience\s+Requirements)\s*[:：-]\s*([^\n|;]+)",
        )
        for pattern in experience_patterns:
            for match in re.finditer(pattern, clean_reader_markdown(text), flags=re.I):
                candidate = _clean_one_line(match.group(1))
                if _valid_source_field_value("experience", candidate):
                    result["experience"] = candidate
                    break
            if result.get("experience"):
                break
    return result


def _extract_bdjobs_company_from_document(text, expected_title):
    lines = _source_field_lines(text)
    expected = normalize_title(expected_title)
    if not lines or not expected:
        return ""
    idx = next((i for i, line in enumerate(lines[:80]) if normalize_title(line) == expected), -1)
    if idx < 0:
        return ""
    known = {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}
    for p in range(idx - 1, max(-1, idx - 6), -1):
        candidate = clean_source_field(lines[p])
        if not candidate or _normalized_source_label(candidate) in known:
            continue
        if is_noise_title(candidate):
            continue
        if re.search(r"\.(?:gif|png|jpe?g|webp|svg)\b", candidate, flags=re.I):
            continue
        if "bdjobs.com" in candidate.lower() or "job list" in candidate.lower():
            continue
        return candidate
    return ""


def _bdjobslive_label_value(soup, labels, field=None):
    """Read a labelled BDJobs Live value without crossing into unrelated page content.

    The detail page normally uses a label element followed immediately by a value
    element (for example ``<span>Salary:</span><strong>Negotiable</strong>``).
    Broad parent traversal is deliberately avoided because the page body can contain
    many unrelated summary blocks such as Application Deadline.
    """
    wanted = {re.sub(r"\s+", " ", safe_text(x).rstrip(":-")).strip().casefold() for x in labels}
    elements = soup.find_all(["span", "p", "label", "dt", "h3", "h4", "h5", "strong", "div"])
    for el in elements:
        raw = _clean_one_line(el.get_text(" ", strip=True))
        norm = re.sub(r"\s+", " ", raw.rstrip(":-")).strip().casefold()
        if norm not in wanted:
            continue

        parent = getattr(el, "parent", None)
        # 1. Most reliable: immediate siblings after the exact label.
        for sibling in getattr(el, "next_siblings", []):
            candidate = _clean_one_line(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
            if not candidate:
                continue
            candidate_norm = re.sub(r"\s+", " ", candidate.rstrip(":-")).strip().casefold()
            if candidate_norm in wanted:
                continue
            if candidate_norm in {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}:
                break
            if len(candidate) <= 300 and not is_noise_title(candidate):
                normalized = _normalize_authoritative_field_value(field, candidate) if field else candidate
                if not field or _valid_source_field_value(field, normalized):
                    return normalized
                # Invalid candidate for the requested field is not allowed to
                # unlock a neighbouring label's content.
                if field == "salary" and not _valid_source_field_value(field, candidate):
                    break
            # Do not walk through a long unrelated node.
            break

        # 2. Same summary cell: next direct child of a non-page-level parent.
        if parent is not None and getattr(parent, "name", "") not in {"body", "html", "main"} and hasattr(parent, "find_all"):
            children = parent.find_all(["strong", "span", "p", "label", "dt", "dd", "div"], recursive=False)
            try:
                idx = children.index(el)
            except ValueError:
                idx = -1
            if idx >= 0:
                for child in children[idx + 1: idx + 3]:
                    candidate = _clean_one_line(child.get_text(" ", strip=True))
                    candidate_norm = re.sub(r"\s+", " ", candidate.rstrip(":-")).strip().casefold()
                    if not candidate or candidate_norm in wanted:
                        continue
                    if candidate_norm in {_normalized_source_label(x) for x in BDJOBS_ALL_SOURCE_LABELS}:
                        break
                    if len(candidate) <= 300 and not is_noise_title(candidate):
                        normalized = _normalize_authoritative_field_value(field, candidate) if field else candidate
                        if not field or _valid_source_field_value(field, normalized):
                            return normalized

        # 3. Definition-list semantics.
        if parent is not None and getattr(parent, "name", "") in {"dl", "div", "section", "article"}:
            if el.name in {"dt", "label", "span", "p", "h3", "h4", "h5"}:
                nxt = el.find_next_sibling(["dd", "strong", "span", "p", "div"])
                if nxt is not None:
                    candidate = _clean_one_line(nxt.get_text(" ", strip=True))
                    candidate_norm = re.sub(r"\s+", " ", candidate.rstrip(":-")).strip().casefold()
                    if candidate and candidate_norm not in wanted and len(candidate) <= 300 and not is_noise_title(candidate):
                        return candidate

        # 4. Narrow labelled block fallback. Only accept a parent whose text starts
        # with the requested label, preventing unrelated body content from leaking in.
        if parent is not None and getattr(parent, "name", "") not in {"body", "html"} and hasattr(parent, "get_text"):
            block = _clean_one_line(parent.get_text(" ", strip=True))
            for label in labels:
                m = re.match(rf"^{re.escape(label)}\s*[:：-]\s*(.+)$", block, flags=re.I)
                if m:
                    value = _clean_one_line(m.group(1))
                    if value and value.casefold() != norm and len(value) <= 300 and not is_noise_title(value):
                        normalized = _normalize_authoritative_field_value(field, value) if field else value
                        if not field or _valid_source_field_value(field, normalized):
                            return normalized
    return ""

def _bdjobslive_dom_extract(page_html, source_url, expected_title=""):
    """Extract BDJobs Live detail fields from stable semantic DOM anchors.

    The current detail page exposes the job as ordinary HTML. The strongest
    anchors are h1, the company-detail link, labelled summary values, and the
    #section-education / experience / skills / responsibilities / company
    sections. No client-side application-state dump is required for the core
    job facts.
    """
    if not page_html:
        return {}
    soup = BeautifulSoup(page_html, "html.parser")
    out = {"dom_used": False}

    h1 = soup.find("h1")
    title = _clean_one_line(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        title = _clean_one_line(meta.get("content", "")) if meta else ""
        if " | " in title:
            title = title.split(" | ", 1)[0].strip()
    if title:
        out["title"] = title

    company_anchor = soup.select_one('a[href*="/company-detail/"]')
    company = _clean_one_line(company_anchor.get_text(" ", strip=True)) if company_anchor else ""
    if company:
        out["company"] = company
        out["company_profile_url"] = urljoin(source_url, safe_text(company_anchor.get("href")))
        parent = company_anchor.parent
        nearby = []
        if parent and hasattr(parent, "get_text"):
            nearby.append(_clean_one_line(parent.get_text(" ", strip=True)))
            for sibling in list(getattr(parent, "next_siblings", []))[:3]:
                if hasattr(sibling, "get_text"):
                    nearby.append(_clean_one_line(sibling.get_text(" ", strip=True)))
                elif isinstance(sibling, NavigableString):
                    nearby.append(_clean_one_line(str(sibling)))
        for block_text in nearby:
            if not block_text:
                continue
            leftovers = block_text.replace(company, " ").strip(" |-")
            if leftovers and len(leftovers) <= 120 and not is_noise_title(leftovers):
                out["company_type"] = leftovers
                break

    summary = _extract_source_label_fields(_text_from_html(page_html))
    semantic_labels = {
        "location": ("Location", "Job Location"),
        "salary": ("Salary",),
        "experience": ("Experience",),
        "age": ("Age",),
        "vacancy": ("Vacancy",),
        "employment_type": ("Job Type", "Employment Status", "Employment Type"),
        "workplace": ("Workplace", "Work Place", "Job Work Place"),
        "application_method": ("Application",),
        "deadline": ("Application Deadline", "Deadline"),
        "posted_date": ("Published", "Posted"),
        "company": ("Company", "Company Name"),
    }
    for key, labels in semantic_labels.items():
        dom_value = _bdjobslive_label_value(soup, labels, field=key)
        if dom_value and _valid_source_field_value(key, dom_value):
            out[key] = dom_value
        elif summary.get(key):
            summary_value = _normalize_authoritative_field_value(key, summary.get(key, ""))
            if summary_value and _valid_source_field_value(key, summary_value):
                out[key] = summary_value

    live_text = soup.get_text("\n", strip=True)
    if not _valid_source_field_value("salary", out.get("salary", "")):
        candidate = _extract_salary_fallback(live_text)
        if candidate:
            out["salary"] = candidate
    if not _valid_source_field_value("experience", out.get("experience", "")):
        candidate = _extract_source_label_fields(live_text).get("experience", "")
        candidate = _normalize_authoritative_field_value("experience", candidate)
        if candidate and _valid_source_field_value("experience", candidate):
            out["experience"] = candidate

    for field, selector in ((
        ("education", "#section-education"),
        ("experience", "#section-experience"),
    )):
        node = soup.select_one(selector)
        if node:
            value = _clean_one_line(node.get_text(" ", strip=True))
            if value:
                out[field] = value.replace("Education", "", 1).strip() if field == "education" else value.replace("Experience", "", 1).strip()

    for field, selector in (("skills", "#section-skills"), ("responsibilities", "#section-responsibilities")):
        node = soup.select_one(selector)
        if node:
            values = []
            for li in node.find_all("li"):
                value = _clean_one_line(li.get_text(" ", strip=True))
                if value:
                    values.append(value)
            if values:
                out[field] = values
            else:
                value = _clean_one_line(node.get_text(" ", strip=True))
                if value:
                    out[field] = value

    gender = _bdjobslive_label_value(soup, ("Gender",), field="gender")
    if gender:
        out["gender"] = gender
    job_shift = _bdjobslive_label_value(soup, ("Job Shift",), field="job_shift")
    if job_shift:
        out["job_shift"] = job_shift
    job_location = _bdjobslive_label_value(soup, ("Job Location", "Location"), field="location")
    if job_location:
        out["job_location"] = job_location

    company_section = soup.select_one("#section-company")
    if company_section:
        section_text = _clean_one_line(company_section.get_text(" ", strip=True))
        if section_text:
            out["company_information"] = section_text
        address_match = re.search(r"Address\s*:\s*(.+?)(?=Website\s*:|$)", section_text, flags=re.I)
        if address_match:
            out["company_address"] = _clean_one_line(address_match.group(1))
        website = company_section.find("a", href=re.compile(r"^https?://", re.I))
        if website:
            out["company_website"] = safe_text(website.get("href"))

    for attr, key in (("description", "meta_description"), ("og:image", "og_image"), ("og:url", "og_url")):
        node = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if node and node.get("content"):
            out[key] = safe_text(node.get("content"))

    description = out.get("meta_description", "")
    category_match = re.search(r"Category\s+(?:in|:)?\s*([^.!|]+)", description, flags=re.I)
    if category_match:
        out["category"] = _clean_one_line(category_match.group(1))

    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href"):
        out["canonical_url"] = urljoin(source_url, safe_text(canonical.get("href")))

    path = urlparse(source_url).path.rstrip("/")
    m = re.search(r"-(\d+)$", path)
    if m:
        out["job_id"] = m.group(1)

    out["dom_used"] = bool(title and company and (out.get("deadline") or out.get("education") or out.get("experience") or out.get("company_information")))
    out["detail_is_job_page"] = out["dom_used"]
    out["detail_identity_status"] = "match" if out["dom_used"] else "unknown"
    return out


def _bdjobs_detail_summary_fields(text, expected_title=""):
    result = _extract_source_label_fields(text)
    if not result.get("company"):
        company = _extract_bdjobs_company_from_document(text, expected_title)
        if company:
            result["company"] = company
    return result


def _first_valid_source_value(field, *values):
    for value in values:
        value = _clean_one_line(value)
        if value and _valid_source_field_value(field, value):
            return value
    return ""


def _extract_salary_fallback(text):
    if not text:
        return ""
    cleaned = clean_reader_markdown(text)
    known = "|".join(re.escape(x) for x in sorted(BDJOBS_ALL_SOURCE_LABELS, key=len, reverse=True))
    patterns = (
        rf"(?:Monthly\s+salary|Monthly\s+Salary)\s*[:：-]\s*(?!&\s*(?:other\s+)?benefits?\b)(.+?)(?=\s+(?:{known})\s*(?:[:：-]|\s)|$)",
        rf"(?:Salary|Salary\s+Range|Minimum\s+Salary|Compensation)\s*[:：-]\s*(?!&\s*(?:other\s+)?benefits?\b)(.+?)(?=\s+(?:{known})\s*(?:[:：-]|\s)|$)",
    )
    for pattern in patterns:
        m = re.search(pattern, cleaned, flags=re.I)
        if m:
            candidate = _clean_one_line(m.group(1))
            if _valid_source_field_value("salary", candidate):
                return candidate
    return ""


def extract_job_fields(text, page_html, source_url, discovery_item):
    text = safe_text(text)
    jsonld = _jobposting_jsonld(page_html) if page_html else {}
    source = safe_text(discovery_item.get("source")) or source_name(source_url)
    is_gov = bool(discovery_item.get("is_government")) or "/gov-job/" in urlparse(source_url).path.lower()
    is_bdjobs = source == "Bdjobs"
    is_bdjobslive = source == "BDJobs Live"
    bd_dom = _bdjobs_dom_extract(page_html, source_url, discovery_item.get("title", "")) if (page_html and is_bdjobs and not is_gov) else {}
    bd_live_dom = _bdjobslive_dom_extract(page_html, source_url, discovery_item.get("title", "")) if (page_html and is_bdjobslive and not is_gov) else {}
    bd_summary = _bdjobs_detail_summary_fields(text, discovery_item.get("title", "")) if (is_bdjobs and not is_gov) else _extract_source_label_fields(text)
    bd_live_summary = _extract_source_label_fields(text) if (is_bdjobslive and not is_gov) else bd_summary
    source_dom = bd_dom if is_bdjobs else bd_live_dom
    source_summary = bd_summary if is_bdjobs else bd_live_summary

    title = (safe_text(source_dom.get("title")) if source_dom.get("dom_used") else "") or safe_text(jsonld.get("title")) or _html_h1(page_html) or _label_value(text, ["Title", "Job Title", "Position", "Post Name"])
    if not is_gov and source_dom.get("dom_used") and safe_text(source_dom.get("title")):
        title = safe_text(source_dom.get("title"))
    company = ""
    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict):
        company = safe_text(hiring.get("name"))
    company = (safe_text(source_dom.get("company")) or company or source_summary.get("company") or safe_text(discovery_item.get("company")) or safe_text((discovery_item.get("listing_fields") or {}).get("company")) or _summary_value(text, [
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

    location = safe_text(source_dom.get("location")) or source_summary.get("location") or _summary_value(text, ["চাকুরি স্থান", "চাকরি স্থান", "কর্মস্থল", "কর্মস্হল", "কর্মক্ষেত্র", "Job Location", "Location", "Job Location(s)", "Work Location"]) or _label_value(text, ["Job Location", "Location", "Job Location(s)", "Work Location", "চাকুরি স্থান", "চাকরি স্থান", "কর্মস্থল", "কর্মস্হল", "কর্মক্ষেত্র"])
    salary = _first_valid_source_value(
        "salary",
        source_dom.get("salary"),
        source_summary.get("salary"),
        _summary_value(text, ["বেতন", "Monthly Salary", "Salary", "Salary Range", "Minimum Salary", "Compensation"]),
        _label_value(text, ["Monthly Salary", "Salary", "Salary Range", "Minimum Salary", "Compensation", "বেতন"]),
        _extract_salary_fallback(text),
    )
    # Age is intentionally extracted only from explicit age-labelled summary content.
    # Never use a generic fallback that can reinterpret the Experience value as Age.
    age = safe_text(source_dom.get("age")) or source_summary.get("age") or _summary_value(text, ["বয়স", "বয়স", "বয়সসীমা", "বয়সসীমা", "Age", "Age Limit", "Age Requirements"])
    employment = safe_text(source_dom.get("employment_type")) or source_summary.get("employment_type") or _summary_value(text, ["চাকরির ধরন", "Employment Status", "Job Type", "Employment Type"]) or _label_value(text, ["Employment Status", "Job Type", "Employment Type", "চাকরির ধরন"])
    published = _first_valid_source_value(
        "posted_date",
        source_dom.get("posted_date"),
        source_summary.get("posted_date"),
        _summary_value(text, ["প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ", "Published", "Posted", "Date Posted", "Publication Date"]),
        _label_value(text, ["Published", "Posted", "Date Posted", "Publication Date", "প্রকাশিত", "প্রকাশ তারিখ", "প্রকাশের তারিখ"]),
    )
    deadline = _first_valid_source_value(
        "deadline",
        source_dom.get("deadline"),
        source_summary.get("deadline"),
        _summary_value(text, ["শেষ তারিখ", "Application Deadline", "Deadline", "Last Date", "Apply Before"]),
        _label_value(text, ["Application Deadline", "Deadline", "Last Date", "Apply Before", "শেষ তারিখ"]),
    )

    # Detail-section fields. Use the same source text as a fallback because some
    # source templates expose the value outside the Job Summary card.
    experience = _first_valid_source_value(
        "experience",
        source_dom.get("experience"),
        source_summary.get("experience"),
        _summary_value(text, ["Experience", "অভিজ্ঞতা"]),
        _label_value(text, ["Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"]),
    )
    education = safe_text(source_dom.get("education")) or source_summary.get("education") or _summary_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"]) or _label_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"])
    vacancy = safe_text(source_dom.get("vacancy")) or source_summary.get("vacancy") or _summary_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা", "খালি পদ"]) or _label_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা", "খালি পদ"])
    if not vacancy and isinstance(jsonld, dict) and jsonld.get("totalJobOpenings") is not None:
        vacancy = safe_text(jsonld.get("totalJobOpenings"))
    workplace = safe_text(source_dom.get("workplace")) or source_summary.get("workplace") or _summary_value(text, ["Job Work Place", "Workplace", "Work Place", "কর্মক্ষেত্র"]) or _label_value(text, ["Job Work Place", "Workplace", "Work Place", "কর্মক্ষেত্র"])
    category = safe_text(source_dom.get("category")) or _label_value(text, ["Category", "Job Category"]) or safe_text(discovery_item.get("category_name"))
    application_method = source_summary.get("application_method") or _label_value(text, ["Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply", "আবেদন প্রক্রিয়া", "আবেদন প্রক্রিয়া", "আবেদনের নিয়ম", "আবেদনের নিয়ম"])
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
        "gender": safe_text(source_dom.get("gender") or source_summary.get("gender") or ""),
        "job_shift": safe_text(source_dom.get("job_shift") or source_summary.get("job_shift") or ""),
        "job_location": compact_location(safe_text(source_dom.get("job_location") or source_dom.get("location") or source_summary.get("job_location") or "")),
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
        "detail_dom_used": bool(source_dom.get("dom_used")),
        "detail_identity_status": safe_text(source_dom.get("detail_identity_status") or source_dom.get("identity_status")) or "not_checked",
        "detail_identity_ok": safe_text(source_dom.get("detail_identity_status") or source_dom.get("identity_status")) in {"match", "matched", "missing", "not_checked"},
        "detail_is_job_page": bool(source_dom.get("detail_is_job_page")) if source_dom else True,
        "detail_title_source": safe_text(source_dom.get("title_source")),
        "detail_fields_filled": int(source_dom.get("fields_filled", 0) or 0),
        "detail_company_address": safe_text(source_dom.get("company_address")),
        "detail_company_size": safe_text(source_dom.get("company_size")),
        "detail_body_text": safe_text(source_dom.get("body_text")),
        "detail_skills": safe_text(source_dom.get("skills")),
        "detail_experience_requirements": safe_text(source_dom.get("experience_requirements")),
        "detail_additional_requirements": safe_text(source_dom.get("additional_requirements")),
        "company_type": safe_text(source_dom.get("company_type")),
        "company_profile_url": safe_text(source_dom.get("company_profile_url")),
        "company_address": safe_text(source_dom.get("company_address")),
        "company_website": safe_text(source_dom.get("company_website")),
        "skills": source_dom.get("skills", []),
        "responsibilities": source_dom.get("responsibilities", []),
        "meta_description": safe_text(source_dom.get("meta_description")),
        "og_image": safe_text(source_dom.get("og_image")),
        "canonical_url": safe_text(source_dom.get("canonical_url")),
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
    """Validate real job content, using current Bdjobs DOM structure when available."""
    raw = safe_text(body)
    if not raw:
        return False, "empty_body"

    is_html = "<html" in raw.lower() or "<body" in raw.lower() or "</" in raw
    if detail and is_html and is_bdjobs_job_url(url):
        try:
            soup = BeautifulSoup(raw, "html.parser")
            main = soup.find("app-details-main")
            if main:
                parsed = _bdjobs_dom_extract(raw, url=url, listing_title="")
                if parsed.get("is_job_page") and parsed.get("title") and parsed.get("fields_filled", 0) >= 2:
                    return True, "ok_bdjobs_dom"
                return False, f"bdjobs_detail_structure_incomplete:{parsed.get('fields_filled', 0)}"
            if soup.find("app-root"):
                return False, "bdjobs_application_shell"
        except Exception:
            pass

    if detail and is_html and is_bdjobslive_job_url(url):
        try:
            parsed = _bdjobslive_dom_extract(raw, url, "")
            if parsed.get("dom_used") and parsed.get("title") and parsed.get("company"):
                core = sum(1 for key in ("deadline", "education", "experience", "location", "salary", "company_information", "responsibilities") if safe_text(parsed.get(key)))
                if core >= 1:
                    return True, "ok_bdjobslive_dom"
                return False, "bdjobslive_detail_structure_incomplete"
            # Some diagnostic/browser fixtures and older templates expose the
            # same facts as labelled text without the current company-detail DOM.
            # Fall through to the generic validation instead of rejecting such a
            # page merely because the semantic DOM adapter was unavailable.
        except Exception:
            pass

    visible = _text_from_html(raw) if is_html else raw
    visible = safe_text(visible)
    if not visible:
        return False, "empty_visible_text"
    if is_cloudflare_response(200, {}, raw):
        return False, "cloudflare_challenge"

    lowered = visible.lower()
    if detail:
        marker_groups = {
            "summary": ("job summary", "company name", "job location", "location", "salary", "vacancy"),
            "requirements": ("education", "experience", "experience requirements", "additional requirements"),
            "employment": ("employment status", "job work place", "workplace", "job type"),
            "application": ("application deadline", "deadline", "apply by bdjobs", "application"),
        }
        group_hits = sum(1 for terms in marker_groups.values() if any(term in lowered for term in terms))
        shell_markers = (
            "find jobs in the no. 1 job site", "active filters", "job search",
            "my bdjobs", "career resources", "accessibility adjustments",
        )
        if any(x in lowered for x in shell_markers) and group_hits < 2:
            return False, "bdjobs_application_shell"
        if group_hits < 2:
            return False, f"non_job_content_groups:{group_hits}"
        if len(visible) < 120:
            return False, f"thin_or_non_job_page:{len(visible)}"
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
    """Extract job content first, then fall back to whole-body text for diagnostics."""
    if page is None:
        return ""
    try:
        job_nodes = page.css("app-details-main")
        if job_nodes:
            first = job_nodes[0]
            text = first.get_all_text(strip=True)
            if text:
                return _clean_one_line(text)
    except Exception:
        pass
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
    """Render a real Bdjobs detail document after HTTP clients receive an app shell.

    Important: the browser is the production fallback, not a decorative extra.
    The old workflow installed Playwright but Scrapling StealthyFetcher uses
    Patchright, so the browser executable was never installed and every detail
    request fell back to the sparse listing card. This function also tries the
    current and legacy detail routes, captures the rendered HTML, and runs the
    Cloudflare solver when the page requires it.
    """
    global SCRAPLING_BROWSER_FETCH_COUNT
    if not SCRAPLING_BROWSER_ENABLED or StealthyFetcher is None:
        return None

    with SCRAPLING_BROWSER_LOCK:
        if SCRAPLING_BROWSER_FETCH_COUNT >= SCRAPLING_BROWSER_DETAIL_LIMIT:
            return None
        SCRAPLING_BROWSER_FETCH_COUNT += 1
        ordinal = SCRAPLING_BROWSER_FETCH_COUNT

    variants = _detail_url_variants(item)
    if not variants:
        return None

    last_reason = "no_route"
    for route in variants[:5]:
        try:
            page = StealthyFetcher.fetch(
                request_safe_url(route),
                headless=True,
                disable_resources=True,
                load_dom=True,
                network_idle=SCRAPLING_BROWSER_NETWORK_IDLE,
                wait=SCRAPLING_BROWSER_WAIT_MS,
                wait_selector=SCRAPLING_BROWSER_WAIT_SELECTOR or None,
                wait_selector_state="attached",
                timeout=SCRAPLING_BROWSER_TIMEOUT,
                google_search=True,
                solve_cloudflare=True,
                block_webrtc=True,
                hide_canvas=True,
                retries=1,
                retry_delay=0.5,
            )
            text = _scrapling_visible_text(page)
            rendered_html = safe_text(getattr(page, "html_content", ""))
            if not rendered_html:
                try:
                    body = getattr(page, "body", b"")
                    rendered_html = body.decode("utf-8", "ignore") if isinstance(body, bytes) else safe_text(body)
                except Exception:
                    rendered_html = ""
            ok, reason = _looks_like_job_document(rendered_html or text, url=route, detail=True)
            if not ok:
                last_reason = reason
                logger.info(
                    "BDJOBS BROWSER REJECT | id=%s | attempt=%d | route=%s | reason=%s | chars=%d",
                    item.get("source_job_id", ""), ordinal, urlparse(route).path, reason, len(text),
                )
                continue

            logger.info(
                "BDJOBS BROWSER SUCCESS | id=%s | attempt=%d | route=%s | chars=%d | html=%d",
                item.get("source_job_id", ""), ordinal, urlparse(route).path, len(text), len(rendered_html),
            )
            return {
                "ok": True, "status": 200, "text": text, "html": rendered_html,
                "url": safe_text(getattr(page, "url", "")) or route,
                "backend": "scrapling_stealthy",
                "cloudflare": False, "detail_quality": "browser_valid",
                "detail_route": route,
            }
        except Exception as exc:
            last_reason = str(exc)
            logger.info(
                "BDJOBS BROWSER FAILED | id=%s | attempt=%d | route=%s | error=%s",
                item.get("source_job_id", ""), ordinal, urlparse(route).path, exc,
            )

    logger.info(
        "BDJOBS BROWSER UNAVAILABLE | id=%s | attempt=%d | reason=%s",
        item.get("source_job_id", ""), ordinal, last_reason,
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

    # Jina should use the current server-rendered /hn/details page first. The old
    # implementation sent Jina to the legacy route, which often returned less content.
    if JINA_ENABLED:
        jina_targets=[]
        for route_index in (1, 0, 2):
            if route_index < len(variants) and variants[route_index] not in jina_targets:
                jina_targets.append(variants[route_index])
        for jina_target in jina_targets[:2]:
            fallback=_fetch_jina(jina_target, timeout=min(12, max(5, int(JINA_TIMEOUT))))
            if not fallback:
                continue
            ok,reason=_looks_like_job_document(fallback.get("text", ""), url=jina_target, detail=True)
            if ok:
                fallback=_detail_payload_from_fetch(fallback)
                fallback["detail_quality"]="jina_valid"
                fallback["detail_route"] = jina_target
                DETAIL_CACHE[key]={"ts":time.monotonic(),"result":fallback}
                logger.info("BDJOBS DETAIL SUCCESS | id=%s | route=jina:%s", item.get("source_job_id",""), urlparse(jina_target).path)
                return fallback
            last_reason=reason
            logger.info("BDJOBS DETAIL JINA REJECT | id=%s | route=%s | reason=%s", item.get("source_job_id",""), urlparse(jina_target).path, reason)

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
        "backend": "careernewsroom_listing_fallback", "cloudflare": False, "detail_quality": "listing_fallback",
    }

def retrieve_job_content(item):
    source=item.get("source", "Bdjobs")
    if source == "Bdjobs":
        fetched=_fetch_bdjobs_detail(item)
    elif source == "BDJobs Live":
        fetched=_fetch_bdjobslive_detail(item)
    else:
        fallback_referer = BDJOBS_LISTING_URL
        fetched=_fetch_source_document(item["url"], timeout=DETAIL_TIMEOUT, referer=fallback_referer)

    if not fetched:
        fallback=_listing_fallback_content(item)
        if fallback:
            logger.info("DETAIL FALLBACK | source=%s | id=%s | backend=bdjobs_listing_fallback", source, item.get("source_job_id", ""))
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
    if not raw_text:
        fallback=_listing_fallback_content(item)
        if fallback:
            logger.info("DETAIL EMPTY -> LISTING FALLBACK | id=%s | backend=%s", item.get("source_job_id",""), backend)
            return fallback
        logger.info("DETAIL PARSE EMPTY | id=%s | backend=%s", item.get("source_job_id",""), backend)
        return None

    # Bdjobs detail validity is structural when rendered HTML is available.
    # Character count alone cannot distinguish a footer-heavy page from a real job page.
    if source == "Bdjobs" and page_html:
        dom_check = _bdjobs_dom_extract(page_html, fetched.get("url") or item.get("url"), item.get("title", ""))
        if dom_check.get("dom_used"):
            if not dom_check.get("is_job_page"):
                fallback=_listing_fallback_content(item)
                if fallback:
                    logger.info(
                        "BDJOBS DETAIL STRUCTURE -> LISTING FALLBACK | id=%s | identity=%s | fields=%s",
                        item.get("source_job_id",""), dom_check.get("identity_status",""), dom_check.get("fields_filled",0),
                    )
                    return fallback
                return None
            if dom_check.get("fields_filled", 0) < 2:
                fallback=_listing_fallback_content(item)
                if fallback:
                    logger.info(
                        "BDJOBS DETAIL TOO SPARSE -> LISTING FALLBACK | id=%s | fields=%s",
                        item.get("source_job_id",""), dom_check.get("fields_filled",0),
                    )
                    return fallback
                return None
        elif len(raw_text) < DETAIL_MIN_TEXT_CHARS:
            fallback=_listing_fallback_content(item)
            if fallback:
                logger.info("BDJOBS DETAIL THIN -> LISTING FALLBACK | id=%s | backend=%s | chars=%d", item.get("source_job_id",""), backend, len(raw_text))
                return fallback
            return None
    elif len(raw_text) < DETAIL_MIN_TEXT_CHARS:
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
    if key in {"salary", "experience", "deadline", "posted_date", "location", "application_method"} and not _valid_source_field_value(key, value):
        return False
    low=value.lower()
    if low in {"none","null","n/a","na","not specified","not available","--","-"}:
        return False
    if key in {"title","company"}:
        if is_noise_title(value):
            return False
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
            candidate = _normalize_authoritative_field_value(key, candidate) if key in {"salary", "experience", "deadline", "posted_date", "location", "application_method"} else _clean_one_line(candidate)
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
    if item.get("source") == "Bdjobs" and retrieved.get("detail_quality") != "listing_fallback":
        identity_status = safe_text(fields.get("detail_identity_status"))
        if identity_status == "mismatch":
            logger.warning(
                "BDJOBS DETAIL IDENTITY MISMATCH | id=%s | listing_title=%s | detail_title=%s | source=%s",
                item.get("source_job_id", ""), item.get("title", ""), fields.get("title", ""),
                fields.get("detail_title_source", ""),
            )
            detail_fields = {}
        elif fields.get("detail_dom_used") and not fields.get("detail_is_job_page"):
            logger.warning(
                "BDJOBS DETAIL STRUCTURE PARTIAL | id=%s | title=%s | identity=%s | fields=%s | preserving_valid_fields=yes",
                item.get("source_job_id", ""), fields.get("title", ""), identity_status, fields.get("detail_fields_filled", ""),
            )
            # Identity is already matched to the discovered job. Preserve each
            # individually validated field so a partial detail card can enrich the
            # listing instead of discarding salary/experience/etc. wholesale.
            detail_fields = fields
        else:
            logger.info(
                "BDJOBS DETAIL DOM OK | id=%s | title_source=%s | identity=%s | fields=%s",
                item.get("source_job_id", ""), fields.get("detail_title_source", ""), identity_status,
                fields.get("detail_fields_filled", ""),
            )
    source_extras = {
        key: value for key, value in detail_fields.items()
        if key in {"company_type", "company_profile_url", "company_address", "company_website", "skills", "responsibilities", "meta_description", "og_image", "canonical_url"}
        and value not in ("", None, [], {})
    }
    fields=merge_job_fields(detail_fields, listing, item)
    if source_extras:
        fields["source_extras"] = source_extras
    source_fields = dict(item.get("raw_source_fields") or {})
    extracted_fields = _extract_source_label_fields(retrieved.get("text", ""))
    source_fields.update({k:v for k,v in extracted_fields.items() if v})
    fields["raw_source_fields"] = source_fields
    fields["source_content"] = safe_text(retrieved.get("text", ""))[:12000]

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
        "lane": item.get("lane", "internship" if item.get("is_internship_source") else ""),
        "is_internship_source": bool(item.get("is_internship_source")),
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
    if not is_domain_allowed(job.get("source_url", ""), PRIVATE_JOB_DOMAINS): return False, "source_not_allowed"
    if job.get("source") == "BDJobs Live":
        live_category = (
            safe_text(job.get("category"))
            or safe_text(job.get("category_name"))
            or safe_text(job.get("source_category_name"))
        )
        is_live_internship = bool(job.get("lane") == "internship" or job.get("is_internship_source"))
        if not is_live_internship:
            if live_category and not _bdjobslive_category_allowed(live_category):
                # Category names are useful guardrails, but they are not authoritative
                # enough to reject a clearly BBA/MBA-relevant business function such as
                # Performance Marketing when the site's taxonomy uses a different label.
                role_probe = dict(job)
                role_probe["raw_text"] = " ".join(
                    x for x in (
                        safe_text(job.get("raw_text")),
                        safe_text(job.get("title")),
                        safe_text(job.get("education")),
                        safe_text(job.get("responsibilities")),
                        safe_text(job.get("category")),
                    ) if x
                )
                role_score = bba_mba_candidate_score(role_probe)
                title_lower = safe_text(job.get("title")).lower()
                clearly_non_business = any(term in title_lower for term in NON_BUSINESS_ROLE_TERMS)
                if (role_score < 30 and role_fit_score(role_probe) < 10) or clearly_non_business:
                    return False, "bdjobslive_category_not_allowed"
            if job.get("discovery") == "bdjobslive_index_fallback" and not live_category:
                return False, "bdjobslive_category_missing"
            if job.get("discovery") == "bdjobslive_homepage_fallback" and not live_category:
                # Homepage candidates are validated by detail-page category/BBA relevance
                # later; the homepage itself intentionally has mixed categories.
                pass
    if deadline_status(job) == "expired": return False, "expired"
    age = posted_age_days(job)
    if age is not None and age > MAX_POST_AGE_DAYS: return False, f"posted_older_than_{MAX_POST_AGE_DAYS}_days"
    if private_experience_too_high(job): return False, f"experience_above_{MAX_PRIVATE_EXPERIENCE_YEARS}_years"
    if private_age_out_of_bounds(job): return False, f"age_outside_{MIN_PRIVATE_AGE_YEARS}_{MAX_PRIVATE_AGE_YEARS}"
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
                    "display_fields": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["location","employment_type","workplace","education","experience","salary","vacancy","age","application_method","deadline","posted_date"]
                        },
                        "minItems": 1,
                        "maxItems": 11,
                    },
                },
                "required": ["id", "publish", "score", "bba_mba_fit", "early_career_fit", "role_fit", "reason", "display_fields"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _judge_prompt():
    return """
You are the semantic audit layer for Career Newsroom V1.
Audience: Bangladesh BBA/MBA students, graduates, freshers and early-career business candidates.
Use only supplied source-backed facts. Never invent missing fields.
For private jobs, audit education match, business-role fit, career-stage fit, semantic contradictions, specialist-degree requirements and seniority.
For government Teletalk jobs, do not apply the private BBA/MBA gate. Audit only source coherence and contradictions.
For every job, also choose which AVAILABLE source fields should appear in the compact Telegram JOB SNAPSHOT. The values themselves must always come from the supplied source fields, never from model inference. Prefer all materially useful available fields; omit only fields that are absent, redundant, or clearly unsuitable for the compact snapshot. ALWAYS include salary when a source-backed salary is supplied. Also always include deadline and posted_date when available; for private jobs include location and experience when available.
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
        display_fields=[x for x in (row.get("display_fields") or []) if x in {"location","employment_type","workplace","education","experience","salary","vacancy","age","application_method","deadline","posted_date"}]
        valid.append({
            "id": idx,
            "publish": bool(row.get("publish", True)),
            "score": score,
            "bba_mba_fit": bba_fit,
            "early_career_fit": early_fit,
            "role_fit": role_fit,
            "reason": safe_text(row.get("reason", "")),
            "display_fields": list(dict.fromkeys(display_fields)),
        })
    return valid


def _call_judge_once(batch, batch_no, retry=False):
    global AI_DISABLED_FOR_RUN, AI_DISABLE_REASON
    if AI_DISABLED_FOR_RUN:
        return []
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
            f"Source fields: {json.dumps(job.get('raw_source_fields', {}), ensure_ascii=False, separators=(',', ':'))}",
            f"Source content: {trim_source_text(job.get('source_content') or job.get('raw_text',''),3000)}",
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
        error_text = safe_text(exc)
        if "402" in error_text or "payment_required" in error_text.lower() or "payment required" in error_text.lower():
            AI_DISABLED_FOR_RUN = True
            AI_DISABLE_REASON = "payment_required"
            logger.error("CEREBRAS DISABLED FOR RUN | reason=payment_required | batch=%d", batch_no)
        logger.warning("CEREBRAS audit batch %d failed%s: %s", batch_no, " on retry" if retry else "", exc)
        return []


def judge_batch(batch, batch_no):
    if AI_DISABLED_FOR_RUN or not get_cerebras() or not batch:
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
        available_display=[key for key in ("location","employment_type","workplace","education","experience","salary","vacancy","age","application_method","deadline","posted_date") if safe_text(candidate.get(key))]
        ai_display=[key for key in (row.get("display_fields") or []) if key in available_display]
        # AI may suggest a compact subset, but it must never hide a source-backed
        # high-impact field. Salary is mandatory whenever the source extractor found it.
        display_fields=list(dict.fromkeys(ai_display or available_display))
        for required_key in ("salary","deadline","posted_date","location"):
            if required_key in available_display and required_key not in display_fields:
                display_fields.append(required_key)
        for core_key in ("deadline","posted_date","location"):
            if core_key in available_display and core_key not in display_fields:
                display_fields.append(core_key)
        if not is_internship_job(candidate) and "experience" in available_display and "experience" not in display_fields:
            display_fields.append("experience")
        candidate.update({
            "ai_score": ai_score if ai_available else None,
            "judge_publish": bool(row.get("publish", True)) if ai_available else True,
            "bba_mba_fit": int(row.get("bba_mba_fit", min(100, education_priority_score(job) * 4))) if ai_available else None,
            "early_career_fit": int(row.get("early_career_fit", min(100, experience_priority_score(job) * 6))) if ai_available else None,
            "role_fit": int(row.get("role_fit", min(100, role_fit_score(job) * 5))) if ai_available else None,
            "judge_reason": safe_text(row.get("reason", "Deterministic source-first ranking.")) if ai_available else "AI unavailable; deterministic score used.",
            "display_fields": display_fields,
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


def _merge_duplicate_metadata(kept, incoming):
    """Preserve the richer identity/lane metadata when two discovery feeds mirror one vacancy."""
    if is_internship_job(incoming):
        kept["lane"] = "internship"
        kept["is_internship_source"] = True
    if safe_text(incoming.get("category_name")) and not safe_text(kept.get("category_name")):
        kept["category_name"] = incoming.get("category_name")
    for key in ("listing_posted", "listing_deadline", "company", "location", "education", "experience", "salary", "vacancy", "canonical", "source_url"):
        if not safe_text(kept.get(key)) and safe_text(incoming.get(key)):
            kept[key] = incoming.get(key)
    existing_discoveries = kept.setdefault("discovery_sources", [])
    if not isinstance(existing_discoveries, list):
        existing_discoveries = [existing_discoveries] if existing_discoveries else []
        kept["discovery_sources"] = existing_discoveries
    for marker in (safe_text(kept.get("discovery")), safe_text(incoming.get("discovery"))):
        if marker and marker not in existing_discoveries:
            existing_discoveries.append(marker)
    return kept


def build_unique_job_pool(jobs):
    unique = []
    event_keys = set()
    event_index = {}
    identity_buckets = {}
    for job in jobs:
        if candidate_already_posted(job):
            logger.info(
                "DROP duplicate | source=%s | id=%s | title=%s | canonical=%s",
                job.get("source", ""), job.get("source_job_id", ""), job.get("title", ""), job.get("canonical", ""),
            )
            continue
        event_key = job_event_key(job)
        if event_key in event_index:
            _merge_duplicate_metadata(event_index[event_key], job)
            continue
        # First-pass bucket by normalized company/title family. This catches category copies
        # cheaply before the stronger fuzzy mirror comparison.
        company_key = _normalized_company(job.get("company", ""))
        title_key = _normalized_identity_title(job.get("title", ""))[:100]
        bucket_keys = []
        if company_key:
            bucket_keys.append(f"company:{company_key}")
        if title_key:
            bucket_keys.append(f"title:{title_key}")
        if not bucket_keys:
            bucket_keys.append(f"raw:{event_key}")
        maybe_same = []
        seen_previous = set()
        for bucket in bucket_keys:
            for previous in identity_buckets.get(bucket, []):
                previous_key = previous.get("canonical") or job_event_key(previous)
                if previous_key not in seen_previous:
                    seen_previous.add(previous_key)
                    maybe_same.append(previous)
        matched = next((previous for previous in maybe_same if likely_same_job(job, previous)), None)
        if matched is not None:
            _merge_duplicate_metadata(matched, job)
            continue
        unique.append(job)
        event_keys.add(event_key)
        event_index[event_key] = job
        for bucket in bucket_keys:
            identity_buckets.setdefault(bucket, []).append(job)
    return unique

def is_internship_job(job):
    """Detect internships using both explicit source lanes and job text."""
    if job.get("lane") == "internship" or job.get("is_internship_source"):
        return True
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


def _eligible_private_ranked(ranked):
    return _private_hard_fill_pool(ranked)


def _select_internship_quota(ranked, limit):
    interns=[j for j in ranked if is_internship_job(j)]
    interns=_sort_source_quality(interns)
    if not interns or limit <= 0:
        return []
    bd=[j for j in interns if j.get("source") == "Bdjobs"]
    live=[j for j in interns if j.get("source") == "BDJobs Live"]
    selected=[]
    selected_keys=set()
    for pool, target in ((bd, INTERNSHIP_BDJOBS_TARGET), (live, INTERNSHIP_BDJOBSLIVE_TARGET)):
        for job in pool[:target]:
            if len(selected) >= limit:
                break
            key=job.get("canonical") or job_event_key(job)
            if key not in selected_keys:
                selected.append(job); selected_keys.add(key)
    if len(selected) < limit:
        for job in interns:
            key=job.get("canonical") or job_event_key(job)
            if key not in selected_keys:
                selected.append(job); selected_keys.add(key)
            if len(selected) >= limit:
                break
    return selected[:limit]


def select_final_jobs(private_ranked, government_jobs):
    """V2 source-balanced selector: 10 private + 4 internship + 5 govt + up to 6 extras."""
    regular=[j for j in private_ranked if not is_internship_job(j)]
    interns=[j for j in private_ranked if is_internship_job(j)]
    regular_target=PRIVATE_TARGET_PER_RUN
    internship_target=INTERNSHIP_TARGET_PER_RUN
    government_target=GOVERNMENT_TARGET_PER_RUN

    # Targets are ceilings/preferences, never hard minimums. If one eligible
    # private job survives the quality gates, publish that one rather than turning
    # the run into zero simply because the target is 10.
    regular_selected=select_private_jobs_by_category(regular, regular_target, minimum_required=0)
    internship_selected=_select_internship_quota(interns, internship_target)
    government_pool=select_government_jobs(government_jobs)[:government_target]

    selected=[]
    for pool in (regular_selected, internship_selected, government_pool):
        for job in pool:
            key=job.get("canonical") or job_event_key(job)
            if key not in {x.get("canonical") or job_event_key(x) for x in selected}:
                selected.append(job)

    # Flexible fill: use the strongest remaining eligible jobs, preferring regular
    # private first, then internships, then government, without relaxing quality.
    used={x.get("canonical") or job_event_key(x) for x in selected}
    extras=[]
    for pool in (_select_diverse_private(_eligible_private_ranked(regular), MAX_STORIES_PER_RUN),
                 _sort_source_quality(interns),
                 government_pool):
        for job in pool:
            key=job.get("canonical") or job_event_key(job)
            if key in used:
                continue
            extras.append(job)
            used.add(key)
    extras=_sort_source_quality(extras)
    selected.extend(extras[:max(0, min(EXTRA_TARGET_PER_RUN, MAX_STORIES_PER_RUN-len(selected)))])

    # Keep source-family diversity stable while preserving the explicit base targets.
    if len(selected) > MAX_STORIES_PER_RUN:
        selected=selected[:MAX_STORIES_PER_RUN]
    logger.info(
        "V1 ALLOCATION | private=%d/%d | internships=%d/%d | government=%d/%d | extras=%d | total=%d/%d",
        len(regular_selected), regular_target, len(internship_selected), internship_target,
        len(government_pool), government_target,
        min(max(0, len(selected)-len(regular_selected)-len(internship_selected)-len(government_pool)), EXTRA_TARGET_PER_RUN),
        len(selected), MAX_STORIES_PER_RUN,
    )
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
        "education": job.get("education", ""),
        "experience": job.get("experience", ""),
        "salary": job.get("salary", ""),
        "vacancy": job.get("vacancy", ""),
        "age": job.get("age", ""),
        "employment_type": job.get("employment_type", ""),
        "workplace": job.get("workplace", ""),
        "application_method": job.get("application_method", ""),
        "career_category": job.get("career_category", ""),
        "display_fields": list(job.get("display_fields") or []),
        "is_government": bool(job.get("is_government")),
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


def _button_markup(job, expired=False):
    if expired:
        return {
            "inline_keyboard": [[
                {
                    "text": "EXPIRED DEADLINE",
                    "style": "danger",
                    "url": TELEGRAM_PROMO_URL,
                }
            ]]
        }

    url = safe_text(job.get("apply_url"))
    target = url if url.startswith(("http://", "https://")) else safe_text(job.get("source_url"))
    return {"inline_keyboard": [[{"text": "APPLY NOW", "url": target}]]}


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


def _event_job_for_expiry(event_id, event):
    """Reconstruct a publishable JobRecord for editing an existing post.

    Current runs keep a full copy in STATE[queue]. Older event records are sparse,
    so non-empty event fields override the queue record while missing values are
    retained from the queue. This keeps the historical post as close as possible
    to its original source-backed snapshot.
    """
    job = {}
    canonical = safe_text(event.get("canonical_url"))
    if canonical:
        queue_item = STATE.get("queue", {}).get(canonical)
        if isinstance(queue_item, dict):
            job.update(queue_item)

    for key, value in event.items():
        if key in {"canonical_url", "status", "selected_at", "published_at", "expired_at", "expiry_update_status", "expiry_error", "expired_apply_url_removed", "expired_source_url_removed"}:
            continue
        if value not in (None, ""):
            job[key] = value

    job["canonical"] = canonical or safe_text(job.get("canonical"))
    job["source_url"] = safe_text(event.get("source_url") or job.get("source_url"))
    job["apply_url"] = safe_text(event.get("apply_url") or job.get("apply_url"))
    job["message_id"] = event.get("message_id") or job.get("message_id")
    job["is_government"] = bool(event.get("is_government", job.get("is_government", False)))
    return job


def _deadline_expiry_datetime(value):
    """Return the effective local expiry instant for a stored deadline.

    Stored source deadlines are normally normalized to YYYY-MM-DD. A date-only
    deadline remains valid through the end of that date in Asia/Dhaka. If a true
    timestamp is supplied, its timestamp is respected.
    """
    raw = safe_text(value)
    if not raw:
        return None
    iso_date = normalize_date_text(raw)
    if iso_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_date):
        try:
            day = datetime.fromisoformat(iso_date).date()
            return datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=BD_TZ)
        except Exception:
            return None

    parsed = parse_datetime(raw)
    if parsed:
        return parsed
    return None


def _deadline_has_expired(value, *, now=None):
    expiry = _deadline_expiry_datetime(value)
    if expiry is None:
        return False
    current = now or datetime.now(BD_TZ)
    return current >= expiry


def _edit_expired_message(job):
    """Replace the historical rich message with an expired, source-link-free version."""
    message_id = safe_text(job.get("message_id"))
    if not message_id.isdigit():
        return {"ok": False, "description": "missing_message_id"}

    expired_job = dict(job)
    expired_job["apply_url"] = ""
    expired_job["source_url"] = ""
    blocks = rich_message_blocks(expired_job, expired=True)
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "message_id": int(message_id),
        "rich_message": json.dumps({"blocks": blocks}, ensure_ascii=False, separators=(",", ":")),
        "reply_markup": json.dumps(_button_markup(expired_job, expired=True), ensure_ascii=False, separators=(",", ":")),
    }
    result = telegram_call("editMessageText", data=data)
    if result.get("ok"):
        return result

    # Button-only fallback keeps the job visibly non-actionable if a client/API
    # edge case prevents the rich body edit. The full edit is retried next run.
    markup_result = telegram_call(
        "editMessageReplyMarkup",
        data={
            "chat_id": TELEGRAM_CHANNEL,
            "message_id": int(message_id),
            "reply_markup": json.dumps(_button_markup(expired_job, expired=True), ensure_ascii=False, separators=(",", ":")),
        },
    )
    if markup_result.get("ok"):
        return {
            "ok": True,
            "partial": True,
            "description": "rich_edit_failed_button_updated",
            "rich_error": result.get("description", ""),
        }
    return {
        "ok": False,
        "description": result.get("description", "rich_edit_failed"),
        "markup_error": markup_result.get("description", ""),
    }


def expire_published_jobs():
    """Sweep previously published CareerNewsroom posts for expired deadlines."""
    if not TELEGRAM_BOT_TOKEN:
        logger.info("DEADLINE SWEEP | skipped | TELEGRAM_BOT_TOKEN not configured")
        return {"checked": 0, "expired": 0, "updated": 0, "partial": 0, "failed": 0, "skipped": 0}

    now = datetime.now(BD_TZ)
    candidates = []
    checked = expired = skipped = failed = updated = partial = 0

    for event_id, event in list(STATE.get("events", {}).items()):
        if not isinstance(event, dict):
            continue
        if event.get("status") != "published":
            continue
        checked += 1
        if safe_text(event.get("expiry_update_status")) == "expired_full":
            continue
        message_id = safe_text(event.get("message_id"))
        deadline = safe_text(event.get("deadline"))
        if not message_id or not deadline:
            skipped += 1
            continue
        if not _deadline_has_expired(deadline, now=now):
            continue
        candidates.append((event_id, event, deadline))

    # Oldest deadline first so a large backlog clears deterministically.
    candidates.sort(key=lambda item: (_deadline_expiry_datetime(item[2]) or now, safe_text(item[1].get("published_at")), item[0]))
    deferred = max(0, len(candidates) - DEADLINE_SWEEP_MAX_UPDATES_PER_RUN)
    candidates = candidates[:DEADLINE_SWEEP_MAX_UPDATES_PER_RUN]

    for event_id, event, deadline in candidates:
        expired += 1
        job = _event_job_for_expiry(event_id, event)
        result = _edit_expired_message(job)
        if not result.get("ok"):
            failed += 1
            event["expiry_update_status"] = "failed"
            event["expiry_error"] = safe_text(result.get("description"))[:500]
            logger.warning(
                "DEADLINE EXPIRE FAILED | event=%s | message_id=%s | deadline=%s | error=%s",
                event_id, event.get("message_id"), deadline, result.get("description", ""),
            )
            continue

        event["expiry_update_status"] = "expired_partial" if result.get("partial") else "expired_full"
        event["expired_at"] = now_iso()
        event["expired_apply_url_removed"] = True
        event["expired_source_url_removed"] = not bool(result.get("partial"))
        event["expiry_error"] = safe_text(result.get("rich_error"))[:500] if result.get("partial") else ""
        event["status"] = "expired" if not result.get("partial") else "published"
        queue_item = STATE.get("queue", {}).get(safe_text(event.get("canonical_url")))
        if isinstance(queue_item, dict) and not result.get("partial"):
            queue_item["status"] = "expired"
        updated += 1
        if result.get("partial"):
            partial += 1
            logger.warning(
                "DEADLINE EXPIRED PARTIAL | event=%s | message_id=%s | button updated; full rich edit will retry",
                event_id, event.get("message_id"),
            )
        else:
            logger.info(
                "DEADLINE EXPIRED | event=%s | message_id=%s | deadline=%s | promo=%s",
                event_id, event.get("message_id"), deadline, TELEGRAM_PROMO_URL,
            )

    save_state(STATE)
    logger.info(
        "DEADLINE SWEEP | checked=%d expired=%d updated=%d partial=%d failed=%d skipped=%d deferred=%d",
        checked, expired, updated, partial, failed, skipped, deferred,
    )
    return {"checked": checked, "expired": expired, "updated": updated, "partial": partial, "failed": failed, "skipped": skipped, "deferred": deferred}


# ============================================================
# JOB POST RENDERING
# ============================================================

def display_value(value):
    return english_display_text(clean_generated_text(safe_text(value)))


def job_hashtags(job):
    tags=[]
    if job.get("is_government"):
        tags.append("#GovtJob")
    if is_internship_job(job):
        tags.append("#Internship")
    family=safe_text(job.get("career_category") or _career_family(job))
    family_tags={
        "Finance & Accounting":"#Finance","Banking & Financial Services":"#Banking",
        "Marketing & Sales":"#Marketing","Business Development":"#BusinessDevelopment",
        "Human Resources":"#HR","Supply Chain & Procurement":"#SupplyChain",
        "Commercial":"#Commercial","Management & Administration":"#Management",
        "Customer Experience":"#CustomerService","E-commerce & Digital Business":"#Ecommerce",
        "Operations":"#Operations","Media / Advertisement / Events":"#Media",
        "Research & Consultancy":"#Research","NGO / Development":"#NGO",
        "Hospitality / Travel / Tourism":"#Hospitality",
    }
    if family_tags.get(family):
        tags.append(family_tags[family])
    blob=f"{safe_text(job.get('title'))} {safe_text(job.get('raw_text'))} {safe_text(job.get('category'))}".lower()
    extras=(("#Sales",("sales","territory sales","key account")),("#Marketing",("marketing","brand","trade marketing")),("#Finance",("finance","account","audit","tax")),("#Banking",("bank","credit","branch")),("#HR",("human resource","recruitment","talent acquisition","hr ")),("#SupplyChain",("supply chain","procurement","logistics","sourcing")))
    for tag,terms in extras:
        if any(term in blob for term in terms): tags.append(tag)
    if any(x in blob for x in EARLY_CAREER_TERMS): tags.append("#EarlyCareer")
    if not tags: tags.append("#Career")
    return list(dict.fromkeys(tags))[:5]


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
    elif is_internship_job(job):
        minimum=INTERNSHIP_SNAPSHOT_MIN_FIELDS
        required_core=("location", "deadline")
        for key in required_core:
            if not safe_text(job.get(key)):
                return False, f"private_missing_core_{key}"
        if not any(safe_text(job.get(k)) for k in ("education", "salary", "vacancy", "employment_type", "workplace", "age", "application_method", "experience")):
            return False, "private_missing_secondary_field"
    else:
        minimum=PRIVATE_SNAPSHOT_MIN_FIELDS
        required_core=("location", "experience", "deadline")
        for key in required_core:
            if not safe_text(job.get(key)):
                return False, f"private_missing_core_{key}"
        if not any(safe_text(job.get(k)) for k in ("education", "salary", "vacancy", "employment_type", "workplace", "age", "application_method", "experience")):
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
    mapping_all=[
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
    wanted=set(job.get("display_fields") or [key for _,key in mapping_all if safe_text(job.get(key))])
    mapping=[(label,key) for label,key in mapping_all if key in wanted]
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


def rich_message_blocks(job, *, expired=False):
    """Native Telegram Rich Message. Media is intentionally disabled.

    When expired=True, the source footer becomes plain text so no source URL is
    retained in the edited historical post.
    """
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
    source_url="" if expired else safe_text(job.get("source_url"))
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
    print("CAREERNEWSROOM SOURCE TEST")
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
        print(f"Bdjobs category {cid}: WARN | unavailable | time={elapsed}s")
        print("Production diagnostic is advisory; the main workflow is not blocked by a temporary source outage.")
        fetched = None
    if not fetched:
        candidates=[]
    else:
        candidates=_bdjobs_listing_candidates(fetched.get("text",""),fetched.get("url") or url,cid,BDBJOBS_CATEGORIES[cid]["name"])
    rich_candidates=sum(
        1 for c in candidates
        if c.get("company")
        and sum(1 for k in ("location","education","experience","deadline") if (c.get("listing_fields") or {}).get(k)) >= 3
    )
    if fetched:
        print(
            f"Bdjobs category {cid}: OK | backend={fetched.get('backend')} | status={fetched.get('status')} | "
            f"candidates={len(candidates)} | listing_rich={rich_candidates} | time={elapsed}s"
        )
        if fetched.get("cloudflare"): print("  Cloudflare: detected")
    else:
        print(
            f"Bdjobs category {cid}: WARN | no response | candidates=0 | listing_rich=0 | time={elapsed}s"
        )
    if not candidates:
        print("Bdjobs detail: SKIPPED | no job candidate on category page")
    elif rich_candidates == 0:
        print("Bdjobs listing: WARN | candidates found but listing metadata is sparse")
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
    print("Production: Teletalk government + Bdjobs + BDJobs Live private")
    if BDJOBSLIVE_ENABLED:
        # Category pages are client-rendered and can be temporarily unavailable.
        # Keep this diagnostic useful without making a source outage fatal to the
        # rest of the production pipeline. Then verify the stable detail parser
        # independently using the first live detail link available from the home page.
        live_name, live_legacy, live_current = BDJOBSLIVE_CATEGORIES[0]
        live_fetch = None
        live_candidates = []
        selected_live_url = ""
        started=time.monotonic()
        for live_url in _bdjobslive_category_urls(live_legacy, live_current):
            candidate_fetch=_fetch_source_document(live_url, timeout=BDJOBSLIVE_TIMEOUT, referer="https://bdjobslive.com/")
            candidate_items=_bdjobslive_listing_candidates((candidate_fetch or {}).get("text", ""), (candidate_fetch or {}).get("url") or live_url, live_name) if candidate_fetch else []
            if not candidate_items:
                browser_fetch=_fetch_bdjobslive_browser_document(live_url, mode="listing")
                if browser_fetch:
                    candidate_fetch=browser_fetch
                    candidate_items=_bdjobslive_listing_candidates(browser_fetch.get("html", "") or browser_fetch.get("text", ""), browser_fetch.get("url") or live_url, live_name)
            if candidate_items:
                live_fetch=candidate_fetch
                live_candidates=candidate_items
                selected_live_url=live_url
                break
        elapsed=round(time.monotonic()-started,2)
        if live_fetch:
            print(f"BDJobs Live category {live_name}: OK | backend={live_fetch.get('backend')} | jobs={len(live_candidates)} | url={selected_live_url} | time={elapsed}s")
        else:
            print(f"BDJobs Live category {live_name}: WARN | dynamic listing unavailable | time={elapsed}s | production remains non-fatal")

        homepage=_fetch_source_document("https://www.bdjobslive.com/", timeout=min(BDJOBSLIVE_TIMEOUT, 12), referer="https://bdjobslive.com/")
        homepage_candidates=_bdjobslive_listing_candidates(
            (homepage or {}).get("text", ""), (homepage or {}).get("url") or "https://www.bdjobslive.com/", ""
        ) if homepage else []
        if homepage_candidates:
            sample=homepage_candidates[0]
            detail=_fetch_bdjobslive_detail(sample)
            if detail:
                parsed=extract_job_fields(detail.get("text", ""), detail.get("html", ""), sample["url"], sample)
                sane=bool(parsed.get("title")) and bool(parsed.get("company")) and bool(parsed.get("deadline"))
                print(f"BDJobs Live detail: {'OK' if sane else 'INVALID_PARSE'} | id={sample.get('source_job_id','')} | backend={detail.get('backend')} | quality={detail.get('detail_quality')} | title={parsed.get('title','')} | company={parsed.get('company','')}")
            else:
                print(f"BDJobs Live detail: WARN | id={sample.get('source_job_id','')} | acquisition failed; production remains non-fatal")
        else:
            print("BDJobs Live detail: WARN | no live homepage detail link available for diagnostic")

    print("Discovery: category-first across Bdjobs + BDJobs Live + Teletalk")
    print("Fallback: curl_cffi -> browser only when required -> Jina -> listing preservation")

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

def _sort_source_quality(items):
    return sorted(
        items,
        key=lambda j: (
            -float(j.get("final_score", j.get("deterministic_score", 0)) or 0),
            -posted_freshness_score({"posted_date": j.get("listing_posted") or j.get("posted_date")}),
            j.get("canonical", ""),
        ),
    )


def _research_priority_key(job):
    """Prioritize candidates worth researching: freshness first, then discovery confidence and BBA/MBA fit."""
    freshness = posted_freshness_score({"posted_date": job.get("listing_posted") or job.get("posted_date")})
    discovery = safe_text(job.get("discovery"))
    fallback_penalty = 1 if discovery in {"bdjobslive_homepage_fallback", "bdjobslive_index_fallback"} else 0
    pre_score = float(job.get("bba_mba_target_score", 0) or 0)
    if not pre_score:
        probe = dict(job)
        probe["raw_text"] = " ".join(
            x for x in (
                safe_text(job.get("raw_text")),
                safe_text(job.get("excerpt")),
                safe_text(job.get("category_name")),
            ) if x
        )
        try:
            pre_score = float(bba_mba_candidate_score(probe))
        except Exception:
            pre_score = 0.0
    return (
        -freshness,
        fallback_penalty,
        -pre_score,
        -float(job.get("final_score", job.get("deterministic_score", job.get("audience_pre_score", 0))) or 0),
        safe_text(job.get("canonical", "")),
    )


def _adaptive_source_reserve(groups, budget, reserve=None):
    """Pick a small source reserve without imposing a fixed source percentage."""
    budget = max(0, int(budget))
    if budget <= 0:
        return {key: 0 for key in groups}
    available = {key: len(value) for key, value in groups.items() if value}
    active = list(available)
    if len(active) <= 1:
        return {key: 0 for key in groups}
    reserve = max(0, int(PRIVATE_DETAIL_SOURCE_RESERVE if reserve is None else reserve))
    reserve = min(reserve, max(1, budget // len(active)))
    counts = {key: 0 for key in groups}
    for key in active:
        counts[key] = min(reserve, available[key])
    total = sum(counts.values())
    while total > budget:
        key = min(active, key=lambda k: (counts[k], available[k]))
        if counts[key] <= 1 and all(counts[k] <= 1 for k in active):
            break
        counts[key] -= 1
        total -= 1
    return counts


def _proportional_source_fill(groups, budget):
    """Fill a remaining research budget proportionally to available supply."""
    budget = max(0, int(budget))
    active = {key: items for key, items in groups.items() if items}
    if budget <= 0 or not active:
        return []
    total = sum(len(items) for items in active.values())
    if budget >= total:
        result = []
        for items in active.values():
            result.extend(items)
        return sorted(result, key=_research_priority_key)[:budget]

    keys = list(active)
    raw = {key: budget * len(active[key]) / total for key in keys}
    allocations = {key: min(len(active[key]), int(raw[key])) for key in keys}
    used = sum(allocations.values())
    # Largest-remainder method makes the proportional split deterministic.
    while used < budget:
        candidates = [key for key in keys if allocations[key] < len(active[key])]
        if not candidates:
            break
        key = max(candidates, key=lambda k: (raw[k] - allocations[k], len(active[k]), k))
        allocations[key] += 1
        used += 1

    result = []
    for key in keys:
        result.extend(active[key][:allocations[key]])
    return sorted(result, key=_research_priority_key)[:budget]


def _fill_research_remainder(items, used_keys, budget):
    """Fill remaining slots freshness-tier first, proportional by source within each tier."""
    budget = max(0, int(budget))
    if budget <= 0:
        return []
    remaining = [
        item for item in items
        if (item.get("canonical") or job_event_key(item)) not in used_keys
    ]
    selected = []
    # Known-fresh listings always outrank unknown-date listings. Within each
    # freshness tier, source volume determines how much of the shared budget it gets.
    tier_groups = {}
    for item in remaining:
        tier = posted_freshness_score({"posted_date": item.get("listing_posted") or item.get("posted_date")})
        tier_groups.setdefault(tier, []).append(item)
    for tier in sorted(tier_groups, reverse=True):
        if len(selected) >= budget:
            break
        items_in_tier = tier_groups[tier]
        groups = {}
        for item in items_in_tier:
            groups.setdefault(safe_text(item.get("source")) or "other", []).append(item)
        for group in groups.values():
            group.sort(key=_research_priority_key)
        take = min(budget - len(selected), len(items_in_tier))
        selected.extend(_proportional_source_fill(groups, take))
    return selected[:budget]


def _balanced_internship_research_pool(internships, budget):
    """Research internships from one combined pool with a small per-source fairness reserve."""
    budget = max(0, int(budget))
    if budget <= 0:
        return []
    groups = {
        "Bdjobs": sorted([x for x in internships if x.get("source") == "Bdjobs"], key=_research_priority_key),
        "BDJobs Live": sorted([x for x in internships if x.get("source") == "BDJobs Live"], key=_research_priority_key),
        "other": sorted([x for x in internships if x.get("source") not in {"Bdjobs", "BDJobs Live"}], key=_research_priority_key),
    }
    reserve = _adaptive_source_reserve(groups, budget, reserve=max(1, min(2, budget // 2)) if budget else 0)
    selected, used = [], set()
    for source, count in reserve.items():
        for job in groups[source][:count]:
            key = job.get("canonical") or job_event_key(job)
            if key not in used:
                selected.append(job); used.add(key)
    selected.extend(_fill_research_remainder(internships, used, max(0, budget-len(selected))))
    return selected[:budget]


def _balanced_regular_private_pool(regular, budget):
    """Use a combined freshness-ranked pool with a small source reserve, not a fixed BDJobs Live quota."""
    budget = max(0, int(budget))
    if budget <= 0:
        return []
    groups = {
        "Bdjobs": sorted([x for x in regular if x.get("source") == "Bdjobs"], key=_research_priority_key),
        "BDJobs Live": sorted([x for x in regular if x.get("source") == "BDJobs Live"], key=_research_priority_key),
        "other": sorted([x for x in regular if x.get("source") not in {"Bdjobs", "BDJobs Live"}], key=_research_priority_key),
    }
    reserve = _adaptive_source_reserve(groups, budget)
    selected, used = [], set()
    for source, count in reserve.items():
        for job in groups[source][:count]:
            key = job.get("canonical") or job_event_key(job)
            if key not in used:
                selected.append(job); used.add(key)
    selected.extend(_fill_research_remainder(regular, used, max(0, budget-len(selected))))
    return selected[:budget]


def _prepare_shortlists(discovered):
    unique = build_unique_job_pool(discovered)
    gov = [x for x in unique if x.get("is_government")]
    private = [x for x in unique if not x.get("is_government")]
    internships = [x for x in private if is_internship_job(x)]
    regular = [x for x in private if not is_internship_job(x)]

    # Internship research is reserved before the ordinary private pool so the
    # dedicated internship feeds cannot disappear behind general Bdjobs volume.
    internship_research = _balanced_internship_research_pool(internships, INTERNSHIP_DETAIL_TARGET)
    internship_keys={x.get("canonical") for x in internship_research}
    remaining_regular_budget=max(0, PRIVATE_DETAIL_TARGET-len(internship_research))
    regular_research=_balanced_regular_private_pool(regular, remaining_regular_budget)

    gov.sort(key=lambda j:(
        -posted_freshness_score({"posted_date":j.get("listing_posted")}),
        -deadline_urgency_score({"deadline":j.get("listing_deadline")}),
        j.get("canonical", ""),
    ))
    private_items = internship_research + regular_research
    logger.info(
        "PRIVATE DETAIL ALLOCATION V1 | regular=%d | internships=%d | BdjobsInternships=%d | BDJobsLiveInternships=%d | BdjobsRegular=%d | BDJobsLiveRegular=%d | budget=%d",
        len(regular_research), len(internship_research),
        sum(1 for x in internship_research if x.get("source") == "Bdjobs"),
        sum(1 for x in internship_research if x.get("source") == "BDJobs Live"),
        sum(1 for x in regular_research if x.get("source") == "Bdjobs"),
        sum(1 for x in regular_research if x.get("source") == "BDJobs Live"),
        PRIVATE_DETAIL_TARGET,
    )
    logger.info(
        "INTERNSHIP DISCOVERED | total=%d | reserved=%d | Bdjobs=%d | BDJobsLive=%d | target=%d",
        len(internships), len(internship_research),
        sum(1 for x in internship_research if x.get("source") == "Bdjobs"),
        sum(1 for x in internship_research if x.get("source") == "BDJobs Live"),
        INTERNSHIP_TARGET_PER_RUN,
    )
    return gov[:GOVERNMENT_DISCOVERY_TARGET], private_items[:PRIVATE_DETAIL_TARGET]


def run(*, dry_run=False, print_ranking=False):
    global AI_DISABLED_FOR_RUN, AI_DISABLE_REASON
    started=time.monotonic()
    AI_DISABLED_FOR_RUN = False
    AI_DISABLE_REASON = ""
    if begin_scheduled_session():
        return {"selected": [], "published": 0, "metrics": {"schedule_skipped": True}}
    logger.info("CAREER NEWS V1 | freshness-first adaptive sources | target=%d max=%d", TARGET_STORIES_PER_RUN, MAX_STORIES_PER_RUN)
    prune_state()
    if dry_run:
        logger.info("DEADLINE SWEEP | skipped in dry-run mode")
    else:
        expire_published_jobs()
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
        logger.info("INTERNSHIP POOL | selected=%d | qualifying_internship_pool=%d", internship_final, sum(1 for j in ranked_private if is_internship_job(j)))
    else:
        logger.info("INTERNSHIP FLOOR | selected=%d/%d configured-minimum", internship_final, MIN_INTERNSHIP_POSTS_PER_RUN)
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
    final_source_mix = {}
    for selected_job in selected:
        src = safe_text(selected_job.get("source")) or "Unknown"
        final_source_mix[src] = final_source_mix.get(src, 0) + 1
    logger.info("FINAL SOURCE MIX | %s", " | ".join(f"{k}={v}" for k,v in sorted(final_source_mix.items())) or "none")
    logger.info(
        "PUBLICATION CAPACITY | private=%d/%d configured-minimum | government=%d/%d configured-minimum | total=%d/%d maximum",
        private_final, MIN_PRIVATE_POSTS_PER_RUN, gov_final, MIN_GOVERNMENT_POSTS_PER_RUN,
        len(selected), MAX_STORIES_PER_RUN,
    )
    if private_final < MIN_PRIVATE_POSTS_PER_RUN:
        logger.info(
            "PRIVATE POOL | selected=%d | qualifying private pool=%d",
            private_final, len(ranked_private),
        )
    if gov_final < MIN_GOVERNMENT_POSTS_PER_RUN:
        logger.info(
            "GOVERNMENT POOL | selected=%d | eligible government pool=%d",
            gov_final, len(select_government_jobs(government_jobs)),
        )
    logger.info("FUNNEL | discovered=%d researched=%d gate_passed=%d snapshot_eligible_private=%d snapshot_eligible_gov=%d private_ranked=%d final_private=%d", len(discovered), len(researched), len(verified), len(snapshot_private), len(snapshot_government), len(ranked_private), private_final)
    logger.info("FUNNEL DETAIL | private_attempted=%d private_success=%d private_fallback=%d private_failed=%d", len(private_items), research_metrics["private"], research_metrics["detail_fallback"], research_metrics["private_failed"])
    research_source_mix = {}
    for researched_job in researched:
        src = safe_text(researched_job.get("source")) or "Unknown"
        research_source_mix[src] = research_source_mix.get(src, 0) + 1
    logger.info("RESEARCH SOURCE MIX | %s", " | ".join(f"{k}={v}" for k,v in sorted(research_source_mix.items())) or "none")
    snapshot_counts=[snapshot_field_quality(j) for j in selected]
    logger.info("SNAPSHOT QUALITY | selected=%d | >=5_fields=%d | avg_fields=%.1f", len(snapshot_counts), sum(1 for n in snapshot_counts if n>=5), (sum(snapshot_counts)/len(snapshot_counts) if snapshot_counts else 0.0))
    logger.info("AI STATUS | disabled_for_run=%s | reason=%s", AI_DISABLED_FOR_RUN, AI_DISABLE_REASON or "none")

    if print_ranking:
        print("=== CAREERNEWSROOM PRIVATE RANKING ===")
        for i,job in enumerate(ranked_private[:25],1):
            print(f"{i}. {job.get('final_score',0):.1f} | {job.get('title','')} | {job.get('company','')} | {job.get('career_category','')}")

    if private_items and research_metrics["private"] == 0 and research_metrics["private_failed"] > 0:
        logger.error("PRIVATE PIPELINE HEALTH | no private research records survived. Government lane may continue, but private lane is unhealthy.")

    if dry_run:
        logger.info("DRY RUN | selected=%d | Telegram not contacted",len(selected))
        STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION
        complete_scheduled_session()
        save_state(STATE)
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
    STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION
    complete_scheduled_session()
    save_state(STATE)
    logger.info("Finished CareerNewsroom. Published=%d | elapsed=%.1fs",published,time.monotonic()-started)
    return {"selected":selected,"published":published,"metrics":{**research_metrics,"gate_counts":gate_counts}}


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    assert PIPELINE_VERSION == "CareerNewsroom V1"
    assert STATE_FORMAT_VERSION == 7
    assert MAX_STORIES_PER_RUN == 25
    assert TARGET_STORIES_PER_RUN == 19
    assert PIPELINE_VERSION == "CareerNewsroom V1"
    assert PRIVATE_TARGET_PER_RUN == 10
    assert MIN_PRIVATE_AGE_YEARS == 18
    assert MAX_PRIVATE_AGE_YEARS == 30
    assert GOVERNMENT_TARGET_PER_RUN == 5
    assert INTERNSHIP_TARGET_PER_RUN == 4
    assert INTERNSHIP_BDJOBS_TARGET == 2
    assert INTERNSHIP_BDJOBSLIVE_TARGET == 2
    assert EXTRA_TARGET_PER_RUN == 6
    assert MIN_PRIVATE_POSTS_PER_RUN == 0
    assert MIN_GOVERNMENT_POSTS_PER_RUN == 0
    assert MIN_PRIVATE_POSTS_PER_RUN + MIN_GOVERNMENT_POSTS_PER_RUN <= MAX_STORIES_PER_RUN
    assert QUALITY_FLOOR == 65
    assert MAX_POST_AGE_DAYS == 3
    freshness_today = datetime.now(BD_TZ).date()
    freshness_ok = {"source":"Bdjobs","title":"Accounts Executive","company":"Example Ltd.","posted_date":(freshness_today - timedelta(days=2)).isoformat(),"deadline":(freshness_today + timedelta(days=2)).isoformat()}
    freshness_old = dict(freshness_ok, posted_date=(freshness_today - timedelta(days=4)).isoformat())
    freshness_unknown = dict(freshness_ok, posted_date="")
    freshness_expired = dict(freshness_ok, deadline=(freshness_today - timedelta(days=1)).isoformat())
    assert len(_filter_private_discovery_by_freshness([freshness_ok])) == 1
    assert len(_filter_private_discovery_by_freshness([freshness_old])) == 0
    assert len(_filter_private_discovery_by_freshness([freshness_unknown])) == 0
    assert len(_filter_private_discovery_by_freshness([freshness_expired])) == 0
    self_test_today = freshness_today
    self_test_posted = self_test_today - timedelta(days=1)
    self_test_deadline = self_test_today + timedelta(days=23)
    self_test_posted_iso = self_test_posted.isoformat()
    self_test_deadline_label = self_test_deadline.strftime("%d %b %Y")
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

    fixture='''<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee","datePosted":"__POSTED__","validThrough":"__DEADLINE_ISO__","hiringOrganization":{"name":"Example Bank"},"jobLocation":{"address":{"addressLocality":"Dhaka","addressCountry":"Bangladesh"}},"employmentType":"FULL_TIME"}</script></head><body><h1>Management Trainee</h1><p>Company Name: Example Bank</p><p>Vacancy: 10</p><p>Education: Bachelor of Business Administration (BBA) or MBA</p><p>Experience: Freshers are encouraged to apply.</p><p>Salary: Tk. 35000 - 45000</p><p>Employment Status: Full Time</p><p>Job Work Place: Work at Office</p><p>Age: 18 to 30 years</p><p>Application: Online</p><p>Application Deadline: __DEADLINE_LABEL__</p></body></html>'''
    fixture=fixture.replace("__POSTED__", self_test_posted_iso).replace("__DEADLINE_ISO__", self_test_deadline.isoformat()).replace("__DEADLINE_LABEL__", self_test_deadline_label)

    fake={"title":"Management Trainee","url":"https://jobs.bdjobs.com/jobdetails.asp?id=123","canonical":canonical_url("https://jobs.bdjobs.com/jobdetails.asp?id=123"),"source":"Bdjobs","discovery":"self_test"}
    text_source=_text_from_html(fixture); fields=extract_job_fields(text_source,fixture,fake["url"],fake); fields.update({"raw_text":text_source,"apply_url":"","canonical":fake["canonical"],"audience_pre_score":job_family_score(fields["title"],text_source)})
    fields["bba_mba_target_score"]=bba_mba_candidate_score(fields)
    assert fields["title"] == "Management Trainee" and fields["company"] == "Example Bank"
    assert "BBA" in fields["education"] and "MBA" in fields["education"]
    assert fields["experience"] == "Freshers" and fields["vacancy"] == "10" and fields["deadline"] == self_test_deadline.isoformat()
    score,components=private_rank_score(fields); assert 0 <= score <= 100 and sum(components.values()) == score
    too_high=dict(fields,experience="5 to 8 years"); ok,reason=deterministic_job_gate(too_high); assert not ok and reason=="experience_above_3_years"
    allowed=dict(fields,experience="2 to 3 years"); ok,reason=deterministic_job_gate(allowed); assert ok and reason=="ok_bba_mba_target"

    listing='''<html><body><div><a href="/jobdetails.asp?id=101">Accounts Executive</a><span>Published: 2026-09-19 Deadline: 2026-10-01 Dhaka</span></div><div><a href="/jobdetails.asp?id=102">Software Engineer</a><span>Published: 2026-09-19 Deadline: 2026-10-01 Dhaka</span></div></body></html>'''
    candidates=_bdjobs_listing_candidates(listing,"https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=1",1,"Accounting / Finance"); assert {x["source_job_id"] for x in candidates} == {"101","102"}
    tel=_teletalk_record_fields({"job_primary_id":"TL-1001","job_title":"Accounts Assistant","org_name":"Example Government Department","vacancy":"12","deadline_date":"2026-10-10","application_site_url":"https://example.teletalk.com.bd/"}); assert tel["source"]=="Teletalk" and tel["source_job_id"]=="TL-1001"
    a={"source":"Bdjobs","source_job_id":"1","title":"Marketing Executive","company":"Example Ltd.","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=1","posted_date":"2026-09-19"}; b={"source":"Bdjobs","source_job_id":"","title":"Executive - Marketing","company":"Example Limited","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=2","posted_date":"2026-09-19"}; assert likely_same_job(a,b)
    c={"source":"Bdjobs","source_job_id":"10","title":"Assistant Manager","company":"Example Ltd.","location":"Dhaka","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=10","posted_date":"2026-09-19"}; d=dict(c,source_job_id="11",source_url="https://jobs.bdjobs.com/jobdetails.asp?id=11"); assert not likely_same_job(c,d)
    circular_a={"source":"Teletalk","source_job_id":"TL-1","title":"Accounts Officer","company":"Example Authority","location":"Dhaka","source_url":"https://example.teletalk.com.bd/1","apply_url":"https://example.teletalk.com.bd/apply","posted_date":"2026-09-24"}; circular_b=dict(circular_a,source_job_id="TL-2",title="Office Assistant",source_url="https://example.teletalk.com.bd/2"); assert not likely_same_job(circular_a,circular_b), "same circular endpoint with different roles must remain distinct"
    live_gate={"title":"Senior Executive - Accounts & Finance","company":"Example Private Ltd.","source":"BDJobs Live","source_url":"https://bdjobslive.com/bdjobs-details/senior-executive-accounts-finance-13031","listing_posted":self_test_posted_iso,"posted_date":self_test_posted_iso,"deadline":self_test_deadline.isoformat(),"experience":"1 to 2 years","education":"BBA","raw_text":"BBA Accounts Finance Executive"}; assert deterministic_job_gate(live_gate)[0], "BDJobs Live private source must pass the same private gate"
    listing_item={
        "title":"Accounts Executive", "url":"https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1",
        "canonical":canonical_url("https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1"), "source":"Bdjobs",
        "source_job_id":"777001", "category_id":1, "category_name":"Accounting / Finance",
        "listing_posted":self_test_posted_iso, "listing_deadline":self_test_deadline.isoformat(),
        "listing_fields":{"company":"Example Finance Ltd.","experience":"1 to 2 years","education":"BBA","deadline":self_test_deadline.isoformat(),"location":"Dhaka","salary":"Tk. 30,000","vacancy":"3"},
        "excerpt":f"Accounts Executive Example Finance Ltd. Dhaka Experience required: 1 to 2 year(s) Deadline: {self_test_deadline_label} Education required: BBA Vacancy: 3",
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

    # Source-label collision regressions: section headings such as `Salary &
    # Other Benefits` and `Additional Requirements` must never become field values.
    salary_collision = """
    Job Summary
    Salary & Other Benefits
    Monthly salary: BDT 18,000 - 22,000
    Experience
    Additional Requirements
    Experience: At Least 3 Year
    Application Deadline
    02 Oct 2026
    """
    collision_fields = _extract_source_label_fields(salary_collision)
    assert collision_fields["salary"] == "BDT 18,000 - 22,000"
    assert collision_fields["experience"] == "At Least 3 Year"
    assert collision_fields["deadline"] == "02 Oct 2026"
    assert not _valid_source_field_value("salary", "& Benefits: Negotiable")
    assert not _valid_source_field_value("salary", "Salary & Other Benefits")

    # Bdjobs Angular regression: the summary may expose a benefits heading while
    # the authoritative monthly salary is present in the body/requirements area.
    bd_salary_fixture = """
    <html><body><app-details-main>
      <button><h2>Example Finance Ltd.</h2></button>
      <h2>Assistant Manager - Sales And Marketing</h2>
      <app-summary><div id="allSection">
        <span>Location:</span><span>Dhanmondi</span>
        <span>Salary:</span><span>&amp; Other Benefits</span>
        <span>Experience:</span><span>At Least 3 Year</span>
        <span>Published:</span><span>24 Sep 2026</span>
      </div></app-summary>
      <div><p>Compensation &amp; Other Benefits</p><ul><li>Monthly salary: Tk. 18,000 - 22,000 (Monthly)</li></ul></div>
      <div><p>Employment Status :</p><p>Full Time</p></div>
      <div><p>Workplace :</p><p>Work at office</p></div>
      <p>Application Deadline :</p><p>02 Oct 2026</p>
      <div id="requirements">
        <div><p>Education</p><ul><li>BBA</li></ul></div>
        <div><p>Experience</p><ul><li>At Least 3 Year</li></ul></div>
      </div>
    </app-details-main></body></html>
    """
    bd_salary_dom = _bdjobs_dom_extract(
        bd_salary_fixture,
        url="https://bdjobs.com/h/details/1533488?ln=1",
        listing_title="Assistant Manager - Sales And Marketing",
    )
    assert bd_salary_dom["salary"] == "Tk. 18,000 - 22,000 (Monthly)"
    bd_salary_fields = extract_job_fields(
        _text_from_html(bd_salary_fixture), bd_salary_fixture,
        "https://bdjobs.com/h/details/1533488?ln=1",
        {"title":"Assistant Manager - Sales And Marketing","source":"Bdjobs"},
    )
    assert bd_salary_fields["salary"] == "Tk. 18,000 - 22,000/month"

    live_salary_fixture = """
    <html><head><meta property="og:title" content="Senior M&E and MIS Officer | Example Bank"></head><body>
      <h1>Senior M&amp;E and MIS Officer</h1>
      <a href="/company-detail/example-bank">Example Bank</a>
      <div class="summary">
        <span>Salary:</span><span>&amp; Benefits</span>
        <span>Experience:</span><span>Additional Requirements</span>
        <span>Location:</span><span>Dhaka</span>
      </div>
      <div id="section-experience"><h3>Experience</h3><p>At least 3 years</p></div>
      <div id="section-education"><h3>Education</h3><p>BBA / MBA</p></div>
      <p>Monthly salary: From 95,325 BDT</p>
      <p>Application Deadline: 30 Sep 2026</p>
      <div id="section-company"><p>Address: Dhaka</p><a href="https://example.com">Website</a></div>
    </body></html>
    """
    live_dom = _bdjobslive_dom_extract(live_salary_fixture, "https://www.bdjobslive.com/bdjobs-details/senior-me-mis-officer-13472", "Senior M&E and MIS Officer")
    assert live_dom["salary"] == "From 95,325 BDT"
    assert "3 years" in live_dom["experience"]

    # Cross-source mirror regression: the same company/title on Bdjobs and BDJobs Live
    # must collapse even when URLs and native source IDs differ.
    mirror_a = {
        "source": "Bdjobs", "source_job_id": "BD-100", "title": "Executive - Finance",
        "company": "Example Finance Ltd.", "location": "Dhaka",
        "source_url": "https://jobs.bdjobs.com/jobdetails.asp?id=100",
        "posted_date": "2026-09-24",
    }
    mirror_b = {
        "source": "BDJobs Live", "source_job_id": "9001", "title": "Executive - Finance",
        "company": "Example Finance Limited", "location": "Dhaka",
        "source_url": "https://bdjobslive.com/bdjobs-details/executive-finance-9001",
        "posted_date": "2026-09-24",
    }
    assert likely_same_job(mirror_a, mirror_b), "BDJobs Live cross-source mirror dedupe failed"

    # The same vacancy can appear in a regular feed and a dedicated internship
    # feed. The merged record must retain the internship lane instead of losing it.
    regular_copy = dict(mirror_a, source="Bdjobs", source_job_id="BI-1", discovery="bdjobs_category")
    internship_copy = dict(mirror_a, source_job_id="BI-1", discovery="bdjobs_internship",
                            lane="internship", is_internship_source=True, category_name="Internship")
    merged_copy = build_unique_job_pool([regular_copy, internship_copy])
    assert len(merged_copy) == 1
    assert is_internship_job(merged_copy[0])
    assert "bdjobs_internship" in merged_copy[0].get("discovery_sources", [])

    live_listing = """
    <html><body>
      <div class="job-card">
        <a href="https://bdjobslive.com/bdjobs-details/executive-finance-9001">Executive - Finance</a>
        <span>Example Finance Limited</span>
        <span>Accounting/ Finance</span>
        <span>Location: Dhaka</span>
        <span>Salary: 25000 - 35000 BDT</span>
        <span>Experience: 0-1 Year</span>
        <span>Application Deadline: 30 Sep 2026</span>
        <span>Published: 24 Sep 2026</span>
      </div>
    </body></html>
    """
    live_candidates = _bdjobslive_listing_candidates(
        live_listing, "https://bdjobslive.com/bdjobs/accounting-finance", "Accounting / Finance"
    )
    assert len(live_candidates) == 1
    live_item = live_candidates[0]
    assert live_item["source"] == "BDJobs Live"
    assert live_item["source_job_id"] == "9001"
    assert live_item["listing_posted"] == "2026-09-24"
    assert live_item["listing_deadline"] == "2026-09-30"
    assert _is_bdjobslive_detail_url(live_item["url"])

    # BDJobs Live browser-fallback regression: direct HTTP may return only the
    # client-rendered shell, so the real-browser path must remain independently usable.
    class _FakeBDJobsLivePage:
        def __init__(self, html_text, url):
            self.html_content = html_text
            self.body = html_text.encode("utf-8")
            self.url = url
        def css(self, selector):
            return []

    class _FakeBDJobsLiveFetcher:
        last_kwargs = {}
        @staticmethod
        def fetch(url, **kwargs):
            _FakeBDJobsLiveFetcher.last_kwargs = dict(kwargs)
            return _FakeBDJobsLivePage(live_detail_text_fixture, url)

    live_detail_text_fixture = """
    <html><body>
      <h1>Executive - Finance</h1>
      <p>Company Name: Example Finance Limited</p>
      <p>Location: Dhaka</p>
      <p>Salary: 25000 - 35000 BDT</p>
      <p>Experience: 0-1 Year</p>
      <p>Education: Bachelor/Honors, BBA</p>
      <p>Application Deadline: 30 Sep 2026</p>
      <p>Published: 24 Sep 2026</p>
      <p>Job Type: Full Time/Permanent</p>
      <p>Workplace: from office</p>
    </body></html>
    """
    original_live_fetcher = globals().get("StealthyFetcher")
    try:
        globals()["StealthyFetcher"] = _FakeBDJobsLiveFetcher
        browser_probe = _fetch_bdjobslive_browser_document(
            live_item["url"], mode="detail"
        )
    finally:
        globals()["StealthyFetcher"] = original_live_fetcher
    assert browser_probe and browser_probe["backend"] == "scrapling_stealthy_bdjobslive"
    assert _FakeBDJobsLiveFetcher.last_kwargs.get("wait_selector") == "h1"
    assert _FakeBDJobsLiveFetcher.last_kwargs.get("solve_cloudflare") is False
    assert _FakeBDJobsLiveFetcher.last_kwargs.get("network_idle") is False
    assert int(_FakeBDJobsLiveFetcher.last_kwargs.get("timeout", 999999)) <= 15000

    # Listing-render regression: slow category templates must wait on the real
    # job-detail anchor and retry with a longer render window when the first
    # browser pass returns only the client shell.
    class _FakeBDJobsLiveListingFetcher:
        calls = []
        @staticmethod
        def fetch(url, **kwargs):
            _FakeBDJobsLiveListingFetcher.calls.append(dict(kwargs))
            if len(_FakeBDJobsLiveListingFetcher.calls) == 1:
                html_text = "<html><body><div id='app'>loading...</div></body></html>"
            else:
                html_text = "<html><body><a href='/bdjobs-details/executive-finance-9002'>Executive - Finance</a></body></html>"
            return _FakeBDJobsLivePage(html_text, url)

    original_live_fetcher = globals().get("StealthyFetcher")
    original_fetch_count = globals().get("BDJOBSLIVE_BROWSER_FETCH_COUNT", 0)
    try:
        globals()["StealthyFetcher"] = _FakeBDJobsLiveListingFetcher
        globals()["BDJOBSLIVE_BROWSER_FETCH_COUNT"] = 0
        listing_browser_probe = _fetch_bdjobslive_browser_document(
            "https://www.bdjobslive.com/bdjobs-circular/bank-financial-institution-jobs", mode="listing"
        )
    finally:
        globals()["StealthyFetcher"] = original_live_fetcher
        globals()["BDJOBSLIVE_BROWSER_FETCH_COUNT"] = original_fetch_count
    assert listing_browser_probe and len(_FakeBDJobsLiveListingFetcher.calls) == 2
    assert all(call.get("wait_selector") == 'a[href*="/bdjobs-details/"]' for call in _FakeBDJobsLiveListingFetcher.calls)
    assert all(call.get("wait_selector_state") == "attached" for call in _FakeBDJobsLiveListingFetcher.calls)
    assert all(call.get("network_idle") is True for call in _FakeBDJobsLiveListingFetcher.calls)
    assert _FakeBDJobsLiveListingFetcher.calls[1]["wait"] > _FakeBDJobsLiveListingFetcher.calls[0]["wait"]

    live_detail_text = """
    Job List
    Example Finance Limited
    Accounting/ Finance
    Executive - Finance
    Application Deadline : 30 Sep 2026
    Vacancy: 2
    Age: At least 18 Years
    Location: Dhaka
    Salary: 25000 - 35000 BDT
    Experience: 0-1 Year
    Gender: No Preference
    Job Type: Full Time/Permanent
    Published: 24 Sep 2026
    Education
    Bachelor/Honors, BBA
    Workplace
    from office
    Employment Status
    Full Time/Permanent
    Application
    Online
    """
    live_detail_html = """
    <html lang="en"><head>
      <title>Junior Audit Officer | Advanced Chemical Industries PLC (ACI) - BDJobs Live</title>
      <link rel="canonical" href="https://www.bdjobslive.com/bdjobs-details/junior-audit-officer-13486">
      <meta name="description" content="Advanced Chemical Industries PLC (ACI) is looking for a Junior Audit Officer on BDJobs Live. Category in Accounting/ Finance.">
      <meta property="og:title" content="Junior Audit Officer | Advanced Chemical Industries PLC (ACI) - BDJobs Live">
      <meta property="og:image" content="https://www.bdjobslive.com/media/v1/uploads/og_images/2026/09/junior-audit-officer-13486.jpg">
    </head><body>
      <h1>Junior Audit Officer</h1>
      <a href="/company-detail/advanced-chemical-industries-plc-aci-3430">Advanced Chemical Industries PLC (ACI)</a>
      <p>Manufacturing Company</p>
      <div>Application Deadline : 20 Oct 2026</div>
      <span>Vacancy:</span><strong>N/A</strong>
      <span>Age:</span><strong>Not Specified</strong>
      <span>Location:</span><strong>Anywhere in Bangladesh</strong>
      <span>Salary:</span><strong>Negotiable</strong>
      <span>Experience:</span><strong>1-2 Year</strong>
      <span>Gender:</span><strong>Male</strong>
      <span>Job Type:</span><strong>Contract</strong>
      <span>Job Shift:</span><strong>Day Shift</strong>
      <span>Published:</span><strong>23 Sept 2026</strong>
      <section id="section-education"><h3>Education</h3><ul><li>Bachelor/Honors, Bachelor of Business Studies (BBS)</li></ul></section>
      <section id="section-experience"><h3>Experience</h3><p>1-2 Year</p></section>
      <section id="section-skills"><h3>Skills</h3><ul><li>market research</li><li>Audit</li></ul></section>
      <section id="section-responsibilities"><h3>Responsibilities &amp; Context</h3><ul><li>Conduct surprise visits to outlets.</li><li>Participate in inventory counts.</li></ul></section>
      <section id="section-company"><h3>Company Information</h3><p>Address: ACI Centre, 245 Tejgaon Industrial Area, Dhaka-1208.</p><p>Website: <a href="https://www.aci-bd.com/">https://www.aci-bd.com/</a></p></section>
    </body></html>
    """
    live_dom_fields = extract_job_fields(
        _text_from_html(live_detail_html), live_detail_html,
        "https://www.bdjobslive.com/bdjobs-details/junior-audit-officer-13486",
        {"source":"BDJobs Live","title":"Junior Audit Officer","source_url":"https://www.bdjobslive.com/bdjobs-details/junior-audit-officer-13486"},
    )
    assert live_dom_fields["title"] == "Junior Audit Officer"
    assert live_dom_fields["company"] == "Advanced Chemical Industries PLC (ACI)"
    assert live_dom_fields["salary"] == "Negotiable"
    assert live_dom_fields["deadline"] == "2026-10-20"
    assert "BBS" in live_dom_fields["education"]
    assert live_dom_fields["company_website"] == "https://www.aci-bd.com/"

    live_fields = extract_job_fields(
        live_detail_text, "", live_item["url"], live_item
    )
    assert live_fields["company"] == "Example Finance Limited"
    assert live_fields["salary"] == "25000 - 35000 BDT"
    assert live_fields["experience"] == "0-1 Year"
    assert "BBA" in live_fields["education"]
    assert live_fields["deadline"] == "2026-09-30"
    assert live_fields["posted_date"] == "2026-09-24"
    live_fields["raw_text"] = live_detail_text
    assert bba_mba_candidate_score(live_fields) > 0

    # Map-based BDJobs Live fields are source-authoritative even when generic
    # labels contain nearby UI headings.
    assert live_dom_fields["experience"] == "1-2 Year"
    assert live_dom_fields["location"] == "Anywhere in Bangladesh"
    assert live_dom_fields["job_shift"] == "Day Shift"
    assert live_dom_fields["gender"] == "Male"

    # Dedicated internship discovery flags override title-text heuristics.
    internship_flagged = {
        "source": "BDJobs Live", "title": "Finance Trainee", "lane": "internship",
        "is_internship_source": True, "location": "Dhaka",
        "deadline": self_test_deadline.isoformat(), "education": "BBA",
        "company": "Example Ltd.", "source_url": "https://www.bdjobslive.com/bdjobs-details/finance-trainee-9002",
    }
    assert is_internship_job(internship_flagged)

    # Internship snapshots still require core freshness/location data, but they
    # do not require normal private-job experience.
    internship_missing_exp = dict(internship_flagged, experience="", salary="Tk. 15,000",
                                  employment_type="Internship", posted_date=self_test_posted_iso)
    ok, reason = snapshot_integrity(internship_missing_exp)
    assert ok, reason
    internship_missing_deadline = dict(internship_missing_exp, deadline="")
    ok, reason = snapshot_integrity(internship_missing_deadline)
    assert not ok and reason == "private_missing_core_deadline"

    live_collision_html = live_detail_html.replace(
        '<span>Salary:</span><strong>Negotiable</strong>',
        '<span>Salary &amp; Benefits</span><strong>&amp; Benefits</strong><div><span>Monthly salary:</span><strong>From 95325 BDT</strong></div>'
    )
    live_collision = extract_job_fields(
        _text_from_html(live_collision_html), live_collision_html,
        "https://www.bdjobslive.com/bdjobs-details/senior-me-mis-officer-13472",
        {"source":"BDJobs Live","title":"Senior M&E and MIS Officer", "category_name":"NGO / Development"},
    )
    assert live_collision["salary"] == "From 95325 BDT"

    # BDJobs Live discovery must remain strictly inside the configured category feeds.
    saved_live_enabled = BDJOBSLIVE_ENABLED
    saved_live_category_fn = globals()["discover_bdjobslive_category"]
    saved_posted_urls = set(POSTED_URLS)
    try:
        globals()["BDJOBSLIVE_ENABLED"] = True
        globals()["discover_bdjobslive_category"] = lambda *args, **kwargs: [{
            "source":"BDJobs Live",
            "title":"Finance Executive",
            "company":"Example Finance Ltd.",
            "source_url":"https://www.bdjobslive.com/bdjobs-details/finance-executive-987654",
            "url":"https://www.bdjobslive.com/bdjobs-details/finance-executive-987654",
            "canonical":"bdjobslive.com/bdjobs-details/finance-executive-987654",
            "source_job_id":"987654",
            "listing_posted":self_test_posted_iso,
            "listing_deadline":self_test_deadline.isoformat(),
            "category_name":"Accounting / Finance",
            "discovery":"bdjobslive_category",
        }]
        POSTED_URLS.clear()
        strict_items = discover_bdjobslive()
        assert len(strict_items) == 1
        assert strict_items[0]["discovery"] == "bdjobslive_category"
    finally:
        globals()["BDJOBSLIVE_ENABLED"] = saved_live_enabled
        globals()["discover_bdjobslive_category"] = saved_live_category_fn
        POSTED_URLS.clear()
        POSTED_URLS.update(saved_posted_urls)

    # Private detail research uses a combined pool. Both private sources receive
    # a small freshness-aware reserve when they have enough supply, then the
    # remaining budget is filled globally. There is no fixed BDJobs Live percentage.
    saved_detail_target = PRIVATE_DETAIL_TARGET
    try:
        globals()["PRIVATE_DETAIL_TARGET"] = 20
        allocation_fixture = []
        for i in range(60):
            allocation_fixture.append({
                "source":"Bdjobs", "source_job_id":f"B{i}", "title":f"Finance Executive {i}",
                "company":f"Bdjobs Example {i}", "location":"Dhaka",
                "canonical":f"https://jobs.bdjobs.com/jobdetails.asp?id={700000+i}",
                "source_url":f"https://jobs.bdjobs.com/jobdetails.asp?id={700000+i}",
                "listing_posted":self_test_posted_iso, "is_government":False,
                "education":"BBA", "experience":"1 to 2 years", "deadline":self_test_deadline.isoformat(),
                "raw_text":"Finance Executive BBA management sales",
            })
        for i in range(10):
            allocation_fixture.append({
                "source":"BDJobs Live", "source_job_id":f"L{i}", "title":f"Marketing Executive Live {i}",
                "company":f"BDJobs Live Example {i}", "location":"Dhaka",
                "canonical":f"https://bdjobslive.com/bdjobs-details/marketing-executive-live-{800000+i}",
                "source_url":f"https://bdjobslive.com/bdjobs-details/marketing-executive-live-{800000+i}",
                "listing_posted":self_test_posted_iso, "is_government":False,
                "category_name":"Marketing / Sales", "education":"BBA", "experience":"1 to 2 years",
                "deadline":self_test_deadline.isoformat(), "raw_text":"Marketing Executive BBA sales",
            })
        _, allocation_private = _prepare_shortlists(allocation_fixture)
        live_count = sum(1 for x in allocation_private if x.get("source") == "BDJobs Live")
        assert live_count >= PRIVATE_DETAIL_SOURCE_RESERVE
        assert 4 <= live_count <= 8, f"adaptive source allocation should preserve proportionality, got {live_count}"

        stale_fixture = [
            dict(allocation_fixture[0], listing_posted=(self_test_today - timedelta(days=8)).isoformat(), canonical="https://jobs.bdjobs.com/jobdetails.asp?id=990001"),
            dict(allocation_fixture[1], listing_posted=self_test_posted_iso, canonical="https://jobs.bdjobs.com/jobdetails.asp?id=990002"),
            dict(allocation_fixture[2], listing_posted="", canonical="https://jobs.bdjobs.com/jobdetails.asp?id=990003"),
        ]
        stale_result = _filter_private_discovery_by_freshness(stale_fixture)
        assert len(stale_result) == 1
        assert all(x.get("discovery_freshness") == "fresh" for x in stale_result)
        assert all(x.get("canonical") != "https://jobs.bdjobs.com/jobdetails.asp?id=990001" for x in stale_result)
        assert all(x.get("canonical") != "https://jobs.bdjobs.com/jobdetails.asp?id=990003" for x in stale_result)
    finally:
        globals()["PRIVATE_DETAIL_TARGET"] = saved_detail_target

    same_source_repost_a = dict(mirror_a)
    same_source_repost_b = dict(mirror_a, source_job_id="BD-101", source_url="https://jobs.bdjobs.com/jobdetails.asp?id=101")
    assert not likely_same_job(same_source_repost_a, same_source_repost_b), "same-source distinct IDs were over-deduped"

    original_detail= _fetch_bdjobs_detail
    try:
        globals()["_fetch_bdjobs_detail"] = lambda item: None
        assert retrieve_job_content(listing_item)["detail_quality"] == "listing_fallback"
    finally:
        globals()["_fetch_bdjobs_detail"] = original_detail

    current_bdjobs = f"""
    Averroes International School
    Logistics Executive
    Application Deadline :
    {self_test_deadline_label}
    Vacancy: 01
    Age: 26 to 28 years
    Location: Dhaka
    Salary: Negotiable
    Experience: 2 to 3 years
    Published: {self_test_posted.strftime("%d %b %Y")}
    Requirements
    Education
    Bachelor of Business Administration (BBA)
    Workplace
    Work at office
    Employment Status
    Full Time
    Application
    Online
    """
    bd_fields=extract_job_fields(current_bdjobs,"","https://bdjobs.com/h/details/999001?ln=1",{"title":"Logistics Executive"})
    assert bd_fields["company"]=="Averroes International School"
    assert bd_fields["location"]=="Dhaka"
    assert bd_fields["salary"]=="Negotiable"
    assert bd_fields["vacancy"]=="01"
    assert bd_fields["experience"]=="2 to 3 years"
    assert bd_fields["age"]=="26-28 Years"
    assert age_bounds("18 to 30 years") == (18, 30)
    assert age_bounds("At least 18 Years") == (18, None)
    assert age_bounds("At most 30 Years") == (None, 30)
    assert age_bounds("30 Years") == (None, 30)
    assert normalize_relative_date_text("today") == self_test_today.isoformat()
    assert normalize_relative_date_text("2 days ago") == (self_test_today - timedelta(days=2)).isoformat()
    assert extract_posted_date_from_fragment("Published: 25 Sep 2026 Deadline: 01 Oct 2026") == "2026-09-25"
    assert extract_posted_date_from_fragment("Posted: 2 days ago Deadline: 30 Sep 2026") == (self_test_today - timedelta(days=2)).isoformat()
    live_marketing = dict(live_gate, category="Other Business Services", title="Media Buyer & Performance Marketing Specialist", raw_text="Media Buyer & Performance Marketing Specialist BBA marketing performance marketing")
    assert deterministic_job_gate(live_marketing)[0], "Clearly business-relevant BDJobs Live marketing roles must not be rejected by taxonomy wording alone"
    age_fixture = dict(bd_fields, source="Bdjobs", source_url="https://bdjobs.com/h/details/999001?ln=1", company="Averroes International School", experience="2 to 3 years", education="BBA", posted_date=self_test_posted_iso, deadline=self_test_deadline.isoformat(), raw_text="Logistics Executive BBA")
    for valid_age in ("18 to 30 Years", "21 to 30 Years", "18 to 28 Years", "At least 18 Years", "At most 30 Years", "Not Specified", ""):
        candidate = dict(age_fixture, age=valid_age)
        ok, reason = deterministic_job_gate(candidate)
        assert ok, f"expected compatible age to pass: {valid_age!r} -> {reason}"
    for invalid_age in ("18 to 31 Years", "20 to 35 Years", "31 Years", "17 to 30 Years", "At least 31 Years", "At most 17 Years"):
        candidate = dict(age_fixture, age=invalid_age)
        ok, reason = deterministic_job_gate(candidate)
        assert not ok and reason == "age_outside_18_30", f"expected incompatible age to fail: {invalid_age!r} -> {reason}"
    assert bd_fields["posted_date"]==self_test_posted_iso
    assert parse_datetime(bd_fields["deadline"]).date()==self_test_deadline
    assert bd_fields["employment_type"]=="Full Time"
    assert bd_fields["workplace"]=="On-site"
    assert bd_fields["application_method"]=="Online"
    assert bd_fields["age"] != bd_fields["experience"]
    assert "uniqueItems" not in json.dumps(JUDGE_SCHEMA)

    # Deadline expiry uses the entire local deadline date, not midnight UTC.
    now_for_test = datetime(2026, 9, 20, 12, 0, tzinfo=BD_TZ)
    assert not _deadline_has_expired("2026-09-20", now=now_for_test)
    assert _deadline_has_expired("2026-09-19", now=now_for_test)
    assert _deadline_expiry_datetime("20 Sep 2026").hour == 23

    # Expired markup is red/danger and points only to the CareerNewsroom promo URL.
    expired_markup = _button_markup({"apply_url":"https://example.com/apply","source_url":"https://example.com/job"}, expired=True)
    expired_button = expired_markup["inline_keyboard"][0][0]
    assert expired_button["text"] == "EXPIRED DEADLINE"
    assert expired_button["style"] == "danger"
    assert expired_button["url"] == TELEGRAM_PROMO_URL
    active_markup = _button_markup({"apply_url":"https://example.com/apply","source_url":"https://example.com/job"})
    assert active_markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    assert active_markup["inline_keyboard"][0][0]["url"] == "https://example.com/apply"

    expired_render_job = dict(bd_fields, source="Bdjobs", source_url="https://example.com/job", apply_url="https://example.com/apply")
    expired_blocks = rich_message_blocks(expired_render_job, expired=True)
    footer_blocks = [b for b in expired_blocks if b.get("type") == "footer"]
    assert footer_blocks and "https://example.com/job" not in json.dumps(footer_blocks, ensure_ascii=False)

    original_telegram_call = telegram_call
    original_state_file_text = Path(STATE_FILE).read_text(encoding="utf-8") if Path(STATE_FILE).exists() else None
    try:
        captured = []
        def fake_telegram_call(method, data=None, files=None):
            captured.append((method, data or {}))
            return {"ok": True, "result": {"message_id": 42}}
        globals()["telegram_call"] = fake_telegram_call
        state_before = json.loads(json.dumps(STATE))
        STATE["queue"]["https://example.com/job"] = dict(expired_render_job, canonical="https://example.com/job", status="posted")
        self_test_expired_date = self_test_today - timedelta(days=1)
        STATE["events"]["expiry-self-test"] = {
            "event_id":"expiry-self-test", "canonical_url":"https://example.com/job",
            "source_url":"https://example.com/job", "apply_url":"https://example.com/apply",
            "source":"Bdjobs", "source_job_id":"999001", "title":expired_render_job["title"],
            "company":expired_render_job["company"], "deadline":self_test_expired_date.isoformat(),
            "status":"published", "published_at":f"{self_test_expired_date.isoformat()}T12:00:00+06:00", "message_id":42,
        }
        original_token = TELEGRAM_BOT_TOKEN
        globals()["TELEGRAM_BOT_TOKEN"] = "self-test-token"
        sweep = expire_published_jobs()
        assert sweep["updated"] >= 1
        assert STATE["events"]["expiry-self-test"]["status"] == "expired"
        assert STATE["events"]["expiry-self-test"]["expiry_update_status"] == "expired_full"
        edit_calls = [x for x in captured if x[0] == "editMessageText"]
        assert edit_calls
        edit_data = edit_calls[-1][1]
        assert json.loads(edit_data["reply_markup"])["inline_keyboard"][0][0]["style"] == "danger"
        rich_payload = json.loads(edit_data["rich_message"])
        assert "https://example.com/job" not in json.dumps(rich_payload, ensure_ascii=False)
    finally:
        globals()["telegram_call"] = original_telegram_call
        globals()["TELEGRAM_BOT_TOKEN"] = original_token
        # Restore the exact in-memory state after the expiry test.
        STATE.clear(); STATE.update(state_before)
        if original_state_file_text is None:
            try:
                Path(STATE_FILE).unlink()
            except FileNotFoundError:
                pass
        else:
            Path(STATE_FILE).write_text(original_state_file_text, encoding="utf-8")

    internship=dict(bd_fields,title="Finance Internship",employment_type="Internship",experience="",is_government=False,source_url="https://bdjobs.com/h/details/999001?ln=1",company="Example Bank")
    ok,reason=snapshot_integrity(internship)
    assert ok, reason
    assert "#Internship" in job_hashtags(internship)
    gov=dict(internship,is_government=True)
    assert "#GovtJob" in job_hashtags(gov)
    internship["display_fields"]=["location","salary","vacancy","deadline","posted_date"]
    assert set(dict(job_snapshot_rows(internship))) >= {"Location","Salary","Vacancy","Deadline","Posted"}

    # AI display preferences must never hide a source-backed salary.
    salary_job=dict(internship)
    salary_job["display_fields"]=["location","deadline","posted_date"]
    salary_job["salary"]="Tk. 25,000 - 35,000 (Monthly)"
    salary_available=[key for key in ("location","employment_type","workplace","education","experience","salary","vacancy","age","application_method","deadline","posted_date") if safe_text(salary_job.get(key))]
    salary_display=list(dict.fromkeys([key for key in salary_job["display_fields"] if key in salary_available]))
    for required_key in ("salary","deadline","posted_date","location"):
        if required_key in salary_available and required_key not in salary_display:
            salary_display.append(required_key)
    salary_job["display_fields"]=salary_display
    assert "Salary" in dict(job_snapshot_rows(salary_job))
    assert dict(job_snapshot_rows(salary_job))["Salary"]=="Tk. 25,000 - 35,000 (Monthly)"

    # Current Angular Bdjobs regression fixture: the real job title is an h2
    # following the company button. The only h1 is footer chrome.
    bd_dom_fixture = f"""
    <html><head>
      <title>Manager / Senior Manager - Recovery : Hudhud Solutions PLC || Bdjobs.com</title>
      <meta property="og:title" content="Manager / Senior Manager - Recovery : Hudhud Solutions PLC || Bdjobs.com">
    </head><body>
      <app-details-main>
        <button><h2>Hudhud Solutions PLC</h2></button>
        <h2>Manager / Senior Manager - Recovery</h2>
        <app-summary><div id="allSection">
          <span>Vacancy:</span><span>--</span>
          <span>Age:</span><span>30 to 35 years</span>
          <span>Location:</span><span>Dhaka</span>
          <span>Salary:</span><span>Tk. 40,000 - 60,000</span>
          <span>Experience:</span><span>3 to 5 years</span>
          <span>Published:</span><span>{self_test_posted_iso}</span>
        </div></app-summary>
        <div id="requirements">
          <div><p>Education</p><ul><li>BBA / MBA</li></ul></div>
          <div><p>Experience</p><ul><li>3 to 5 years in recovery or collections</li></ul></div>
          <div><p>Additional Requirements</p><ul><li>Strong communication skills</li></ul></div>
        </div>
        <div><p>Workplace :</p><p>Work at office</p></div>
        <div><p>Employment Status :</p><p>Full Time</p></div>
        <div id="skills"><button>Recovery</button><button>Collection</button></div>
        <p>Application Deadline :</p><p>{(self_test_today + timedelta(days=21)).strftime("%d %b %Y")}</p>
      </app-details-main>
      <footer id="footerhide"><h1>Our Valuable Partners</h1></footer>
    </body></html>
    """
    bd_dom = _bdjobs_dom_extract(
        bd_dom_fixture,
        url="https://bdjobs.com/h/details/1533487?ln=1",
        listing_title="Manager / Senior Manager - Recovery",
    )
    assert bd_dom["title"] == "Manager / Senior Manager - Recovery"
    assert bd_dom["company"] == "Hudhud Solutions PLC"
    assert bd_dom["identity_status"] == "matched"
    assert bd_dom["is_job_page"]
    assert bd_dom["vacancy"] is None
    assert bd_dom["education"] == "BBA / MBA"
    self_test_dom_deadline_label = (self_test_today + timedelta(days=21)).strftime("%d %b %Y")
    assert bd_dom["deadline"] == self_test_dom_deadline_label
    assert bd_dom["workplace"] == "Work at office"
    assert bd_dom["employment_type"] == "Full Time"

    dom_fields = extract_job_fields(
        _text_from_html(bd_dom_fixture),
        bd_dom_fixture,
        "https://bdjobs.com/h/details/1533487?ln=1",
        {"title":"Manager / Senior Manager - Recovery","source":"Bdjobs"},
    )
    assert dom_fields["title"] == "Manager / Senior Manager - Recovery"
    assert dom_fields["company"] == "Hudhud Solutions PLC"
    assert dom_fields["vacancy"] == ""
    assert dom_fields["detail_identity_status"] == "matched"

    # Case C: strip the company/site suffix from <title>.
    head_case = """<html><head><title>Accounts Executive : Acme Ltd || Bdjobs.com</title></head>
      <body><app-details-main><button><h2>Acme Ltd</h2></button><h2>Accounts Executive</h2>
      <app-summary><div id="allSection"><span>Vacancy:</span><span>2</span><span>Location:</span><span>Dhaka</span></div></app-summary>
      </app-details-main></body></html>"""
    case_c = _bdjobs_dom_extract(head_case, listing_title="Accounts Executive")
    assert case_c["title"] == "Accounts Executive"

    # Wrong rendered job must not be merged into the listing record.
    wrong_case = bd_dom_fixture.replace("Manager / Senior Manager - Recovery", "Marketing Executive")
    wrong_dom = _bdjobs_dom_extract(
        wrong_case, listing_title="Manager / Senior Manager - Recovery"
    )
    assert wrong_dom["identity_status"] == "mismatch"
    assert wrong_dom["is_job_page"] is False

    generic_overlap = """<html><head><title>Marketing Executive : Acme Ltd || Bdjobs.com</title></head><body><app-details-main><button><h2>Acme Ltd</h2></button><h2>Marketing Executive</h2><app-summary><div id=\"allSection\"><span>Vacancy:</span><span>2</span><span>Location:</span><span>Dhaka</span></div></app-summary></app-details-main></body></html>"""
    generic_dom = _bdjobs_dom_extract(generic_overlap, listing_title="Accounts Executive")
    assert generic_dom["identity_status"] == "mismatch"
    assert generic_dom["is_job_page"] is False

    footer_only = "<html><body><footer><h1>Our Valuable Partners</h1></footer></body></html>"
    ok, reason = _looks_like_job_document(footer_only, url="https://bdjobs.com/h/details/123", detail=True)
    assert not ok

    header_only = "<html><body><app-details-main><button><h2>Acme Ltd</h2></button><h2>Accounts Executive</h2></app-details-main><footer><h1>Our Valuable Partners</h1></footer></body></html>"
    ok, reason = _looks_like_job_document(header_only, url="https://bdjobs.com/h/details/123", detail=True)
    assert not ok

    # Dynamic publication is opportunistic: an empty qualifying pool is valid.
    saved_max = globals()["MAX_STORIES_PER_RUN"]
    try:
        globals()["MAX_STORIES_PER_RUN"] = 25
        assert select_final_jobs([], []) == []
        sparse_jobs = [
            {
                "source": "Bdjobs", "source_job_id": str(9000 + i),
                "title": f"Management Executive {i}", "company": f"Example {i}",
                "location": "Dhaka", "canonical": f"example.com/jobs/{i}",
                "source_url": f"https://example.com/jobs/{i}", "final_score": 70 - i,
                "bba_mba_target_score": 50, "judge_publish": True,
                "career_category": "Management / Admin",
                "is_government": False, "experience": "1 to 2 years",
                "deadline": self_test_deadline.isoformat(), "education": "BBA",
                "salary": "Tk. 30,000", "workplace": "Work at office",
            }
            for i in range(4)
        ]
        assert len(select_final_jobs(sparse_jobs, [])) == 4
        # A target is not a minimum. One valid private job must remain publishable.
        assert len(select_final_jobs([sparse_jobs[0]], [])) == 1

        # Full V2 allocation regression: when enough quality records exist, the
        # selector reaches the 25-post hard cap while preserving the base buckets.
        full_private=[]
        for i in range(16):
            src = "BDJobs Live" if i < 4 else "Bdjobs"
            full_private.append({
                "source": src, "source_job_id": f"R{i}", "title": f"Business Executive {i}",
                "company": f"Example Private {i}", "location": "Dhaka",
                "canonical": f"https://example.com/private/{i}",
                "source_url": f"https://example.com/private/{i}", "final_score": 85-i%5,
                "bba_mba_target_score": 60, "judge_publish": True, "career_category": "Management / Admin",
                "is_government": False, "experience": "1 to 2 years",
                "deadline": self_test_deadline.isoformat(), "education": "BBA",
                "salary": "Tk. 30,000", "workplace": "Work at office",
            })
        for i in range(2):
            full_private.append({
                "source": "Bdjobs", "source_job_id": f"IB{i}", "title": f"Finance Internship B{i}",
                "company": f"Intern Co B{i}", "location": "Dhaka",
                "canonical": f"https://example.com/intern-b/{i}", "source_url": f"https://example.com/intern-b/{i}",
                "final_score": 82-i, "bba_mba_target_score": 60, "judge_publish": True,
                "is_government": False, "lane": "internship", "is_internship_source": True,
                "employment_type": "Internship", "experience": "", "deadline": self_test_deadline.isoformat(),
                "education": "BBA", "salary": "Tk. 15,000", "workplace": "Work at office",
            })
        for i in range(2):
            full_private.append({
                "source": "BDJobs Live", "source_job_id": f"IL{i}", "title": f"Marketing Internship L{i}",
                "company": f"Intern Co L{i}", "location": "Dhaka",
                "canonical": f"https://example.com/intern-l/{i}", "source_url": f"https://example.com/intern-l/{i}",
                "final_score": 81-i, "bba_mba_target_score": 60, "judge_publish": True,
                "is_government": False, "lane": "internship", "is_internship_source": True,
                "employment_type": "Internship", "experience": "", "deadline": self_test_deadline.isoformat(),
                "education": "BBA", "salary": "Tk. 15,000", "workplace": "Work at office",
            })
        full_gov=[dict(sparse_jobs[0], source="Teletalk", source_job_id=f"G{i}", title=f"Govt Accounts Officer {i}",
                       canonical=f"https://example.com/gov/{i}", source_url=f"https://example.com/gov/{i}",
                       is_government=True, final_score=90-i) for i in range(8)]
        full_selected = select_final_jobs(full_private, full_gov)
        assert len(full_selected) == 25
        assert sum(1 for j in full_selected if j.get("is_government")) == 5
        full_interns=[j for j in full_selected if is_internship_job(j)]
        assert len(full_interns) == 4
        assert sum(1 for j in full_interns if j.get("source") == "Bdjobs") == 2
        assert sum(1 for j in full_interns if j.get("source") == "BDJobs Live") == 2

    finally:
        globals()["MAX_STORIES_PER_RUN"] = saved_max

    remote_state = {
        "format_version": 6,
        "queue": {"q1": {"status": "posted", "posted_at": "2026-09-23T08:00:00+06:00"}},
        "events": {"e1": {"event_id": "e1", "status": "published", "message_id": 101, "published_at": "2026-09-23T08:00:00+06:00"}},
        "recent_titles": ["Remote Job"],
        "last_run": "2026-09-23T08:00:00+06:00",
        "pipeline_version": "CareerNewsroom V1",
    }
    local_state = {
        "format_version": 6,
        "queue": {"q2": {"status": "posted", "posted_at": "2026-09-23T09:00:00+06:00"}},
        "events": {
            "e1": {"event_id": "e1", "status": "expired", "message_id": 101, "expired_at": "2026-09-23T09:05:00+06:00"},
            "e2": {"event_id": "e2", "status": "published", "message_id": 202, "published_at": "2026-09-23T09:00:00+06:00"},
        },
        "recent_titles": ["Local Job", "Remote Job"],
        "last_run": "2026-09-23T09:05:00+06:00",
        "pipeline_version": "CareerNewsroom V1",
    }
    reconciled = merge_state_data(remote_state, local_state)
    assert set(reconciled["queue"]) == {"q1", "q2"}
    assert set(reconciled["events"]) == {"e1", "e2"}
    assert reconciled["events"]["e1"]["status"] == "expired"
    assert reconciled["events"]["e1"]["message_id"] == 101
    assert reconciled["events"]["e2"]["message_id"] == 202
    assert reconciled["last_run"] == "2026-09-23T09:05:00+06:00"

    # Scheduler guard: repeated recovery triggers must not run the session twice.
    import os as _os
    _state_backup = json.loads(json.dumps(STATE))
    _env_backup = {k: _os.environ.get(k) for k in ("GITHUB_EVENT_NAME", "CAREER_SCHEDULE_CRON")}
    try:
        _os.environ["GITHUB_EVENT_NAME"] = "schedule"
        _os.environ["CAREER_SCHEDULE_CRON"] = "5 10 * * *"
        STATE["schedule_guard"] = {}
        assert begin_scheduled_session() is False
        complete_scheduled_session()
        assert begin_scheduled_session() is True
        STATE["schedule_guard"] = {"2026-09-26:morning": {"status": "running", "started_at": (datetime.now(BD_TZ) - timedelta(minutes=SCHEDULE_LOCK_MAX_MINUTES + 5)).isoformat()}}
        assert begin_scheduled_session() is False
    finally:
        STATE.clear(); STATE.update(_state_backup)
        for _k, _v in _env_backup.items():
            if _v is None:
                _os.environ.pop(_k, None)
            else:
                _os.environ[_k] = _v
        save_state(STATE)

    logger.info("CareerNewsroom V1 self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CareerNewsroom")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--source-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-ranking", action="store_true")
    parser.add_argument("--reconcile-state", metavar="SNAPSHOT_DIR", help="Merge a saved local state snapshot into the current checked-out state.")
    parser.add_argument("--claim-schedule", action="store_true", help="Claim the current scheduled session and persist its guard; exit 2 when already claimed.")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.source_test:
        source_test()
    elif args.reconcile_state:
        reconcile_state_files(args.reconcile_state)
    elif args.claim_schedule:
        raise SystemExit(0 if claim_scheduled_session_for_workflow() else 2)
    else:
        run(dry_run=args.dry_run, print_ranking=args.print_ranking)
