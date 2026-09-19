import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
import shutil
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
from bs4 import BeautifulSoup

try:
    from scrapling.fetchers import Fetcher as ScraplingFetcher, DynamicFetcher as ScraplingDynamicFetcher
except ImportError:
    ScraplingFetcher = None
    ScraplingDynamicFetcher = None

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
PIPELINE_VERSION = "Career News Bot"
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# Publication rules for Career News Bot:
# - Ingest roughly 100 newest Bdjobs candidates, not just 20 search hits.
# - Reduce 100 -> ~40 with cheap deterministic scoring, then enrich only those.
# - Score private jobs on a 100-point BBA/MBA career-fit model.
# - Cerebras is a semantic auditor/tie-breaker, never the publication gate.
# - Final selection is diversity-aware and quality-first, with a hard cap of 20.
# - Government remains an independent Teletalk stream without a BBA/MBA gate.
MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 20
MIN_GOVERNMENT_POSTS_PER_RUN = 0
MAX_GOVERNMENT_POSTS_PER_RUN = 5
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "1.0"))
FUTURE_TOLERANCE_MINUTES = 20
DISCOVERY_LOOKBACK_DAYS = int(os.environ.get("DISCOVERY_LOOKBACK_DAYS", "21"))
ACTIVE_JOB_RETENTION_DAYS = int(os.environ.get("ACTIVE_JOB_RETENTION_DAYS", "60"))
MAX_EXA_CANDIDATES = int(os.environ.get("MAX_EXA_CANDIDATES", "100"))
MAX_BDJOBS_DISCOVERY_PAGES = int(os.environ.get("MAX_BDJOBS_DISCOVERY_PAGES", "3"))
MAX_BDJOBS_DETAIL_CANDIDATES = int(os.environ.get("MAX_BDJOBS_DETAIL_CANDIDATES", "32"))
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000

# Scrapling is the fast HTML acquisition layer for official Bdjobs listing/detail pages.
# The browser renderer is intentionally disabled in production because the current SPA
# can return HTTP 200 while exposing zero job-card records.
SCRAPLING_ENABLED = (os.environ.get("SCRAPLING_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})
SCRAPLING_DYNAMIC_ENABLED = (os.environ.get("SCRAPLING_DYNAMIC_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"})
SCRAPLING_STATIC_TIMEOUT = int(os.environ.get("SCRAPLING_STATIC_TIMEOUT", "15"))
SCRAPLING_DYNAMIC_TIMEOUT_MS = int(os.environ.get("SCRAPLING_DYNAMIC_TIMEOUT_MS", "15000"))
SCRAPLING_DYNAMIC_WAIT_MS = int(os.environ.get("SCRAPLING_DYNAMIC_WAIT_MS", "1500"))
SCRAPLING_DYNAMIC_MAX_CANDIDATES = int(os.environ.get("SCRAPLING_DYNAMIC_MAX_CANDIDATES", "40"))
SCRAPLING_BROWSER_EXECUTABLE = (os.environ.get("SCRAPLING_BROWSER_EXECUTABLE") or "").strip()

# Source policy: no cross-board private fallback. Production private acquisition stays on Bdjobs.
# Official sources used in production are Teletalk for government and Bdjobs for private.
# Private discovery uses a broad newest pool once per run, then local category intelligence.
FAST_PRIVATE_CANDIDATE_TARGET = int(os.environ.get("FAST_PRIVATE_CANDIDATE_TARGET", "160"))
FAST_GOVERNMENT_CANDIDATE_TARGET = int(os.environ.get("FAST_GOVERNMENT_CANDIDATE_TARGET", "10"))
FAST_DETAIL_WORKERS = int(os.environ.get("FAST_DETAIL_WORKERS", "10"))
FAST_AI_CANDIDATE_LIMIT = int(os.environ.get("FAST_AI_CANDIDATE_LIMIT", "32"))
FAST_DISCOVERY_TIMEOUT = int(os.environ.get("FAST_DISCOVERY_TIMEOUT", "15"))
FAST_DETAIL_TIMEOUT = int(os.environ.get("FAST_DETAIL_TIMEOUT", "18"))
BDJOBS_CATEGORY_CANDIDATES_PER_CATEGORY = int(os.environ.get("BDJOBS_CATEGORY_CANDIDATES_PER_CATEGORY", "10"))
BDJOBS_CATEGORY_WORKERS = int(os.environ.get("BDJOBS_CATEGORY_WORKERS", "8"))
MAX_PRIVATE_POST_AGE_DAYS = int(os.environ.get("MAX_PRIVATE_POST_AGE_DAYS", "5"))
PRIVATE_RESEARCH_TARGET = int(os.environ.get("PRIVATE_RESEARCH_TARGET", "32"))
PRIVATE_QUALITY_FLOOR = int(os.environ.get("PRIVATE_QUALITY_FLOOR", "64"))

# Bdjobs anti-abuse policy: production discovery intentionally uses only the
# two routes that were observed working in previous successful runs. Do not
# fan out to 14 category pages every cycle. Category targeting is performed
# locally after the broad newest pool is acquired. This keeps request volume
# low enough for a 3-hour schedule and avoids turning a transient 403 into a
# self-inflicted source outage.
BDJOBS_CACHE_MAX_DAYS = int(os.environ.get("BDJOBS_CACHE_MAX_DAYS", "5"))


BDJOBS_SEARCH_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
BDJOBS_DYNAMIC_SEARCH_URL = "https://bdjobs.com/h/jobs"
# Bdjobs API and legacy cache listing are the production acquisition routes.
# Current category links redirect into the Angular SPA, while repeated category
# probing from GitHub-hosted IPs has produced 403s. We therefore acquire the
# newest broad pool once, then classify it locally into the configured career lanes.
BDJOBS_API_URL = "https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch"
BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]

# Every configured BBA/MBA-oriented functional category is checked on every run.
BDJOBS_BBA_MBA_CATEGORIES = [
    {"id":"1","name":"Accounting / Finance","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=1"},
    {"id":"2","name":"Bank / Non-Bank Financial Institution","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=2"},
    {"id":"3","name":"Commercial / Supply Chain","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=3"},
    {"id":"9","name":"Marketing / Sales","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=9"},
    {"id":"17","name":"HR / Organization Development","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=17"},
    {"id":"7","name":"General Management / Admin","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=7"},
    {"id":"16","name":"Customer Service / Call Centre","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=16"},
    {"id":"10","name":"Media / Advertisement / Event Management","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=10"},
    {"id":"13","name":"Research / Consultancy","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=13"},
    {"id":"12","name":"NGO / Development","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=12"},
    {"id":"20","name":"Hospitality / Travel / Tourism","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=20"},
    {"id":"6","name":"Garments / Textile","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=6"},
    {"id":"8","name":"IT / Telecom - Business Roles","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=8"},
    {"id":"4","name":"Education / Training - Business Roles","url":"https://jobs.bdjobs.com/jobsearch.asp?fcatId=4"},
]

# Government primary source. The endpoint is documented by an independent
# open-source Teletalk AllJobs search project and returns structured records
# including job_primary_id, title, organization, vacancy, deadline and
# application_site_url.
TELETALK_API_URL = "https://alljobs.teletalk.com.bd/api/v1/published-jobs/search"
TELETALK_HOME_URL = "https://alljobs.teletalk.com.bd/"
TELETALK_DOMAIN = "alljobs.teletalk.com.bd"
TELETALK_API_TIMEOUT = int(os.environ.get("TELETALK_API_TIMEOUT", "15"))

# No third-party job-board bridge is required. The production pipeline uses only
# the official Teletalk government API and official Bdjobs API/listing pages.

DOHAJ_DOMAIN = "dohaj.com"

DOHAJ_PRIVATE_SECTION_URLS = [
    "https://dohaj.com/category/accounting-finance",
    "https://dohaj.com/category/marketing-sales",
    "https://dohaj.com/category/hr-org-development",
    "https://dohaj.com/category/gen-mgt-admin",
    "https://dohaj.com/category/commercial-supply-chain",
    "https://dohaj.com/category/secretary-receptionist",
    "https://dohaj.com/category/bank-non-bank-fin-institution",
    "https://dohaj.com/category/customer-service-call-centre",
    "https://dohaj.com/category/media-advertisement-event-mgt",
    "https://dohaj.com/category/production-operation",
    "https://dohaj.com/category/ngo-development",
    "https://dohaj.com/jobs/all",
]
DOHAJ_GOVERNMENT_URL = "https://dohaj.com/gov-jobs"
# Backward-compatible aliases for state/self-tests from earlier releases.
DOHAJ_SECTION_URLS = DOHAJ_PRIVATE_SECTION_URLS + [DOHAJ_GOVERNMENT_URL]
DOHAJ_TOP5_URLS = DOHAJ_SECTION_URLS

DOHAJ_CATEGORY_NAMES = {
    "accounting-finance": "Accounting/Finance",
    "marketing-sales": "Marketing/Sales",
    "hr-org-development": "HR/Org. Development",
    "gen-mgt-admin": "General Management/Admin",
    "commercial-supply-chain": "Commercial/Supply Chain",
    "secretary-receptionist": "Secretary/Receptionist",
    "bank-non-bank-fin-institution": "Bank/Non-Bank Fin. Institution",
    "customer-service-call-centre": "Customer Service/Call Centre",
    "media-advertisement-event-mgt": "Media/Advertisement/Event",
    "production-operation": "Production/Operation",
    "ngo-development": "NGO/Development",
    "jobs": "All Jobs",
    "gov-jobs": "Government Jobs",
}

BBA_MBA_TERMS = (
    "bba", "mba", "bachelor of business administration", "master of business administration",
    "business administration", "business studies", "bbs", "mbs", "commerce", "finance",
    "accounting", "marketing", "human resources", "hrm", "management", "banking",
    "business development", "supply chain", "commercial", "operations", "economics",
)
EARLY_CAREER_TERMS = (
    "fresher", "freshers", "no experience", "entry level", "entry-level", "trainee",
    "management trainee", "graduate trainee", "graduate program", "intern", "internship",
    "0-1", "0 to 1", "0-2", "0 to 2", "0-3", "0 to 3", "1-2", "1 to 2", "1-3", "1 to 3",
)
TARGET_FUNCTION_TERMS = (
    "account", "finance", "audit", "tax", "bank", "relationship", "credit", "treasury",
    "marketing", "sales", "brand", "hr", "human resource", "recruitment", "business development",
    "management", "commercial", "procurement", "supply chain", "operations", "admin", "analyst",
    "customer service", "merchandising", "corporate affairs", "front desk", "hotline", "f-commerce",
    "coordination", "retail", "client service", "customer experience",
)

BUSINESS_ROLE_TERMS = (
    "executive", "officer", "assistant", "associate", "intern", "trainee", "management trainee",
    "graduate trainee", "management", "sales", "marketing", "account", "finance", "bank",
    "relationship", "credit", "hr", "human resource", "recruitment", "commercial", "procurement",
    "supply chain", "operations", "admin", "analyst", "customer service", "business development",
    "front desk", "receptionist", "coordinator", "merchandising", "corporate affairs",
    "client service", "customer experience",
)

NON_BUSINESS_ROLE_TERMS = (
    "software engineer", "web developer", "mobile developer", "frontend developer", "backend developer",
    "full stack developer", "devops", "network engineer", "civil engineer", "electrical engineer",
    "mechanical engineer", "biomedical engineer", "pharmacist", "medical officer", "doctor",
    "nurse", "lab technologist", "teacher", "lecturer", "architect",
)
NOISE_TITLE_TERMS = (
    "calculator", "quiz", "mcq", "question solution", "answer key", "exam result", "admission",
    "scholarship", "career advice", "cv writing", "resume tips", "interview tips", "salary calculator",
    "course", "training course", "webinar", "seminar", "job fair", "how to get a job", "job preparation",
)
SENIOR_TERMS = (
    "chief", "cfo", "ceo", "director", "head of", "general manager", "agm", "dgm", "senior manager",
    "vice president", "vp ", "8 years", "9 years", "10 years", "10+ years", "15 years", "20 years",
)

SOURCE_NAMES = {
    "bdjobs.com": "Bdjobs",
    "jobs.bdjobs.com": "Bdjobs",
    "dohaj.com": "Dohaj",
    "alljobs.teletalk.com.bd": "Teletalk",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("career-news-bot")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)
retry_policy = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20),
)
session.mount(
    "http://",
    HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20),
)


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
    if "/category/" in path or path.endswith("/gov-jobs") or "/search" in path:
        return True
    return "/job-details/" not in path and "/gov-job/" not in path and "/jobdetails" not in path and path.count("/") <= 2


def is_dohaj_job_url(url):
    if not is_domain_allowed(url, [DOHAJ_DOMAIN]):
        return False
    path = urlparse(safe_text(url)).path.lower()
    return "/job-details/" in path or "/gov-job/" in path


def dohaj_page_url(base_url, page_no):
    if page_no <= 1:
        return base_url
    return base_url + ("&" if "?" in base_url else "?") + f"page={page_no}"


BDJOBS_JOB_RE = re.compile(
    r"^https?://(?:www\.)?jobs\.bdjobs\.com/(?:jobdetails(?:\.asp)?)(?:/)?(?:\?|#)?[^#]*\bid=\d+",
    re.I,
)

def is_bdjobs_job_url(url):
    raw = safe_text(url)
    if not is_domain_allowed(raw, BDJOBS_DOMAINS):
        return False
    parsed = urlparse(raw)
    path = parsed.path.lower().rstrip("/")
    if "/jobdetails" in path and bool(re.search(r"(?:^|[?&])id=\d+", parsed.query, re.I)):
        return True
    # Current Bdjobs web app routes may use /h/jobs/<slug-or-id> instead of
    # the legacy jobdetails.asp?id=<n> route. Keep the route broad enough for
    # discovery, while excluding the bare search landing page itself.
    return path.startswith("/h/jobs/") and len(path.split("/")) >= 4


def is_vacancy_url(url):
    if is_dohaj_job_url(url):
        return True
    if is_bdjobs_job_url(url):
        return True
    return False


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
    # the same vacancy mirrored on Dohaj/Bdjobs can still collapse into one event.
    return hashlib.sha1(_job_identity_text(job).encode("utf-8")).hexdigest()[:24]


def _same_application_target(a, b):
    ua = canonical_url(a.get("apply_url", ""))
    ub = canonical_url(b.get("apply_url", ""))
    return bool(ua and ub and ua == ub)


def likely_same_job(a, b):
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
# V4 field schema is intentionally incompatible with the old photo/field mappings.
# Keep posted history, but do not reuse legacy pending queue records.
if int(STATE.get("format_version", 0) or 0) < 4:
    for _key, _item in STATE.get("queue", {}).items():
        if isinstance(_item, dict) and _item.get("status") in {"pending", "selected"}:
            _item["status"] = "legacy_ignored"
    STATE["format_version"] = 4


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
# SOURCE HELPERS / STABLE HTTP ACQUISITION
# ============================================================

def _listing_date_from_text(card_text):
    return normalize_date_text(_first_match(card_text, [
        r"(?:Posted|Published|প্রকাশিত|পোস্টেড)\s*[:：-]?\s*([^|]+?)(?=\s+(?:Deadline|শেষ তারিখ|View|দেখুন)\b|$)",
    ]))


def _decode_scrapling_body(response):
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        encoding = safe_text(getattr(response, "encoding", "")) or "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")
    return safe_text(body)


def _browser_executable():
    if SCRAPLING_BROWSER_EXECUTABLE and os.path.exists(SCRAPLING_BROWSER_EXECUTABLE):
        return SCRAPLING_BROWSER_EXECUTABLE
    for candidate in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.exists(candidate):
            return candidate
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _scrapling_static_html(url, *, params=None, timeout=None):
    """Fast Scrapling HTTP fetch. Returns a requests-like dict or None."""
    if not SCRAPLING_ENABLED or ScraplingFetcher is None:
        return None
    try:
        response = ScraplingFetcher.get(
            request_safe_url(url),
            params=params or {},
            headers=HEADERS,
            follow_redirects=True,
            timeout=timeout or SCRAPLING_STATIC_TIMEOUT,
            retries=1,
            retry_delay=0.25,
            impersonate="chrome",
            stealthy_headers=False,
        )
        status = int(getattr(response, "status", 0) or 0)
        if status >= 400 or status == 0:
            return None
        body = _decode_scrapling_body(response)
        final_url = safe_text(getattr(response, "url", "")) or url
        return {"status": status, "text": body, "url": final_url, "backend": "scrapling_static"}
    except Exception as exc:
        logger.debug("Scrapling static fetch failed %s: %s", url, exc)
        return None


def _scrapling_dynamic_html(url, *, wait_selector=None):
    """One-shot browser render for JS-heavy pages, intentionally last-resort."""
    if not SCRAPLING_ENABLED or not SCRAPLING_DYNAMIC_ENABLED or ScraplingDynamicFetcher is None:
        return None
    kwargs = {
        "headless": True,
        "disable_resources": True,
        "load_dom": True,
        "wait": max(0, SCRAPLING_DYNAMIC_WAIT_MS),
        "timeout": max(5000, SCRAPLING_DYNAMIC_TIMEOUT_MS),
        "retries": 1,
        "retry_delay": 0.25,
        "google_search": False,
        "extra_headers": HEADERS,
    }
    if wait_selector:
        kwargs["wait_selector"] = wait_selector
    executable = _browser_executable()
    if executable:
        kwargs["executable_path"] = executable
    try:
        response = ScraplingDynamicFetcher.fetch(request_safe_url(url), **kwargs)
        status = int(getattr(response, "status", 0) or 0)
        if status >= 400 or status == 0:
            return None
        body = _decode_scrapling_body(response)
        final_url = safe_text(getattr(response, "url", "")) or url
        return {"status": status, "text": body, "url": final_url, "backend": "scrapling_dynamic"}
    except Exception as exc:
        logger.warning("Scrapling dynamic fetch failed %s: %s", url, exc)
        return None


def _scrapling_dynamic_bdjobs_candidates():
    """Render the current Bdjobs SPA only when the faster source paths are short."""
    rendered = _scrapling_dynamic_html(BDJOBS_DYNAMIC_SEARCH_URL)
    if not rendered or not rendered.get("text"):
        return []
    page_html = rendered["text"]
    soup = BeautifulSoup(page_html, "html.parser")
    found=[]; seen=set()
    for a in soup.find_all("a", href=True):
        href=urljoin(rendered.get("url") or BDJOBS_DYNAMIC_SEARCH_URL, safe_text(a.get("href")))
        if not is_bdjobs_job_url(href):
            continue
        canonical=canonical_url(href)
        if not canonical or canonical in seen or canonical in POSTED_URLS:
            continue
        title=_clean_one_line(a.get_text(" ",strip=True))
        if not title or is_noise_title(title,href):
            continue
        parent=a.find_parent(["article","li","div","section","tr"])
        card_text=_clean_one_line(parent.get_text(" ",strip=True)) if parent else title
        blob=f"{title} {card_text}".lower()
        if not any(term in blob for term in BDJOBS_NATIVE_CATEGORY_HINTS):
            # A rendered SPA can omit card metadata; title alone still gets a
            # second-stage business filter from our normal research pipeline.
            if not any(term in title.lower() for term in BUSINESS_ROLE_TERMS):
                continue
        seen.add(canonical)
        found.append({
            "title":title,"url":href,"canonical":canonical,"source":"Bdjobs","source_url":href,
            "discovery":"bdjobs_scrapling_dynamic","excerpt":trim_source_text(card_text,2200),
            "discovered_at":now_iso(),
        })
        if len(found)>=SCRAPLING_DYNAMIC_MAX_CANDIDATES:
            break
    logger.info("BDJOBS SCRAPLING DYNAMIC DISCOVERY: %d",len(found))
    return found


def extract_dohaj_page(category_url, category_name, page_no=1, limit=12):
    """Fetch one current Dohaj listing page and keep only a small latest window."""
    page_url=dohaj_page_url(category_url,page_no)
    is_gov=category_name=="Government Jobs" or "/gov-jobs" in urlparse(category_url).path.lower()
    try:
        fetched=_scrapling_static_html(page_url, timeout=FAST_DISCOVERY_TIMEOUT)
        if fetched:
            page_html=fetched["text"]; response_url=fetched["url"]; response_status=fetched["status"]
        else:
            response=session.get(request_safe_url(page_url),headers=HEADERS,timeout=FAST_DISCOVERY_TIMEOUT)
            response.raise_for_status(); page_html=response.text; response_url=response.url; response_status=response.status_code
        if response_status>=400:
            logger.warning("DOHAJ listing failed %s HTTP=%s",page_url,response_status); return []
        soup=BeautifulSoup(page_html,"html.parser")
        results=[]; seen=set()
        for anchor in soup.find_all("a",href=True):
            href=urljoin(response_url,safe_text(anchor.get("href")))
            if not is_dohaj_job_url(href): continue
            canonical=canonical_url(href)
            if not canonical or canonical in seen or canonical in POSTED_URLS: continue
            title=safe_text(anchor.get_text(" ",strip=True))
            if not title or is_noise_title(title,href): continue
            parent=anchor.find_parent(["article","li","div","section"])
            card_text=safe_text(parent.get_text(" ",strip=True)) if parent else title
            deadline_match=re.search(r"(?:Deadline|শেষ তারিখ)\s*[:：-]?\s*(.+?)(?=\s+(?:View|দেখুন)\b|$)",card_text,flags=re.I)
            seen.add(canonical)
            results.append({
                "title":title,"url":href,"canonical":canonical,"source":"Dohaj","source_url":href,
                "discovery":"dohaj_direct_latest","dohaj_category":category_name,"is_government":is_gov,
                "excerpt":trim_source_text(card_text,1200),
                "listing_deadline":normalize_date_text(deadline_match.group(1)) if deadline_match else "",
                "listing_posted":_listing_date_from_text(card_text),"discovered_at":now_iso(),
            })
            if len(results)>=limit: break
        logger.info("DOHAJ LATEST | %s | page=%d | %d",category_name,page_no,len(results))
        return results
    except Exception as exc:
        logger.warning("DOHAJ listing error %s page=%d: %s",category_name,page_no,exc); return []


def _dohaj_private_title_signal(title):
    blob=safe_text(title).lower()
    return bool(blob) and any(term in blob for term in BUSINESS_ROLE_TERMS) and not any(term in blob for term in NON_BUSINESS_ROLE_TERMS)


def _discover_private_section(url):
    path=urlparse(url).path.rstrip("/"); slug=path.split("/")[-1]
    category_name="All Jobs" if path=="/jobs/all" else DOHAJ_CATEGORY_NAMES.get(slug,slug)
    items=extract_dohaj_page(url,category_name,1,DOHAJ_CATEGORY_LINKS_PER_PAGE)
    return [x for x in items if _dohaj_private_title_signal(x["title"])]


def discover_dohaj_government():
    government=[]; seen=set()
    # Secondary government fallback only. Teletalk is the primary path in V4.
    for page_no in range(1,DOHAJ_GOVERNMENT_MAX_PAGES+1):
        items=extract_dohaj_page(DOHAJ_GOVERNMENT_URL,"Government Jobs",page_no,FAST_GOVERNMENT_CANDIDATE_TARGET)
        for item in items:
            if item["canonical"] not in seen:
                seen.add(item["canonical"]); government.append(item)
        if len(government)>=FAST_GOVERNMENT_CANDIDATE_TARGET or (page_no>=2 and len(government)>=MIN_GOVERNMENT_POSTS_PER_RUN): break
    logger.info("DOHAJ GOVERNMENT FALLBACK: %d",len(government))
    return government


def discover_dohaj():
    private=[]; seen=set()
    # Private dedicated sections are independent, so fetch page 1 concurrently.
    with ThreadPoolExecutor(max_workers=min(8,len(DOHAJ_PRIVATE_SECTION_URLS))) as executor:
        futures=[executor.submit(_discover_private_section,url) for url in DOHAJ_PRIVATE_SECTION_URLS]
        for future in as_completed(futures):
            try: items=future.result()
            except Exception as exc:
                logger.warning("DOHAJ private section worker failed: %s",exc); continue
            for item in items:
                if item["canonical"] in seen: continue
                seen.add(item["canonical"]); private.append(item)

    # If page 1 did not provide enough private candidates, use page 2 only for the
    # few sections most likely to contain business roles, still concurrently.
    if len(private)<FAST_PRIVATE_CANDIDATE_TARGET and DOHAJ_PRIVATE_PAGES_PER_SECTION>=2:
        priority_urls=DOHAJ_PRIVATE_SECTION_URLS[:8]
        def page2(url):
            path=urlparse(url).path.rstrip("/"); slug=path.split("/")[-1]
            category_name="All Jobs" if path=="/jobs/all" else DOHAJ_CATEGORY_NAMES.get(slug,slug)
            return [x for x in extract_dohaj_page(url,category_name,2,DOHAJ_CATEGORY_LINKS_PER_PAGE) if _dohaj_private_title_signal(x["title"])]
        with ThreadPoolExecutor(max_workers=min(8,len(priority_urls))) as executor:
            futures=[executor.submit(page2,url) for url in priority_urls]
            for future in as_completed(futures):
                try: items=future.result()
                except Exception: continue
                for item in items:
                    if item["canonical"] in seen: continue
                    seen.add(item["canonical"]); private.append(item)
                    if len(private)>=FAST_PRIVATE_CANDIDATE_TARGET: break
                if len(private)>=FAST_PRIVATE_CANDIDATE_TARGET: break

    logger.info("DOHAJ PRIVATE DISCOVERY: %d",len(private))
    return private


def _teletalk_record_fields(record):
    if not isinstance(record, dict):
        return None
    source_id=safe_text(record.get("job_primary_id") or record.get("jobPrimaryId") or record.get("id"))
    title=_clean_one_line(record.get("job_title") or record.get("jobTitle") or record.get("title"))
    if not source_id or not title: return None
    company=_clean_one_line(record.get("org_name") or record.get("orgName") or record.get("organization") or record.get("company"))
    vacancy=compact_vacancy(record.get("vacancy"))
    deadline=normalize_date_text(record.get("deadline_date") or record.get("deadlineDate") or record.get("deadline"))
    posted=normalize_date_text(record.get("published_date") or record.get("publish_date") or record.get("posted_date") or record.get("postedDate"))
    apply_url=safe_text(record.get("application_site_url") or record.get("applicationSiteUrl") or record.get("apply_url") or record.get("applyUrl"))
    education=_strip_html_fragment(record.get("education") or record.get("education_qualification") or "")
    location=_clean_one_line(record.get("location") or record.get("job_location") or "")
    employment=compact_employment(record.get("employment_type") or record.get("job_type") or record.get("jobType"))
    raw_text=" | ".join(x for x in [title,company,location,education] if x)
    # Keep the source identity independent from the application target. Multiple
    # Teletalk jobs can legitimately share one application domain/form, so the
    # native job ID must drive canonicalization and persistent duplicate state.
    source_url=f"https://alljobs.teletalk.com.bd/?job_primary_id={quote(source_id)}"
    return {
        "title":title,"company":company,"location":location,"salary":"","experience":"",
        "education":compact_education(education),"vacancy":vacancy,"employment_type":employment,
        "workplace":"","age":"","category":"","application_method":"Online" if apply_url else "",
        "selection_process":"","application_period":"","application_start":"","application_end":"",
        "posted_date":posted,"deadline":deadline,"source":"Teletalk","source_url":source_url,
        "url":source_url,"apply_url":apply_url,"source_job_id":source_id,
        "discovery":"teletalk_api","is_government":True,"raw_text":raw_text,
    }


def _teletalk_records(payload):
    if not isinstance(payload,dict): return []
    for key in ("govtJobs","data","jobs","results"):
        value=payload.get(key)
        if isinstance(value,list): return value
    return []


def _discover_teletalk_api():
    discovered=[]; seen=set()
    try:
        response=session.get(TELETALK_API_URL,params={"searchKeyword":""},headers=HEADERS,timeout=TELETALK_API_TIMEOUT)
        response.raise_for_status(); payload=response.json()
        records=_teletalk_records(payload)
        for record in records:
            fields=_teletalk_record_fields(record)
            if not fields: continue
            canonical=canonical_url(fields["source_url"])
            if not canonical or canonical in seen or canonical in POSTED_URLS: continue
            seen.add(canonical)
            discovered.append({
                "title":fields["title"],"url":fields["url"],"canonical":canonical,
                "source":"Teletalk","source_url":fields["source_url"],"discovery":"teletalk_api",
                "excerpt":trim_source_text(fields.get("raw_text",""),1800),
                "listing_posted":fields.get("posted_date",""),"listing_deadline":fields.get("deadline",""),
                "discovered_at":now_iso(),"api_fields":fields,"is_government":True,
            })
            if len(discovered)>=FAST_GOVERNMENT_CANDIDATE_TARGET: break
        logger.info("TELETALK API DISCOVERY: %d",len(discovered))
    except Exception as exc:
        logger.warning("TELETALK API discovery failed: %s",exc)
    return discovered


def _discover_ever_jobs_bdjobs():
    """Optional self-hosted Ever Jobs bridge. Never required for V4 operation."""
    if not EVER_JOBS_API_URL: return []
    discovered=[]; seen=set()
    headers={"Accept":"application/json","Content-Type":"application/json"}
    if EVER_JOBS_API_KEY: headers["x-api-key"]=EVER_JOBS_API_KEY
    body={"searchTerm":"","siteType":["bdjobs"],"resultsWanted":min(60,FAST_PRIVATE_CANDIDATE_TARGET),"descriptionFormat":"markdown"}
    try:
        response=session.post(f"{EVER_JOBS_API_URL}/api/jobs/search",headers=headers,json=body,timeout=EVER_JOBS_TIMEOUT)
        response.raise_for_status(); payload=response.json()
        records=payload.get("jobs") if isinstance(payload,dict) else payload
        if not isinstance(records,list): records=[]
        for record in records:
            if not isinstance(record,dict): continue
            title=_clean_one_line(record.get("title") or record.get("jobTitle"))
            url=safe_text(record.get("jobUrl") or record.get("job_url") or record.get("url") or record.get("link"))
            if not title or not url or not is_bdjobs_job_url(url): continue
            company=_clean_one_line(record.get("company") or record.get("companyName"))
            source_id=safe_text(record.get("jobId") or record.get("id") or "")
            canonical=canonical_url(url)
            if canonical in seen or canonical in POSTED_URLS: continue
            seen.add(canonical)
            posted=normalize_date_text(record.get("datePosted") or record.get("postedDate") or record.get("date_posted"))
            deadline=normalize_date_text(record.get("deadline") or record.get("validThrough"))
            apply_url=safe_text(record.get("applicationUrl") or record.get("application_url") or record.get("applyUrl") or record.get("apply_url"))
            description=_strip_html_fragment(record.get("description") or "")
            fields={
                "title":title,"company":company,"location":_clean_one_line(record.get("location") or ""),
                "salary":compact_salary(record.get("salary") or record.get("compensation") or ""),
                "experience":compact_experience(record.get("experience") or ""),
                "education":compact_education(_strip_html_fragment(record.get("education") or "")),
                "vacancy":compact_vacancy(record.get("vacancy") or ""),
                "employment_type":compact_employment(record.get("jobType") or record.get("employmentType") or ""),
                "workplace":compact_workplace(record.get("workplace") or record.get("remote") or ""),
                "age":"","category":"","application_method":"Online" if apply_url else "",
                "selection_process":"","application_period":"","application_start":"","application_end":"",
                "posted_date":posted,"deadline":deadline,"source":"Bdjobs","source_url":url,"url":url,
                "apply_url":apply_url,"source_job_id":source_id,"discovery":"ever_jobs_bdjobs",
                "is_government":False,"raw_text":trim_source_text(" | ".join(x for x in [title,company,description] if x),MAX_JOB_CONTENT_CHARS),
            }
            discovered.append({
                "title":title,"url":url,"canonical":canonical,"source":"Bdjobs","source_url":url,
                "discovery":"ever_jobs_bdjobs","excerpt":trim_source_text(fields.get("raw_text",""),2200),
                "listing_posted":posted,"listing_deadline":deadline,"discovered_at":now_iso(),"api_fields":fields,
            })
            if len(discovered)>=FAST_PRIVATE_CANDIDATE_TARGET: break
        logger.info("EVER JOBS BDJOBS BRIDGE: %d",len(discovered))
    except Exception as exc:
        logger.warning("Ever Jobs BDJobs bridge failed: %s",exc)
    return discovered


BDJOBS_NATIVE_CATEGORY_HINTS = (
    "account", "finance", "bank", "commercial", "management", "admin", "hr",
    "human resource", "marketing", "sales", "business development", "supply chain",
    "procurement", "operation", "relationship", "credit", "customer service",
    "audit", "tax", "treasury", "merchandising", "corporate affairs", "analyst",
    "trainee", "intern", "executive", "officer",
)

def _bdjobs_pagination_links(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        label = safe_text(a.get_text(" ", strip=True))
        href = urljoin(base_url, safe_text(a.get("href")))
        if label.isdigit() and 1 <= int(label) <= MAX_BDJOBS_DISCOVERY_PAGES and is_domain_allowed(href, BDJOBS_DOMAINS):
            links.append((int(label), href))
    return sorted(dict(links).items())

def _bdjobs_listing_candidates(page_html, page_url, *, category=None, limit=None):
    """Extract Bdjobs vacancy links with broad recall. Category is a discovery signal,
    not a final eligibility decision."""
    soup=BeautifulSoup(page_html,"html.parser")
    found=[]; seen=set()
    for a in soup.find_all("a",href=True):
        href=urljoin(page_url,safe_text(a.get("href")))
        if not is_bdjobs_job_url(href): continue
        canonical=canonical_url(href)
        if not canonical or canonical in seen or canonical in POSTED_URLS: continue
        title=_clean_one_line(a.get_text(" ",strip=True))
        if not title or is_noise_title(title,href): continue
        parent=a.find_parent(["li","div","article","section","tr"])
        card_text=_clean_one_line(parent.get_text(" ",strip=True)) if parent else title
        seen.add(canonical)
        found.append({
            "title":title,"url":href,"canonical":canonical,"source":"Bdjobs","source_url":href,
            "discovery":"bdjobs_category",
            "source_category_id":safe_text((category or {}).get("id")),
            "source_category":safe_text((category or {}).get("name")),
            "excerpt":trim_source_text(card_text,2200),
            "listing_posted":_listing_date_from_text(card_text),
            "listing_deadline":_first_match(card_text,[r"(?:Deadline|শেষ তারিখ)\s*[:：-]?\s*(.+)$"]),
            "discovered_at":now_iso(),
        })
        if limit and len(found)>=limit: break
    return found

def _discover_bdjobs_html_fallback():
    """Scrape the .asp listing page's raw HTML for job links. Runs every cycle
    alongside the JSON API (not only when the API fails) because the live
    listing page is largely client-rendered and a plain HTTP GET of it may
    return a thin/empty page independent of whether the API is healthy --
    confirmed by fetching the bdjobs.com homepage directly and finding no
    job content in the raw response, consistent with heavy client-side
    rendering."""
    discovered=[]; seen=set(); pages=[(1,BDJOBS_SEARCH_URL)]
    try:
        while pages and len(discovered)<FAST_PRIVATE_CANDIDATE_TARGET:
            page_no,page_url=pages.pop(0)
            fetched=_scrapling_static_html(page_url, timeout=FAST_DISCOVERY_TIMEOUT)
            if fetched:
                page_html=fetched["text"]; response_url=fetched["url"]; response_status=fetched["status"]
            else:
                response=session.get(page_url,headers=HEADERS,timeout=FAST_DISCOVERY_TIMEOUT)
                if response.status_code>=400: continue
                page_html=response.text; response_url=response.url; response_status=response.status_code
            if response_status>=400: continue
            for item in _bdjobs_listing_candidates(page_html,response_url):
                if item["canonical"] in seen or item["canonical"] in POSTED_URLS: continue
                seen.add(item["canonical"]); discovered.append(item)
                if len(discovered)>=FAST_PRIVATE_CANDIDATE_TARGET: break
            if page_no==1 and len(discovered)<FAST_PRIVATE_CANDIDATE_TARGET:
                for n,href in _bdjobs_pagination_links(page_html,response_url):
                    if 2 <= n <= MAX_BDJOBS_DISCOVERY_PAGES:
                        pages.append((n,href))
    except Exception as exc:
        logger.warning("BDJOBS HTML discovery failed: %s",exc)
    logger.info("BDJOBS HTML DISCOVERY (raw): %d",len(discovered))
    return discovered


# ============================================================
# BDJOBS JSON API DISCOVERY (bounded primary probe)
# ============================================================

BDJOBS_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://jobs.bdjobs.com/jobsearch-cache.asp",
    "Origin": "https://jobs.bdjobs.com",
}


def _bdjobs_api_job_url(job_id):
    return f"https://jobs.bdjobs.com/jobdetails.asp?id={job_id}"


def _bdjobs_api_record_fields(record):
    """Map one Bdjobs API search-result record onto the bot's internal job
    schema. The listing API already returns source-backed structured values
    for nearly every table field, so this does not need any label/regex
    guessing the way HTML-page extraction does."""
    if not isinstance(record, dict):
        return None
    job_id = safe_text(record.get("Jobid"))
    title = _clean_one_line(record.get("jobTitle"))
    if not job_id or not title:
        return None
    url = _bdjobs_api_job_url(job_id)
    company = _clean_one_line(record.get("companyName"))
    education_raw = _strip_html_fragment(record.get("eduRec"))
    context_text = _strip_html_fragment(record.get("jobContext"))
    description_text = _strip_html_fragment(record.get("jobDescription"))
    raw_text = " | ".join(x for x in [title, company, education_raw, context_text, description_text] if x)
    posted_iso = normalize_date_text(record.get("publishDate"))
    deadline_iso = normalize_date_text(record.get("deadlineDB") or record.get("deadline"))
    return {
        "title": title,
        "company": company,
        "location": compact_location(record.get("location")),
        "salary": compact_salary(record.get("Salary")),
        "experience": compact_experience(record.get("experience")),
        "education": compact_education(education_raw),
        "vacancy": compact_vacancy(record.get("Vacancies")),
        "employment_type": compact_employment(record.get("JobType")),
        "workplace": compact_workplace(record.get("WorkPlace")),
        "age": "",
        "category": "",
        "application_method": "",
        "selection_process": "",
        "application_period": "",
        "application_start": "",
        "application_end": "",
        "posted_date": posted_iso,
        "deadline": deadline_iso,
        "source": "Bdjobs",
        "source_url": url,
        "url": url,
        "source_job_id": job_id,
        "discovery": "bdjobs_api",
        "dohaj_category": "",
        "is_government": False,
        "raw_text": raw_text,
    }


def _bdjobs_api_candidates(records):
    """Normalize the official API records without applying audience gates early.

    The search API is the freshest structured source. Filtering it by the partial
    listing fields caused valid business jobs to disappear before the authoritative
    detail page could enrich them. Discovery therefore keeps real job records; the
    normal deterministic gate runs after detail retrieval.
    """
    found = []
    for record in records:
        fields = _bdjobs_api_record_fields(record)
        if not fields or is_noise_title(fields["title"], fields["url"]):
            continue
        found.append(fields)
    return found


def _bdjobs_api_fetch(page_no):
    """Fetch only the newest unparameterized Bdjobs API page.

    The structured API lane intentionally probes only the first current response.
    Additional candidates come from the broad legacy/cache listing, not speculative
    category/API fan-out."""
    if page_no != 1:
        return []
    headers = dict(HEADERS); headers.update(BDJOBS_API_HEADERS)
    try:
        response = session.get(BDJOBS_API_URL, headers=headers, timeout=FAST_DISCOVERY_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("BDJOBS API request failed: %s", exc)
        return []
    if not isinstance(payload,dict) or safe_text(payload.get("statuscode")) != "1":
        return []
    return list(payload.get("data") or []) + list(payload.get("premiumData") or [])


def _discover_bdjobs_api():
    """Probe Bdjobs' own JSON search response without speculative pagination.

    The response is useful for structured fields, but current freshness and
    pagination behavior are not sufficiently documented to justify guessing
    request parameters. The API lane therefore limits this path to the newest response
    and uses independent fallbacks when the candidate pool remains short."""
    discovered = []
    seen = set()
    target = FAST_PRIVATE_CANDIDATE_TARGET
    try:
        for page_no in range(1, MAX_BDJOBS_DISCOVERY_PAGES + 1):
            records = _bdjobs_api_fetch(page_no)
            if not records:
                break
            new_this_page = 0
            for fields in _bdjobs_api_candidates(records):
                canonical = canonical_url(fields["url"])
                if not canonical or canonical in seen or canonical in POSTED_URLS:
                    continue
                seen.add(canonical); new_this_page += 1
                discovered.append({
                    "title": fields["title"], "url": fields["url"], "canonical": canonical,
                    "source": "Bdjobs", "source_url": fields["url"], "discovery": "bdjobs_api",
                    "excerpt": trim_source_text(fields.get("raw_text", ""), 2200),
                    "listing_posted": fields.get("posted_date", ""),
                    "listing_deadline": fields.get("deadline", ""),
                    "discovered_at": now_iso(), "api_fields": fields,
                })
            if new_this_page == 0 or len(discovered) >= target:
                break
    except Exception as exc:
        logger.warning("BDJOBS API discovery failed: %s", exc)
    return discovered


def _bdjobs_category_url_variants(category):
    """Return the source-native URL for diagnostics/documentation only.

    Production does not fetch every category URL. The current site redirects
    category routes into the SPA, and broad acquisition followed by local
    category classification is considerably less request-heavy.
    """
    cid = safe_text(category.get("id"))
    return [f"https://jobs.bdjobs.com/JobSearch.asp?fcatId={cid}&icatId=&requestType=new"]

def _infer_bdjobs_category(title, text=""):
    blob = f"{safe_text(title)} {safe_text(text)}".lower()
    rules = [
        ("Accounting / Finance", ("account", "finance", "audit", "tax", "treasury")),
        ("Bank / Non-Bank Financial Institution", ("bank", "banking", "credit", "loan", "relationship officer", "relationship manager")),
        ("Commercial / Supply Chain", ("supply chain", "procurement", "purchase", "purchasing", "commercial", "sourcing", "logistics")),
        ("Marketing / Sales", ("marketing", "sales", "business development", "brand", "trade marketing", "territory sales")),
        ("HR / Organization Development", ("human resource", "human resources", "hr ", "hr&", "recruitment", "talent acquisition", "people operations")),
        ("General Management / Admin", ("admin", "administration", "office management", "executive assistant", "general management")),
        ("Customer Service / Call Centre", ("customer service", "call center", "call centre", "contact center", "customer experience", "client service")),
        ("Media / Advertisement / Event Management", ("media", "advertisement", "advertising", "event management", "public relations", "pr executive")),
        ("Research / Consultancy", ("research", "consultancy", "consultant", "analyst", "mis", "planning")),
        ("NGO / Development", ("ngo", "development", "program officer", "project officer", "field officer")),
        ("Hospitality / Travel / Tourism", ("hotel", "hospitality", "travel", "tourism", "front office", "reservation")),
        ("Garments / Textile", ("garment", "textile", "merchandising", "merchandiser", "knit", "woven")),
        ("IT / Telecom - Business Roles", ("business analyst", "product", "product manager", "erp", "crm", "it sales", "telecom sales")),
        ("Education / Training - Business Roles", ("education coordinator", "admission officer", "academic coordinator", "training coordinator", "school admin")),
    ]
    best = None
    for category, terms in rules:
        score = sum(2 if term in safe_text(title).lower() else 1 for term in terms if term in blob)
        if score and (best is None or score > best[0]):
            best = (score, category)
    return best[1] if best else "Other Business"


def _cached_bdjobs_candidates():
    """Use recently researched but unpublished Bdjobs jobs during a transient block.

    The cache is intentionally short-lived and only contains official Bdjobs
    records written by the normal research stage. This prevents one temporary
    403 from producing an empty private feed without ever publishing stale jobs.
    """
    cutoff = datetime.now(BD_TZ) - timedelta(days=BDJOBS_CACHE_MAX_DAYS)
    cached = []
    seen = set()
    for key, item in STATE.get("queue", {}).items():
        if not isinstance(item, dict) or safe_text(item.get("source")) != "Bdjobs":
            continue
        url = canonical_url(item.get("source_url") or item.get("url") or key)
        if not url or url in seen or url in POSTED_URLS:
            continue
        dt = parse_datetime(item.get("last_seen") or item.get("posted_date") or item.get("first_seen"))
        if not dt or dt < cutoff:
            continue
        if deadline_status(item) == "expired":
            continue
        cached_item = dict(item)
        cached_item["canonical"] = url
        cached_item["discovery"] = "bdjobs_recent_cache"
        cached_item["source_category"] = safe_text(cached_item.get("source_category")) or _infer_bdjobs_category(cached_item.get("title", ""), cached_item.get("raw_text", ""))
        cached.append(cached_item)
        seen.add(url)
    cached.sort(key=lambda j: (-posted_freshness_score(j), -deadline_urgency_score(j), j.get("canonical", "")))
    logger.info("BDJOBS CACHE FALLBACK | candidates=%d | max_age=%dd", len(cached), BDJOBS_CACHE_MAX_DAYS)
    return cached


def _tag_and_count_bdjobs_categories(items):
    """Assign every broad candidate to a local business category and log coverage."""
    counts = {c["name"]: 0 for c in BDJOBS_BBA_MBA_CATEGORIES}
    other = 0
    for item in items or []:
        category = safe_text(item.get("source_category")) or _infer_bdjobs_category(item.get("title", ""), item.get("excerpt", "") or item.get("raw_text", ""))
        item["source_category"] = category
        item["source_category_id"] = next((c["id"] for c in BDJOBS_BBA_MBA_CATEGORIES if c["name"] == category), "")
        if category in counts:
            counts[category] += 1
        else:
            other += 1
    covered = sum(1 for v in counts.values() if v)
    compact = " | ".join(f"{name}={counts[name]}" for name in counts if counts[name])
    logger.info("BDJOBS CATEGORY COVERAGE | covered=%d/%d | other=%d | %s", covered, len(counts), other, compact or "none")
    return items


def discover_bdjobs():
    """Low-request Bdjobs discovery with a recent-state safety net.

    Primary order:
      1. official Bdjobs JSON search response;
      2. official broad legacy/cache listing;
      3. recently researched, unpublished Bdjobs records from local state.

    The production job intentionally does NOT perform 14 category HTTP probes.
    Current category links are SPA redirects and repeated probing has caused
    GitHub-hosted traffic to receive 403. Categories remain part of the
    selection algorithm through local classification and diversity quotas.
    """
    merged = []
    seen = set()

    def merge(label, items):
        accepted = 0
        for item in items or []:
            key = safe_text(item.get("canonical")) or canonical_url(item.get("source_url") or item.get("url") or "")
            if not key or key in seen or key in POSTED_URLS:
                continue
            item["canonical"] = key
            if not item.get("source_category"):
                item["source_category"] = _infer_bdjobs_category(item.get("title", ""), item.get("excerpt", "") or item.get("raw_text", ""))
            seen.add(key)
            merged.append(item)
            accepted += 1
            if len(merged) >= FAST_PRIVATE_CANDIDATE_TARGET:
                break
        logger.info("BDJOBS %s DISCOVERY | raw=%d accepted=%d merged=%d", label, len(items or []), accepted, len(merged))

    # One structured request. The previous production run showed this endpoint
    # can deliver a compact, already-structured set of newest records.
    api_items = _discover_bdjobs_api()
    merge("API", api_items)

    # One broad listing request, plus at most the small pagination depth exposed
    # by that page. This is deliberately bounded and never multiplied by category.
    if len(merged) < FAST_PRIVATE_CANDIDATE_TARGET:
        html_items = _discover_bdjobs_html_fallback()
        merge("HTML", html_items)

    # During a temporary source block, reuse fresh state rather than publishing a
    # misleading government-only run. State candidates are still date/deadline checked
    # again by the normal gate before publication.
    fresh = _tag_and_count_bdjobs_categories(merged)
    if len(merged) < FAST_PRIVATE_CANDIDATE_TARGET:
        cached = _cached_bdjobs_candidates()
        if cached:
            merge("CACHE", cached)
            fresh = _tag_and_count_bdjobs_categories(merged)

    if not merged:
        logger.error("BDJOBS PRIVATE SOURCE UNHEALTHY | fresh and cached private candidates both empty")
    elif any(item.get("discovery") == "bdjobs_recent_cache" for item in merged):
        logger.warning("BDJOBS PRIVATE SOURCE HEALTHY+STATE | live candidates supplemented by recent state")
    else:
        logger.info("BDJOBS PRIVATE SOURCE HEALTHY | candidates=%d", len(merged))
    return merged[:FAST_PRIVATE_CANDIDATE_TARGET]
def discover_all():
    """Discover from the two primary official sources only.

    There is deliberately no cross-source fallback: Teletalk owns the government
    stream and Bdjobs owns the private stream. A source returning fewer jobs simply
    contributes fewer jobs for that run.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_teletalk=executor.submit(_discover_teletalk_api)
        f_bdjobs=executor.submit(discover_bdjobs)
        try: government=f_teletalk.result()
        except Exception as exc: logger.warning("Teletalk source worker failed: %s",exc); government=[]
        try: bdjobs=f_bdjobs.result()
        except Exception as exc: logger.warning("Bdjobs source worker failed: %s",exc); bdjobs=[]

    all_items=[]; seen=set()
    for item in government+bdjobs:
        canonical=item.get("canonical") or canonical_url(item.get("source_url", ""))
        if not canonical or canonical in seen:
            continue
        seen.add(canonical); all_items.append(item)
    logger.info("DISCOVERED | Teletalk=%d | Bdjobs=%d | merged=%d",len(government),len(bdjobs),len(all_items))
    return all_items


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
        if source == "Dohaj" and is_domain_allowed(target, [DOHAJ_DOMAIN]):
            # Dohaj itself is the details/source layer, not the original third-party application.
            continue
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
    "প্রতিষ্ঠানের নাম", "চাকুরি স্থান", "চাকরি স্থান", "বয়সসীমা", "বয়সসীমা", "বেতন",
    "চাকরির ধরন", "প্রকাশিত", "শেষ তারিখ", "অভিজ্ঞতা", "শিক্ষাগত যোগ্যতা", "পদ সংখ্যা", "পদসংখ্যা",
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
    # Dohaj sometimes renders the Experience label/value as a separate summary block.
    # If the first label extraction missed it, recover only the label-specific value.
    if not blob and raw_text:
        recovered = _label_value(raw_text, [
            "Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"
        ])
        blob = _clean_one_line(recovered)
    if not blob:
        return ""
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
    return trim_source_text(blob, 80) if blob else ""


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
    blob=_clean_one_line(value).translate(BENGALI_DIGIT_MAP)
    blob=blob.replace("থেকে","to").replace("বছর","years").replace("বছরের","years").replace("ন্যূনতম","minimum")
    if not blob:
        return ""
    m=re.search(r"\b(\d{1,2})\s*(?:to|[-–])\s*(\d{1,2})\s*(?:years?|year)?\b",blob,flags=re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)} Years"
    m=re.search(r"\b(?:at\s+least|minimum(?:\s+age)?|not\s+less\s+than|minimum\s+of)\s*:?\s*(\d{1,2})\s*(?:years?|year)\b",blob,flags=re.I)
    if m:
        return f"{m.group(1)} Years"
    m=re.search(r"\b(\d{1,2})\s*(?:\+|plus|years?|year)\b",blob,flags=re.I)
    if m:
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
    return ""


def compact_workplace(value):
    blob = _clean_one_line(value).lower()
    if not blob:
        return ""
    if "work from home" in blob or "remote" in blob:
        return "Remote"
    if "hybrid" in blob:
        return "Hybrid"
    if "office" in blob or "on-site" in blob or "onsite" in blob:
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


def _dohaj_summary_lines(text):
    """Return the most likely Dohaj Job Summary block, not an arbitrary page section."""
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
    # Break ties toward the later block, matching current Dohaj page structure.
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return candidates[0][3] if candidates else lines


def _dohaj_summary_value(text, labels):
    wanted = {re.sub(r"\s+", " ", safe_text(x).lower().rstrip(":-")).strip() for x in labels}
    lines = _dohaj_summary_lines(text)
    for i, line in enumerate(lines):
        norm = _normalized_line_label(line)
        if norm in wanted and i + 1 < len(lines):
            return lines[i+1]
        for label in sorted(wanted, key=len, reverse=True):
            if norm.startswith(label + ":"):
                return norm[len(label)+1:].strip()
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


def extract_job_fields(text, page_html, source_url, discovery_item):
    text = safe_text(text)
    jsonld = _jobposting_jsonld(page_html) if page_html else {}
    is_gov = bool(discovery_item.get("is_government")) or "/gov-job/" in urlparse(source_url).path.lower()

    title = safe_text(jsonld.get("title")) or _html_h1(page_html) or _label_value(text, ["Title", "Job Title", "Position", "Post Name"])
    company = ""
    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict):
        company = safe_text(hiring.get("name"))
    company = (company or _dohaj_summary_value(text, [
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

    location = _dohaj_summary_value(text, ["চাকুরি স্থান", "চাকরি স্থান", "Job Location", "Location", "Job Location(s)", "Work Location"]) or _label_value(text, ["Job Location", "Location", "Job Location(s)", "Work Location", "চাকুরি স্থান", "চাকরি স্থান"])
    salary = _dohaj_summary_value(text, ["বেতন", "Salary", "Salary Range", "Minimum Salary", "Compensation"]) or _label_value(text, ["Salary", "Salary Range", "Minimum Salary", "Compensation", "বেতন"])
    age = _dohaj_summary_value(text, ["বয়সসীমা", "বয়সসীমা", "Age", "Age Limit", "Age Requirements"]) or _label_value(text, ["Age", "Age Limit", "Age Requirements", "বয়সসীমা", "বয়সসীমা"])
    employment = _dohaj_summary_value(text, ["চাকরির ধরন", "Employment Status", "Job Type", "Employment Type"]) or _label_value(text, ["Employment Status", "Job Type", "Employment Type", "চাকরির ধরন"])
    published = _dohaj_summary_value(text, ["প্রকাশিত", "Published", "Posted", "Date Posted", "Publication Date"]) or _label_value(text, ["Published", "Posted", "Date Posted", "Publication Date", "প্রকাশিত"])
    deadline = _dohaj_summary_value(text, ["শেষ তারিখ", "Application Deadline", "Deadline", "Last Date", "Apply Before"]) or _label_value(text, ["Application Deadline", "Deadline", "Last Date", "Apply Before", "শেষ তারিখ"])

    # Detail-section fields. Use the same source text as a fallback because some
    # Dohaj templates expose the value outside the Job Summary card.
    experience = _dohaj_summary_value(text, ["Experience", "অভিজ্ঞতা"]) or _label_value(text, ["Experience", "Experience Requirements", "Experience Requirement", "অভিজ্ঞতা"])
    education = _dohaj_summary_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"]) or _label_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements", "শিক্ষাগত যোগ্যতা"])
    vacancy = _dohaj_summary_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা"]) or _label_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions", "পদ সংখ্যা", "পদসংখ্যা"])
    if not vacancy and isinstance(jsonld, dict) and jsonld.get("totalJobOpenings") is not None:
        vacancy = safe_text(jsonld.get("totalJobOpenings"))
    workplace = _dohaj_summary_value(text, ["Job Work Place", "Workplace", "Work Place"]) or _label_value(text, ["Job Work Place", "Workplace", "Work Place"])
    category = _label_value(text, ["Category", "Job Category"])
    application_method = _label_value(text, ["Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply", "আবেদন প্রক্রিয়া", "আবেদন প্রক্রিয়া", "আবেদনের নিয়ম", "আবেদনের নিয়ম"])
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

    title = _clean_one_line(title) or _clean_one_line(discovery_item.get("title", ""))
    company = _clean_one_line(company) or _clean_one_line(discovery_item.get("company", ""))
    location = compact_location(location)
    salary = compact_salary(salary)
    experience = compact_experience(experience, text)
    education = compact_education(education, text)
    vacancy = compact_vacancy(vacancy)
    employment = compact_employment(employment)
    workplace = compact_workplace(workplace)
    age = compact_age(age)
    application_method = compact_application(application_method)
    selection_process = compact_selection(selection_process)
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
        "source": "Dohaj" if is_domain_allowed(source_url, [DOHAJ_DOMAIN]) else source_name(source_url),
        "source_url": source_url,
        "url": source_url,
        "discovery": discovery_item.get("discovery", ""),
        "dohaj_category": discovery_item.get("dohaj_category", ""),
        "is_government": is_gov,
    }


def request_safe_url(url):
    """Percent-encode Unicode URL paths so requests can fetch Dohaj Bengali slugs safely."""
    raw=safe_text(url)
    parsed=urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path=quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=%")
    return parsed._replace(path=path).geturl()


def retrieve_job_content(item):
    url=request_safe_url(item["url"])
    try:
        fetched=_scrapling_static_html(url, timeout=FAST_DETAIL_TIMEOUT)
        backend=""
        if fetched:
            page_html=fetched["text"]; final_url=fetched["url"]; backend=fetched["backend"]
        else:
            response=session.get(url,headers=HEADERS,timeout=FAST_DETAIL_TIMEOUT,allow_redirects=True)
            response.raise_for_status(); page_html=response.text; final_url=response.url; backend="direct_http"

        # Dohaj keeps a large portion of the authoritative Job Summary in a page
        # block that high-precision article extraction can omit. For source-backed
        # structured fields, always use the full visible HTML text first.
        full_text = _text_from_html(page_html)
        article_text = trafilatura.extract(
            page_html,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        ) if trafilatura else None

        if is_domain_allowed(final_url, [DOHAJ_DOMAIN]):
            raw_text = full_text
        else:
            raw_text = article_text or full_text

        if not raw_text or len(raw_text) < 250:
            return None

        # Add article text only when it contributes content missing from the full page
        # extraction. This preserves responsibilities/details without losing the summary.
        if article_text and len(article_text) > len(raw_text) * 0.35 and article_text not in raw_text:
            raw_text = raw_text + "\n" + article_text

        return {
            "text": raw_text[:MAX_JOB_CONTENT_CHARS],
            "html": page_html,
            "final_url": final_url,
            "apply_url": extract_apply_url(page_html, final_url, item.get("source", "")),
            "backend": backend or "direct_http",
        }
    except Exception as exc:
        logger.warning("DETAIL retrieval failed %s: %s",url,exc)
        return None


def _research_teletalk_job(item):
    fields=dict(item.get("api_fields") or {})
    fields.update({
        "canonical":item.get("canonical",canonical_url(item.get("source_url",""))),
        "source":"Teletalk","source_url":item.get("source_url",fields.get("source_url","")),
        "apply_url":fields.get("apply_url",item.get("apply_url","")),
        "retrieval_backend":"teletalk_api","listing_posted":item.get("listing_posted",""),
        "listing_deadline":item.get("listing_deadline",""),"is_government":True,
        "source_job_id":fields.get("source_job_id",item.get("source_job_id","")),
    })
    fields["application_method"] = compact_application(fields.get("application_method",""), fields.get("apply_url",""))
    fields["audience_pre_score"] = job_family_score(fields.get("title",""), fields.get("raw_text",""))
    fields["bba_mba_target_score"] = bba_mba_candidate_score(fields)
    fields["event_id"] = job_event_key(fields)
    return fields


def _research_bdjobs_api_job(item):
    """Use Bdjobs API fields as authoritative structured evidence.

    The official search API already supplies the fields required for ranking and
    rendering. Avoiding a detail request here keeps the 100->40 pipeline fast;
    HTML-discovered candidates still receive detail-page enrichment later.
    """
    fields = dict(item.get("api_fields") or {})
    fields.update({
        "canonical": item["canonical"],
        "apply_url": fields.get("apply_url", ""),
        "retrieval_backend": "bdjobs_api",
        "listing_posted": item.get("listing_posted", ""),
        "listing_deadline": item.get("listing_deadline", ""),
        "source": "Bdjobs",
        "source_url": item.get("source_url", item.get("url", "")),
        "is_government": False,
    })
    fields["application_method"] = compact_application(fields.get("application_method", ""), fields.get("apply_url", ""))
    fields["audience_pre_score"] = job_family_score(fields.get("title", ""), fields.get("raw_text", ""))
    fields["bba_mba_target_score"] = bba_mba_candidate_score(fields)
    fields["event_id"] = job_event_key(fields)
    return fields

def research_job(item):
    if item.get("source")=="Teletalk" and item.get("api_fields"):
        return _research_teletalk_job(item)
    if item.get("api_fields"):
        return _research_bdjobs_api_job(item)
    # Retrieve the authoritative source page directly.
    retrieved = retrieve_job_content(item)
    if not retrieved:
        return None
    fields = extract_job_fields(retrieved["text"], retrieved.get("html", ""), item["url"], item)
    apply_url = retrieved.get("apply_url", "")
    if not apply_url and fields["application_method"]:
        urls = re.findall(r"https?://[^\s<>]+", fields["application_method"])
        for candidate in urls:
            if item.get("source") != "Dohaj" or not is_domain_allowed(candidate, [DOHAJ_DOMAIN]):
                apply_url = candidate.rstrip(".,)")
                break
    fields.update({
        "canonical": item["canonical"], "apply_url": apply_url,
        "retrieval_backend": retrieved.get("backend", ""),
        "raw_text": retrieved["text"],
        "listing_posted": item.get("listing_posted", ""),
        "listing_deadline": item.get("listing_deadline", ""),
        "is_government": bool(item.get("is_government")) or "/gov-job/" in urlparse(item["url"]).path.lower(),
    })
    if not fields.get("posted_date") and item.get("listing_posted"):
        fields["posted_date"] = item.get("listing_posted")
    if not fields.get("deadline") and item.get("listing_deadline"):
        fields["deadline"] = item.get("listing_deadline")
    fields["source"] = source_name(item["url"])
    fields["title"] = fields["title"] or item.get("title", "")
    fields["company"] = fields["company"] or item.get("company", "")
    fields["application_method"] = compact_application(fields.get("application_method", ""), apply_url)
    fields["audience_pre_score"] = job_family_score(fields["title"], retrieved["text"][:9000])
    fields["bba_mba_target_score"] = bba_mba_candidate_score(fields)
    fields["event_id"] = job_event_key(fields)
    return fields


# ============================================================
# CAREER INTELLIGENCE SCORING
# ============================================================

DISCOVERY_END = datetime.now(BD_TZ) + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)
DISCOVERY_START = datetime.now(BD_TZ) - timedelta(days=DISCOVERY_LOOKBACK_DAYS)

CORE_BUSINESS_FAMILIES = {
    "Finance & Accounting": ("finance", "account", "accounts", "accounting", "audit", "tax", "treasury"),
    "Banking & Financial Services": ("bank", "banking", "relationship officer", "relationship manager", "credit", "loan", "microfinance"),
    "Marketing & Brand": ("marketing", "brand", "digital marketing", "e-commerce", "ecommerce", "trade marketing"),
    "Sales & Business Development": ("sales", "business development", "bd", "territory sales", "corporate sales", "key account"),
    "HR & Recruitment": ("hr", "human resource", "human resources", "recruitment", "talent acquisition", "people operations"),
    "Supply Chain & Procurement": ("supply chain", "procurement", "purchase", "purchasing", "sourcing", "logistics", "commercial"),
    "Management & Administration": ("management", "admin", "administration", "general management", "executive assistant", "office management"),
    "Operations": ("operations", "operation", "process", "business operations", "service operations"),
    "Corporate & Compliance": ("corporate affairs", "company secretary", "compliance", "regulatory", "internal control"),
    "Research & Analytics": ("analyst", "business analyst", "research", "consultancy", "planning", "mis"),
}

ADJACENT_BUSINESS_FAMILIES = {
    "Customer & Client Service": ("customer service", "client service", "customer experience", "call center", "call centre", "contact center", "front desk", "receptionist", "relationship"),
    "NGO & Development": ("ngo", "development", "program officer", "project officer", "field officer"),
    "Retail & Branch Operations": ("retail", "showroom", "branch", "store", "shop"),
    "Merchandising": ("merchandising", "merchandiser"),
}

NON_BUSINESS_FAMILIES = {
    "Engineering/Technical": ("engineer", "developer", "devops", "network", "system administrator", "technician", "architect"),
    "Healthcare": ("doctor", "medical officer", "nurse", "pharmacist", "physiotherapist", "lab technologist"),
    "Education": ("teacher", "lecturer", "professor", "principal"),
    "Design/Creative": ("graphic designer", "motion designer", "visualizer", "illustrator"),
}

SENIOR_ROLE_PATTERNS = (
    "chief", "cfo", "ceo", "director", "head of", "general manager", "deputy general manager",
    "agm", "dgm", "vice president", "svp", "avp", "senior manager", "country manager",
)

EXPLICIT_BBA_TERMS = (
    "bba", "bachelor of business administration", "business administration", "business studies",
)
EXPLICIT_MBA_TERMS = ("mba", "master of business administration")
BUSINESS_DEGREE_TERMS = ("bbs", "mbs", "commerce", "management", "business", "marketing", "finance", "accounting")
GENERIC_DEGREE_TERMS = ("bachelor", "bsc", "b.com", "honours", "honors", "graduate", "master", "degree")
SPECIALIST_DEGREE_TERMS = ("b.sc in engineering", "engineering degree", "mbbs", "pharmacy", "nursing", "computer science", "software engineering")


def _job_date(job, *keys):
    for key in keys:
        value = safe_text(job.get(key))
        if value:
            dt = parse_datetime(value)
            if dt:
                return dt
    return None


def deadline_status(job):
    dt = _job_date(job, "deadline", "listing_deadline")
    if not dt:
        return "unknown"
    return "expired" if dt < datetime.now(BD_TZ) else "active"


def posted_freshness_score(job):
    dt = _job_date(job, "posted_date", "listing_posted")
    if not dt:
        return 3
    age_hours = max(0, (datetime.now(BD_TZ) - dt).total_seconds() / 3600)
    if age_hours <= 24: return 15
    if age_hours <= 48: return 13
    if age_hours <= 72: return 12
    if age_hours <= 120: return 10
    if age_hours <= 168: return 8
    if age_hours <= 336: return 5
    if age_hours <= 504: return 3
    return 1


def classify_business_family(job):
    title = safe_text(job.get("title")).lower()
    body = f"{title} {safe_text(job.get('education'))} {safe_text(job.get('raw_text'))}".lower()
    best = ("", 0, False)
    for family, terms in CORE_BUSINESS_FAMILIES.items():
        title_hits = sum(1 for term in terms if term in title)
        body_hits = sum(1 for term in terms if term in body)
        score = min(20, title_hits * 12 + max(0, body_hits - title_hits) * 3)
        if score > best[1]: best = (family, score, False)
    for family, terms in ADJACENT_BUSINESS_FAMILIES.items():
        title_hits = sum(1 for term in terms if term in title)
        body_hits = sum(1 for term in terms if term in body)
        score = min(16, title_hits * 9 + max(0, body_hits - title_hits) * 2)
        if score > best[1]: best = (family, score, True)
    for family, terms in NON_BUSINESS_FAMILIES.items():
        if any(term in title for term in terms):
            return family, 0, False
    if not best[0]:
        best = ("Other Business", 6, True)
    return best


def education_priority_score(job):
    edu=f"{safe_text(job.get('education'))} {safe_text(job.get('raw_text'))}".lower()
    title=safe_text(job.get("title")).lower()
    explicit=any(x in edu for x in EXPLICIT_BBA_TERMS+EXPLICIT_MBA_TERMS)
    business=any(x in edu for x in BUSINESS_DEGREE_TERMS)
    generic=any(x in edu for x in GENERIC_DEGREE_TERMS)
    specialist=any(x in edu for x in SPECIALIST_DEGREE_TERMS)
    _,role,_=classify_business_family(job)
    if explicit:return 20
    if specialist and not business:return 2
    if business:return 18 if role>=12 else 16
    if generic and role>=12:return 14
    if generic:return 12
    if any(x in title for x in ("management trainee","graduate trainee","executive","officer","associate")):return 10
    return 7

def _experience_band(value):
    blob = _clean_one_line(value).lower()
    if not blob or blob in {"na", "n/a", "not applicable", "not mentioned", "none"}:
        return (None, None)
    if any(x in blob for x in ("fresh", "fresher", "no experience", "entry-level", "entry level", "graduate trainee")):
        return (0, 0)
    m = re.search(r"(\d+)\s*(?:to|[-–])\s*(\d+)\s*years?", blob)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s*(\d+)\s*years?", blob)
    if m:
        return (int(m.group(1)), None)
    m = re.search(r"(\d+)\s*\+\s*years?", blob)
    if m:
        return (int(m.group(1)), None)
    m = re.search(r"(\d+)\s*years?", blob)
    if m:
        return (int(m.group(1)), int(m.group(1)))
    return (None, None)


def experience_priority_score(job):
    low,high=_experience_band(safe_text(job.get("experience")))
    if low==0:return 10
    if low is None:return 8
    value=high if high is not None else low
    if value<=1:return 10
    if value<=2:return 9
    if value<=3:return 8
    if value<=5:return 6
    if value<=7:return 4
    if value<=10:return 2
    return 1


def age_priority_score(job):
    blob=_clean_one_line(safe_text(job.get("age"))).lower()
    if not blob:return 3
    nums=[int(x) for x in re.findall(r"\d+",blob)]
    if not nums:return 3
    if len(nums)>=2:
        lo,hi=nums[0],nums[1]
        if lo>=18 and hi<=30:return 5
        if lo<=18 and hi<=35:return 4
        if hi<=40:return 3
        return 1
    value=nums[0]
    if 18<=value<=30:return 5
    if value<=35:return 4
    if value<=40:return 2
    return 1


def deadline_urgency_score(job):
    dt = _job_date(job, "deadline", "listing_deadline")
    if not dt:
        return 2
    days = (dt - datetime.now(BD_TZ)).total_seconds() / 86400
    if days < 0: return 0
    if days <= 1: return 10
    if days <= 3: return 9
    if days <= 7: return 8
    if days <= 14: return 6
    if days <= 30: return 4
    return 2


def _numeric_salary_midpoint(value):
    text = safe_text(value).lower().replace(",", "")
    if not text or any(x in text for x in ("negotiable", "competitive", "attractive", "not disclosed")):
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums: return None
    # Avoid interpreting years/other metadata as salary.
    nums = [n for n in nums if n >= 1000]
    if not nums: return None
    return sum(nums[:2]) / min(2, len(nums[:2]))


def salary_priority_score(job, cohort=None):
    midpoint = _numeric_salary_midpoint(job.get("salary"))
    if midpoint is None:
        return 1
    values = [_numeric_salary_midpoint(x.get("salary")) for x in (cohort or [])]
    values = sorted(v for v in values if v is not None)
    if len(values) < 4:
        if midpoint >= 60000: return 5
        if midpoint >= 40000: return 4
        if midpoint >= 30000: return 3
        if midpoint >= 20000: return 2
        return 1
    rank = sum(1 for v in values if v <= midpoint) / len(values)
    return max(1, min(5, int(rank * 5) + 1))


def vacancy_priority_score(job):
    raw = safe_text(job.get("vacancy"))
    nums = re.findall(r"\d+", raw.replace(",", ""))
    if not nums:
        return 1
    try:
        n = max(1, int(nums[0]))
    except Exception:
        return 1
    if n >= 50: return 5
    if n >= 10: return 4
    if n >= 5: return 3
    if n >= 2: return 2
    return 1


def job_quality_score(job):
    score = 0
    for key in ("title", "company", "location", "education", "experience", "salary", "vacancy", "deadline", "posted_date"):
        if safe_text(job.get(key)): score += 1
    if safe_text(job.get("apply_url")) or safe_text(job.get("source_url")): score += 1
    return min(5, round(score / 2))


def category_confidence_score(job):
    category=safe_text(job.get("source_category")).lower()
    family,role,adjacent=classify_business_family(job)
    if not category:return 3 if role>=10 else 2
    expected={
        "accounting / finance":("finance","account"),
        "bank / non-bank financial institution":("bank","finance"),
        "commercial / supply chain":("supply","commercial","procurement","logistics"),
        "marketing / sales":("marketing","sales"),
        "hr / organization development":("hr","human resource"),
        "general management / admin":("management","admin"),
        "customer service / call centre":("customer","client"),
        "media / advertisement / event management":("marketing","media"),
        "research / consultancy":("research","analytics"),
        "ngo / development":("ngo","development"),
        "hospitality / travel / tourism":("customer","operations","hospitality"),
        "garments / textile":("merchandising","supply","marketing","operations"),
        "it / telecom - business roles":("business","product","operations","sales","customer","analytics"),
        "education / training - business roles":("management","admin","coordination","marketing","training"),
    }.get(category,())
    if any(x in family.lower() for x in expected):return 5
    if role>=12 and not adjacent:return 4
    if role>=8:return 3
    return 2


def private_base_score(job, cohort=None):
    family,role_fit,adjacent=classify_business_family(job)
    education=education_priority_score(job)
    experience=experience_priority_score(job)
    age=age_priority_score(job)
    freshness=posted_freshness_score(job)
    deadline=deadline_urgency_score(job)
    salary=salary_priority_score(job,cohort)
    vacancy=vacancy_priority_score(job)
    quality=job_quality_score(job)
    category=category_confidence_score(job)
    score=education+role_fit+experience+age+freshness+deadline+salary+vacancy+quality+category
    return min(100,int(score)),{
        "education":education,"role":role_fit,"career_stage":experience,"age":age,
        "freshness":freshness,"deadline":deadline,"salary":salary,"vacancy":vacancy,"quality":quality,
        "category_confidence":category,"career_family":family,"adjacent_family":adjacent,
    }


def _posted_age_days(job):
    dt=_job_date(job,"posted_date","listing_posted")
    if not dt:return None
    return max(0.0,(datetime.now(BD_TZ)-dt).total_seconds()/86400)

def discovery_priority_score(item):
    title=safe_text(item.get("title")).lower()
    text=f"{title} {safe_text(item.get('excerpt'))}".lower()
    role=0
    for terms in CORE_BUSINESS_FAMILIES.values():
        if any(term in title for term in terms):role=max(role,20)
    for terms in ADJACENT_BUSINESS_FAMILIES.values():
        if any(term in title for term in terms):role=max(role,15)
    if any(x in title for x in ("management trainee","graduate trainee","intern","internship","fresher")):role+=8
    if any(x in title for x in ("executive","officer","associate","assistant","coordinator","representative")):role=max(role,10)
    if any(any(term in title for term in terms) for terms in NON_BUSINESS_FAMILIES.values()):role=0
    fake={"title":item.get("title",""),"education":text,"raw_text":text,"listing_posted":item.get("listing_posted",""),"listing_deadline":item.get("listing_deadline","")}
    return role+posted_freshness_score(fake)+min(10,deadline_urgency_score(fake))+(3 if safe_text(item.get("source_category")) else 0)


def deterministic_job_gate(job):
    if not job.get("title"):return False,"missing_title"
    if is_noise_title(job["title"],job.get("source_url","")):return False,"noise_title"
    if job.get("is_government"):
        if safe_text(job.get("source"))!="Teletalk":return False,"government_source_not_allowed"
        if deadline_status(job)=="expired":return False,"expired"
        return True,"ok_government"
    if safe_text(job.get("source"))!="Bdjobs":return False,"private_source_not_allowed"
    if not job.get("company"):return False,"missing_company"
    if deadline_status(job)=="expired":return False,"expired"
    if not is_domain_allowed(job.get("source_url",""),BDJOBS_DOMAINS):return False,"source_not_allowed"
    age_days=_posted_age_days(job)
    if age_days is not None and age_days>MAX_PRIVATE_POST_AGE_DAYS:return False,f"posted_older_than_{MAX_PRIVATE_POST_AGE_DAYS}_days"
    family,role_fit,_=classify_business_family(job)
    if role_fit<=0 or family in NON_BUSINESS_FAMILIES:return False,"non_business_role"
    base,detail=private_base_score(job)
    job["bba_mba_target_score"]=education_priority_score(job); job["career_family"]=family
    job["base_score"]=base; job["score_breakdown"]=detail
    if education_priority_score(job)<7:return False,"weak_business_education_fit"
    return True,"ok_private"


# ============================================================
# CEREBRAS SEMANTIC AUDITOR
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
                    "semantic_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "red_flag": {"type": "boolean"},
                    "reason": {"type": "string", "maxLength": 180},
                },
                "required": ["id", "semantic_fit", "red_flag", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _judge_prompt():
    return """
You are a semantic auditor for a Bangladesh BBA/MBA career feed.
The deterministic scorer has already checked source validity, dates and basic business relevance.
Your job is NOT to decide publication. Audit each private job for hidden mismatch using only supplied facts.
Check:
- whether the real education requirement fits BBA/MBA/business graduates;
- whether the actual role/function is a business-career role;
- whether the experience/seniority is plausible for a student, fresher or early-career applicant;
- whether the title and description materially contradict each other;
- whether a specialist degree or specialist technical license is actually mandatory.
Set red_flag=true only for a concrete source-backed mismatch or materially misleading listing.
semantic_fit is a 0-100 semantic fit estimate. Do not invent missing facts.
Return every input candidate exactly once.
"""


def judge_batch(batch, batch_no):
    if not get_cerebras():
        logger.info("CEREBRAS unavailable; deterministic ranking only")
        return []
    payload_parts = []
    for idx, job in enumerate(batch, start=1):
        payload_parts.append("\n".join([
            f"ID: {idx}",
            f"Title: {job.get('title','')}",
            f"Company: {job.get('company','')}",
            f"Career family: {job.get('career_family','')}",
            f"Location: {job.get('location','')}",
            f"Education: {trim_source_text(job.get('education',''), 260)}",
            f"Experience: {trim_source_text(job.get('experience',''), 140)}",
            f"Salary: {trim_source_text(job.get('salary',''), 90)}",
            f"Vacancy: {job.get('vacancy','')}",
            f"Posted: {job.get('posted_date','')}",
            f"Deadline: {job.get('deadline','')}",
            f"Description evidence: {trim_source_text(job.get('raw_text',''), 900)}",
            "",
        ]))
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _judge_prompt()},
                {"role": "user", "content": "\n".join(payload_parts)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"career_job_audit_{batch_no}",
                    "strict": True,
                    "schema": JUDGE_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=2800,
        )
        return json.loads(safe_text(response.choices[0].message.content)).get("results", [])
    except Exception as exc:
        logger.error("CEREBRAS audit batch %d failed: %s", batch_no, exc)
        return []


def rank_jobs(jobs):
    """Score the strongest researched private candidates, then apply one semantic audit."""
    if not jobs:
        return []
    salary_cohort = list(jobs)
    base_ranked = []
    for job in jobs:
        base, breakdown = private_base_score(job, salary_cohort)
        candidate = dict(job)
        candidate["base_score"] = base
        candidate["score_breakdown"] = breakdown
        candidate["career_family"] = breakdown["career_family"]
        base_ranked.append(candidate)
    base_ranked.sort(key=lambda j:(-j["base_score"], -posted_freshness_score(j), j.get("canonical", "")))
    ai_candidates = base_ranked[:max(0, min(FAST_AI_CANDIDATE_LIMIT, len(base_ranked)))]
    judged_by_key = {}
    if get_cerebras() and ai_candidates:
        logger.info("CEREBRAS SINGLE AUDIT | %d jobs", len(ai_candidates))
        for row in judge_batch(ai_candidates, 1):
            try: idx = int(row.get("id"))
            except Exception: continue
            if 1 <= idx <= len(ai_candidates):
                judged_by_key[ai_candidates[idx-1].get("canonical")] = row
    ranked = []
    for job in base_ranked:
        row = judged_by_key.get(job.get("canonical"), {})
        semantic = int(row.get("semantic_fit", 75)) if row else 75
        red_flag = bool(row.get("red_flag", False)) if row else False
        final_score = round(job["base_score"] * 0.85 + semantic * 0.15, 2)
        if red_flag:
            final_score = max(0, round(final_score - 12, 2))
        candidate = dict(job)
        candidate.update({
            "semantic_fit": semantic,
            "ai_red_flag": red_flag,
            "ai_reason": safe_text(row.get("reason", "")),
            "final_score": final_score,
            "private_rank_score": final_score,
        })
        if final_score >= PRIVATE_QUALITY_FLOOR:
            ranked.append(candidate)
    ranked.sort(key=lambda j:(-j["final_score"], -j["base_score"], -posted_freshness_score(j), j.get("canonical", "")))
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
    # Cross-source persistent mirror check. A Dohaj/Bdjobs/Teletalk copy can have different
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


def select_private_jobs_by_category(ranked, limit):
    pool=[j for j in ranked if j.get("final_score",0)>=PRIVATE_QUALITY_FLOOR and deadline_status(j)!="expired" and not candidate_already_posted(j)]
    selected=[]; family_counts={}; company_counts={}; category_counts={}; used=set()
    while pool and len(selected)<max(0,int(limit)):
        best=None; best_adjusted=None
        for job in pool:
            key=job.get("canonical")
            if not key or key in used:continue
            family=job.get("career_family") or classify_business_family(job)[0]
            company=_normalized_company(job.get("company",""))
            category=safe_text(job.get("source_category")) or family
            adjusted=float(job.get("final_score",0))
            catc=category_counts.get(category,0); fc=family_counts.get(family,0); cc=company_counts.get(company,0)
            if catc==0:adjusted+=3
            elif catc>=3:adjusted-=3*(catc-2)
            if fc>=4:adjusted-=5*(fc-3)
            if cc==1:adjusted-=5
            elif cc>=2:adjusted-=15
            if best_adjusted is None or adjusted>best_adjusted:best,best_adjusted=job,adjusted
        if best is None:break
        selected.append(best); used.add(best.get("canonical"))
        family=best.get("career_family") or classify_business_family(best)[0]
        company=_normalized_company(best.get("company","")); category=safe_text(best.get("source_category")) or family
        family_counts[family]=family_counts.get(family,0)+1; company_counts[company]=company_counts.get(company,0)+1; category_counts[category]=category_counts.get(category,0)+1
        pool=[x for x in pool if x.get("canonical")!=best.get("canonical")]
    return selected

def select_government_jobs(government_jobs):
    eligible = []
    seen = set()
    for job in government_jobs:
        if candidate_already_posted(job):
            continue
        if deadline_status(job) == "expired":
            continue
        key = job.get("canonical") or job_event_key(job)
        if key in seen:
            continue
        seen.add(key)
        candidate = dict(job)
        candidate["government_rank_score"] = round(
            posted_freshness_score(candidate) + deadline_urgency_score(candidate) + job_quality_score(candidate), 2
        )
        eligible.append(candidate)
    eligible.sort(key=lambda j:(-j.get("government_rank_score", 0), -posted_freshness_score(j), -deadline_urgency_score(j), j.get("canonical", "")))
    return eligible[:MAX_GOVERNMENT_POSTS_PER_RUN]


def select_final_jobs(private_ranked, government_jobs):
    gov_selected = select_government_jobs(government_jobs)
    remaining = max(0, MAX_STORIES_PER_RUN - len(gov_selected))
    private_selected = select_private_jobs_by_category(private_ranked, remaining)
    selected = gov_selected + private_selected
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
        label = "READ MORE"
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
    tags = []
    category = safe_text(job.get("category") or job.get("dohaj_category"))
    blob = f"{job.get('title','')} {job.get('education','')} {job.get('category','')}".lower()
    if any(x in blob for x in ("account", "finance", "audit", "tax")):
        tags.append("#Finance")
    if "bank" in blob:
        tags.append("#Banking")
    if any(x in blob for x in ("marketing", "sales", "brand")):
        tags.append("#Marketing")
    if any(x in blob for x in ("human resource", "hr", "recruitment")):
        tags.append("#HR")
    if any(x in blob for x in ("supply chain", "procurement", "commercial")):
        tags.append("#SupplyChain")
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
    # Diagnostics must not inherit the production retry policy; a dead DNS/host
    # should be reported quickly instead of consuming a large retry/backoff budget.
    started=time.monotonic()
    probe_session=requests.Session()
    probe_session.headers.update(HEADERS)
    try:
        response=probe_session.get(url,params=params,headers=HEADERS,timeout=timeout,allow_redirects=True)
        elapsed=time.monotonic()-started
        payload=None
        if json_expected:
            try: payload=response.json()
            except Exception: payload=None
        return {"ok":response.ok,"status":response.status_code,"elapsed":round(elapsed,2),"payload":payload,"bytes":len(response.content or b"")}
    except Exception as exc:
        return {"ok":False,"status":0,"elapsed":round(time.monotonic()-started,2),"error":str(exc)}


def source_test():
    """Low-impact live diagnostics. Never fan out across 14 categories."""
    print("CAREER NEWS BOT SOURCE TEST")
    print(f"Scrapling static: {'available' if ScraplingFetcher and SCRAPLING_ENABLED else 'disabled/unavailable'}")
    probes = [
        ("Teletalk API", TELETALK_API_URL, {"searchKeyword": ""}, TELETALK_API_TIMEOUT, True),
        ("Bdjobs API", BDJOBS_API_URL, None, FAST_DISCOVERY_TIMEOUT, True),
        ("Bdjobs HTML", BDJOBS_SEARCH_URL, None, FAST_DISCOVERY_TIMEOUT, False),
    ]
    results = []
    for label, url, params, timeout, json_expected in probes:
        result = _probe_get(url, params=params, timeout=timeout, json_expected=json_expected)
        results.append((label, result))
        extra = f"status={result.get('status')} time={result.get('elapsed')}s bytes={result.get('bytes', 0)}"
        if label == "Teletalk API" and result.get("ok"):
            records = _teletalk_records(result.get("payload") or {})
            extra += f" jobs={len(records)}"
        elif label == "Bdjobs API" and result.get("ok"):
            payload = result.get("payload") or {}
            total = len(list(payload.get("data") or [])) + len(list(payload.get("premiumData") or [])) if isinstance(payload, dict) else 0
            extra += f" jobs={total}"
        print(f"{label}: {'OK' if result.get('ok') else 'FAIL'} | {extra}")
        if result.get("error"):
            print(f"  error={result['error']}")

    html_probe = next((r for label, r in results if label == "Bdjobs HTML"), None)
    if html_probe and html_probe.get("ok"):
        print("Bdjobs HTML direct probe: reachable")
    else:
        print("Bdjobs HTML direct probe: blocked/unavailable")

    print("Production discovery: 1 Bdjobs API + 1 broad Bdjobs listing + local category classification")
    print("Category HTTP fan-out: disabled")
    print("Fallback policy: recent unpublished Bdjobs state only; no cross-board private fallback")
    return results


# ============================================================
# MAIN
# ============================================================

def _research_items_parallel(items):
    results=[]
    if not items: return results
    with ThreadPoolExecutor(max_workers=max(1,FAST_DETAIL_WORKERS)) as executor:
        futures={executor.submit(research_job,item):item for item in items}
        for future in as_completed(futures):
            item=futures[future]
            try: researched=future.result()
            except Exception as exc:
                logger.warning("RESEARCH worker failed %s: %s",item.get("url"),exc); researched=None
            if not researched: continue
            researched["source_url"]=item["url"]; researched["canonical"]=item["canonical"]
            researched["is_government"]=bool(item.get("is_government")) or "/gov-job/" in urlparse(item["url"]).path.lower()
            researched["source"] = item.get("source") or source_name(item["url"])
            researched["source_category_id"] = item.get("source_category_id", "")
            researched["source_category"] = item.get("source_category", "")
            results.append(researched)
    return results


def _prepare_shortlists(discovered):
    unique = build_unique_job_pool(discovered)
    government = [x for x in unique if x.get("is_government")]
    private = [x for x in unique if not x.get("is_government")]
    # The first private funnel deliberately looks broad. Its only job is to
    # identify the ~40 records worth spending detail retrieval on.
    private.sort(key=lambda j:(-discovery_priority_score(j), -posted_freshness_score(j), -deadline_urgency_score(j), j.get("canonical", "")))
    government.sort(key=lambda j:(-posted_freshness_score(j), -deadline_urgency_score(j), j.get("canonical", "")))
    return government[:FAST_GOVERNMENT_CANDIDATE_TARGET], private[:FAST_PRIVATE_CANDIDATE_TARGET]

def run():
    started=time.monotonic()
    logger.info("CAREER NEWS BOT | broad Bdjobs acquisition + local BBA/MBA category intelligence | categories=%d | max=%d | research=%d | ai=%d | fresh<=%dd",len(BDJOBS_BBA_MBA_CATEGORIES),MAX_STORIES_PER_RUN,PRIVATE_RESEARCH_TARGET,FAST_AI_CANDIDATE_LIMIT,MAX_PRIVATE_POST_AGE_DAYS)
    prune_state()
    discovered=discover_all()
    gov_items,private_pool=_prepare_shortlists(discovered)
    logger.info("DISCOVERY POOL | government=%d private=%d total=%d",len(gov_items),len(private_pool),len(discovered))
    if not private_pool:
        logger.error("PRIVATE DISCOVERY FAILED | Bdjobs returned no fresh or cached private candidates. Aborting before publication so a broken source cannot masquerade as a successful run.")
        raise RuntimeError("Bdjobs private source unavailable")
    cheap_ranked=sorted(private_pool,key=lambda j:(-discovery_priority_score(j),-posted_freshness_score(j),-deadline_urgency_score(j),j.get("canonical","")))
    private_research_items=cheap_ranked[:PRIVATE_RESEARCH_TARGET]
    logger.info("FUNNEL | private_discovered=%d -> private_research=%d",len(private_pool),len(private_research_items))

    researched=_research_items_parallel(gov_items+private_research_items)
    verified=[]; gate_counts={}
    for job in researched:
        ok,reason=deterministic_job_gate(job); gate_counts[reason]=gate_counts.get(reason,0)+1
        if not ok:
            logger.info("DROP gate: %s | %s | %s",reason,job.get("source",""),job.get("title","")); continue
        save_job_to_queue(job)
        if not candidate_already_posted(job):verified.append(job)
    save_state(STATE)
    logger.info("GATE SUMMARY | %s"," | ".join(f"{k}={v}" for k,v in sorted(gate_counts.items())))

    unique=build_unique_job_pool(verified)
    government_jobs=[j for j in unique if j.get("is_government")]
    private_jobs=[j for j in unique if not j.get("is_government")]
    logger.info("VERIFIED POOL | government=%d private=%d",len(government_jobs),len(private_jobs))
    ranked_private=rank_jobs(private_jobs)
    logger.info("RANKED PRIVATE | %d candidates above quality floor",len(ranked_private))
    for idx,job in enumerate(ranked_private[:12],start=1):
        bd=job.get("score_breakdown",{})
        logger.info("TOP %02d | %.1f | %s | %s | family=%s | category=%s | edu=%s role=%s career=%s age=%s fresh=%s deadline=%s salary=%s vacancy=%s quality=%s",idx,job.get("final_score",0),job.get("title",""),job.get("company",""),job.get("career_family",""),safe_text(job.get("source_category")),bd.get("education",0),bd.get("role",0),bd.get("career_stage",0),bd.get("age",0),bd.get("freshness",0),bd.get("deadline",0),bd.get("salary",0),bd.get("vacancy",0),bd.get("quality",0))

    selected=select_final_jobs(ranked_private,government_jobs)
    safe_selected=[]
    for job in selected:
        if job.get("is_government"):
            if deadline_status(job)!="expired":safe_selected.append(job)
            continue
        if deadline_status(job)=="expired" or job.get("final_score",0)<PRIVATE_QUALITY_FLOOR:continue
        age_days=_posted_age_days(job)
        if age_days is not None and age_days>MAX_PRIVATE_POST_AGE_DAYS:continue
        safe_selected.append(job)
    selected=safe_selected[:MAX_STORIES_PER_RUN]

    selected=translate_government_jobs(selected)
    for job in selected:
        for key in ("title","company","location","salary","experience","education","vacancy","employment_type","workplace","age","application_method","selection_process","category"):
            if _contains_bengali(job.get(key,"")):job[key]=fallback_government_translate(job.get(key,""))
        job["title"]=english_display_text(job.get("title","")); job["company"]=english_display_text(job.get("company",""))

    logger.info("FINAL SELECTED=%d | government=%d | private=%d | discovery=%d | elapsed_before_publish=%.1fs",len(selected),len([j for j in selected if j.get("is_government")]),len([j for j in selected if not j.get("is_government")]),len(discovered),time.monotonic()-started)
    published_count=0
    for index,job in enumerate(selected,start=1):
        blocks=fit_rich_blocks(job)
        if rich_blocks_visible_length(blocks)>MAX_RICH_CHARACTERS:logger.warning("SKIP render-too-large | %s",job.get("title","")); continue
        store_selected_event(job,published=False)
        result=send_rich_text(blocks,job)
        if not result.get("ok"):
            logger.warning("Rich Message failed; text fallback: %s",result.get("description")); result=send_bot_api_text_fallback(job,plain_job_text(job))
        if result.get("ok"):
            published_count+=1; message=result.get("result",{}); message_id=message.get("message_id") if isinstance(message,dict) else None
            POSTED_URLS.add(canonical_url(job["source_url"])); save_posted_url(canonical_url(job["source_url"]))
            item=STATE["queue"].get(job["canonical"])
            if item:item.update({"status":"posted","posted_at":now_iso(),"pipeline_version":PIPELINE_VERSION,"judge_score":job.get("semantic_fit",0),"final_score":job.get("final_score",0)})
            store_selected_event(job,published=True,message_id=message_id); STATE["recent_titles"].append(normalize_title(job["title"]))
            logger.info("PUBLISHED %d/%d | %.1f | %s | %s | category=%s",published_count,len(selected),job.get("final_score",0),job.get("source"),job.get("title"),safe_text(job.get("source_category")))
        else:logger.error("Telegram failed: %s",result.get("description"))
        save_state(STATE)
        if POST_DELAY_SECONDS>0 and index<len(selected):time.sleep(POST_DELAY_SECONDS)
    STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION; save_state(STATE)
    logger.info("Finished Career News Bot. Published=%d | elapsed=%.1fs | hard_max=%d",published_count,time.monotonic()-started,MAX_STORIES_PER_RUN)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    assert PIPELINE_VERSION == "Career News Bot"
    assert len(BDJOBS_BBA_MBA_CATEGORIES) == 14
    assert MAX_PRIVATE_POST_AGE_DAYS == 5
    assert MAX_STORIES_PER_RUN == 20
    assert FAST_PRIVATE_CANDIDATE_TARGET >= 100
    assert PRIVATE_RESEARCH_TARGET >= 20
    assert FAST_AI_CANDIDATE_LIMIT <= 40
    assert MAX_GOVERNMENT_POSTS_PER_RUN == 5
    assert callable(send_rich_text)
    assert BDJOBS_CACHE_MAX_DAYS >= MAX_PRIVATE_POST_AGE_DAYS
    assert MAX_BDJOBS_DISCOVERY_PAGES <= 3

    assert is_noise_title("Our Valuable Partners", "https://jobs.bdjobs.com/jobdetails.asp?id=123")
    assert not is_noise_title("Management Trainee Officer", "https://jobs.bdjobs.com/jobdetails.asp?id=123")
    assert is_bdjobs_job_url("https://jobs.bdjobs.com/jobdetails.asp?id=1534666")
    assert is_bdjobs_job_url("https://bdjobs.com/h/jobs/1534666")
    assert not is_bdjobs_job_url("https://bdjobs.com/h/jobs")
    assert "fcatId=1" in _bdjobs_category_url_variants(BDJOBS_BBA_MBA_CATEGORIES[0])[0]
    assert _infer_bdjobs_category("Accounts Executive", "Finance and accounting") == "Accounting / Finance"
    assert _infer_bdjobs_category("Sales Executive", "Business development and marketing") == "Marketing / Sales"

    bd_fixture = """
    <html><body>
      <a href='/jobdetails.asp?id=101'><span>Accounts Executive</span></a>
      <div>Accounts Executive Example Bank Dhaka</div>
      <a href='/jobdetails.asp?id=102'><span>Graphic Designer</span></a>
      <div>Graphic Designer Creative Studio Dhaka</div>
      <a href='/jobdetails.asp?id=103'><span>HR Executive</span></a>
      <div>HR Executive Example Group Human Resource Dhaka</div>
    </body></html>
    """
    bd_candidates = _bdjobs_listing_candidates(bd_fixture, "https://jobs.bdjobs.com/jobsearch-cache.asp", category=BDJOBS_BBA_MBA_CATEGORIES[0], limit=10)
    assert len(bd_candidates) == 3
    assert all(x["source_category"] == "Accounting / Finance" for x in bd_candidates)
    age_job = dict(base if "base" in locals() else {"title":"Management Trainee","company":"Example Bank","source":"Bdjobs","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=111","education":"BBA","raw_text":"Management Trainee BBA"}, age="18 to 30 years")
    assert age_priority_score(age_job) == 5

    bd_api_records = [
        {"Jobid":"1534666","jobTitle":"ADMIN EXECUTIVE","companyName":"Averroes International School",
         "eduRec":"Bachelor of Business Administration (BBA) in Management","experience":"2 to 3 years",
         "location":"Dhaka","JobType":"FullTime","Vacancies":1,"Salary":"Tk. 30000 - 40000","WorkPlace":"Office",
         "deadlineDB":"2026-10-17T00:00:00Z","publishDate":"2026-09-17T06:09:00Z","jobContext":None,
         "jobDescription":"Administration, coordination and office management."},
        {"Jobid":"1534815","jobTitle":"IT Officer","companyName":"Hi-Tech Group","eduRec":"Bachelor degree",
         "experience":"NA","location":"Dhaka","JobType":"FullTime","Vacancies":1,"Salary":"Tk. 30000","WorkPlace":"Office",
         "deadlineDB":"2026-10-17T00:00:00Z","publishDate":"2026-09-17T12:08:00Z",
         "jobContext":"Mikrotik router configure, networking, website design.","jobDescription":"Technical support."},
        {"Jobid":"1535011","jobTitle":"Assistant Manager / Sr. Executive, Sales & Marketing","companyName":"Spark International",
         "eduRec":"Minimum Graduate degree","experience":"5 to 8 years","location":"Banani","JobType":"FullTime","Vacancies":2,"Salary":"Tk. 70000","WorkPlace":"Office",
         "deadlineDB":"2026-09-28T00:00:00Z","publishDate":"2026-09-17T11:47:00Z","jobContext":None,
         "jobDescription":"Sales and marketing management."},
    ]
    api_fields = _bdjobs_api_candidates(bd_api_records)
    assert len(api_fields) == 3
    admin = next(x for x in api_fields if x["title"] == "ADMIN EXECUTIVE")
    assert admin["company"] == "Averroes International School"
    assert "BBA" in admin["education"]
    assert admin["vacancy"] == "1"
    assert admin["salary"] == "Tk. 30000 - 40000"
    assert admin["deadline"] == "2026-10-17"
    assert admin["posted_date"] == "2026-09-17"
    assert admin["source"] == "Bdjobs"

    tel_fixture = {"job_primary_id":"TL-1001","job_title":"Steno-Typist Cum-Computer Operator",
                   "org_name":"Example Government Department","vacancy":"12","deadline_date":"2026-10-10",
                   "application_site_url":"https://example.teletalk.com.bd/","published_date":"2026-09-18"}
    tel_fields = _teletalk_record_fields(tel_fixture)
    ok, reason = deterministic_job_gate(tel_fields)
    assert ok and reason == "ok_government"
    gov_no_edu = dict(tel_fields, education="SSC", company="")
    assert deterministic_job_gate(gov_no_edu)[0]

    base = {"title":"Management Trainee","company":"Example Bank","location":"Dhaka","education":"BBA / MBA",
            "salary":"Tk. 35000 - 45000","vacancy":"10","deadline":"2026-10-10","posted_date":"2026-09-18",
            "source":"Bdjobs","source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=111",
            "raw_text":"Management trainee program for business graduates."}
    fresh = dict(base, experience="Freshers")
    mid = dict(base, experience="3 to 5 years")
    senior = dict(base, experience="8 years")
    fresh_base, fresh_bd = private_base_score(fresh, [fresh, mid, senior])
    mid_base, _ = private_base_score(mid, [fresh, mid, senior])
    senior_base, senior_bd = private_base_score(senior, [fresh, mid, senior])
    assert fresh_base > mid_base > senior_base
    assert fresh_bd["career_stage"] > senior_bd["career_stage"]
    assert fresh_bd["career_family"] in CORE_BUSINESS_FAMILIES

    for title, expected in (("Finance Executive", "Finance & Accounting"), ("Marketing Executive", "Marketing & Brand"),
                            ("HR Executive", "HR & Recruitment"), ("Procurement Officer", "Supply Chain & Procurement"),
                            ("Business Development Executive", "Sales & Business Development")):
        family, score, _ = classify_business_family(dict(base, title=title, raw_text=title))
        assert family == expected and score > 0

    tech = dict(base, title="Software Engineer", education="BSc in Computer Science", raw_text="Software engineering and development.")
    ok, reason = deterministic_job_gate(tech)
    assert not ok and reason == "non_business_role"

    five_year = dict(base, title="Sales Manager", experience="5 years", education="BBA", raw_text="Sales management.", posted_date="2026-09-18")
    ok, reason = deterministic_job_gate(five_year)
    assert ok and reason == "ok_private"
    old_job = dict(base, title="Marketing Executive", posted_date="2026-09-01")
    ok, reason = deterministic_job_gate(old_job)
    assert (not ok) and reason == "posted_older_than_5_days"

    ranked = []
    examples = [
        ("Finance & Accounting","Finance Executive","Bank A",95), ("Finance & Accounting","Accounts Officer","Bank A",94),
        ("Finance & Accounting","Senior Accounts Executive","Bank B",93), ("Marketing & Brand","Marketing Executive","Brand A",92),
        ("HR & Recruitment","HR Executive","Brand B",91), ("Supply Chain & Procurement","Procurement Officer","Supply A",90),
        ("Sales & Business Development","Sales Executive","Sales A",89),
    ]
    for i, (family, title, company, score) in enumerate(examples):
        ranked.append(dict(base, canonical=f"r-{i}", event_id=f"r-{i}", source_url=f"https://jobs.bdjobs.com/jobdetails.asp?id={200+i}",
                            title=title, company=company, career_family=family, final_score=score, base_score=score,
                            semantic_fit=90, experience="Freshers", education="BBA", raw_text=title, apply_url=""))
    selected = select_private_jobs_by_category(ranked, 6)
    assert len(selected) == 6
    assert len(set(x["company"] for x in selected)) >= 4
    assert len(set(x["career_family"] for x in selected)) >= 4

    govs = []
    for i in range(7):
        govs.append(dict(tel_fields, canonical=f"g-{i}", event_id=f"g-{i}", source_url=f"https://alljobs.teletalk.com.bd/?job_primary_id=G{i}",
                         source_job_id=f"G{i}", posted_date="2026-09-18", deadline="2026-10-10"))
    priv = []
    for i in range(30):
        priv.append(dict(ranked[i % len(ranked)], canonical=f"p-{i}", event_id=f"p-{i}", source_url=f"https://jobs.bdjobs.com/jobdetails.asp?id={5000+i}",
                         company=f"Co-{i}", final_score=85-i*0.2, base_score=85-i*0.2))
    selected = select_final_jobs(priv, govs)
    assert len(selected) <= 20
    assert sum(1 for x in selected if x.get("is_government")) == 5
    assert all(x.get("is_government") for x in selected[:5])

    rendered = rich_message_blocks(fresh)
    assert not any(b.get("type") == "photo" for b in rendered)
    assert any(b.get("type") == "pullquote" and "Your next opportunity starts here." in str(b.get("text", "")) for b in rendered)
    assert not any(label in {"Application Start", "Application End", "Application Period"} for label, _ in job_snapshot_rows(fresh))

    class _FakeScraplingResponse:
        status = 200
        encoding = "utf-8"
        url = "https://example.com/"
        body = b"<html><body><h1>ok</h1></body></html>"
    assert "<h1>ok</h1>" in _decode_scrapling_body(_FakeScraplingResponse())

    logger.info("Career News Bot self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--source-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.source_test:
        source_test()
    else:
        run()
