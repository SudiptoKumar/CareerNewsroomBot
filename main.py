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
PIPELINE_VERSION = "Career News Bot V3"
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# Publication rules for CareerNewsroom V3:
# - Total hard maximum: 20 posts per run.
# - Minimum total target: 5 posts when enough eligible jobs exist.
# - Government: 3-5 posts FIRST in every run when 3-5 eligible/unposted government
#   vacancies are available. No BBA/MBA suitability gate is applied to government jobs.
# - Private: only BBA/MBA/business-candidate-relevant jobs.
# - Private ranking prioritizes education fit, then low/no experience, then freshness,
#   deadline, and verified job quality.
MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 20
MIN_GOVERNMENT_POSTS_PER_RUN = 3
MAX_GOVERNMENT_POSTS_PER_RUN = 5
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "1.0"))
FUTURE_TOLERANCE_MINUTES = 20
DISCOVERY_LOOKBACK_DAYS = int(os.environ.get("DISCOVERY_LOOKBACK_DAYS", "14"))
ACTIVE_JOB_RETENTION_DAYS = int(os.environ.get("ACTIVE_JOB_RETENTION_DAYS", "60"))
MAX_EXA_CANDIDATES = int(os.environ.get("MAX_EXA_CANDIDATES", "100"))
MAX_BDJOBS_DISCOVERY_PAGES = int(os.environ.get("MAX_BDJOBS_DISCOVERY_PAGES", "1"))
MAX_BDJOBS_DETAIL_CANDIDATES = int(os.environ.get("MAX_BDJOBS_DETAIL_CANDIDATES", "160"))
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000

# Scrapling is the HTML acquisition layer for source pages. Static Fetcher is
# preferred because it is lightweight; browser rendering is used only for the
# Bdjobs SPA when the fast API/static paths do not produce enough candidates.
SCRAPLING_ENABLED = (os.environ.get("SCRAPLING_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})
SCRAPLING_DYNAMIC_ENABLED = (os.environ.get("SCRAPLING_DYNAMIC_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})
SCRAPLING_STATIC_TIMEOUT = int(os.environ.get("SCRAPLING_STATIC_TIMEOUT", "15"))
SCRAPLING_DYNAMIC_TIMEOUT_MS = int(os.environ.get("SCRAPLING_DYNAMIC_TIMEOUT_MS", "15000"))
SCRAPLING_DYNAMIC_WAIT_MS = int(os.environ.get("SCRAPLING_DYNAMIC_WAIT_MS", "1500"))
SCRAPLING_DYNAMIC_MAX_CANDIDATES = int(os.environ.get("SCRAPLING_DYNAMIC_MAX_CANDIDATES", "40"))
SCRAPLING_BROWSER_EXECUTABLE = (os.environ.get("SCRAPLING_BROWSER_EXECUTABLE") or "").strip()

# Dohaj has source-owned fixed category feeds. The categories below cover the
# business-heavy areas where BBA/MBA candidates commonly search, plus the main
# all-jobs feed for recent roles that may be filed under a changing category.
DOHAJ_PRIVATE_PAGES_PER_SECTION = int(os.environ.get("DOHAJ_PRIVATE_PAGES_PER_SECTION", "2"))
DOHAJ_GOVERNMENT_MAX_PAGES = int(os.environ.get("DOHAJ_GOVERNMENT_MAX_PAGES", "3"))
DOHAJ_CATEGORY_LINKS_PER_PAGE = int(os.environ.get("DOHAJ_CATEGORY_LINKS_PER_PAGE", "12"))
FAST_PRIVATE_CANDIDATE_TARGET = int(os.environ.get("FAST_PRIVATE_CANDIDATE_TARGET", "60"))
FAST_GOVERNMENT_CANDIDATE_TARGET = int(os.environ.get("FAST_GOVERNMENT_CANDIDATE_TARGET", "10"))
FAST_DETAIL_WORKERS = int(os.environ.get("FAST_DETAIL_WORKERS", "8"))
FAST_AI_CANDIDATE_LIMIT = int(os.environ.get("FAST_AI_CANDIDATE_LIMIT", "20"))
FAST_DISCOVERY_TIMEOUT = int(os.environ.get("FAST_DISCOVERY_TIMEOUT", "15"))
FAST_DETAIL_TIMEOUT = int(os.environ.get("FAST_DETAIL_TIMEOUT", "18"))
# Private-channel experience gate: BBA/MBA students, freshers and early-career candidates.
# 3 years is the maximum accepted experience band; 3-7 years / 7+ years are rejected.
MAX_PRIVATE_EXPERIENCE_YEARS = int(os.environ.get("MAX_PRIVATE_EXPERIENCE_YEARS", "3"))

BDJOBS_SEARCH_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
BDJOBS_DYNAMIC_SEARCH_URL = "https://bdjobs.com/h/jobs"
# Bdjobs backend search endpoint is a bounded newest-page probe. Its current pagination
# contract is not publicly documented/verified, so V3 does not guess parameter names.
# If one page is insufficient, the acquisition ladder moves to another independent path.
BDJOBS_API_URL = "https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch"
BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]

# Government primary source. The endpoint is documented by an independent
# open-source Teletalk AllJobs search project and returns structured records
# including job_primary_id, title, organization, vacancy, deadline and
# application_site_url.
TELETALK_API_URL = "https://alljobs.teletalk.com.bd/api/v1/published-jobs/search"
TELETALK_HOME_URL = "https://alljobs.teletalk.com.bd/"
TELETALK_DOMAIN = "alljobs.teletalk.com.bd"
TELETALK_API_TIMEOUT = int(os.environ.get("TELETALK_API_TIMEOUT", "15"))

# Optional self-hosted Ever Jobs bridge. Disabled unless explicitly configured.
# Ever Jobs exposes POST /api/jobs/search and supports siteType=["bdjobs"].
EVER_JOBS_API_URL = (os.environ.get("EVER_JOBS_API_URL") or "").strip().rstrip("/")
EVER_JOBS_API_KEY = (os.environ.get("EVER_JOBS_API_KEY") or "").strip()
EVER_JOBS_TIMEOUT = int(os.environ.get("EVER_JOBS_TIMEOUT", "15"))

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
# SOURCE DISCOVERY: DOHAJ LATEST WINDOWS
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
    # Secondary government fallback only. Teletalk is the primary path in V3.
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
    """Optional self-hosted Ever Jobs bridge. Never required for V3 operation."""
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

def _bdjobs_listing_candidates(page_html, page_url):
    """Extract current Bdjobs vacancy links from the official listing page."""
    soup = BeautifulSoup(page_html, "html.parser")
    found = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, safe_text(a.get("href")))
        if not is_bdjobs_job_url(href):
            continue
        canonical = canonical_url(href)
        if not canonical or canonical in seen:
            continue
        title = _clean_one_line(a.get_text(" ", strip=True))
        if not title or is_noise_title(title, href):
            continue
        parent = a.find_parent(["li", "div", "article", "section", "tr"])
        card_text = _clean_one_line(parent.get_text(" ", strip=True)) if parent else title
        blob = f"{title} {card_text}".lower()
        if not any(term in blob for term in BDJOBS_NATIVE_CATEGORY_HINTS):
            continue
        seen.add(canonical)
        found.append({
            "title": title,
            "url": href,
            "canonical": canonical,
            "source": "Bdjobs",
            "source_url": href,
            "discovery": "bdjobs_direct",
            "excerpt": trim_source_text(card_text, 2200),
            "discovered_at": now_iso(),
        })
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
                    if n==2: pages.append((n,href)); break
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
    """Gate each record against the same BBA/MBA + early-career hard filters
    used before publication. The API already supplies every field that gate
    needs, so irrelevant categories (IT, medical, engineering...) are dropped
    right here instead of spending a detail-page fetch on them later."""
    found = []
    for record in records:
        fields = _bdjobs_api_record_fields(record)
        if not fields or is_noise_title(fields["title"], fields["url"]):
            continue
        if private_experience_too_high(fields):
            continue
        if bba_mba_candidate_score(fields) < 25:
            continue
        found.append(fields)
    return found


def _bdjobs_api_fetch(page_no):
    """Fetch only the newest unparameterized Bdjobs API page.

    V3 intentionally avoids guessing undocumented pagination parameters.
    Any additional depth must come from an independent path (Ever Jobs bridge
    or the direct HTML listing) rather than duplicate API responses."""
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
    request parameters. V3 therefore limits this path to the newest response
    and uses independent fallbacks when the candidate pool remains short."""
    discovered = []
    seen = set()
    target = min(FAST_PRIVATE_CANDIDATE_TARGET, MAX_BDJOBS_DETAIL_CANDIDATES)
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


def discover_bdjobs():
    """Bounded Bdjobs acquisition ladder.

    1. Official backend API, newest unparameterized page only.
    2. Optional self-hosted Ever Jobs bridge, when configured.
    3. Scrapling static HTTP + direct HTML listing fallback.
    4. Scrapling DynamicFetcher for the current Angular SPA, only as the final fallback.

    The ladder stops as soon as the private candidate target is reached.
    Each rung is invoked lazily so an unnecessary fallback never costs time.
    """
    seen=set(); merged=[]

    def merge_items(label, items):
        accepted=0
        for item in items or []:
            canonical=safe_text(item.get("canonical")) or canonical_url(item.get("source_url") or item.get("url") or "")
            if not canonical or canonical in seen or canonical in POSTED_URLS:
                continue
            item["canonical"]=canonical
            seen.add(canonical); merged.append(item); accepted += 1
            if len(merged)>=FAST_PRIVATE_CANDIDATE_TARGET:
                break
        logger.info("BDJOBS %s DISCOVERY: %d | accepted=%d | merged=%d",label,len(items or []),accepted,len(merged))

    api_items=_discover_bdjobs_api()
    merge_items("API", api_items)
    if len(merged)>=FAST_PRIVATE_CANDIDATE_TARGET:
        return merged

    ever_items=_discover_ever_jobs_bdjobs()
    merge_items("EVER", ever_items)
    if len(merged)>=FAST_PRIVATE_CANDIDATE_TARGET:
        return merged

    html_items=_discover_bdjobs_html_fallback()
    merge_items("HTML", html_items)
    if len(merged)>=FAST_PRIVATE_CANDIDATE_TARGET:
        return merged

    # Final fallback for the current Angular SPA. It is deliberately a single
    # browser render after the fast API, optional bridge, and static HTML paths
    # have failed to provide enough candidates.
    dynamic_items=_scrapling_dynamic_bdjobs_candidates()
    merge_items("SCRAPLING_DYNAMIC", dynamic_items)
    return merged


def discover_all():
    # Source discovery is parallelized across primary government, private and Bdjobs paths.
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_teletalk=executor.submit(_discover_teletalk_api)
        f_dohaj=executor.submit(discover_dohaj)
        f_bdjobs=executor.submit(discover_bdjobs)
        try: government=f_teletalk.result()
        except Exception as exc: logger.warning("Teletalk source worker failed: %s",exc); government=[]
        try: dohaj_private=f_dohaj.result()
        except Exception as exc: logger.warning("Dohaj private source worker failed: %s",exc); dohaj_private=[]
        try: bdjobs=f_bdjobs.result()
        except Exception as exc: logger.warning("Bdjobs source worker failed: %s",exc); bdjobs=[]

    # Only use Dohaj government when the primary Teletalk source is insufficient.
    if len(government)<MIN_GOVERNMENT_POSTS_PER_RUN:
        government.extend(discover_dohaj_government())

    all_items=[]; seen=set()
    for item in government+dohaj_private+bdjobs:
        canonical=item.get("canonical") or canonical_url(item.get("source_url",""))
        if not canonical or canonical in seen: continue
        seen.add(canonical); all_items.append(item)
    logger.info("DISCOVERED | Teletalk=%d | DohajPrivate=%d | Bdjobs=%d | merged=%d",len(government),len(dohaj_private),len(bdjobs),len(all_items))
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
        job["source"]="Dohaj"
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

        # If the current Bdjobs SPA route is returned, render that route once
        # instead of trying to parse the thin application shell with BeautifulSoup.
        if (is_domain_allowed(item.get("url", ""), BDJOBS_DOMAINS) and
                ("/h/jobs" in urlparse(final_url).path.lower() or len(_text_from_html(page_html)) < 250)):
            dynamic=_scrapling_dynamic_html(final_url)
            if dynamic and dynamic.get("text"):
                page_html=dynamic["text"]; final_url=dynamic["url"]; backend=dynamic["backend"]

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
    """Bdjobs API discovery already carries a full, source-backed record.
    Still attempt one detail-page fetch to recover a verified external apply
    link and a longer description for relevance scoring, but a failed/blocked
    detail page must never drop an otherwise complete candidate -- unlike the
    HTML-extraction path, there is no missing data to recover from it."""
    fields = dict(item["api_fields"])
    apply_url = ""
    retrieved = retrieve_job_content(item)
    if retrieved:
        apply_url = retrieved.get("apply_url", "")
        detail_text = safe_text(retrieved.get("text"))
        if len(detail_text) > len(fields.get("raw_text", "")):
            fields["raw_text"] = detail_text[:MAX_JOB_CONTENT_CHARS]
    fields.update({
        "canonical": item["canonical"], "apply_url": apply_url,
        "retrieval_backend": "bdjobs_api" + ("+detail_page" if retrieved else "_only"),
        "listing_posted": item.get("listing_posted", ""),
        "listing_deadline": item.get("listing_deadline", ""),
    })
    fields["application_method"] = compact_application(fields.get("application_method", ""), apply_url)
    fields["audience_pre_score"] = job_family_score(fields["title"], fields.get("raw_text", "")[:9000])
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
    fields["source"] = "Dohaj" if is_domain_allowed(item["url"], [DOHAJ_DOMAIN]) else source_name(item["url"])
    fields["title"] = fields["title"] or item.get("title", "")
    fields["company"] = fields["company"] or item.get("company", "")
    fields["application_method"] = compact_application(fields.get("application_method", ""), apply_url)
    fields["audience_pre_score"] = job_family_score(fields["title"], retrieved["text"][:9000])
    fields["bba_mba_target_score"] = bba_mba_candidate_score(fields)
    fields["event_id"] = job_event_key(fields)
    return fields


# ============================================================
# FRESHNESS / DEADLINE
# ============================================================

DISCOVERY_END = datetime.now(BD_TZ) + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)
DISCOVERY_START = datetime.now(BD_TZ) - timedelta(days=DISCOVERY_LOOKBACK_DAYS)


def deadline_status(job):
    raw = safe_text(job.get("deadline"))
    if not raw:
        return "unknown"
    dt = parse_datetime(raw)
    if not dt:
        try:
            dt = datetime.fromisoformat(raw).replace(tzinfo=BD_TZ)
        except Exception:
            return "unknown"
    return "expired" if dt < datetime.now(BD_TZ) else "active"


def posted_freshness_score(job):
    dt = parse_datetime(job.get("posted_date"))
    if not dt:
        return 8
    age_hours = max(0, (datetime.now(BD_TZ)-dt).total_seconds()/3600)
    if age_hours <= 24: return 24
    if age_hours <= 72: return 20
    if age_hours <= 120: return 16
    if age_hours <= 168: return 12
    if age_hours <= 336: return 7
    return 2


def education_priority_score(job):
    blob = f"{job.get('education','')} {job.get('raw_text','')}".lower()
    exact = 0
    if re.search(r"\bbba\b|bachelor\s+of\s+business\s+administration|business\s+administration", blob):
        exact += 1
    if re.search(r"\bmba\b|master\s+of\s+business\s+administration", blob):
        exact += 1
    if exact >= 2: return 35
    if exact == 1: return 32
    if any(x in blob for x in ("bbs", "mbs", "business studies", "commerce")): return 29
    if any(x in blob for x in ("bachelor", "bsc", "b.com", "honours", "honors", "graduate")): return 21
    if any(x in blob for x in ("master", "degree")): return 18
    return 10


def _experience_years(value):
    blob=_clean_one_line(value).lower()
    if any(x in blob for x in ("fresh", "no experience", "entry-level", "entry level")):
        return 0
    m=re.search(r"(\d+)\s*(?:to|[-–])\s*(\d+)\s*years?", blob)
    if m: return int(m.group(1))
    m=re.search(r"(\d+)\+\s*years?", blob)
    if m: return int(m.group(1))
    m=re.search(r"(\d+)\s*years?", blob)
    if m: return int(m.group(1))
    return None


def experience_priority_score(job):
    exp=safe_text(job.get("experience"))
    if not exp:
        # Missing/unspecified experience is still useful to an early-career audience.
        # Keep it just below explicit fresher roles and above roles demanding 3+ years.
        return 29
    years=_experience_years(exp)
    if years == 0: return 30
    if years is None: return 29
    if years <= 1: return 28
    if years <= 2: return 26
    if years <= 3: return 23
    if years <= 5: return 18
    if years <= 7: return 11
    return 5


def deadline_urgency_score(job):
    dt=parse_datetime(job.get("deadline"))
    if not dt:
        return 1
    days=(dt-datetime.now(BD_TZ)).total_seconds()/86400
    if days < 0: return -20
    if days <= 1: return 12
    if days <= 3: return 10
    if days <= 7: return 8
    if days <= 14: return 6
    return 3


def job_quality_score(job):
    score=0
    if job.get("title"): score+=1
    if job.get("company"): score+=1
    if job.get("location"): score+=1
    if job.get("education"): score+=1
    if job.get("experience"): score+=1
    if job.get("salary"): score+=1
    if job.get("deadline"): score+=1
    if job.get("posted_date"): score+=1
    if job.get("apply_url") or job.get("source_url"): score+=1
    if job.get("application_method"): score+=1
    return min(10, score)


def private_rank_score(job):
    education=education_priority_score(job)
    experience=experience_priority_score(job)
    freshness=posted_freshness_score(job)
    deadline=max(0, min(12, deadline_urgency_score(job)))
    quality=job_quality_score(job)
    ai=min(5, max(0, int(job.get("judge_score", 0))/20))
    role=int(job.get("role_fit", 0))
    # Education is deliberately the largest factor, followed by experience level,
    # then freshness/deadline/quality. This matches the stated audience priority.
    return round(education + experience + freshness + deadline + quality + min(5, role/20) + ai, 3)


def government_rank_score(job):
    freshness=posted_freshness_score(job)
    deadline=max(0, min(12, deadline_urgency_score(job)))
    quality=job_quality_score(job)
    return round(freshness + deadline + quality, 3)


def experience_upper_bound(value):
    """Return the strictest numeric upper bound represented by an experience field."""
    blob=_clean_one_line(value).lower()
    if not blob:
        return None
    if any(x in blob for x in ("fresh", "no experience", "entry-level", "entry level")):
        return 0

    m=re.search(r"(\d+)\s*(?:to|[-–])\s*(\d+)\s*years?", blob, flags=re.I)
    if m:
        return int(m.group(2))

    m=re.search(r"(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s*(\d+)\s*years?", blob, flags=re.I)
    if m:
        return int(m.group(1))

    m=re.search(r"(\d+)\s*\+\s*years?", blob, flags=re.I)
    if m:
        return int(m.group(1))

    m=re.search(r"(\d+)\s*years?", blob, flags=re.I)
    if m:
        return int(m.group(1))
    return None

def private_experience_too_high(job):
    """Hard gate any private role whose explicit experience band exceeds the early-career cap."""
    upper=experience_upper_bound(job.get("experience",""))
    return upper is not None and upper > MAX_PRIVATE_EXPERIENCE_YEARS

def deterministic_job_gate(job):
    if not job.get("title"):
        return False, "missing_title"
    if is_noise_title(job["title"], job.get("source_url", "")):
        return False, "noise_title"
    # Government jobs have no BBA/MBA suitability filter. Do not reject a circular
    # merely because a company/employer field is absent or a date could not be parsed.
    if job.get("is_government"):
        source=safe_text(job.get("source"))
        if source not in {"Dohaj", "Teletalk"} and not is_domain_allowed(job.get("source_url", ""), [DOHAJ_DOMAIN, TELETALK_DOMAIN]):
            return False, "government_source_not_allowed"
        return True, "ok_government"
    if not job.get("company"):
        return False, "missing_company"
    if deadline_status(job) == "expired":
        return False, "expired"
    if not (is_domain_allowed(job.get("source_url", ""), BDJOBS_DOMAINS) or is_domain_allowed(job.get("source_url", ""), [DOHAJ_DOMAIN])):
        return False, "source_not_allowed"
    if private_experience_too_high(job):
        return False, f"experience_above_{MAX_PRIVATE_EXPERIENCE_YEARS}_years"
    target_score=bba_mba_candidate_score(job)
    job["bba_mba_target_score"]=target_score
    if target_score < 25:
        return False, "not_bba_mba_business_candidate_relevant"
    return True, "ok_bba_mba_target"


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
You are the final editorial judge for @CareerNewsroom.

Audience: Bangladesh BBA/MBA students, graduates, freshers and early-career business candidates.

Judge EACH job independently using only the supplied source-backed facts. Do not invent missing information.
For PRIVATE jobs, publish only when the role is meaningfully suitable for a BBA/MBA/business candidate.
For GOVERNMENT jobs from Dohaj's government section, allow publication when the vacancy is genuine,
active/current enough to be useful, and located in Bangladesh. Government posts are a mandatory coverage stream.

Strong private-job signals:
- BBA, MBA, BBS, MBS, business administration, business studies, commerce
- finance/accounting/audit/tax, banking, relationship/credit
- marketing/sales/brand/business development
- HR/recruitment
- management/admin/commercial
- supply chain/procurement/operations
- analyst/customer/client service/front desk/receptionist/business coordination
- management trainee, graduate trainee, internship, fresher, early-career

Do not treat the mere word "MBA" inside a senior specialist requirement as enough.
Do not publish clearly technical/medical/engineering/teaching roles for the private audience unless the
source clearly states a business/management track suitable for BBA/MBA candidates.

Output score meanings are only for filtering, not for public presentation:
- 80-100 strong direct fit
- 60-79 acceptable target fit
- below 60 generally reject private jobs
Government jobs can be published based on genuine government coverage even if their explicit degree fit is not stated.

Return every input candidate.
"""


def judge_batch(batch, batch_no):
    if not get_cerebras():
        logger.info("CEREBRAS unavailable; deterministic ranking only")
        return []
    payload_parts = []
    for idx, job in enumerate(batch, start=1):
        payload_parts.append("\n".join([
            f"ID: {idx}",
            f"Source: {job.get('source','')}",
            f"Title: {job.get('title','')}",
            f"Company: {job.get('company','')}",
            f"Category: {job.get('category','')}",
            f"Location: {job.get('location','')}",
            f"Employment: {job.get('employment_type','')}",
            f"Education: {trim_source_text(job.get('education',''), 180)}",
            f"Experience: {trim_source_text(job.get('experience',''), 120)}",
            f"Salary: {trim_source_text(job.get('salary',''), 100)}",
            f"Vacancy: {job.get('vacancy','')}",
            f"Posted: {job.get('posted_date','')}",
            f"Deadline: {job.get('deadline','')}",
            f"Workplace: {job.get('workplace','')}",
            f"Business relevance pre-score: {job.get('audience_pre_score',0)}",
            f"Listing evidence: {trim_source_text(job.get('raw_text',''), 650)}",
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
                    "name": f"career_job_judgment_{batch_no}",
                    "strict": True,
                    "schema": JUDGE_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=2000,
        )
        return json.loads(safe_text(response.choices[0].message.content)).get("results", [])
    except Exception as exc:
        logger.error("CEREBRAS judge batch %d failed: %s", batch_no, exc)
        return []


def rank_jobs(jobs):
    """Deterministic ranking first, with one optional compact Cerebras review."""
    if not jobs: return []
    pre_ranked=sorted(jobs,key=lambda j:(-education_priority_score(j),-experience_priority_score(j),-posted_freshness_score(j),-deadline_urgency_score(j),-job_quality_score(j),j.get("canonical","")))
    ai_candidates=pre_ranked[:FAST_AI_CANDIDATE_LIMIT]
    judged_by_key={}
    if get_cerebras() and ai_candidates:
        logger.info("CEREBRAS SINGLE REVIEW | %d jobs",len(ai_candidates))
        for row in judge_batch(ai_candidates,1):
            try: idx=int(row.get("id"))
            except Exception: continue
            if 1<=idx<=len(ai_candidates): judged_by_key[ai_candidates[idx-1].get("canonical")]=row
    ranked=[]
    for job in pre_ranked:
        # Defense-in-depth: experience can be enriched after the first gate.
        if private_experience_too_high(job):
            logger.info("DROP rank gate: experience_above_%d_years | %s", MAX_PRIVATE_EXPERIENCE_YEARS, job.get("title",""))
            continue
        row=judged_by_key.get(job.get("canonical"),{})
        candidate=dict(job)
        candidate.update({
            "judge_publish":bool(row.get("publish",True)),
            "judge_score":int(row.get("score",70 if not row else 0)),
            "bba_mba_fit":int(row.get("bba_mba_fit",candidate.get("bba_mba_target_score",0))),
            "early_career_fit":int(row.get("early_career_fit",experience_priority_score(candidate)*3)),
            "role_fit":int(row.get("role_fit",candidate.get("audience_pre_score",0))),
            "judge_reason":safe_text(row.get("reason","Deterministic source-first ranking.")),
        })
        if candidate.get("bba_mba_target_score",0)>=25 and deadline_status(candidate)!="expired" and candidate.get("judge_publish",True):
            candidate["private_rank_score"]=private_rank_score(candidate); ranked.append(candidate)
    ranked.sort(key=lambda j:(-j.get("private_rank_score",0),-education_priority_score(j),-experience_priority_score(j),-posted_freshness_score(j),-job_quality_score(j),j.get("canonical","")))
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
    """Select the highest-ranked private jobs without category quotas displacing audience fit."""
    eligible=[
        job for job in ranked
        if job.get("judge_publish")
        and job.get("judge_score",0)>=60
        and deadline_status(job)!="expired"
        and job.get("bba_mba_target_score",0)>=25
        and not private_experience_too_high(job)
        and not candidate_already_posted(job)
    ]
    return eligible[:max(0, int(limit))]


def select_government_jobs(government_jobs):
    eligible=[]
    seen=set()
    for job in government_jobs:
        if candidate_already_posted(job):
            continue
        if deadline_status(job)=="expired":
            continue
        key=job.get("canonical") or job_event_key(job)
        if key in seen:
            continue
        seen.add(key)
        candidate=dict(job)
        candidate["government_rank_score"]=government_rank_score(candidate)
        eligible.append(candidate)
    eligible.sort(key=lambda j:(
        -j.get("government_rank_score",0),
        -posted_freshness_score(j),
        -deadline_urgency_score(j),
        j.get("canonical","")
    ))
    return eligible[:min(MAX_GOVERNMENT_POSTS_PER_RUN, 5)]


def select_final_jobs(private_ranked, government_jobs):
    # Government posts always occupy the first positions. When at least five valid
    # government vacancies are available, publish five. Otherwise publish the available
    # 3-5 range and then fill the remaining slots with ranked private jobs.
    gov_selected=select_government_jobs(government_jobs)
    remaining=max(0, MAX_STORIES_PER_RUN-len(gov_selected))
    private_selected=select_private_jobs_by_category(private_ranked, remaining)
    selected=gov_selected+private_selected

    # If there are at least MIN_STORIES_PER_RUN candidates overall but the first pass was
    # short because the AI judge returned incomplete results, use remaining judged private
    # candidates. Still never exceed the hard cap of 20.
    if len(selected)<MIN_STORIES_PER_RUN:
        used={j.get("canonical") for j in selected}
        for job in private_ranked:
            if len(selected)>=MIN_STORIES_PER_RUN or len(selected)>=MAX_STORIES_PER_RUN:
                break
            key=job.get("canonical")
            if not key or key in used or candidate_already_posted(job):
                continue
            if not job.get("judge_publish") or job.get("judge_score",0)<60:
                continue
            if deadline_status(job)=="expired":
                continue
            if job.get("bba_mba_target_score",0)<25:
                continue
            if private_experience_too_high(job):
                continue
            selected.append(job); used.add(key)

    # Absolute hard cap. This is intentionally the final step so no other fallback
    # can accidentally reproduce the old 160-post behavior.
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

    source="Dohaj" if job.get("is_government") and is_domain_allowed(job.get("source_url",""), [DOHAJ_DOMAIN]) else safe_text(job.get("source","Source"))
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
    source="Dohaj" if job.get("is_government") and is_domain_allowed(job.get("source_url",""), [DOHAJ_DOMAIN]) else job.get("source","Source")
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
    print("CAREER NEWS BOT V3 SOURCE TEST")
    print(f"Scrapling static: {'available' if ScraplingFetcher and SCRAPLING_ENABLED else 'disabled/unavailable'}")
    print(f"Scrapling dynamic: {'available' if ScraplingDynamicFetcher and SCRAPLING_DYNAMIC_ENABLED else 'disabled/unavailable'} | browser={_browser_executable() or 'not found'}")
    probes=[
        ("Teletalk API",TELETALK_API_URL,{"searchKeyword":""},TELETALK_API_TIMEOUT,True),
        ("Bdjobs API",BDJOBS_API_URL,None,FAST_DISCOVERY_TIMEOUT,True),
        ("Bdjobs HTML",BDJOBS_SEARCH_URL,None,FAST_DISCOVERY_TIMEOUT,False),
        ("Dohaj Government",DOHAJ_GOVERNMENT_URL,None,FAST_DISCOVERY_TIMEOUT,False),
    ]
    results=[]
    for label,url,params,timeout,jexp in probes:
        result=_probe_get(url,params=params,timeout=timeout,json_expected=jexp); results.append((label,result))
        extra=f"status={result.get('status')} time={result.get('elapsed')}s bytes={result.get('bytes',0)}"
        if label=="Teletalk API" and result.get("ok"):
            records=_teletalk_records(result.get("payload") or {}); extra+=f" jobs={len(records)}"
        elif label=="Bdjobs API" and result.get("ok"):
            payload=result.get("payload") or {}; extra+=f" jobs={len(list(payload.get('data') or []))+len(list(payload.get('premiumData') or [])) if isinstance(payload,dict) else 0}"
        print(f"{label}: {'OK' if result.get('ok') else 'FAIL'} | {extra}")
        if result.get("error"): print(f"  error={result['error']}")
    if ScraplingFetcher and SCRAPLING_ENABLED:
        started=time.monotonic()
        probe=_scrapling_static_html(BDJOBS_SEARCH_URL, timeout=SCRAPLING_STATIC_TIMEOUT)
        elapsed=round(time.monotonic()-started,2)
        if probe:
            count=len(_bdjobs_listing_candidates(probe["text"], probe["url"]))
            print(f"Scrapling static Bdjobs: OK | status={probe['status']} time={elapsed}s bytes={len(probe['text'].encode('utf-8', errors='ignore'))} candidates={count} final={probe['url']}")
        else:
            print(f"Scrapling static Bdjobs: FAIL | time={elapsed}s")
    if ScraplingDynamicFetcher and SCRAPLING_DYNAMIC_ENABLED and _browser_executable():
        started=time.monotonic()
        probe=_scrapling_dynamic_html(BDJOBS_DYNAMIC_SEARCH_URL)
        elapsed=round(time.monotonic()-started,2)
        if probe:
            count=len(_bdjobs_listing_candidates(probe["text"], probe["url"]))
            print(f"Scrapling dynamic Bdjobs: OK | status={probe['status']} time={elapsed}s candidates={count} final={probe['url']}")
        else:
            print(f"Scrapling dynamic Bdjobs: FAIL | time={elapsed}s")
    if EVER_JOBS_API_URL:
        print(f"Ever Jobs bridge: configured at {EVER_JOBS_API_URL}")
    else:
        print("Ever Jobs bridge: disabled (set EVER_JOBS_API_URL to enable)")
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
            researched["source"]=("Dohaj" if is_domain_allowed(item["url"],[DOHAJ_DOMAIN]) else source_name(item["url"]))
            results.append(researched)
    return results


def _prepare_shortlists(discovered):
    unique=build_unique_job_pool(discovered)
    gov=[x for x in unique if x.get("is_government")]
    private=[x for x in unique if not x.get("is_government")]
    gov.sort(key=lambda j:(-posted_freshness_score({"posted_date":j.get("listing_posted")}),j.get("canonical","")))
    private.sort(key=lambda j:(-job_family_score(j.get("title",""),j.get("excerpt","")),-posted_freshness_score({"posted_date":j.get("listing_posted")}),j.get("canonical","")))
    return gov[:FAST_GOVERNMENT_CANDIDATE_TARGET],private[:FAST_PRIVATE_CANDIDATE_TARGET]


def run():
    started=time.monotonic()
    logger.info("CAREER NEWS BOT V3 | Scrapling-backed fast pipeline | max=%d | gov first=%d-%d",MAX_STORIES_PER_RUN,MIN_GOVERNMENT_POSTS_PER_RUN,MAX_GOVERNMENT_POSTS_PER_RUN)
    prune_state()
    discovered=discover_all()
    gov_items,private_items=_prepare_shortlists(discovered)
    logger.info("SHORTLISTS | government=%d private=%d",len(gov_items),len(private_items))
    researched=_research_items_parallel(gov_items+private_items)
    verified=[]
    for job in researched:
        ok,reason=deterministic_job_gate(job)
        if not ok:
            logger.info("DROP gate: %s | %s | %s",reason,job.get("source",""),job.get("title","")); continue
        save_job_to_queue(job)
        if not candidate_already_posted(job): verified.append(job)
    save_state(STATE)
    unique=build_unique_job_pool(verified)
    government_jobs=[j for j in unique if j.get("is_government")]
    private_jobs=[j for j in unique if not j.get("is_government")]
    ranked_private=rank_jobs(private_jobs)
    selected=select_final_jobs(ranked_private,government_jobs)
    gov_part=[j for j in selected if j.get("is_government")]
    priv_part=[j for j in selected if not j.get("is_government")]
    selected=(gov_part+priv_part)[:MAX_STORIES_PER_RUN]
    selected=translate_government_jobs(selected)
    # Final publication safety: never publish a private role that now exposes an
    # experience requirement above the early-career cap, even if a later enrichment
    # step changed the field after the first gate.
    safe_selected=[]
    for job in selected:
        if not job.get("is_government") and private_experience_too_high(job):
            logger.info("DROP final safety gate: experience_above_%d_years | %s", MAX_PRIVATE_EXPERIENCE_YEARS, job.get("title",""))
            continue
        safe_selected.append(job)
    selected=safe_selected[:MAX_STORIES_PER_RUN]
    # No Bengali script may reach Telegram. Government jobs get the AI/local translator;
    # any remaining Bengali is locally normalized before rendering.
    for job in selected:
        for key in ("title","company","location","salary","experience","education","vacancy","employment_type","workplace","age","application_method","selection_process","category"):
            if _contains_bengali(job.get(key,"")):
                job[key]=fallback_government_translate(job.get(key,""))
        job["title"]=english_display_text(job.get("title",""))
        job["company"]=english_display_text(job.get("company",""))
    logger.info("FINAL SELECTED=%d | government=%d | private=%d | discovery=%d | elapsed_before_publish=%.1fs",len(selected),len([j for j in selected if j.get("is_government")]),len([j for j in selected if not j.get("is_government")]),len(discovered),time.monotonic()-started)
    published_count=0
    for index,job in enumerate(selected,start=1):
        blocks=fit_rich_blocks(job)
        if rich_blocks_visible_length(blocks)>MAX_RICH_CHARACTERS: continue
        store_selected_event(job,published=False)
        result=send_rich_text(blocks,job)
        if not result.get("ok"):
            logger.warning("Rich Message failed; text fallback: %s",result.get("description")); result=send_bot_api_text_fallback(job,plain_job_text(job))
        if result.get("ok"):
            published_count+=1
            message=result.get("result",{}); message_id=message.get("message_id") if isinstance(message,dict) else None
            POSTED_URLS.add(canonical_url(job["source_url"])); save_posted_url(canonical_url(job["source_url"]))
            item=STATE["queue"].get(job["canonical"])
            if item: item.update({"status":"posted","posted_at":now_iso(),"pipeline_version":PIPELINE_VERSION,"judge_score":job.get("judge_score",0)})
            store_selected_event(job,published=True,message_id=message_id); STATE["recent_titles"].append(normalize_title(job["title"]))
            logger.info("PUBLISHED %d/%d | %s | %s",published_count,len(selected),job.get("source"),job.get("title"))
        else: logger.error("Telegram failed: %s",result.get("description"))
        save_state(STATE)
        if POST_DELAY_SECONDS>0 and index<len(selected): time.sleep(POST_DELAY_SECONDS)
    STATE["last_run"]=now_iso(); STATE["pipeline_version"]=PIPELINE_VERSION; save_state(STATE)
    logger.info("Finished Career News Bot. Published=%d | elapsed=%.1fs | hard_max=%d",published_count,time.monotonic()-started,MAX_STORIES_PER_RUN)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    # Private extraction fixture: every table field must map to its own source label.
    fixture="""
    <html><head>
    <meta property="article:published_time" content="2026-09-18T08:00:00+06:00">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee",
     "datePosted":"2026-09-18","validThrough":"2026-10-18",
     "hiringOrganization":{"name":"Example Bank"},
     "jobLocation":{"address":{"addressLocality":"Dhaka","addressCountry":"Bangladesh"}},
     "employmentType":"FULL_TIME"}
    </script></head><body>
    <h1>Management Trainee</h1>
    <p>Company Name: Example Bank</p>
    <p>Vacancy: 10</p>
    <p>Education: Bachelor of Business Administration (BBA) or MBA</p>
    <p>Experience: Freshers are encouraged to apply.</p>
    <p>Salary: Tk. 35000 - 45000</p>
    <p>Employment Status: Full Time</p>
    <p>Job Work Place: Work at Office</p>
    <p>Age: 18 to 30 years</p>
    <p>Application: Online</p>
    <p>Selection Process: Written exam and viva exam</p>
    <p>Application Deadline: 18 Oct 2026</p>
    </body></html>
    """
    fake={"title":"Management Trainee","url":"https://dohaj.com/job-details/example-123","canonical":canonical_url("https://dohaj.com/job-details/example-123"),"source":"Dohaj","discovery":"self_test"}
    text_source=_text_from_html(fixture)
    fields=extract_job_fields(text_source,fixture,fake["url"],fake)
    fields.update({"raw_text":text_source,"apply_url":"","canonical":fake["canonical"],"audience_pre_score":job_family_score(fields["title"],text_source)})
    assert fields["title"]=="Management Trainee"
    assert fields["company"]=="Example Bank"
    assert "BBA" in fields["education"] and "MBA" in fields["education"]
    assert fields["experience"]=="Freshers"
    assert fields["vacancy"]=="10"
    assert fields["posted_date"]=="2026-09-18"
    assert fields["deadline"]=="2026-10-18"
    assert fields["selection_process"]=="Written + Viva"

    rows=job_snapshot_rows(fields)
    assert "Experience" in [x[0] for x in rows]
    assert all("\n" not in v for _,v in rows)
    assert {label for label,_ in rows} >= {"Deadline","Posted"}
    assert "Application Start" not in {label for label,_ in rows} and "Application End" not in {label for label,_ in rows}
    empty_fields=dict(fields)
    for key in ("location","employment_type","workplace","education","experience","salary","vacancy","age","application_start","application_end","posted_date"):
        empty_fields[key]=""
    empty_rows=job_snapshot_rows(empty_fields)
    assert all(v!="—" for _,v in empty_rows)
    assert {label for label,_ in empty_rows} == {"Application","Deadline"}
    assert format_date_display("2026-09-18")=="18-09-2026"
    assert format_date_display("18 Oct 2026")=="18-10-2026"
    period_fields=dict(fields)
    period_fields["application_start"]="2026-09-15"
    period_fields["application_end"]="2026-10-06"
    period_map=dict(job_snapshot_rows(period_fields))
    assert "Application Start" not in period_map and "Application End" not in period_map
    assert "Deadline" in period_map and "Posted" in period_map
    assert compact_age("at least 25 years")=="25 Years"
    assert compact_age("18 to 30 years")=="18-30 Years"
    assert smart_title_case("global asia bangladesh limited")=="Global Asia Bangladesh Limited"
    blocks=rich_message_blocks(fields)
    table=next(b for b in blocks if b.get("type")=="table")
    assert len(table["cells"])==len(job_snapshot_rows(fields))+1  # header + available data rows
    assert table["is_bordered"] is True
    assert table["is_striped"] is True
    assert table["is_compact"] is False
    assert not any(b.get("type")=="photo" for b in blocks)

    # Experience must not absorb technical responsibilities.
    assert compact_experience("Preparing monthly financial statements and management report.") == ""
    assert compact_experience("2 to 4 years") == "2 to 4 years"

    # Strict vacancy parsing prevents salary/age numbers from becoming vacancy.
    assert compact_vacancy("Tk. 30000 - 40000 (Monthly)") == ""
    assert compact_vacancy("Vacancy: 2") == "2"
    assert compact_vacancy("পদসংখ্যা: 09") == "09"

    # Government Bengali summary fixture, including current Dohaj label/date style.
    gov_text="""
    ময়মনসিংহ বিভাগীয় কমিশনার কার্যালয় নিয়োগ ‍বিজ্ঞপ্তি ২০২৬
    আবেদন শুরুের সময়: ১৫ সেপ্টেম্বর ২০২৬ তারিখ সকাল ১০:০০ টা থেকে
    আবেদনের শেষ সময়: ০৬ অক্টোবর ২০২৬ তারিখ বিকাল ০৪:০০ টা পর্যন্ত
    Job Summary
    চাকরির ধরন
    সরকারি চাকরি
    প্রতিষ্ঠানের নাম
    ময়মনসিংহ বিভাগীয় কমিশনার কার্যালয়
    চাকুরি স্থান
    Mymensingh
    বয়সসীমা
    ১৮ থেকে ৩০ বছর
    বেতন
    ১১,০০০-২৬,৫৯০ টাকা
    প্রকাশিত
    বুধবার ১৬ই সেপ্টেম্বর ২০২৬
    শেষ তারিখ
    মঙ্গলবার ৬ই অক্টোবর ২০২৬
    """
    gov_item={"title":"Mymensingh","url":"https://dohaj.com/gov-job/mymensingh-division-commissioner-office-job-circular-2026","canonical":"dohaj.com/gov-job/mymensingh-division-commissioner-office-job-circular-2026","source":"Dohaj","is_government":True,"discovery":"self_test"}
    gov_fields=extract_job_fields(gov_text,"<h1>Mymensingh Division Commissioner Office Job Circular</h1>",gov_item["url"],gov_item)
    assert gov_fields["company"]=="ময়মনসিংহ বিভাগীয় কমিশনার কার্যালয়"
    assert gov_fields["location"]=="Mymensingh"
    assert gov_fields["posted_date"]=="2026-09-16"
    assert gov_fields["deadline"]=="2026-10-06"
    assert gov_fields["source"]=="Dohaj"
    assert gov_fields["application_start"]=="2026-09-15"
    assert gov_fields["application_end"]=="2026-10-06"
    unicode_url="https://dohaj.com/gov-job/তথ্য-ও-যোগাযোগ-প্রযুক্তি-বিভাগ-নিয়োগ-বিজ্ঞপ্তি-2025"
    safe_url=request_safe_url(unicode_url)
    assert "%" in safe_url and "তথ্য" not in safe_url
    assert canonical_url(unicode_url)==canonical_url(safe_url)
    ok,reason=deterministic_job_gate(gov_fields)
    assert ok and reason=="ok_government"

    # Government receives no BBA/MBA filter and does not require a company field.
    bad_education=dict(gov_fields); bad_education["education"]="SSC"
    ok,_=deterministic_job_gate(bad_education)
    assert ok
    gov_no_company=dict(gov_fields); gov_no_company["company"]=""
    ok,_=deterministic_job_gate(gov_no_company)
    assert ok
    no_company_blocks=rich_message_blocks(gov_no_company)
    assert any("Government Organization" in str(b.get("text","")) for b in no_company_blocks if isinstance(b,dict))

    # Strong duplicate filter, including cross-source mirrors.
    a={"source_url":"https://dohaj.com/job-details/a","title":"Accounts Executive","company":"Example Ltd","location":"Dhaka","posted_date":"2026-09-18"}
    b={"source_url":"https://jobs.bdjobs.com/jobdetails.asp?id=99","title":"Accounts Executive","company":"Example Ltd.","location":"Dhaka","posted_date":"2026-09-18"}
    assert likely_same_job(a,b)

    # Private ranking: BBA/MBA + fresher outranks an otherwise similar experienced role.
    fresh={**fields,"education":"BBA","experience":"Freshers","posted_date":"2026-09-18","deadline":"2026-10-10","judge_score":80,"role_fit":80,"bba_mba_target_score":80}
    unspecified={**fields,"education":"BBA","experience":"","posted_date":"2026-09-18","deadline":"2026-10-10","judge_score":80,"role_fit":80,"bba_mba_target_score":80}
    experienced={**fields,"education":"BBA","experience":"5 years","posted_date":"2026-09-18","deadline":"2026-10-10","judge_score":80,"role_fit":80,"bba_mba_target_score":80}
    assert private_rank_score(fresh)>private_rank_score(unspecified)>private_rank_score(experienced)

    # Hard max and government-first selection.
    govs=[]
    for i in range(7):
        g={**gov_fields,"canonical":f"gov-{i}","source_url":f"https://dohaj.com/gov-job/{i}","event_id":f"gov-{i}","posted_date":"2026-09-18","deadline":"2026-10-10"}
        govs.append(g)
    priv=[]
    for i in range(30):
        p={**fresh,"canonical":f"priv-{i}","source_url":f"https://dohaj.com/job-details/priv-{i}","event_id":f"priv-{i}","judge_publish":True,"judge_score":80,"private_rank_score":80,"bba_mba_target_score":80}
        priv.append(p)
    selected=select_final_jobs(priv,govs)
    assert len(selected)<=20
    assert sum(1 for x in selected if x.get("is_government"))==5
    assert all(x.get("is_government") for x in selected[:5])

    # Photo feature is fully disabled.
    rendered=rich_message_blocks(fresh)
    assert not any(b.get("type")=="photo" for b in rendered)
    assert any(b.get("type")=="pullquote" and "Your next opportunity starts here." in str(b.get("text","")) for b in rendered)
    assert sum(1 for b in rendered if b.get("type")=="paragraph" and b.get("text")=="─────────────────────") == 2
    channel_link = next(b for b in rendered if b.get("type")=="paragraph" and isinstance(b.get("text"), dict) and b["text"].get("type")=="bold" and isinstance(b["text"].get("text"), dict) and b["text"]["text"].get("type")=="url")
    assert channel_link["text"]["text"] == {"type":"url","text":"Career News","url":"https://t.me/CareerNewsroom"}
    # Rendering never includes application start/end rows.
    assert not any(label in {"Application Start","Application End","Application Period"} for label,_ in job_snapshot_rows(fresh))

    # Dohaj full-page Job Summary must supply the authoritative private fields.
    dohaj_fixture="""
    <html><body>
    <h1>HR &amp; Admin Officer</h1>
    <div>Job Description</div>
    <div>Company Name: Global Asia Bangladesh Limited</div>
    <div>Vacancy: --</div>
    <div>Age: At least 18 years</div>
    <div>Job Location: Dhaka (Uttara)</div>
    <div>Salary: Negotiable</div>
    <div>Experience:</div>
    <div>At least 3 years</div>
    <div>Published: 2026-09-16</div>
    <div>Application Deadline: 2026-09-26</div>
    <div>Education:</div>
    <div>HSC</div>
    <div>HSC/BBA/ Hon's/ Masters in business background</div>
    <div>Employment Status: Full Time</div>
    <div>Job Work Place: Work at office</div>
    <div>Job Summary</div>
    <div>Company Name</div><div>Global Asia Bangladesh Limited</div>
    <div>Job Location</div><div>Dhaka (Uttara)</div>
    <div>Vacancy</div><div>--</div>
    <div>Job Type</div><div>Full Time</div>
    <div>Salary</div><div>Negotiable</div>
    <div>Published</div><div>16 Sep 2026</div>
    <div>Deadline</div><div>26 Sep 2026</div>
    </body></html>
    """
    dohaj_text=_text_from_html(dohaj_fixture)
    dohaj_item={"title":"HR & Admin Officer","url":"https://dohaj.com/job-details/hr-admin-officer","is_government":False,"source":"Dohaj","listing_posted":"","listing_deadline":""}
    dohaj_fields=extract_job_fields(dohaj_text,dohaj_fixture,dohaj_item["url"],dohaj_item)
    assert dohaj_fields["company"]=="Global Asia Bangladesh Limited"
    assert dohaj_fields["location"]=="Dhaka (Uttara)"
    assert dohaj_fields["salary"]=="Negotiable"
    assert dohaj_fields["experience"]=="At least 3 years"
    assert "BBA" in dohaj_fields["education"]
    assert dohaj_fields["employment_type"]=="Full Time"
    assert dohaj_fields["workplace"]=="On-site"
    assert dohaj_fields["age"]=="18 Years"
    assert dohaj_fields["vacancy"]==""
    assert dohaj_fields["deadline"]=="2026-09-26"

    # Private early-career gate: reject 3-7 years and 7+ years before AI ranking.
    over_range=dict(dohaj_fields)
    over_range["experience"]="3 to 7 years"
    assert experience_upper_bound(over_range["experience"])==7
    ok,reason=deterministic_job_gate(over_range)
    assert not ok and reason=="experience_above_3_years"

    over_plus=dict(dohaj_fields)
    over_plus["experience"]="7+ years"
    ok,reason=deterministic_job_gate(over_plus)
    assert not ok and reason=="experience_above_3_years"

    allowed_three=dict(dohaj_fields)
    allowed_three["experience"]="3 years"
    ok,reason=deterministic_job_gate(allowed_three)
    assert ok and reason=="ok_bba_mba_target"

    # Bdjobs parser regression test: official listing candidates are no longer silently dropped.
    bd_fixture="""
    <html><body>
      <a href="/jobdetails.asp?id=101"><span>Accounts Executive</span></a>
      <div>Accounts Executive Example Bank Finance Dhaka</div>
      <a href="/jobdetails.asp?id=102"><span>Software Engineer</span></a>
      <div>Software Engineer Tech Ltd Engineering Dhaka</div>
      <a href="/jobdetails.asp?id=103"><span>HR Executive</span></a>
      <div>HR Executive Example Group Human Resource Dhaka</div>
    </body></html>
    """
    bd_candidates=_bdjobs_listing_candidates(bd_fixture,"https://jobs.bdjobs.com/jobsearch-cache.asp")
    assert any(x["url"].endswith("id=101") for x in bd_candidates)
    assert any(x["url"].endswith("id=103") for x in bd_candidates)

    # Bdjobs JSON API regression test: fixture modeled on the live API response
    # shape (verified against the real endpoint). One BBA-eligible early-career
    # record, one irrelevant category, one over-experience business record --
    # only the first should survive discovery-time filtering.
    bd_api_records=[
        {
            "Jobid":"1534666","jobTitle":"ADMIN EXECUTIVE","companyName":"Averroes International School",
            "eduRec":"Bachelor of Business Administration (BBA)\nBachelor of Business Administration (BBA) in Management\n<ul><li><p>Bachelor's degree in Business Administration, Management, or a relevant field.</p></li></ul>",
            "experience":"2 to 3 years","location":"Dhaka","JobType":"FullTime","Vacancies":1,"Salary":"--",
            "WorkPlace":"","deadlineDB":"2026-10-17T00:00:00Z","publishDate":"2026-09-17T06:09:00Z",
            "jobContext":None,"jobDescription":"Bachelor of Business Administration (BBA) in Management.","Cat_id":7,
        },
        {
            "Jobid":"1534815","jobTitle":"IT Officer","companyName":"Hi-Tech Group",
            "eduRec":"","experience":"NA","location":"Dhaka","JobType":"FullTime","Vacancies":1,"Salary":"--",
            "WorkPlace":"Office","deadlineDB":"2026-10-17T00:00:00Z","publishDate":"2026-09-17T12:08:00Z",
            "jobContext":"<p>Mikrotik router configure, networking, website design.</p>","jobDescription":"","Cat_id":8,
        },
        {
            "Jobid":"1535011","jobTitle":"Assistant Manager / Sr. Executive, Sales & Marketing","companyName":"Spark International",
            "eduRec":"<ul><li><p>Minimum Graduate degree from a reputed educational institution.</p></li></ul>",
            "experience":"5 to 8 years","location":"DOHS Banani","JobType":"FullTime","Vacancies":1,"Salary":"--",
            "WorkPlace":"","deadlineDB":"2026-09-28T00:00:00Z","publishDate":"2026-09-17T11:47:00Z",
            "jobContext":None,"jobDescription":"<ul><li><p>Minimum Graduate degree.</p></li></ul>","Cat_id":9,
        },
    ]
    bd_api_filtered=_bdjobs_api_candidates(bd_api_records)
    assert len(bd_api_filtered)==1, f"expected only the BBA-eligible record, got {[x['title'] for x in bd_api_filtered]}"
    bd_api_job=bd_api_filtered[0]
    assert bd_api_job["title"]=="ADMIN EXECUTIVE"
    assert bd_api_job["company"]=="Averroes International School"
    assert "BBA" in bd_api_job["education"]
    assert bd_api_job["experience"]=="2 to 3 years"
    assert bd_api_job["vacancy"]=="1"
    assert bd_api_job["deadline"]=="2026-10-17"
    assert bd_api_job["posted_date"]=="2026-09-17"
    assert bd_api_job["source"]=="Bdjobs"
    assert bd_api_job["url"]=="https://jobs.bdjobs.com/jobdetails.asp?id=1534666"
    assert is_bdjobs_job_url(bd_api_job["url"])
    assert "<" not in bd_api_job["education"] and "<" not in bd_api_job["raw_text"]

    # Malformed API records (no Jobid/title) must be skipped, never crash the pipeline.
    assert _bdjobs_api_record_fields({"jobTitle":"No Id"}) is None
    assert _bdjobs_api_record_fields({"Jobid":"999"}) is None
    assert _bdjobs_api_record_fields("not a dict") is None

    # The over-experience business role must be excluded by the experience gate
    # specifically (not just happen to score low), matching the private-role cap.
    over_exp_fields=_bdjobs_api_record_fields(bd_api_records[2])
    assert private_experience_too_high(over_exp_fields)

    translated={**gov_fields,"title":"কর অঞ্চল-১৬, ঢাকা নিয়োগ বিজ্ঞপ্তি ২০২৬","company":"ময়মনসিংহ বিভাগীয় কমিশনার কার্যালয়","location":"ঢাকা"}
    # Without Cerebras this must still produce non-Bengali publishable text.
    translated=translate_government_jobs([translated])[0]
    assert not any(_contains_bengali(translated.get(k,"")) for k in ("title","company","location"))

    # Teletalk structured-source fixture. No detail-page retrieval is needed.
    teletalk_fixture={
        "job_primary_id": "TL-1001", "job_title": "Accounts Assistant",
        "org_name": "Example Government Department", "vacancy": "12",
        "deadline_date": "2026-10-10", "application_site_url": "https://example.teletalk.com.bd/"
    }
    tel_fields=_teletalk_record_fields(teletalk_fixture)
    assert tel_fields["source"]=="Teletalk" and tel_fields["is_government"]
    assert tel_fields["source_job_id"]=="TL-1001" and tel_fields["vacancy"]=="12"
    assert tel_fields["apply_url"]=="https://example.teletalk.com.bd/"
    tel_item={"canonical":canonical_url(tel_fields["source_url"]),"source":"Teletalk","source_url":tel_fields["source_url"],"api_fields":tel_fields,"is_government":True,"listing_posted":"","listing_deadline":"2026-10-10"}
    tel_researched=_research_teletalk_job(tel_item)
    assert tel_researched["event_id"]==job_event_key(tel_researched)

    # Source-native IDs must distinguish two otherwise identical recruitment records.
    tel_a=dict(tel_researched,source_job_id="A")
    tel_b=dict(tel_researched,source_job_id="B")
    assert job_event_key(tel_a)!=job_event_key(tel_b)

    assert PIPELINE_VERSION == "Career News Bot V3"
    assert MAX_STORIES_PER_RUN == 20
    assert MIN_GOVERNMENT_POSTS_PER_RUN == 3
    assert MAX_PRIVATE_EXPERIENCE_YEARS == 3
    assert MAX_GOVERNMENT_POSTS_PER_RUN == 5
    assert FAST_DETAIL_WORKERS >= 1
    assert FAST_AI_CANDIDATE_LIMIT <= 20
    assert callable(send_rich_text)

    # Current Bdjobs SPA routes are accepted by the normalized URL gate.
    assert is_bdjobs_job_url("https://bdjobs.com/h/jobs/1534666")
    assert not is_bdjobs_job_url("https://bdjobs.com/h/jobs")

    # Scrapling adapter contract can be exercised without the package installed
    # by mocking is intentionally handled by the normal fallback path. When the
    # package is present in CI, verify the decoder contract with a tiny fake response.
    class _FakeScraplingResponse:
        status=200
        encoding="utf-8"
        url="https://example.com/"
        body=b"<html><body><h1>ok</h1></body></html>"
    assert "<h1>ok</h1>" in _decode_scrapling_body(_FakeScraplingResponse())

    logger.info("Career News Bot V3 self-test passed.")


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
