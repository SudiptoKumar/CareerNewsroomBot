import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFile, ImageStat
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

CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# Publication rules requested for CareerNewsroom:
# - At least 5 posts per run when enough eligible jobs exist.
# - No hard maximum post count.
# - Government: cover all newly discovered eligible posts from Dohaj's government section,
#   with a minimum of 3 published in a run when at least 3 eligible/unposted jobs exist.
# - Private: only BBA/MBA/business-candidate-relevant jobs.
# - For private categories represented in the eligible pool, target at least 2 posts per category
#   before filling with the remaining ranked jobs.
MIN_STORIES_PER_RUN = 5
MIN_GOVERNMENT_POSTS_PER_RUN = 3
MIN_PRIVATE_POSTS_PER_CATEGORY = 2
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "3.0"))
FUTURE_TOLERANCE_MINUTES = 20
DISCOVERY_LOOKBACK_DAYS = int(os.environ.get("DISCOVERY_LOOKBACK_DAYS", "14"))
GOVERNMENT_LOOKBACK_DAYS = int(os.environ.get("GOVERNMENT_LOOKBACK_DAYS", "60"))
ACTIVE_JOB_RETENTION_DAYS = int(os.environ.get("ACTIVE_JOB_RETENTION_DAYS", "45"))
MAX_EXA_CANDIDATES = int(os.environ.get("MAX_EXA_CANDIDATES", "100"))
MAX_BDJOBS_DISCOVERY_PAGES = int(os.environ.get("MAX_BDJOBS_DISCOVERY_PAGES", "8"))
MAX_BDJOBS_DETAIL_CANDIDATES = int(os.environ.get("MAX_BDJOBS_DETAIL_CANDIDATES", "120"))
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000

# Dohaj pages are newest-first. The bot crawls fixed category URLs with pagination.
# Private pages are bounded to the newest pages to keep the scheduled run practical.
DOHAJ_PRIVATE_PAGES_PER_SECTION = int(os.environ.get("DOHAJ_PRIVATE_PAGES_PER_SECTION", "3"))
DOHAJ_GOVERNMENT_PAGES_PER_RUN = int(os.environ.get("DOHAJ_GOVERNMENT_PAGES_PER_RUN", "12"))
DOHAJ_CATEGORY_LINKS_PER_PAGE = int(os.environ.get("DOHAJ_CATEGORY_LINKS_PER_PAGE", "40"))

BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]
DOHAJ_DOMAIN = "dohaj.com"

DOHAJ_SECTION_URLS = [
    "https://dohaj.com/category/accounting-finance",
    "https://dohaj.com/category/marketing-sales",
    "https://dohaj.com/category/hr-org-development",
    "https://dohaj.com/category/gen-mgt-admin",
    "https://dohaj.com/category/commercial",
    "https://dohaj.com/category/supply-chain-procurement",
    "https://dohaj.com/category/bank-non-bank-fin-institution",
    # Broader newest-jobs feed catches business roles whose category changes or is not
    # in the fixed business sections above. Detail-page BBA/MBA filtering remains required.
    "https://dohaj.com/jobs/all",
    "https://dohaj.com/gov-jobs",
]

# Backward-compatible alias used by the self-test and older state assumptions.
DOHAJ_TOP5_URLS = DOHAJ_SECTION_URLS

DOHAJ_CATEGORY_NAMES = {
    "accounting-finance": "Accounting/Finance",
    "marketing-sales": "Marketing/Sales",
    "hr-org-development": "HR/Org. Development",
    "gen-mgt-admin": "General Management/Admin",
    "commercial": "Commercial",
    "supply-chain-procurement": "Supply Chain/Procurement",
    "bank-non-bank-fin-institution": "Bank/Non-Bank Fin. Institution",
    "jobs": "All Jobs",
    "gov-jobs": "Government Jobs",
}

# The working ScienceNewsroom architecture uses one global ranked pool.
# We retain the same approach, but the candidate universe is now only BDjobs
# and the explicitly allowed Dohaj category pages.
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
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("career-news-bot")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 50_000_000

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
    path = parsed.path or "/"
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
    return "/jobdetails" in parsed.path.lower() and bool(re.search(r"(?:^|[?&])id=\d+", parsed.query, re.I))


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


def job_event_key(job):
    base = "|".join([
        normalize_title(job.get("title", "")),
        normalize_title(job.get("company", "")),
        normalize_title(job.get("location", "")),
    ])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


def likely_same_job(a, b):
    if title_similarity(a.get("title", ""), b.get("title", "")) >= 0.92:
        ca = normalize_title(a.get("company", ""))
        cb = normalize_title(b.get("company", ""))
        if ca and cb and SequenceMatcher(None, ca, cb).ratio() >= 0.85:
            return True
        if a.get("source") == b.get("source"):
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

exa = None
cerebras = None


def get_exa():
    global exa
    if exa is None:
        exa = Exa(api_key=EXA_API_KEY)
    return exa


def get_cerebras():
    global cerebras
    if cerebras is None:
        cerebras = Cerebras(api_key=CEREBRAS_API_KEY)
    return cerebras


# ============================================================
# SOURCE DISCOVERY: DOHAJ TOP-5 ONLY
# ============================================================

def extract_dohaj_page(category_url, category_name, page_no=1, limit=40):
    """Read one paginated Dohaj section page and collect job detail links."""
    page_url = dohaj_page_url(category_url, page_no)
    is_gov = category_name == "Government Jobs"
    try:
        response = session.get(page_url, headers=HEADERS, timeout=30)
        if response.status_code >= 400:
            logger.warning("DOHAJ section failed %s HTTP=%s", page_url, response.status_code)
            return []
        soup = BeautifulSoup(response.text, "html.parser")
        results, seen = [], set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, safe_text(anchor.get("href")))
            if not is_dohaj_job_url(href):
                continue
            canonical = canonical_url(href)
            if not canonical or canonical in seen:
                continue
            title = safe_text(anchor.get_text(" ", strip=True))
            if not title or is_noise_title(title, href):
                continue
            parent = anchor.find_parent(["article", "li", "div", "section"])
            card_text = safe_text(parent.get_text(" ", strip=True)) if parent else title
            seen.add(canonical)
            results.append({
                "title": title,
                "url": href,
                "canonical": canonical,
                "source": "Dohaj",
                "source_url": href,
                "discovery": "dohaj_direct",
                "dohaj_category": category_name,
                "is_government": is_gov,
                "excerpt": trim_source_text(card_text, 1800),
                "discovered_at": now_iso(),
            })
            if len(results) >= limit:
                break
        logger.info("DOHAJ DIRECT | %s | page=%d | %d", category_name, page_no, len(results))
        return results
    except Exception as exc:
        logger.warning("DOHAJ section error %s page=%d: %s", category_name, page_no, exc)
        return []


def _dohaj_private_title_signal(title):
    blob = safe_text(title).lower()
    if not blob:
        return False
    # BBA/MBA suitable jobs are broad, so retain common business-role titles even
    # when the listing card itself does not expose the education requirement.
    return any(term in blob for term in BUSINESS_ROLE_TERMS) and not any(term in blob for term in NON_BUSINESS_ROLE_TERMS)


def discover_dohaj():
    results, seen = [], set()
    gov_url = "https://dohaj.com/gov-jobs"

    # Government section: crawl multiple pages so newly published circulars are not
    # restricted to the first five entries as in V2. Gov jobs have a separate /gov-job/ path.
    for page_no in range(1, DOHAJ_GOVERNMENT_PAGES_PER_RUN + 1):
        page_items = extract_dohaj_page(gov_url, "Government Jobs", page_no, DOHAJ_CATEGORY_LINKS_PER_PAGE)
        if not page_items:
            break
        for item in page_items:
            if item["canonical"] not in seen:
                seen.add(item["canonical"])
                results.append(item)

    # Private sections: crawl the newest pages of every fixed category URL, but prefilter
    # obvious non-business titles before requesting their detail pages. The detail-page
    # gate remains authoritative for BBA/MBA relevance.
    for url in DOHAJ_SECTION_URLS:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1]
        if slug == "gov-jobs":
            continue
        category_name = "All Jobs" if path == "/jobs/all" else DOHAJ_CATEGORY_NAMES.get(slug, slug)
        for page_no in range(1, DOHAJ_PRIVATE_PAGES_PER_SECTION + 1):
            page_items = extract_dohaj_page(url, category_name, page_no, DOHAJ_CATEGORY_LINKS_PER_PAGE)
            if not page_items:
                break
            for item in page_items:
                if not _dohaj_private_title_signal(item["title"]):
                    continue
                if item["canonical"] in seen:
                    continue
                seen.add(item["canonical"])
                results.append(item)

    logger.info("DOHAJ TOTAL DISCOVERY: %d | government=%d | private=%d", len(results), sum(1 for x in results if x.get("is_government")), sum(1 for x in results if not x.get("is_government")))
    return results


# ============================================================
# SOURCE DISCOVERY: BDJOBS DIRECT WEBSITE
# ============================================================

BDJOBS_SEARCH_URL = "https://jobs.bdjobs.com/jobsearch-cache.asp"
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
        if label.isdigit() and 1 <= int(label) <= MAX_BDJOBS_DISCOVERY_PAGES:
            if is_domain_allowed(href, BDJOBS_DOMAINS):
                links.append((int(label), href))
    return sorted(dict(links).items())

def _bdjobs_listing_candidates(page_html, page_url):
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
        title = safe_text(a.get_text(" ", strip=True))
        if not title or is_noise_title(title, href):
            continue
        parent = a.find_parent(["li", "div", "article", "section"])
        card_text = safe_text(parent.get_text(" ", strip=True)) if parent else title
        blob = f"{title} {card_text}".lower()
        if not any(term in blob for term in BDJOBS_NATIVE_CATEGORY_HINTS):
            continue
        seen.add(canonical)
        found.append({
            "title": title, "url": href, "canonical": canonical,
            "source": "Bdjobs", "source_url": href,
            "discovery": "bdjobs_direct",
            "excerpt": trim_source_text(card_text, 2200),
            "discovered_at": now_iso(),
        })
    return found

def discover_bdjobs():
    discovered, seen = [], set()
    pages_to_visit = [(1, BDJOBS_SEARCH_URL)]
    try:
        while pages_to_visit and len(discovered) < MAX_BDJOBS_DETAIL_CANDIDATES:
            page_no, page_url = pages_to_visit.pop(0)
            response = session.get(page_url, headers=HEADERS, timeout=30)
            if response.status_code >= 400:
                logger.warning("BDJOBS direct page failed %s HTTP=%s", page_url, response.status_code)
                continue
            candidates = _bdjobs_listing_candidates(response.text, response.url)
            for item in candidates:
                if item["canonical"] in seen or item["canonical"] in POSTED_URLS:
                    continue
                seen.add(item["canonical"])
                discovered.append(item)
                if len(discovered) >= MAX_BDJOBS_DETAIL_CANDIDATES:
                    break
            if page_no == 1:
                for n, href in _bdjobs_pagination_links(response.text, response.url):
                    if n > 1 and n <= MAX_BDJOBS_DISCOVERY_PAGES:
                        pages_to_visit.append((n, href))
        logger.info("BDJOBS DIRECT DISCOVERY: %d", len(discovered))
        return discovered
    except Exception as exc:
        logger.warning("BDJOBS direct discovery failed: %s", exc)
        return discovered


def discover_all():
    dohaj = discover_dohaj()
    bdjobs = discover_bdjobs()
    all_items = []
    seen = set()
    for item in dohaj + bdjobs:
        canonical = item["canonical"]
        if canonical in seen:
            continue
        seen.add(canonical)
        all_items.append(item)
    logger.info("DISCOVERED | Dohaj=%d | Bdjobs=%d | merged=%d", len(dohaj), len(bdjobs), len(all_items))
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
    """Extract one labelled block without swallowing the next labelled field."""
    raw = safe_text(text)
    if not raw:
        return ""

    wanted = {re.sub(r"\s+", " ", safe_text(label).lower().rstrip(":")).strip() for label in labels}
    lines = [re.sub(r"\s+", " ", x).strip() for x in raw.splitlines() if x.strip()]

    for i, line in enumerate(lines):
        # Inline form: Experience: 2 years
        for label in sorted(wanted, key=len, reverse=True):
            if line.lower().startswith(label + ":"):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value

        normalized = _normalized_line_label(line)
        if normalized not in wanted:
            continue

        # A bare field label gets a block until the next known source label.
        collected = []
        for nxt in lines[i + 1:]:
            nxt_norm = _normalized_line_label(nxt)
            if nxt_norm in JOB_LABEL_NORMALIZED:
                break
            # Stop on common section headings that are visually equivalent to labels.
            if re.match(r"^(responsibilities|requirements|additional requirements|benefits|company information)\s*:?$", nxt, re.I):
                break
            collected.append(nxt)
        return " ".join(collected).strip()

    # Flattened pages: stop at the next explicit known job label, not an arbitrary
    # word followed by a colon. This avoids the V2 bug where Experience became Published.
    label_pattern = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
    all_label_pattern = "|".join(re.escape(x) for x in sorted(JOB_LABELS, key=len, reverse=True))
    if label_pattern:
        match = re.search(
            rf"(?:^|[\n|])\s*(?:{label_pattern})\s*[:\-]\s*(.*?)(?=\s+(?:{all_label_pattern})\s*[:\-]|[\n|]\s*(?:{all_label_pattern})\s*[:\-]|$)",
            raw,
            flags=re.I | re.S,
        )
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
    # Prefer explicit numeric/fresher evidence and never publish a prose responsibility.
    blob = _clean_one_line(value)
    if not blob:
        return ""
    patterns = [
        r"\b(at\s+least\s+\d+\s+years?)(?:\s+of\s+experience)?\b",
        r"\b(\d+\s*[-–]\s*\d+\s*years?)\b",
        r"\b(\d+\s+to\s+\d+\s+years?)\b",
        r"\b(\d+\+\s*years?)\b",
        r"\b(\d+\s+years?)\b",
        r"\b(fresh(?:er|ers)|no\s+experience|entry[- ]level)\b",
    ]
    found = _first_match(blob, patterns)
    if found:
        return found.replace("–", "-")

    # Some Dohaj listings use "Area of Experience:" instead of a years-based requirement.
    # Keep only the actual short area, never the surrounding job-responsibility prose.
    area = re.search(r"\barea\s+of\s+experience\s*[:\-]\s*(.+)$", blob, flags=re.I)
    if area:
        value = _clean_one_line(area.group(1))
        # Keep the first requirement sentence, not the later preference/responsibility text.
        value = re.split(r"[.;]", value, maxsplit=1)[0].strip()
        return trim_source_text(value, 88)

    experience_in = re.search(r"\bexperience\s+in\s+(.+)$", blob, flags=re.I)
    if experience_in:
        return trim_source_text(_clean_one_line(experience_in.group(1)), 88)
    return ""


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
    match = re.search(r"\b(\d{1,5})\b", blob.replace(",", ""))
    return match.group(1) if match else ""


def compact_age(value):
    blob = _clean_one_line(value)
    if not blob:
        return ""
    for pattern in (
        r"\b(at\s+least\s+\d+\s+years?)\b",
        r"\b(\d+\s*(?:to|[-–])\s*\d+\s*years?)\b",
        r"\b(\d+\+\s*years?)\b",
    ):
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return re.sub(r"\s*[-–]\s*", "-", match.group(1))
    return ""


def compact_employment(value):
    blob = _clean_one_line(value).lower()
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


def normalize_date_text(value):
    raw = safe_text(value)
    if not raw:
        return ""
    parsed = parse_datetime(raw)
    if parsed:
        return parsed.date().isoformat()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except Exception:
            pass
    return raw


def extract_job_fields(text, page_html, source_url, discovery_item):
    text = safe_text(text)
    jsonld = _jobposting_jsonld(page_html) if page_html else {}

    title = safe_text(jsonld.get("title")) or _label_value(text, ["Title", "Job Title", "Position", "Post Name"])
    company = ""
    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict):
        company = safe_text(hiring.get("name"))
    company = company or _label_value(text, ["Company Name", "Company", "Organization Name", "Employer"])
    location = _label_value(text, ["Job Location", "Location", "Job Location(s)", "Work Location"])
    salary = _label_value(text, ["Salary", "Salary Range", "Minimum Salary", "Compensation"])
    experience = _label_value(text, ["Experience", "Experience Requirements", "Experience Requirement"])
    education = _label_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements"])
    vacancy = _label_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions"])
    employment = _label_value(text, ["Employment Status", "Job Type", "Employment Type"])
    workplace = _label_value(text, ["Job Work Place", "Workplace", "Work Place"])
    age = _label_value(text, ["Age", "Age Limit", "Age Requirements"])
    category = _label_value(text, ["Category", "Job Category"])
    application_method = _label_value(text, ["Application", "Application Process", "Application Procedure", "How to Apply", "Read Before Apply"])
    selection_process = _label_value(text, ["Selection Process", "Recruitment Process", "Selection Procedure", "Hiring Process", "Interview Process"])
    application_period = _label_value(text, ["Application Period", "Application Date", "Interview Date", "Walk-in Date"])

    published = _label_value(text, ["Published", "Posted", "Date Posted", "Publication Date"])
    deadline = _label_value(text, ["Application Deadline", "Deadline", "Last Date", "Apply Before"])

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
        jl = jsonld.get("jobLocation")
        if not location and isinstance(jl, dict):
            addr = jl.get("address")
            if isinstance(addr, dict):
                location = ", ".join(safe_text(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")) if safe_text(x))
            elif isinstance(addr, str):
                location = addr

    title = _clean_one_line(title) or discovery_item.get("title", "")
    company = _clean_one_line(company) or discovery_item.get("company", "")
    published_iso = normalize_date_text(published or discovery_item.get("published_at_exa", ""))
    deadline_iso = normalize_date_text(deadline)

    # Normalize to compact candidate-facing facts before the editor/rendering layers see them.
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
    application_period = _clean_one_line(application_period)

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
        "category": category,
        "application_method": application_method,
        "selection_process": selection_process,
        "application_period": application_period,
        "posted_date": published_iso,
        "deadline": deadline_iso,
        "source": discovery_item.get("source") or source_name(source_url),
        "source_url": source_url,
        "url": source_url,
        "discovery": discovery_item.get("discovery", ""),
        "dohaj_category": discovery_item.get("dohaj_category", ""),
    }


def retrieve_job_content(item):
    url = item["url"]
    # First use the exact working architecture: direct HTTP + Trafilatura.
    try:
        response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=30)
        if response.status_code < 400:
            page_html = response.text
            text = trafilatura.extract(
                page_html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            raw_text = text or _text_from_html(page_html)
            if raw_text and len(raw_text) >= 400:
                apply_url = extract_apply_url(page_html, response.url, item.get("source", ""))
                image_candidates = find_image_candidates(response.url, page_html, response.url)
                return {
                    "text": raw_text[:MAX_JOB_CONTENT_CHARS],
                    "html": page_html,
                    "final_url": response.url,
                    "apply_url": apply_url,
                    "image_candidates": image_candidates,
                    "backend": "direct_http",
                }
    except Exception as exc:
        logger.warning("Direct retrieval failed %s: %s", url, exc)

    # Exa fallback for blocked/thin pages.
    try:
        result_set = get_exa().get_contents(
            [url],
            text={"max_characters": MAX_JOB_CONTENT_CHARS},
            max_age_hours=24,
        )
        if getattr(result_set, "results", None):
            result = result_set.results[0]
            text = safe_text(getattr(result, "text", ""))
            if text:
                image = safe_text(getattr(result, "image", ""))
                return {
                    "text": text[:MAX_JOB_CONTENT_CHARS],
                    "html": "",
                    "final_url": safe_text(getattr(result, "url", "")) or url,
                    "apply_url": "",
                    "image_candidates": [],
                    "backend": "exa_contents",
                }
    except Exception as exc:
        logger.warning("Exa contents fallback failed %s: %s", url, exc)
    return None


def research_job(item):
    # Always retrieve the actual source page first. Exa is only a fallback for
    # blocked/thin pages; it is never the primary Bdjobs discovery layer.
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
        "image_candidates": retrieved.get("image_candidates", []),
        "raw_text": retrieved["text"],
        "is_government": bool(item.get("is_government")),
    })
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
        return 20
    age_hours = max(0, (datetime.now(BD_TZ) - dt).total_seconds() / 3600)
    if age_hours <= 24:
        return 30
    if age_hours <= 72:
        return 25
    if age_hours <= 120:
        return 18
    if age_hours <= 168:
        return 12
    return 4


def deterministic_job_gate(job):
    if not job.get("title") or not job.get("company"):
        return False, "missing_identity"
    if is_noise_title(job["title"], job.get("source_url", "")):
        return False, "noise_title"
    if deadline_status(job) == "expired":
        return False, "expired"
    if not (is_domain_allowed(job.get("source_url", ""), BDJOBS_DOMAINS) or is_domain_allowed(job.get("source_url", ""), [DOHAJ_DOMAIN])):
        return False, "source_not_allowed"
    # Government jobs are a separate required coverage stream and are not restricted
    # by the private BBA/MBA gate.
    if job.get("is_government"):
        return True, "ok_government"

    target_score = bba_mba_candidate_score(job)
    job["bba_mba_target_score"] = target_score
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
            f"Education: {trim_source_text(job.get('education',''), 1200)}",
            f"Experience: {trim_source_text(job.get('experience',''), 700)}",
            f"Salary: {job.get('salary','')}",
            f"Vacancy: {job.get('vacancy','')}",
            f"Age: {job.get('age','')}",
            f"Posted: {job.get('posted_date','')}",
            f"Deadline: {job.get('deadline','')}",
            f"Application: {trim_source_text(job.get('application_method',''), 900)}",
            f"Selection process: {trim_source_text(job.get('selection_process',''), 900)}",
            f"Application period: {trim_source_text(job.get('application_period',''), 500)}",
            f"Workplace: {job.get('workplace','')}",
            f"Business relevance pre-score: {job.get('audience_pre_score',0)}",
            f"Source text evidence: {trim_source_text(job.get('raw_text',''), 2600)}",
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
            max_completion_tokens=3500,
        )
        return json.loads(safe_text(response.choices[0].message.content)).get("results", [])
    except Exception as exc:
        logger.error("CEREBRAS judge batch %d failed: %s", batch_no, exc)
        return []


def rank_jobs(jobs):
    """Judge private BBA/MBA-targeted jobs. Government jobs bypass this ranking stream."""
    if not jobs:
        return []
    pre_ranked = sorted(
        jobs,
        key=lambda j: (
            -j.get("bba_mba_target_score", j.get("audience_pre_score", 0)),
            -posted_freshness_score(j),
            1 if deadline_status(j) == "active" else 0,
        ),
    )[:int(os.environ.get("MAX_PRIVATE_JUDGE_CANDIDATES", "160"))]

    judged = []
    for offset in range(0, len(pre_ranked), 15):
        batch = pre_ranked[offset:offset + 15]
        logger.info("CEREBRAS JUDGE BATCH %d | %d jobs", offset // 15 + 1, len(batch))
        rows = judge_batch(batch, offset // 15 + 1)
        by_id = {i: job for i, job in enumerate(batch, start=1)}
        for row in rows:
            try:
                idx = int(row.get("id"))
            except Exception:
                continue
            if idx not in by_id:
                continue
            job = dict(by_id[idx])
            job.update({
                "judge_publish": bool(row.get("publish")),
                "judge_score": int(row.get("score", 0)),
                "bba_mba_fit": int(row.get("bba_mba_fit", 0)),
                "early_career_fit": int(row.get("early_career_fit", 0)),
                "role_fit": int(row.get("role_fit", 0)),
                "judge_reason": safe_text(row.get("reason")),
            })
            # Enforce the deterministic private audience gate even if the model makes a mistake.
            if job.get("bba_mba_target_score", 0) < 25:
                job["judge_publish"] = False
            judged.append(job)

    judged_keys = {j.get("canonical") for j in judged}
    for job in pre_ranked:
        if job.get("canonical") in judged_keys:
            continue
        fallback_score = min(72, 48 + int(job.get("bba_mba_target_score", 0)) // 2 + posted_freshness_score(job) // 6)
        if fallback_score >= 60:
            fallback = dict(job)
            fallback.update({
                "judge_publish": True,
                "judge_score": fallback_score,
                "bba_mba_fit": min(85, int(job.get("bba_mba_target_score", 0))),
                "early_career_fit": min(85, int(job.get("bba_mba_target_score", 0))),
                "role_fit": min(85, int(job.get("bba_mba_target_score", 0))),
                "judge_reason": "Deterministic fallback after incomplete AI judgment; source page was verified.",
            })
            judged.append(fallback)

    judged.sort(
        key=lambda j: (
            -j.get("judge_score", 0),
            -j.get("bba_mba_fit", 0),
            -j.get("early_career_fit", 0),
            -posted_freshness_score(j),
        )
    )
    return judged


# ============================================================
# STATE / DEDUP / SELECTION
# ============================================================

def save_job_to_queue(job):
    key = job["canonical"]
    existing = STATE["queue"].get(key, {})
    existing.update(job)
    existing.setdefault("status", "pending")
    existing.setdefault("first_seen", now_iso())
    existing["last_seen"] = now_iso()
    STATE["queue"][key] = existing


def candidate_already_posted(job):
    canonical = canonical_url(job.get("source_url", ""))
    if canonical in POSTED_URLS:
        return True
    event_id = job.get("event_id")
    event = STATE.get("events", {}).get(event_id, {})
    return event.get("status") == "published"


def build_unique_job_pool(jobs):
    unique = []
    for job in jobs:
        if candidate_already_posted(job):
            continue
        if any(likely_same_job(job, previous) for previous in unique):
            continue
        unique.append(job)
    return unique


def select_private_jobs_by_category(ranked):
    """Select eligible private jobs with a minimum target per represented category, then fill by rank."""
    eligible = [
        job for job in ranked
        if job.get("judge_publish")
        and job.get("judge_score", 0) >= 60
        and deadline_status(job) != "expired"
        and job.get("bba_mba_target_score", 0) >= 25
    ]
    selected, used = [], set()

    categories = {}
    for job in eligible:
        category = safe_text(job.get("category") or job.get("dohaj_category") or "Career")
        categories.setdefault(category, []).append(job)

    # First satisfy the 2-per-represented-category target where possible.
    for category in sorted(categories):
        for job in categories[category][:MIN_PRIVATE_POSTS_PER_CATEGORY]:
            key = job.get("canonical")
            if key and key not in used:
                selected.append(job)
                used.add(key)

    # Then publish every remaining eligible private job. There is no hard publication cap.
    for job in eligible:
        key = job.get("canonical")
        if key and key not in used:
            selected.append(job)
            used.add(key)
    return selected


def select_final_jobs(private_ranked, government_jobs):
    private_selected = select_private_jobs_by_category(private_ranked)
    gov_selected = []
    for job in sorted(
        government_jobs,
        key=lambda j: (j.get("posted_date", ""), j.get("deadline", ""), j.get("canonical", "")),
        reverse=True,
    ):
        if deadline_status(job) == "expired" or candidate_already_posted(job):
            continue
        gov_selected.append(job)

    # Government is deliberately first so those required posts are not displaced by private jobs.
    selected = gov_selected + private_selected

    # Ensure the total target of 5 whenever enough eligible private jobs exist, without
    # imposing a maximum. Any remaining ranked private jobs can fill the run.
    if len(selected) < MIN_STORIES_PER_RUN:
        used = {j.get("canonical") for j in selected}
        for job in private_ranked:
            key = job.get("canonical")
            if not key or key in used:
                continue
            if not job.get("judge_publish") or job.get("judge_score", 0) < 60:
                continue
            if deadline_status(job) == "expired":
                continue
            selected.append(job)
            used.add(key)
            if len(selected) >= MIN_STORIES_PER_RUN:
                break

    return selected


def store_selected_event(job, published=False, message_id=None):
    event_id = job.get("event_id") or job_event_key(job)
    STATE["events"][event_id] = {
        "event_id": event_id,
        "canonical_url": job.get("canonical", ""),
        "source_url": job.get("source_url", ""),
        "apply_url": job.get("apply_url", ""),
        "source": job.get("source", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "score": job.get("judge_score", 0),
        "status": "published" if published else "selected",
        "selected_at": now_iso(),
        "published_at": now_iso() if published else "",
        "message_id": message_id,
    }
    return event_id


# ============================================================
# IMAGE PIPELINE: COPIED FROM WORKING NEWSROOM ARCHITECTURE
# ============================================================

def _unique_image_urls(urls, base_url=""):
    seen, result = set(), []
    for value in urls:
        raw = safe_text(value)
        if not raw:
            continue
        absolute = urljoin(base_url or "", raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        key = absolute.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _jsonld_image_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for x in value:
            out.extend(_jsonld_image_values(x))
        return out
    if isinstance(value, dict):
        out = []
        for key in ("url", "contentUrl", "image"):
            if key in value:
                out.extend(_jsonld_image_values(value[key]))
        return out
    return []


def _image_candidate_is_source_context(tag):
    # Skip navigation/header/footer/sidebar assets. Accept article/main/figure media.
    for parent in [tag] + list(tag.parents):
        name = getattr(parent, "name", "")
        if name in {"header", "nav", "footer", "aside"}:
            return False
        attrs = " ".join([
            safe_text(parent.get("id")),
            " ".join(parent.get("class", [])) if isinstance(parent.get("class"), list) else safe_text(parent.get("class")),
        ]).lower()
        if any(x in attrs for x in ("logo", "avatar", "profile", "navbar", "sidebar", "footer", "header")):
            return False
    return True


def find_image_candidates(url, page_html=None, final_url=None, initial_url=""):
    """Find source-owned job photos only. No generated fallback and no arbitrary site-wide images."""
    candidates = []
    base_url = final_url or url
    try:
        if page_html is None:
            response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=20)
            if response.status_code >= 400:
                return []
            page_html = response.text
            base_url = response.url

        soup = BeautifulSoup(page_html, "html.parser")
        # Highest confidence: OpenGraph/Twitter/itemprop metadata from the source page.
        for attrs in (
            {"property": "og:image"}, {"property": "og:image:url"},
            {"name": "twitter:image"}, {"name": "twitter:image:src"}, {"itemprop": "image"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                content = safe_text(tag.get("content"))
                if content:
                    candidates.append(content)

        # JobPosting JSON-LD image only. Do not collect arbitrary JSON-LD site logos.
        for obj in _jsonld_objects(page_html):
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(safe_text(x).lower() == "jobposting" for x in types):
                candidates.extend(_jsonld_image_values(obj.get("image")))

        # Contextual article media. Avoid every <img> on the page.
        for tag in soup.select("article img, main img, figure img")[:30]:
            if not _image_candidate_is_source_context(tag):
                continue
            value = ""
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                value = safe_text(tag.get(attr))
                if value:
                    break
            if not value:
                continue
            alt = safe_text(tag.get("alt")).lower()
            classes = " ".join(tag.get("class", [])) if isinstance(tag.get("class"), list) else safe_text(tag.get("class"))
            hint = f"{alt} {classes}".lower()
            if any(x in hint for x in ("logo", "avatar", "profile", "icon", "favicon", "placeholder", "banner ad")):
                continue
            candidates.append(value)

        unique = _unique_image_urls(candidates, base_url)
        return [x for x in unique if not _image_url_looks_like_placeholder(x)]
    except Exception as exc:
        logger.warning("Image candidate extraction failed %s: %s", url, exc)
        return _unique_image_urls(candidates, base_url)




def _image_url_looks_like_placeholder(url):
    path = urlparse(safe_text(url)).path.lower()
    name = path.rsplit("/", 1)[-1]
    bad_terms = (
        "placeholder", "no-image", "no_image", "noimage", "default-image",
        "default_image", "defaultimage", "dummy", "blank", "spacer",
        "transparent", "favicon", "avatar", "profile-image", "profile_image",
        "site-logo", "site_logo", "sitelogo", "company-logo", "company_logo",
    )
    return any(term in path or term in name for term in bad_terms)


def is_usable_job_image(image):
    """Reject black/blank/placeholder-like media before a source photo can be uploaded."""
    try:
        if image is None:
            return False
        image = image.convert("RGB")
        if image.width < 300 or image.height < 160:
            return False
        sample = image.resize((96, 54), Image.Resampling.BILINEAR)
        gray = sample.convert("L")
        pixels = list(gray.get_flattened_data())
        if not pixels:
            return False

        stat = ImageStat.Stat(sample)
        means = stat.mean
        stds = stat.stddev
        overall_std = sum(stds) / 3.0
        near_black = sum(1 for px in pixels if px <= 18) / len(pixels)
        near_white = sum(1 for px in pixels if px >= 242) / len(pixels)

        # The reported black rectangle in V2 is rejected here even if the URL looked valid.
        if near_black >= 0.90 or near_white >= 0.97:
            return False
        if overall_std < 6.0:
            return False

        # Reject nearly monochrome images. Genuine photos generally have either luminance or color variation.
        channel_spread = max(means) - min(means)
        if channel_spread < 2.5 and overall_std < 10.0:
            return False
        return True
    except Exception:
        return False


def download_image(url, referer=""):
    if _image_url_looks_like_placeholder(url):
        return None
    try:
        response = session.get(url, headers={**HEADERS, "Referer": referer or url}, timeout=20)
        if response.status_code >= 400 or not response.content:
            return None
        image = Image.open(BytesIO(response.content))
        image.load()
        if not is_usable_job_image(image):
            logger.info("Rejected unusable/blank image: %s", url)
            return None
        return image.convert("RGB")
    except Exception:
        return None


def prepare_image(job, index):
    """Return only the original source photo. Never create a card, logo fallback, or watermark."""
    candidates = _unique_image_urls(job.get("image_candidates", []), job.get("source_url", ""))
    for image_url in candidates[:10]:
        try:
            image = download_image(image_url, referer=job.get("source_url", ""))
            if image is None:
                continue
            path = f"/tmp/career_news_{index}.jpg"
            # Keep the source composition intact. Only convert to JPEG for reliable multipart upload.
            image.save(path, "JPEG", quality=94, optimize=True)
            logger.info("SOURCE PHOTO SELECTED | %s", image_url)
            return path
        except Exception as exc:
            logger.warning("Image candidate failed %s: %s", image_url, exc)
    logger.info("NO SOURCE PHOTO | text-only post | %s", job.get("title", ""))
    return None


# ============================================================
# TELEGRAM HTTP LAYER: SAME RELIABLE RETRY PATTERN
# ============================================================

def telegram_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last = {"ok": False, "description": "Unknown error"}
    for attempt in range(1, 6):
        try:
            response = session.post(url, data=data or {}, files=files, timeout=90)
            result = response.json()
            if result.get("ok"):
                return result
            last = result
            if response.status_code == 429:
                retry_after = int(result.get("parameters", {}).get("retry_after", 5))
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


def send_bot_api_fallback(image_path, job, plain_text):
    text = plain_text[:3800] if len(plain_text) > 3800 else plain_text
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    return telegram_call("sendMessage", data=data)


def send_rich_text(blocks, job):
    rich_message = {
        "blocks": blocks,
        "skip_entity_detection": False,
    }
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "rich_message": json.dumps(rich_message, ensure_ascii=False),
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    return telegram_call("sendRichMessage", data=data)


def send_rich_photo(image_path, blocks, job):
    rich_message = {
        "blocks": blocks,
        "skip_entity_detection": False,
    }
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "rich_message": json.dumps(rich_message, ensure_ascii=False),
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    try:
        with open(image_path, "rb") as photo:
            return telegram_call("sendRichMessage", data=data, files={"photo": photo})
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


# ============================================================
# JOB POST RENDERING
# ============================================================

def display_value(value):
    return clean_generated_text(safe_text(value))


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


def job_snapshot_rows(job):
    """Return high-impact, one-line source-backed job facts. Experience is intentionally separate."""
    rows = []
    mapping = [
        ("Location", "location"),
        ("Education", "education"),
        ("Employment", "employment_type"),
        ("Workplace", "workplace"),
        ("Salary", "salary"),
        ("Vacancy", "vacancy"),
        ("Age", "age"),
        ("Application", "application_method"),
        ("Selection", "selection_process"),
        ("Deadline", "deadline"),
    ]
    for label, key in mapping:
        value = display_value(job.get(key))
        if not value:
            continue
        if value.lower() in {"--", "n/a", "na", "not available", "not specified", "none", "null"}:
            continue
        # Table values are intentionally short enough to stay visually usable on phones.
        rows.append((label, trim_source_text(value, 90)))
    return rows


def _rich_bold(text):
    return {"type": "bold", "text": safe_text(text)}


def _rich_url(text, url):
    return {"type": "url", "text": safe_text(text), "url": safe_text(url)}


def rich_message_blocks(job, include_photo=False):
    """Build Telegram's native Rich Message blocks, including a real bordered table."""
    blocks = []
    if include_photo:
        blocks.append({
            "type": "photo",
            "photo": {"type": "photo", "media": "attach://photo"},
        })

    blocks.extend([
        {"type": "heading", "size": 1, "text": "📣 " + display_value(job.get("title"))},
        {"type": "paragraph", "text": _rich_bold("🏢 " + display_value(job.get("company")))},
        {"type": "heading", "size": 2, "text": "JOB SNAPSHOT"},
    ])

    cells = [
        [
            {"text": "FIELD", "is_header": True, "align": "center", "valign": "middle"},
            {"text": "DETAILS", "is_header": True, "align": "center", "valign": "middle"},
        ]
    ]
    for label, value in job_snapshot_rows(job):
        icon = _field_icon(label)
        cells.append([
            {"text": _rich_bold(icon + " " + label), "align": "left", "valign": "middle"},
            {"text": value, "align": "left", "valign": "middle"},
        ])

    blocks.append({
        "type": "table",
        "cells": cells,
        "is_bordered": True,
        "is_striped": False,
        "is_compact": True,
    })

    experience = display_value(job.get("experience"))
    if experience:
        blocks.extend([
            {"type": "heading", "size": 3, "text": "EXPERIENCE"},
            {"type": "paragraph", "text": "🧑‍💼 " + experience},
        ])

    tags = " ".join(job_hashtags(job))
    if tags:
        blocks.append({"type": "paragraph", "text": tags})

    source = safe_text(job.get("source", "Source"))
    source_url = safe_text(job.get("source_url"))
    footer_text = ["Source: "]
    if source_url:
        footer_text.append(_rich_url(source, source_url))
    else:
        footer_text.append(source)
    blocks.append({"type": "footer", "text": footer_text})
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


def dynamic_rich_html(job, include_photo=False):
    """Legacy HTML preview retained for tests/debugging. Sending uses native blocks in V3."""
    lines = [
        html.escape("📣 " + display_value(job.get("title")), quote=False),
        html.escape("🏢 " + display_value(job.get("company")), quote=False),
        "JOB SNAPSHOT",
    ]
    for label, value in job_snapshot_rows(job):
        lines.append(f"{_field_icon(label)} {label}: {value}")
    if job.get("experience"):
        lines.extend(["EXPERIENCE", "🧑‍💼 " + display_value(job.get("experience"))])
    lines.append(" ".join(job_hashtags(job)))
    lines.append(f"Source: {html.escape(job.get('source','Source'), quote=False)}")
    if include_photo:
        lines.insert(0, "[PHOTO]")
    return "\n".join(lines)


def plain_job_text(job):
    lines = ["📣 " + display_value(job.get("title")), "🏢 " + display_value(job.get("company")), "", "JOB SNAPSHOT"]
    for label, value in job_snapshot_rows(job):
        lines.append(f"{_field_icon(label)} {label}: {value}")
    experience = display_value(job.get("experience"))
    if experience:
        lines.extend(["", "EXPERIENCE", "🧑‍💼 " + experience])
    tags = " ".join(job_hashtags(job))
    if tags:
        lines.extend(["", tags])
    lines.append(f"Source: {job.get('source','Source')}")
    return "\n".join(lines)


def rich_visible_length(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def fit_rich_blocks(job, include_photo=False):
    blocks = rich_message_blocks(job, include_photo=include_photo)
    if rich_blocks_visible_length(blocks) <= MAX_RICH_CHARACTERS:
        return blocks
    # Values are already compact, but trim the few fields that can contain long labels.
    candidate = dict(job)
    for key, limit in (("title", 180), ("company", 120), ("experience", 70), ("location", 70), ("salary", 70)):
        if candidate.get(key):
            candidate[key] = trim_source_text(candidate[key], limit)
    return rich_message_blocks(candidate, include_photo=include_photo)


# ============================================================
# MAIN
# ============================================================

def run():
    logger.info("CAREERNEWSROOM V3")
    logger.info(
        "Channel=%s | Sources=Bdjobs+Dohaj | Minimum=%d | Gov minimum=%d | No hard post maximum",
        TELEGRAM_CHANNEL, MIN_STORIES_PER_RUN, MIN_GOVERNMENT_POSTS_PER_RUN,
    )
    logger.info(
        "Dohaj private pages/section=%d | gov pages/run=%d | gov lookback=%dd",
        DOHAJ_PRIVATE_PAGES_PER_SECTION, DOHAJ_GOVERNMENT_PAGES_PER_RUN, GOVERNMENT_LOOKBACK_DAYS,
    )
    prune_state()

    discovered = discover_all()
    verified = []
    rejected = 0

    for item in discovered:
        # Skip old queued URLs already published; source discovery itself remains source-authoritative.
        researched = research_job(item)
        if not researched:
            rejected += 1
            logger.info("DROP retrieval: %s | %s", item.get("source", ""), item.get("title", ""))
            continue
        researched["source_url"] = item["url"]
        researched["canonical"] = item["canonical"]
        researched["is_government"] = bool(item.get("is_government"))
        ok, reason = deterministic_job_gate(researched)
        if not ok:
            rejected += 1
            logger.info("DROP gate: %s | %s | %s", reason, item.get("source", ""), item.get("title", ""))
            continue
        save_job_to_queue(researched)
        if not candidate_already_posted(researched):
            verified.append(researched)

    save_state(STATE)
    unique = build_unique_job_pool(verified)

    # Government coverage is independent of private-job ranking. Any eligible, unposted
    # government job discovered from the Dohaj gov section can be published in this run.
    government_jobs = [
        job for job in unique
        if job.get("is_government") and deadline_status(job) != "expired"
    ]

    # Carry forward unpublished active government jobs from the queue so a transient retrieval/API
    # failure on one run does not silently violate the government coverage requirement.
    queued_gov = []
    for queued in STATE.get("queue", {}).values():
        if not queued.get("is_government"):
            continue
        if queued.get("status") not in {"pending", "selected"}:
            continue
        if candidate_already_posted(queued) or deadline_status(queued) == "expired":
            continue
        # Recompute the target-independent identity/event fields for old V2 state safely.
        queued = dict(queued)
        queued.setdefault("source", "Dohaj")
        queued.setdefault("source_url", queued.get("url", ""))
        queued.setdefault("canonical", canonical_url(queued.get("source_url", "")))
        queued.setdefault("event_id", job_event_key(queued))
        queued_gov.append(queued)

    government_jobs = build_unique_job_pool(government_jobs + queued_gov)

    private_jobs = [job for job in unique if not job.get("is_government")]
    ranked_private = rank_jobs(private_jobs)

    logger.info(
        "VERIFIED=%d | REJECTED=%d | GOV=%d | PRIVATE=%d | PRIVATE_JUDGED=%d",
        len(verified), rejected, len(government_jobs), len(private_jobs), len(ranked_private),
    )

    selected = select_final_jobs(ranked_private, government_jobs)

    # If fewer than the minimum total are available from current discovery, use still-active
    # queued private jobs that were already judged as publishable. This does not introduce a
    # publication ceiling and never bypasses the BBA/MBA gate.
    if len(selected) < MIN_STORIES_PER_RUN:
        used = {j.get("canonical") for j in selected}
        fallback_private = []
        for queued in STATE.get("queue", {}).values():
            if queued.get("is_government") or queued.get("status") not in {"pending", "selected"}:
                continue
            if candidate_already_posted(queued) or deadline_status(queued) == "expired":
                continue
            target_score = queued.get("bba_mba_target_score", bba_mba_candidate_score(queued))
            if target_score < 25 or queued.get("judge_score", 0) < 60:
                continue
            fallback = dict(queued)
            fallback["bba_mba_target_score"] = target_score
            fallback_private.append(fallback)
        fallback_private.sort(key=lambda j: (-j.get("judge_score", 0), -posted_freshness_score(j)))
        for job in fallback_private:
            key = job.get("canonical")
            if not key or key in used:
                continue
            selected.append(job)
            used.add(key)
            if len(selected) >= MIN_STORIES_PER_RUN:
                break

    # Explicitly enforce the minimum government count when three or more eligible gov jobs exist.
    # This is normally already satisfied because all eligible government jobs are selected.
    gov_selected_count = sum(1 for j in selected if j.get("is_government"))
    logger.info(
        "FINAL SELECTED=%d | government=%d | private=%d | minimum_total=%d | minimum_gov=%d",
        len(selected), gov_selected_count, len(selected) - gov_selected_count,
        MIN_STORIES_PER_RUN, MIN_GOVERNMENT_POSTS_PER_RUN,
    )

    published_count = 0
    for index, job in enumerate(selected, start=1):
        blocks = fit_rich_blocks(job)
        if rich_blocks_visible_length(blocks) > MAX_RICH_CHARACTERS:
            logger.error("DROP render length: %s", job.get("title", ""))
            continue
        store_selected_event(job, published=False)

        image_path = prepare_image(job, index)
        if image_path:
            result = send_rich_photo(image_path, fit_rich_blocks(job, include_photo=True), job)
        else:
            # No source photo = no photo block at all.
            result = send_rich_text(blocks, job)

        if not result.get("ok"):
            logger.warning("Rich Message failed; Bot API text fallback: %s", result.get("description"))
            result = send_bot_api_fallback(None, job, plain_job_text(job))

        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            POSTED_URLS.add(canonical_url(job["source_url"]))
            save_posted_url(canonical_url(job["source_url"]))
            item = STATE["queue"].get(job["canonical"])
            if item:
                item.update({
                    "status": "posted",
                    "posted_at": now_iso(),
                    "judge_score": job.get("judge_score", 0),
                    "apply_url": job.get("apply_url", ""),
                })
            store_selected_event(job, published=True, message_id=message_id)
            STATE["recent_titles"].append(normalize_title(job["title"]))
            logger.info("PUBLISHED %d | %s | %s | photo=%s", published_count, job.get("source"), job.get("title"), bool(image_path))
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)

    STATE["last_run"] = now_iso()
    save_state(STATE)
    logger.info("Finished. Published=%d | no hard maximum", published_count)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    fixture = """
    <html><head>
    <meta property="og:image" content="/images/job.jpg">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee",
     "datePosted":"2026-09-17","validThrough":"2026-10-17",
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
    <p>Application Deadline: 2026-10-17</p>
    <a href="https://careers.examplebank.com/jobs/123">Apply Online</a>
    </body></html>
    """
    fake_item = {
        "title": "Management Trainee",
        "url": "https://dohaj.com/job-details/example-123",
        "canonical": canonical_url("https://dohaj.com/job-details/example-123"),
        "source": "Dohaj",
        "discovery": "self_test",
    }
    source_text = _text_from_html(fixture)
    fields = extract_job_fields(source_text, fixture, fake_item["url"], fake_item)
    apply_url = extract_apply_url(fixture, fake_item["url"], "Dohaj")
    fields.update({
        "apply_url": apply_url,
        "canonical": fake_item["canonical"],
        "audience_pre_score": job_family_score(fields["title"], source_text),
        "raw_text": source_text,
        "is_government": False,
    })

    assert fields["title"] == "Management Trainee"
    assert fields["company"] == "Example Bank"
    assert fields["vacancy"] == "10"
    assert fields["education"] == "BBA/MBA"
    assert fields["experience"] == "Freshers"
    assert fields["salary"].startswith("Tk")
    assert fields["deadline"] == "2026-10-17"
    assert fields["employment_type"] == "Full Time"
    assert fields["workplace"] == "On-site"
    assert fields["age"] == "18 to 30 years"
    assert fields["application_method"] == "Online"
    assert fields["selection_process"] == "Written + Viva"
    assert apply_url == "https://careers.examplebank.com/jobs/123"
    ok, _ = deterministic_job_gate(fields)
    assert ok

    rows = job_snapshot_rows(fields)
    assert "Experience" not in [label for label, _ in rows]
    assert all("\n" not in value for _, value in rows)

    blocks = rich_message_blocks(fields)
    table = next(block for block in blocks if block.get("type") == "table")
    assert table["is_bordered"] is True
    assert table["is_compact"] is True
    assert len(table["cells"]) >= 2
    assert not any("Experience" in str(cell) for row in table["cells"] for cell in row)
    assert any(block.get("type") == "heading" and block.get("text") == "EXPERIENCE" for block in blocks)
    assert not any(block.get("type") == "photo" for block in blocks)
    photo_blocks = rich_message_blocks(fields, include_photo=True)
    assert any(block.get("type") == "photo" for block in photo_blocks)

    missing = dict(fields)
    missing["education"] = ""
    missing["selection_process"] = ""
    missing["application_method"] = ""
    missing_blocks = rich_message_blocks(missing)
    missing_table = next(block for block in missing_blocks if block.get("type") == "table")
    flat_cells = str(missing_table["cells"])
    assert "Education" not in flat_cells
    assert "Selection" not in flat_cells
    assert "Application" not in flat_cells

    # Regression test for the V2 extraction bug: a bare Experience field followed by
    # Published must remain empty rather than inheriting the Published value.
    broken_fixture = "Experience:\nPublished:\n17 Sep 2026\nEducation:\nBBA"
    assert _label_value(broken_fixture, ["Experience"]) == ""
    assert compact_experience(_label_value("Experience:\nPublished:\n17 Sep 2026", ["Experience"])) == ""

    # Image regression: black/white/flat source images are not publishable.
    black = Image.new("RGB", (1200, 675), (0, 0, 0))
    white = Image.new("RGB", (1200, 675), (255, 255, 255))
    flat = Image.new("RGB", (1200, 675), (90, 90, 90))
    real = Image.new("RGB", (1200, 675))
    draw = ImageDraw.Draw(real)
    draw.rectangle((0, 0, 600, 675), fill=(20, 80, 160))
    draw.rectangle((600, 0, 1200, 675), fill=(220, 180, 60))
    assert not is_usable_job_image(black)
    assert not is_usable_job_image(white)
    assert not is_usable_job_image(flat)
    assert is_usable_job_image(real)
    assert _image_url_looks_like_placeholder("https://example.com/images/placeholder-job.jpg")
    assert _image_url_looks_like_placeholder("https://example.com/favicon.ico")

    # Source path regression for government Dohaj pages.
    assert is_dohaj_job_url("https://dohaj.com/gov-job/ministry-of-home-affairs-job-circular-2026")
    assert is_vacancy_url("https://dohaj.com/gov-job/ministry-of-home-affairs-job-circular-2026")
    assert dohaj_page_url("https://dohaj.com/gov-jobs", 2) == "https://dohaj.com/gov-jobs?page=2"
    assert dohaj_page_url("https://dohaj.com/category/marketing-sales", 3) == "https://dohaj.com/category/marketing-sales?page=3"

    # Source-universe contract.
    assert all(is_domain_allowed(url, [DOHAJ_DOMAIN]) for url in DOHAJ_TOP5_URLS)
    assert all(is_domain_allowed(url, BDJOBS_DOMAINS) for url in [
        "https://www.bdjobs.com/job/123",
        "https://hotjobs.bdjobs.com/jobs/example/example1.htm",
    ])
    assert not is_domain_allowed("https://example.com/job/123", BDJOBS_DOMAINS)

    markup = _button_markup(fields)
    assert markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    assert markup["inline_keyboard"][0][0]["url"] == apply_url

    # Category selection regression: 2-per-category target, then all other eligible jobs.
    base = dict(fields)
    base.update({"judge_publish": True, "judge_score": 80, "bba_mba_target_score": 70})
    test_jobs = []
    for category in ("Finance", "Marketing"):
        for i in range(3):
            test_jobs.append({**base, "category": category, "canonical": f"{category.lower()}-{i}", "event_id": f"{category.lower()}-{i}", "title": f"{category} Executive {i}"})
    selected = select_private_jobs_by_category(test_jobs)
    assert len(selected) == 6
    assert sum(1 for j in selected if j["category"] == "Finance") == 3
    assert sum(1 for j in selected if j["category"] == "Marketing") == 3

    logger.info("CareerNewsroom V3 self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        run()
