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
from urllib.parse import urlparse, urljoin, quote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO
from collections import Counter, defaultdict
from threading import Lock
from typing import Any, Dict, List, Tuple

import requests
import feedparser
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFile
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
NEWS_MODE = (os.environ.get("NEWS_MODE") or "update").strip().lower()
VERSION_NAME = "CareerNewsroom"

VALID_NEWS_MODES = {"update"}
if NEWS_MODE not in VALID_NEWS_MODES:
    raise ValueError(f"Invalid NEWS_MODE={NEWS_MODE!r}; expected one of {sorted(VALID_NEWS_MODES)}")

CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")

POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# CareerNewsroom V1: high-recall discovery, low-token ranking, fact-locked publishing.
MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 15
RANKING_POOL_SIZE = 45
MAX_PROCESS_CANDIDATES = 45
PRIMARY_PROCESS_CANDIDATES = 30
MAX_POSTS_PER_SOURCE_PER_RUN = 3
AI_RANK_INPUT_LIMIT = 30
AI_RANK_BATCH_SIZE = 30
AI_RANK_MAX_BATCHES = 1
MAX_AI_RECORD_FALLBACKS = 4
MAX_EXA_CONTENT_FALLBACKS = 10
ENABLE_AI_CLAIM_VERIFY = False
DISCOVERY_LOOKBACK_HOURS = 72
DEADLINE_SOFT_TARGET_DAYS = 7
DEADLINE_URGENT_DAYS = 3
MIN_PUBLISH_SCORE = 62

POST_DELAY_SECONDS = 1.0
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 5
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES = 240
MAX_EXA_CANDIDATES = 60
MAX_GOOGLE_NEWS_CANDIDATES = 40
MAX_EXCERPT_ENRICH = 12
THIN_EXCERPT_CHARS = 150
MAX_RICH_CHARACTERS = 32768
MAX_SOURCE_PER_RUN = 99
SOURCE_DIVERSITY_TARGET = 5

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "from", "with", "by", "at", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "this", "that", "these", "those", "it", "its", "their", "they",
    "them", "he", "she", "his", "her", "we", "our", "you", "your",
    "new", "after", "before", "over", "into", "than", "about", "from",
}

# RSS-first, Bangladesh job-oriented source universe.
RSS_FEEDS = [
    {"name": "ProjobsBD", "region": "Career", "url": "https://projobsbd.com/feed/"},
    {"name": "ProjobsBD Running Jobs", "region": "Career", "url": "https://projobsbd.com/category/running-job-circular/feed/"},
    {"name": "BD Govt Jobs", "region": "Career", "url": "https://bdgovtjobs.com/feed/"},
    {"name": "JobPagol", "region": "Career", "url": "https://jobpagol.com/feed/"},
    {"name": "Bangladesh Pratidin Jobs", "region": "Career", "url": "https://bdpratidin.net/rss/category/job"},
    {"name": "JagoNews24", "region": "Career", "url": "https://www.jagonews24.com/rss/rss.xml"},
    {"name": "Bangla Tribune", "region": "Career", "url": "https://www.banglatribune.com/feed"},
    {"name": "BD24Live", "region": "Career", "url": "https://www.bd24live.com/bangla/feed"},
    {"name": "RisingBD", "region": "Career", "url": "https://risingbd.com/rss/rss.xml"},
    {"name": "Bangladesh Journal", "region": "Career", "url": "https://www.bd-journal.com/feed/latest-rss.xml"},
    {"name": "Prothom Alo", "region": "Career", "url": "https://www.prothomalo.com/feed/"},
    {"name": "Jugantor", "region": "Career", "url": "https://www.jugantor.com/feed/rss.xml"},
    {"name": "Kaler Kantho", "region": "Career", "url": "https://www.kalerkantho.com/rss.xml"},
    {"name": "The Daily Star", "region": "Career", "url": "https://www.thedailystar.net/jobs/rss.xml"},
    {"name": "The Daily Ittefaq", "region": "Career", "url": "https://www.ittefaq.com.bd/rss.xml"},
]

CAREER_JOB_PORTAL_DOMAINS = [
    "jobs.bdjobs.com", "bdjobs.com", "bdjobslive.com", "dohaj.com", "job.com.bd",
    "smartjob.portal.gov.bd", "alljobs.teletalk.com.bd", "jobs.teletalk.com.bd",
    "bpsc.gov.bd", "erecruitment.bcc.gov.bd", "jobsnoticebd.com", "jobsinfo.bd",
    "jobfeeds.online", "circularbd.com", "bangladesherkhabor.net",
    "dhakapost.com", "dhakatribune.com", "banglatribune.com", "jagonews24.com",
    "prothomalo.com", "risingbd.com", "bd24live.com", "bd-journal.com",
    "jugantor.com", "kalerkantho.com", "thedailystar.net", "ittefaq.com.bd",
    "projobsbd.com", "bdgovtjobs.com", "jobpagol.com", "bdpratidin.net",
]

CAREER_NEWS_DOMAINS = [
    "jagonews24.com", "banglatribune.com", "bd24live.com", "risingbd.com",
    "bd-journal.com", "prothomalo.com", "jugantor.com", "kalerkantho.com",
    "thedailystar.net", "ittefaq.com.bd", "dhakapost.com", "dhakatribune.com",
    "bdpratidin.net",
]

PRIMARY_CAREER_DOMAINS = sorted(set(CAREER_JOB_PORTAL_DOMAINS + CAREER_NEWS_DOMAINS))
FALLBACK_CAREER_DOMAINS = []
ALL_PRIMARY_DOMAINS = PRIMARY_CAREER_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_CAREER_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS

JOB_SIGNAL_RE = re.compile(
    r"(job|jobs|vacancy|vacancies|career|careers|recruit|recruitment|hiring|apply|internship|trainee|management trainee|circular|নিয়োগ|চাকরি|শূন্যপদ|আবেদন|নিয়োগ বিজ্ঞপ্তি|চাকরির বিজ্ঞপ্তি)",
    re.I,
)
BAD_JOB_RE = re.compile(
    r"(job tips|career advice|how to prepare|exam result|admission|scholarship|job fair|training course|workshop|seminar|career guide|salary guide|interview tips|cv tips|resume tips)",
    re.I,
)
BANGLADESH_RE = re.compile(
    r"(bangladesh|bangladeshi|বাংলাদেশ|ঢাকা|চট্টগ্রাম|চট্টগ্রাম|রাজশাহী|খুলনা|বরিশাল|সিলেট|রংপুর|ময়মনসিংহ|ময়মনসিংহ|নারায়ণগঞ্জ|গাজীপুর|কুমিল্লা|সাভার|anywhere in bangladesh)",
    re.I,
)
OVERSEAS_RE = re.compile(
    r"(india|indian|pakistan|pakistani|uae|dubai|abu dhabi|saudi arabia|qatar|kuwait|oman|bahrain|singapore|malaysia|japan|south korea|australia|canada|united kingdom|uk|usa|united states|europe|germany|france|italy|overseas|abroad|বিদেশে|বিদেশী)",
    re.I,
)

EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
BANGLA_MONTHS = {
    "জানুয়ারি": 1, "জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারি": 2, "মার্চ": 3,
    "এপ্রিল": 4, "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "সেপ্টেম্বর": 9,
    "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12,
}

DIRECT_JOB_SOURCES = [
    {"name": "Bdjobs", "url": "https://jobs.bdjobs.com/jobsearch-cache.asp", "type": "job_portal"},
    {"name": "BDJobs Live", "url": "https://bdjobslive.com/bdjobs-live/live", "type": "job_portal"},
    {"name": "Dohaj", "url": "https://dohaj.com/jobs", "type": "job_portal"},
    {"name": "Job.com.bd", "url": "https://job.com.bd/jobs/new_jobs/", "type": "job_portal"},
    {"name": "Smart Job", "url": "https://smartjob.portal.gov.bd/", "type": "official_job_portal"},
    {"name": "Alljobs Teletalk", "url": "https://alljobs.teletalk.com.bd/", "type": "official_job_portal"},
    {"name": "BPSC", "url": "https://bpsc.gov.bd/", "type": "official_job_portal"},
    {"name": "BCC e-Recruitment", "url": "https://erecruitment.bcc.gov.bd/", "type": "official_job_portal"},
    {"name": "JobsNoticeBD", "url": "https://jobsnoticebd.com/", "type": "job_portal"},
    {"name": "JobsInfo", "url": "https://jobsinfo.bd/", "type": "job_portal"},
    {"name": "JobFeeds", "url": "https://jobfeeds.online/", "type": "job_portal"},
    {"name": "CircularBD", "url": "https://www.circularbd.com/alljobs", "type": "job_portal"},
    {"name": "Bangladesher Khabor", "url": "https://www.bangladesherkhabor.net/jobs", "type": "news_jobs"},
    {"name": "Dhaka Post", "url": "https://www.dhakapost.com/jobs-career/", "type": "news_jobs"},
    {"name": "Dhaka Tribune", "url": "https://bangla.dhakatribune.com/jobs", "type": "news_jobs"},
    {"name": "Bangla Tribune", "url": "https://www.banglatribune.com/jobs", "type": "news_jobs"},
    {"name": "JagoNews24", "url": "https://www.jagonews24.com/topic/%E0%A6%9A%E0%A6%BE%E0%A6%95%E0%A6%B0%E0%A6%BF", "type": "news_jobs"},
    {"name": "Prothom Alo", "url": "https://www.prothomalo.com/collection/chakri-all", "type": "news_jobs"},
]

# ============================================================
# TAXONOMY: CAREER NEWS
# ============================================================

TOPICS = {
    "Career": [
        "Government Jobs",
        "Corporate Jobs",
        "Bank Jobs",
        "NGO Jobs",
        "Education Jobs",
        "Engineering Jobs",
        "IT Jobs",
        "Sales and Marketing Jobs",
        "Finance and Accounting Jobs",
        "HR Jobs",
        "Healthcare Jobs",
        "Internships",
        "Management Trainee",
        "Graduate Jobs",
        "Remote Jobs",
        "Entry-Level Jobs",
        "Experienced Jobs",
        "Recruitment Notices",
    ]
}

INSTITUTIONS = []
SOURCE_NAMES = {
    "jobs.bdjobs.com": "Bdjobs",
    "bdjobs.com": "Bdjobs",
    "bdjobslive.com": "BDJobs Live",
    "dohaj.com": "Dohaj",
    "job.com.bd": "Job.com.bd",
    "smartjob.portal.gov.bd": "Smart Job",
    "alljobs.teletalk.com.bd": "Alljobs Teletalk",
    "jobs.teletalk.com.bd": "Alljobs Teletalk",
    "bpsc.gov.bd": "BPSC",
    "erecruitment.bcc.gov.bd": "BCC e-Recruitment",
    "jobsnoticebd.com": "JobsNoticeBD",
    "jobsinfo.bd": "JobsInfo",
    "jobfeeds.online": "JobFeeds",
    "circularbd.com": "CircularBD",
    "bangladesherkhabor.net": "Bangladesher Khabor",
    "dhakapost.com": "Dhaka Post",
    "dhakatribune.com": "Dhaka Tribune",
    "banglatribune.com": "Bangla Tribune",
    "jagonews24.com": "JagoNews24",
    "prothomalo.com": "Prothom Alo",
    "risingbd.com": "RisingBD",
    "bd24live.com": "BD24Live",
    "bd-journal.com": "Bangladesh Journal",
    "jugantor.com": "Jugantor",
    "kalerkantho.com": "Kaler Kantho",
    "thedailystar.net": "The Daily Star",
    "ittefaq.com.bd": "The Daily Ittefaq",
    "projobsbd.com": "ProjobsBD",
    "bdgovtjobs.com": "BD Govt Jobs",
    "jobpagol.com": "JobPagol",
    "bdpratidin.net": "Bangladesh Pratidin",
}

CATEGORY_HASHTAGS = {
    "Government Jobs": ["#GovernmentJob", "#BangladeshJob"],
    "Corporate Jobs": ["#PrivateJob", "#BangladeshJob"],
    "Bank Jobs": ["#BankJob", "#BangladeshJob"],
    "NGO Jobs": ["#NGOJob", "#BangladeshJob"],
    "Education Jobs": ["#EducationJob", "#BangladeshJob"],
    "Engineering Jobs": ["#EngineeringJob", "#BangladeshJob"],
    "IT Jobs": ["#ITJob", "#BangladeshJob"],
    "Sales and Marketing Jobs": ["#SalesJob", "#MarketingJob"],
    "Finance and Accounting Jobs": ["#FinanceJob", "#AccountingJob"],
    "HR Jobs": ["#HRJob", "#BangladeshJob"],
    "Healthcare Jobs": ["#HealthcareJob", "#BangladeshJob"],
    "Internships": ["#Internship", "#BangladeshJob"],
    "Management Trainee": ["#ManagementTrainee", "#BangladeshJob"],
    "Graduate Jobs": ["#GraduateJob", "#BangladeshJob"],
    "Remote Jobs": ["#RemoteJob", "#BangladeshJob"],
    "Entry-Level Jobs": ["#EntryLevelJob", "#BangladeshJob"],
    "Experienced Jobs": ["#ExperiencedJob", "#BangladeshJob"],
    "Recruitment Notices": ["#JobCircular", "#BangladeshJob"],
}

CATEGORY_GROUPS = {
    "Government": {"Government Jobs", "Recruitment Notices"},
    "Corporate": {"Corporate Jobs", "Sales and Marketing Jobs", "HR Jobs"},
    "Finance": {"Bank Jobs", "Finance and Accounting Jobs"},
    "Development": {"NGO Jobs"},
    "Professional": {"Engineering Jobs", "IT Jobs", "Healthcare Jobs", "Education Jobs"},
    "Graduate": {"Internships", "Management Trainee", "Graduate Jobs", "Entry-Level Jobs"},
    "Experience": {"Experienced Jobs", "Remote Jobs"},
}

TOPIC_ALIASES = {
    "govt": "Government Jobs", "government": "Government Jobs", "government job": "Government Jobs",
    "bank": "Bank Jobs", "banking": "Bank Jobs", "private": "Corporate Jobs", "corporate": "Corporate Jobs",
    "ngo": "NGO Jobs", "education": "Education Jobs", "teacher": "Education Jobs",
    "engineering": "Engineering Jobs", "it": "IT Jobs", "technology": "IT Jobs",
    "sales": "Sales and Marketing Jobs", "marketing": "Sales and Marketing Jobs",
    "finance": "Finance and Accounting Jobs", "accounting": "Finance and Accounting Jobs",
    "hr": "HR Jobs", "internship": "Internships", "management trainee": "Management Trainee",
    "graduate": "Graduate Jobs", "remote": "Remote Jobs", "healthcare": "Healthcare Jobs",
    "entry level": "Entry-Level Jobs", "experienced": "Experienced Jobs", "recruitment notice": "Recruitment Notices",
}


def canonical_topic(topic, region="Career"):
    raw = safe_text(topic).strip().lower()
    if raw in TOPIC_ALIASES:
        return TOPIC_ALIASES[raw]
    for topic_name in TOPICS["Career"]:
        if topic_name.lower() == raw:
            return topic_name
    patterns = [
        (("government", "govt", "ministry", "bpsc", "teletalk", "সরকার"), "Government Jobs"),
        (("bank", "banking", "ব্যাংক"), "Bank Jobs"),
        (("ngo", "development sector", "non-government"), "NGO Jobs"),
        (("teacher", "school", "college", "university", "education"), "Education Jobs"),
        (("engineer", "engineering"), "Engineering Jobs"),
        (("developer", "software", "it ", "information technology", "technology"), "IT Jobs"),
        (("sales", "marketing", "business development"), "Sales and Marketing Jobs"),
        (("finance", "accounting", "accounts", "audit"), "Finance and Accounting Jobs"),
        (("hr", "human resources", "recruiter", "talent acquisition"), "HR Jobs"),
        (("doctor", "nurse", "medical", "pharma", "healthcare"), "Healthcare Jobs"),
        (("intern", "internship"), "Internships"),
        (("management trainee", "mt"), "Management Trainee"),
        (("graduate", "fresher", "entry level"), "Graduate Jobs"),
        (("remote", "work from home"), "Remote Jobs"),
        (("experienced", "senior", "lead", "manager"), "Experienced Jobs"),
        (("job circular", "recruitment notice", "নিয়োগ বিজ্ঞপ্তি"), "Recruitment Notices"),
    ]
    for needles, canonical in patterns:
        if any(needle in raw for needle in needles):
            return canonical
    return "Corporate Jobs"



def category_hashtags(story):
    tags = []
    topic = safe_text(story.get("topic"))
    for tag in CATEGORY_HASHTAGS.get(topic, []):
        if tag not in tags:
            tags.append(tag)
    if "#CareerNewsroom" not in tags:
        tags.append("#CareerNewsroom")
    if "#BangladeshJob" not in tags:
        tags.append("#BangladeshJob")
    return tags[:4]


def coverage_state():
    return STATE.setdefault("category_coverage", {})


def update_category_coverage(story):
    topic = safe_text(story.get("topic"))
    if topic:
        coverage_state()[topic] = now_iso()


def refresh_category_coverage():
    coverage = coverage_state()
    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or published_at.date() != NOW_BD.date():
            continue
        topic = safe_text(event.get("topic"))
        if topic:
            coverage[topic] = event.get("selected_at", published_at.isoformat())


# ============================================================
# LOGGING + HTTP
# ============================================================

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

# Discovery traffic must fail fast. 403/404/429 are source-health signals, not
# reasons to spend minutes retrying the same source. Telegram uses its own retry
# path inside telegram_call().
retry_policy = Retry(
    total=1,
    connect=1,
    read=1,
    backoff_factor=0.2,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=False,
)

adapter = HTTPAdapter(
    max_retries=retry_policy,
    pool_connections=30,
    pool_maxsize=30,
)

session.mount("https://", adapter)
session.mount("http://", adapter)


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

    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("amp.")
    )

    path = parsed.path or "/"
    path = path.rstrip("/")
    path = re.sub(r"/amp$", "", path, flags=re.I)
    path = re.sub(r"\.amp$", "", path, flags=re.I)

    return f"{host}{path}"


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    text = normalize_title(text)
    return {
        token
        for token in text.split()
        if len(token) >= 3
    }


def token_jaccard(a, b):
    aa = title_tokens(a)
    bb = title_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


def title_similarity(a, b):
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    sequence = SequenceMatcher(None, na, nb).ratio()
    jaccard = token_jaccard(na, nb)
    return max(sequence, jaccard)


def event_similarity(a, b):
    """Cheap event-level similarity without an embedding dependency."""
    sequence = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    jaccard = token_jaccard(a, b)
    return (0.55 * sequence) + (0.45 * jaccard)


def likely_same_event(a, b):
    return (
        title_similarity(a, b) >= 0.90
        or event_similarity(a, b) >= 0.80
    )


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def feed_entry_datetime(entry):
    for key in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(
                    *parsed[:6],
                    tzinfo=timezone.utc,
                ).astimezone(BD_TZ)
            except Exception:
                pass

    for key in (
        "published",
        "updated",
        "created",
    ):
        dt = parse_datetime(
            entry.get(key)
        )
        if dt:
            return dt

    return None


def trim_source_text(text, limit):
    """Trim source text before rendering. Never appends ellipses."""
    text = safe_text(text)
    if len(text) <= limit:
        return text

    trimmed = text[:limit].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]

    return trimmed.rstrip(" ,:;-/—")


def clean_generated_text(text):
    text = safe_text(text)

    # Prevent visible truncation artifacts.
    text = re.sub(r"\.{2,}", ".", text)
    text = text.replace("\u2026", "")

    # Remove incomplete endings.
    text = re.sub(
        r"\s*[,;:]\s*$",
        "",
        text,
    )
    text = re.sub(
        r"\s*[-—]\s*$",
        "",
        text,
    )

    return text.strip()


def complete_text(text):
    raw = safe_text(text)
    if not raw:
        return False

    # A text that clean_generated_text() would mutilate
    # (trailing dash/comma/colon) is INCOMPLETE.
    if re.search(r"[\s,;:\-—…]+$", raw):
        return False

    text = clean_generated_text(raw)
    if not text:
        return False

    return not text.endswith(
        (",", ";", ":", "-", "—", "…")
    )


def source_name(url):
    domain = urlparse(safe_text(url)).netloc.lower().removeprefix("www.")
    return SOURCE_NAMES.get(domain, domain or "Source")



def article_region(url):
    return "Career"



def now_iso():
    return datetime.now(
        BD_TZ
    ).isoformat()


# ============================================================
# STATE: QUEUE + EVENTS + KNOWLEDGE
# ============================================================

def default_state():
    return {
        "feeds": {},
        "queue": {},
        "events": {},
        "event_clusters": {},
        "posted_event_ids": [],
        "recent_titles": [],
        "source_health": {},
    }


def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(
            data,
            dict,
        ):
            return default_state()

        base = default_state()
        base.update(data)

        return base

    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp,
        STATE_FILE,
    )


def load_posted_urls():
    try:
        with open(
            POSTED_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return {
                canonical_url(line)
                for line in f
                if safe_text(line)
            }
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return

    with open(
        POSTED_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            canonical
            + "\n"
        )


STATE = load_state()
POSTED_URLS = load_posted_urls()


def prune_state():
    cutoff_queue = (
        datetime.now(BD_TZ)
        - timedelta(
            days=QUEUE_RETENTION_DAYS
        )
    )

    cutoff_events = (
        datetime.now(BD_TZ)
        - timedelta(
            days=EVENT_RETENTION_DAYS
        )
    )

    queue = STATE.get(
        "queue",
        {},
    )

    keep_queue = {}

    for key, item in queue.items():
        dt = parse_datetime(
            item.get("last_seen")
            or item.get("published_date")
        )

        if (
            dt
            and dt >= cutoff_queue
        ):
            keep_queue[key] = item

    STATE["queue"] = keep_queue

    events = STATE.get(
        "events",
        {},
    )

    keep_events = {}

    for key, event in events.items():
        dt = parse_datetime(
            event.get("published_at")
            or event.get("selected_at")
        )

        if (
            dt
            and dt >= cutoff_events
        ):
            keep_events[key] = event

    STATE["events"] = keep_events

    titles = STATE.get(
        "recent_titles",
        [],
    )

    STATE["recent_titles"] = titles[-400:]


# ============================================================
# TIME WINDOWS
# ============================================================

NOW_BD = datetime.now(
    BD_TZ
)

TODAY_START = NOW_BD.replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

YESTERDAY_START = (
    TODAY_START
    - timedelta(days=1)
)

DISCOVERY_START = (
    NOW_BD
    - timedelta(
        hours=ROLLING_DISCOVERY_HOURS
    )
)
DISCOVERY_END = (
    NOW_BD
    + timedelta(
        minutes=FUTURE_TOLERANCE_MINUTES
    )
)

DISCOVERY_TARGET_PER_REGION = 18


# ============================================================
# CLIENTS
# ============================================================

exa = None
cerebras = None
AI_RECORD_FALLBACKS_USED = 0
AI_RECORD_LOCK = Lock()
EXA_CONTENT_FALLBACKS_USED = 0
EXA_CONTENT_LOCK = Lock()


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
# CANDIDATE FILTERING
# ============================================================

JOB_DETAIL_PATH_RE = re.compile(
    r"/(?:job|jobs|job-details?|jobdetails?|jobdetail|career|careers|vacancy|vacancies|recruitment|recruit|circular|apply|internship|trainee)(?:/|\b|\?|#)",
    re.I,
)
PORTAL_UTILITY_PATH_RE = re.compile(
    r"/(?:about|contact|login|register|account|post-job|post-jobs|employer|seekers?|career-tools?|hot-jobs?|new_jobs?|new-jobs?|alljobs|jobsearch(?:-cache)?|search|category|categories|filter|filters)(?:/|\b|\?|#)|/(?:bdjobs|bdjobs-live)(?:/?$)",
    re.I,
)
JOB_TITLE_LINGUISTIC_RE = re.compile(
    r"(manager|officer|executive|assistant|associate|coordinator|analyst|intern|trainee|engineer|developer|accountant|auditor|relationship|representative|specialist|consultant|teacher|lecturer|supervisor|head|lead|director|internship|management|finance|hr|marketing|sales|business development|procurement|supply chain|operations|merchandising|banking)",
    re.I,
)


def job_detail_link_score(url, title="", source=""):
    url = safe_text(url)
    title = safe_text(title)
    if not url:
        return 0
    score = 0
    if JOB_DETAIL_PATH_RE.search(url):
        score += 60
    if re.search(r"(?:jobdetails?|job-id|jobid|vacancy|circular|apply)", url, re.I):
        score += 20
    if JOB_TITLE_LINGUISTIC_RE.search(title):
        score += 20
    if PORTAL_UTILITY_PATH_RE.search(url):
        score -= 70
    source = safe_text(source).lower()
    if source == "bdjobs" and not PORTAL_UTILITY_PATH_RE.search(url) and re.search(r"(?:jobdetails?|job-details?|job/)", url, re.I):
        score += 30
    if source == "bdjobs live" and re.search(r"/(?:job|jobs)(?:/|\?)", url, re.I):
        score += 25
    if source == "dohaj" and "/jobs/" in url.lower():
        score += 25
    return max(0, min(100, score))

BAD_PATH_RE = re.compile(
    r"/(opinion|editorial|sponsored|"
    r"tag|topic|live-blog|liveblog|"
    r"photo|photos|video)(/|$)",
    re.I,
)

BAD_TITLE_RE = re.compile(
    r"\b(sponsored|advertisement|"
    r"promo|opinion|editorial)\b",
    re.I,
)


def candidate_basic_allowed(item):
    """Fast pre-AI gate. Keep real vacancy-looking detail pages and reject obvious noise."""
    title = safe_text(item.get("title"))
    url = safe_text(item.get("url"))
    excerpt = safe_text(item.get("excerpt"))
    if not title or not url or not canonical_url(url):
        return False
    if not allowed_source_for_region(url, "Career"):
        return False

    is_direct = item.get("discovery") == "direct_portal"
    published_dt = parse_datetime(item.get("published_date"))
    if published_dt:
        if not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
            return False
    elif not is_direct:
        return False

    blob = f"{title} {excerpt}".strip()
    if BAD_JOB_RE.search(blob) or CAREER_NON_JOB_RE.search(blob):
        return False

    detail_signal = job_detail_link_score(url, title, item.get("source"))
    intent = job_intent_score(item)
    if is_direct and detail_signal < 45 and intent < 45:
        return False
    if not is_direct and intent < 28 and detail_signal < 45:
        return False

    # Known Bangladesh job portals are themselves strong geographic evidence.
    domain = normalized_domain(url)
    known_bd = domain in set(CAREER_JOB_PORTAL_DOMAINS)
    official = item.get("source_type") == "official_job_portal"
    bangladesh = bool(BANGLADESH_RE.search(blob))
    overseas = bool(OVERSEAS_RE.search(blob))
    if overseas and not bangladesh and not known_bd and not official:
        return False
    if not (bangladesh or known_bd or official):
        return False

    if CAREER_NON_JOB_RE.search(blob) and detail_signal < 70:
        return False
    if CAREER_LOW_FIT_RE.search(blob) and not CAREER_PRIORITY_RE.search(blob) and detail_signal < 70:
        return False

    return True

def title_duplicate_against_state(title):
    for previous in STATE.get(
        "recent_titles",
        [],
    )[-250:]:
        if title_similarity(
            title,
            previous,
        ) >= 0.88:
            return True

    return False


def title_duplicate_against_list(
    title,
    candidates,
    threshold=0.88,
):
    for candidate in candidates:
        if title_similarity(
            title,
            candidate["title"],
        ) >= threshold:
            return True

    return False


# ============================================================
# RSS INGESTION + PERSISTENT QUEUE
# ============================================================

def extract_entry_image(
    entry,
    page_url,
):
    for key in (
        "media_content",
        "media_thumbnail",
    ):
        for item in entry.get(
            key,
            [],
        ):
            image_url = safe_text(
                item.get("url")
            )

            if image_url:
                return urljoin(
                    page_url,
                    image_url,
                )

    for enclosure in entry.get(
        "enclosures",
        [],
    ):
        href = safe_text(
            enclosure.get("href")
        )

        mime = safe_text(
            enclosure.get("type")
        ).lower()

        if (
            href
            and (
                not mime
                or mime.startswith(
                    "image/"
                )
            )
        ):
            return urljoin(
                page_url,
                href,
            )

    return ""


def queue_candidate(item):
    canonical = safe_text(item.get("canonical")) or canonical_url(item.get("url", ""))
    if not canonical:
        return
    item = dict(item)
    item["canonical"] = canonical
    item["source_url"] = item.get("source_url") or item.get("url", "")
    item["apply_url"] = safe_text(item.get("apply_url") or item.get("application_url"))

    existing = STATE["queue"].get(canonical)
    if existing:
        existing["last_seen"] = now_iso()
        for key in ("image", "apply_url", "source_url", "deadline_hint", "deadline_evidence", "page_posted_date", "job_record", "source_job_id"):
            incoming = item.get(key)
            if incoming:
                existing[key] = incoming
        # Preserve the first observed publication/first-seen timestamps.
        if item.get("published_date") and not existing.get("published_date"):
            existing["published_date"] = item["published_date"]
        return

    STATE["queue"][canonical] = {
        **item,
        "status": "pending",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
    }

def fetch_rss_feed(
    feed_def,
):
    url = feed_def["url"]

    old = STATE["feeds"].get(
        url,
        {},
    )

    headers = dict(
        HEADERS
    )

    if old.get("etag"):
        headers["If-None-Match"] = old[
            "etag"
        ]

    if old.get(
        "last_modified"
    ):
        headers["If-Modified-Since"] = old[
            "last_modified"
        ]

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=12,
        )

        # 304 means the queue remains intact. The feed is reachable,
        # so this counts as healthy and clears any fail streak.
        if response.status_code == 304:
            logger.info(
                "RSS 304: %s",
                feed_def["name"],
            )
            mark_feed_healthy(feed_def, old)
            return 0

        if response.status_code >= 400:
            logger.warning(
                "RSS %s returned %s",
                feed_def["name"],
                response.status_code,
            )
            mark_feed_failed(feed_def, old)
            return 0

        STATE["feeds"][url] = {
            "etag": response.headers.get(
                "ETag",
                old.get("etag"),
            ),
            "last_modified": response.headers.get(
                "Last-Modified",
                old.get("last_modified"),
            ),
            "last_checked": now_iso(),
            "fail_count": 0,
            "alerted": False,
        }

        parsed = feedparser.parse(
            response.content
        )

        added = 0

        for entry in parsed.entries:
            published_dt = feed_entry_datetime(
                entry
            )

            date_estimated = False

            if not published_dt:
                # Some feeds send a date format we cannot parse.
                # Do not throw the story away: use fetch time instead,
                # and mark it so downstream code knows it is a guess.
                published_dt = datetime.now(
                    BD_TZ
                )
                date_estimated = True

            article_url = urljoin(
                url,
                safe_text(
                    entry.get("link")
                ),
            )

            title = safe_text(
                entry.get("title")
            )

            if not article_url or not title:
                continue

            item = {
                "title": title,
                "url": article_url,
                "canonical": canonical_url(
                    article_url
                ),
                "published_dt": published_dt.isoformat(),
                "published_date": published_dt.isoformat(),
                "source": feed_def["name"],
                "region": feed_def["region"],
                "excerpt": BeautifulSoup(
                    safe_text(
                        entry.get(
                            "summary"
                        )
                        or entry.get(
                            "description"
                        )
                    ),
                    "html.parser",
                ).get_text(
                    " ",
                    strip=True,
                )[:2000],
                "image": extract_entry_image(
                    entry,
                    article_url,
                ),
                "discovery": "rss",
                "date_estimated": date_estimated,
            }

            if not candidate_basic_allowed(
                {
                    **item,
                    "published_dt": published_dt,
                }
            ):
                continue

            if (
                item["canonical"]
                in POSTED_URLS
            ):
                continue

            before = item["canonical"] in STATE[
                "queue"
            ]

            queue_candidate(
                item
            )

            if not before:
                added += 1

        return added

    except Exception as exc:
        logger.warning(
            "RSS failed %s: %s",
            feed_def["name"],
            exc,
        )
        mark_feed_failed(feed_def, old)
        return 0


# Consecutive failed runs before we alert about a broken feed.
FEED_FAIL_ALERT_THRESHOLD = 3


def mark_feed_healthy(feed_def, old):
    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": 0,
        "alerted": False,
    }


def mark_feed_failed(feed_def, old):
    fail_count = int(old.get("fail_count", 0)) + 1

    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": fail_count,
    }

    if fail_count >= FEED_FAIL_ALERT_THRESHOLD and not old.get("alerted"):
        alert_feed_down(feed_def, fail_count)
        STATE["feeds"][feed_def["url"]]["alerted"] = True


def alert_feed_down(feed_def, fail_count):
    """Tell the admin a source has gone quiet, instead of failing silently forever."""
    message = (
        f"Feed down: {feed_def['name']} ({feed_def['region']})\n"
        f"Failed {fail_count} runs in a row.\n"
        f"URL: {feed_def['url']}\n"
        f"It will keep retrying, but this source is not feeding the bot right now."
    )

    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            telegram_call(
                "sendMessage",
                data={
                    "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                    "text": message,
                },
            )
        except Exception as exc:
            logger.warning(
                "Feed-down alert failed to send: %s",
                exc,
            )

    logger.error(message)


def collect_rss():
    added = 0

    for feed_def in RSS_FEEDS:
        added += fetch_rss_feed(
            feed_def
        )

    # Critical: queue is saved together with feed validators.
    # A later 304 cannot erase unposted queued stories.
    save_state(
        STATE
    )

    logger.info(
        "RSS queue additions: %d",
        added,
    )

    return added


# ============================================================
# EXA GAP-FILL DISCOVERY
# ============================================================

# ============================================================
# SOURCE UNIVERSE
# ============================================================
# CareerNewsBot uses the same domain-gating framework as the reference bot,
# but the allowed universe is the Bangladesh career/job source universe.
# These aliases intentionally point to the Career lists defined above.
ALL_PRIMARY_DOMAINS = PRIMARY_CAREER_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_CAREER_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS

def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")

def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)

def primary_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_PRIMARY_DOMAINS)

def fallback_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_FALLBACK_DOMAINS)

def allowed_source_for_region(url, region=None):
    return primary_domain_allowed(url, region) or fallback_domain_allowed(url, region)

# ============================================================
# GOOGLE NEWS RSS: FREE GAP FILL
# ============================================================

GOOGLE_NEWS_QUERIES = {
    "Career": [
        "latest Bangladesh job circular recruitment vacancy deadline",
        "latest Bangladesh government recruitment job circular vacancy",
        "latest Bangladesh private company hiring vacancy job",
        "latest Bangladesh bank NGO education IT recruitment vacancy",
        "latest Bangladesh internship management trainee graduate vacancy",
        "latest Bangladesh BBA MBA finance accounting banking internship jobs",
        "latest Bangladesh marketing sales HR business development jobs",
        "latest Bangladesh fresh graduate entry level jobs Bdjobs",
        "latest Bangladesh job recruitment notice application deadline",
    ],
}

GOOGLE_NEWS_LOCALE = {"Career": ("en-US", "BD", "BD:en")}


def resolve_google_news_url(link):
    """Google News RSS gives a redirect link, not the publisher URL.
    Follow it once (without downloading the full page) to get the
    real article URL. Return "" if it cannot be resolved safely."""
    try:
        response = session.get(
            link,
            timeout=10,
            allow_redirects=True,
            headers=HEADERS,
            stream=True,
        )
        real_url = safe_text(response.url)
        response.close()

        if not real_url or "news.google.com" in real_url:
            return ""

        return real_url

    except Exception:
        return ""


def google_news_gap_fill(region, existing_count=0, needed=15):
    """Always-on secondary discovery. It is intentionally cheap and complementary."""
    queries = [
        "Bangladesh job vacancy recruitment internship BBA MBA",
        "Bangladesh government private bank NGO job circular deadline",
        "Bangladesh management trainee graduate finance accounting marketing HR jobs",
        "site:bdjobs.com Bangladesh new job vacancy internship trainee",
    ]
    hl, gl, ceid = ("en-US", "BD", "BD:en")
    added = 0
    for query in queries:
        try:
            feed_url = "https://news.google.com/rss/search?q=" + quote(f"{query} when:3d") + f"&hl={hl}&gl={gl}&ceid={ceid}"
            response = session.get(feed_url, timeout=8, headers=HEADERS)
            if response.status_code >= 400:
                continue
            parsed = feedparser.parse(response.content)
            for entry in parsed.entries[:10]:
                title = safe_text(entry.get("title")); link = safe_text(entry.get("link"))
                if not title or not link:
                    continue
                real_url = resolve_google_news_url(link)
                if not real_url or not primary_domain_allowed(real_url, region):
                    continue
                published_dt = feed_entry_datetime(entry)
                if not published_dt or not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
                    continue
                summary = BeautifulSoup(safe_text(entry.get("summary")), "html.parser").get_text(" ", strip=True)
                item = {
                    "title": title, "url": real_url, "canonical": canonical_url(real_url),
                    "published_dt": published_dt.isoformat(), "published_date": published_dt.isoformat(),
                    "source": source_name(real_url), "source_type": "google_news", "region": "Career",
                    "excerpt": summary[:2500], "image": "", "discovery": "google_news", "date_estimated": False,
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Google News discovery failed: %s", exc)
    return added

def exa_gap_fill(region, existing_count=0, needed=15, fallback=False):
    """Always-on indexed discovery. Returns only source-domain candidates."""
    domains = FALLBACK_CAREER_DOMAINS if fallback else PRIMARY_CAREER_DOMAINS
    if not domains:
        return 0
    queries = [
        "Bangladesh latest job vacancy internship management trainee BBA MBA",
        "Bangladesh government private bank NGO recruitment job circular",
        "Bangladesh finance accounting marketing HR graduate jobs",
    ]
    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query, type="auto", num_results=10,
                include_domains=domains,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 900}},
            )
            for result in getattr(results, "results", []) or []:
                url = safe_text(getattr(result, "url", "")); title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt or not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
                    continue
                if not primary_domain_allowed(url, region):
                    continue
                raw_highlights = getattr(result, "highlights", None) or []
                if not isinstance(raw_highlights, list):
                    raw_highlights = [raw_highlights]
                excerpt = " ".join(safe_text(v) for v in raw_highlights if safe_text(v))[:2200]
                item = {
                    "title": title, "url": url, "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(), "published_date": published_dt.isoformat(),
                    "source": source_name(url), "source_type": "exa", "region": region,
                    "excerpt": excerpt, "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa", "source_pool": "primary", "date_estimated": False,
                }
                if not candidate_basic_allowed(item) or item["canonical"] in POSTED_URLS:
                    continue
                queue_candidate(item); added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa discovery failed: %s", exc)
    return added

def queue_candidates_for_region(
    region,
):
    count = 0

    for item in STATE[
        "queue"
    ].values():
        if (
            item.get("region")
            == region
            and item.get("status")
            == "pending"
        ):
            published = parse_datetime(
                item.get(
                    "published_date"
                )
            )

            if (
                published
                and DISCOVERY_START
                <= published
                <= DISCOVERY_END
            ):
                count += 1

    return count


# ============================================================
# CANDIDATE NORMALIZATION
# ============================================================

# ============================================================
# VERSION 1 EDITORIAL RANKING
# ============================================================

# ============================================================
# CAREER INTELLIGENCE SCORING
# ============================================================

def deadline_status_score(deadline_dt):
    if not deadline_dt:
        return 35
    now = career_now()
    d = deadline_dt.astimezone(BD_TZ) if deadline_dt.tzinfo else deadline_dt.replace(tzinfo=BD_TZ)
    if d.hour == 0 and d.minute == 0 and d.second == 0 and d.microsecond == 0:
        d = d.replace(hour=23, minute=59, second=59)
    days = (d-now).total_seconds()/86400
    if days <= 0: return 0
    if days >= 30: return 100
    if days >= 21: return 92
    if days >= 14: return 84
    if days >= 7: return 76
    if days >= 5: return 65
    if days >= 3: return 52
    if days >= 1: return 35
    return 15

def freshness_score(published_dt, estimated=False):
    if not published_dt: return 20
    if estimated:
        # Current portal snapshots are useful discovery evidence but are not treated
        # as equally fresh as a verified article/listing timestamp.
        return 55
    age=max(0,(career_now()-published_dt).total_seconds()/3600)
    if age<=6: return 100
    if age<=12: return 95
    if age<=24: return 90
    if age<=48: return 75
    if age<=72: return 55
    return 10

def source_reliability_score(item):
    domain=normalized_domain(item.get("url",""))
    if item.get("source_type")=="official_job_portal": return 100
    if domain in {"jobs.bdjobs.com","bdjobs.com","bdjobslive.com","dohaj.com","job.com.bd"}: return 96
    return {"rss":92,"direct_portal":90,"google_news":82,"exa":80}.get(item.get("discovery"),70)

def completeness_score(item):
    fields=[item.get("title"),item.get("source"),item.get("url"),item.get("excerpt"),item.get("deadline_hint"),item.get("apply_url")]
    return round(100*sum(bool(safe_text(x)) for x in fields)/len(fields))

CAREER_AUDIENCE_STRONG_RE = re.compile(
    r"""(bba|mba|business administration|business studies|management trainee|trainee|internship|intern|graduate|fresh graduate|fresher|entry[- ]level|
       accounting|finance|accounts|audit|bank|banking|credit|relationship manager|marketing|sales|hr|human resources|
       business development|operations|supply chain|procurement|customer service|analyst|admin|management|commercial|merchandising)""",
    re.I | re.X,
)
CAREER_AUDIENCE_WEAK_RE = re.compile(
    r"(executive|officer|assistant|associate|coordinator|relationship|support|representative)",
    re.I,
)
CAREER_PRIORITY_RE = re.compile(
    r"(management trainee|graduate trainee|internship|intern|bba|mba|business|finance|accounting|banking|credit|audit|hr|human resources|marketing|sales|business development|operations|supply chain|procurement|commercial|management|analyst|admin|customer service|merchandising)",
    re.I,
)
CAREER_LOW_FIT_RE = re.compile(
    r"(driver|security guard|guard|cleaner|caretaker|helper|cook|chef|waiter|waitress|peon|laborer|labourer|delivery rider|rider|domestic worker|garments operator|machine operator|electrician|plumber|mason|nurse|medical officer|physician|dentist|pharmacist|civil engineer|mechanical engineer|electrical engineer)",
    re.I,
)
CAREER_NON_JOB_RE = re.compile(
    r"(career advice|career tips|job tips|interview tips|cv tips|resume tips|exam result|admission|scholarship|job fair|career fair|training course|workshop|seminar|career guide|salary guide|how to get a job)",
    re.I,
)
VACANCY_EVIDENCE_RE = re.compile(
    r"(vacancy|vacancies|job description|responsibilities|requirements|qualification|apply now|application|deadline|last date|closing date|salary|experience|education|নিয়োগ|চাকরি|শূন্যপদ|আবেদন|যোগ্যতা|শেষ তারিখ)",
    re.I,
)

def audience_fit_score(item):
    raw = f"{item.get('title','')} {item.get('excerpt','')}"
    if CAREER_NON_JOB_RE.search(raw):
        return 10
    strong = len(CAREER_AUDIENCE_STRONG_RE.findall(raw))
    weak = len(CAREER_AUDIENCE_WEAK_RE.findall(raw))
    priority = len(CAREER_PRIORITY_RE.findall(raw))
    low_fit = len(CAREER_LOW_FIT_RE.findall(raw))
    score = 50 + min(35, strong * 9) + min(10, priority * 3) + (5 if weak else 0) - min(35, low_fit * 12)
    return max(0, min(100, score))


def job_intent_score(item):
    raw = f"{item.get('title','')} {item.get('excerpt','')}"
    if CAREER_NON_JOB_RE.search(raw):
        return 0
    evidence = len(VACANCY_EVIDENCE_RE.findall(raw))
    signals = len(JOB_SIGNAL_RE.findall(raw))
    score = 35 + min(35, evidence * 7) + min(20, signals * 4)
    if re.search(r"(apply|application|deadline|last date|আবেদন|শেষ তারিখ)", raw, re.I):
        score += 10
    return max(0, min(100, score))


def local_job_score(item):
    published=parse_datetime(item.get("published_date"))
    deadline=parse_date_text(item.get("deadline_hint","")) or choose_deadline(item.get("excerpt",""))
    raw = f"{item.get('title','')} {item.get('excerpt','')}"
    source = source_reliability_score(item)
    # Bdjobs is intentionally important but not dominant. Relevance decides publication.
    if normalized_domain(item.get("url","")) in {"jobs.bdjobs.com","bdjobs.com"}:
        source = min(100, source + 2)
    parts={
        "freshness":freshness_score(published, bool(item.get("date_estimated"))),
        "deadline":deadline_status_score(deadline),
        "source":source,
        "completeness":completeness_score(item),
        "job_intent":job_intent_score(item),
        "audience_fit":audience_fit_score(item),
    }
    score=round(
        .20*parts["freshness"]
        + .10*parts["deadline"]
        + .12*parts["source"]
        + .12*parts["completeness"]
        + .24*parts["job_intent"]
        + .22*parts["audience_fit"]
    )
    return score,parts

def deadline_is_expired(deadline_dt):
    if not deadline_dt: return False
    now=career_now()
    d=deadline_dt.astimezone(BD_TZ) if deadline_dt.tzinfo else deadline_dt.replace(tzinfo=BD_TZ)
    if d.hour==0 and d.minute==0 and d.second==0 and d.microsecond==0: d=d.replace(hour=23,minute=59,second=59)
    return d < now


def deadline_sort_key(deadline_dt):
    """Prefer later usable deadlines without ever rejecting non-expired jobs."""
    if not deadline_dt:
        return float("-inf")
    d=deadline_dt.astimezone(BD_TZ) if deadline_dt.tzinfo else deadline_dt.replace(tzinfo=BD_TZ)
    return d.timestamp()


RANK_SCHEMA = {
    "type":"object","properties":{"ranked":{"type":"array","items":{"type":"object","properties":{
        "id":{"type":"integer"},"rank":{"type":"integer","minimum":1},"score":{"type":"integer","minimum":0,"maximum":100},
        "relevance":{"type":"integer","minimum":0,"maximum":100},"career_value":{"type":"integer","minimum":0,"maximum":100},
        "reason":{"type":"string"},"topic":{"type":"string"},"institution":{"type":"string"},"event_key":{"type":"string"}},
        "required":["id","rank","score","relevance","career_value","reason","topic","institution","event_key"],"additionalProperties":False}}},
    "required":["ranked"],"additionalProperties":False}


def enrich_thin_excerpt(item):
    """Best-effort article enrichment. Failure never removes a candidate."""
    try:
        downloaded = trafilatura.fetch_url(item["url"])
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return safe_text(text)[:1200] if text else None
    except Exception:
        return None


def enrich_thin_excerpts(regional):
    enriched = 0
    for item in regional:
        if enriched >= MAX_EXCERPT_ENRICH:
            break
        excerpt = safe_text(item.get("excerpt", ""))
        if len(excerpt) >= THIN_EXCERPT_CHARS:
            continue
        fuller = enrich_thin_excerpt(item)
        if fuller and len(fuller) > len(excerpt):
            item["excerpt"] = fuller
            enriched += 1
    return regional


def _rank_prompt(region):
    return """You rank Bangladesh job vacancies for @CareerNewsroom.
Return every candidate in the batch. Do not invent, rewrite, merge, or substitute vacancy facts.
The source title and source URL identify the vacancy. Score 0-100 for publication usefulness.
Target audience: Bangladesh young professionals roughly 20-30, with particular interest in BBA/MBA/business,
finance/accounting/banking, marketing/sales, HR, operations, management trainee, graduate, entry-level and internships.
Also keep strong government, NGO, education and reputable corporate opportunities when genuinely relevant.
Do not assume nationality from the text; this is a Bangladesh jobs channel, so the audience context is implicit.
Prefer actual vacancies over career advice, job lists, category pages, profiles, ads, scholarship/training content,
results, opinions, and portal utility pages. Prefer roles with clear company, role, location, application route and deadline.
Deadline distance is a ranking factor only. Expired vacancies should score 0.
A missing field is not itself a reason to reject a real vacancy.
Return concise reason text only.
"""

def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title','')}",
            f"Source: {item.get('source','')}",
            f"Published: {published}",
            age_note,
            f"Description/Excerpt: {trim_source_text(item.get('excerpt',''), 650)}",
            "",
        ]))

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _rank_prompt(region)},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"career_job_rank_batch_{batch_no}",
                    "strict": True,
                    "schema": RANK_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=2600,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []


def rank_candidates(candidates, region):
    if not candidates:
        return []
    # Strong deterministic ordering first. Only the best 30 candidates consume AI budget.
    regional_all = sorted(
        candidates,
        key=lambda x: (
            local_job_score(x)[0],
            freshness_score(parse_datetime(x.get("published_date")), bool(x.get("date_estimated"))),
            source_reliability_score(x),
        ),
        reverse=True,
    )[:RANKING_POOL_SIZE]
    ai_regional = regional_all[:AI_RANK_INPUT_LIMIT]
    rows_by_url = {}
    used_batches = 0
    for offset in range(0, len(ai_regional), AI_RANK_BATCH_SIZE):
        if used_batches >= AI_RANK_MAX_BATCHES:
            break
        batch = ai_regional[offset:offset + AI_RANK_BATCH_SIZE]
        used_batches += 1
        logger.info("Career RANK BATCH %d: %d candidates", used_batches, len(batch))
        rows = _rank_batch(batch, region, used_batches)
        by_id = {i: x for i, x in enumerate(batch, 1)}
        for row in rows:
            try:
                idx = int(row.get("id"))
            except Exception:
                continue
            if idx not in by_id:
                continue
            item = dict(by_id[idx])
            local, parts = local_job_score(item)
            ai = max(0, min(100, int(row.get("score", local))))
            relevance = max(0, min(100, int(row.get("relevance", local))))
            career_value = max(0, min(100, int(row.get("career_value", local))))
            item.update({
                "importance_score": round(.65 * ai + .35 * local),
                "ai_score": ai, "local_score": local, "score_components": parts,
                "topic": canonical_topic(safe_text(row.get("topic")), region),
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")) or normalize_title(item.get("title", "")),
                "rank_reason": safe_text(row.get("reason")),
                "relevance_score": relevance, "career_value_score": career_value,
                "batch_rank": int(row.get("rank", 9999)),
            })
            rows_by_url[item.get("canonical")] = item

    for original in regional_all:
        if original.get("canonical") in rows_by_url:
            continue
        item = dict(original)
        local, parts = local_job_score(item)
        item.update({
            "importance_score": local, "ai_score": None, "local_score": local, "score_components": parts,
            "topic": canonical_topic(item.get("topic"), region),
            "institution": safe_text(item.get("institution")),
            "event_key": normalize_title(item.get("title", "")),
            "rank_reason": "Local relevance ranking fallback.",
            "relevance_score": parts.get("audience_fit", local),
            "career_value_score": parts.get("audience_fit", local),
            "batch_rank": 9999,
        })
        rows_by_url[item.get("canonical")] = item

    ranked = list(rows_by_url.values())
    ranked.sort(key=lambda x: (
        -float(x.get("importance_score", 0)),
        -float(x.get("relevance_score", 0)),
        -float(x.get("local_score", 0)),
        -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
    ))
    for i, item in enumerate(ranked, 1):
        item["editor_rank"] = i
    logger.info("Career RANKED RETURNED: %d/%d (AI=%d, batches=%d)", len(ranked), len(regional_all), len(ai_regional), used_batches)
    return ranked

def extract_entities(text):
    words = re.findall(r"[A-Za-z][A-Za-z&'-]{1,}", safe_text(text).lower())
    return {w for w in words if w not in STOPWORDS}


def entity_overlap(a, b):
    ea = extract_entities(f"{a.get('title','')} {a.get('excerpt','')}")
    eb = extract_entities(f"{b.get('title','')} {b.get('excerpt','')}")
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / max(1, min(len(ea), len(eb)))


def event_similarity_v04(a, b):
    title_score = title_similarity(a.get("title", ""), b.get("title", ""))
    entity_score = entity_overlap(a, b)
    return (0.75 * title_score) + (0.25 * entity_score)


def same_event_window(a, b, hours=30):
    da = parse_datetime(a.get("published_date"))
    db = parse_datetime(b.get("published_date"))
    if not da or not db:
        return False
    return abs((da - db).total_seconds()) <= hours * 3600


def cluster_ranked_events(ranked):
    """Conservative event clustering. Uncertain items are always kept."""
    clusters = []
    ordered = sorted(
        ranked,
        key=lambda x: x.get("editor_rank", 9999),
    )
    for item in ordered:
        placed = False
        for cluster in clusters:
            representative = cluster[0]
            item_event_key = safe_text(item.get("event_key"))
            rep_event_key = safe_text(representative.get("event_key"))
            same_key = bool(
                item_event_key
                and rep_event_key
                and item_event_key == rep_event_key
                and (
                    entity_overlap(item, representative) >= 0.25
                    or title_similarity(item.get("title", ""), representative.get("title", "")) >= 0.55
                )
            )
            if same_key or (
                same_event_window(item, representative)
                and event_similarity_v04(item, representative) >= 0.88
            ):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    output = []
    for index, cluster in enumerate(clusters, start=1):
        representative = cluster[0]
        stable_key = normalize_title(representative.get("title", "")) or representative.get("canonical", "")
        digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:10]
        cluster_id = f"evt_{digest}"
        sources = sorted({safe_text(x.get("source")) for x in cluster if safe_text(x.get("source"))})
        for member in cluster:
            row = dict(member)
            row.update({
                "event_cluster_id": cluster_id,
                "event_cluster_size": len(cluster),
                "event_sources": sources,
                "event_source_count": len(sources),
                "event_confidence": 1.0 if len(cluster) > 1 else 0.6,
            })
            output.append(row)
    return output


def collapse_event_clusters(ranked):
    clustered = cluster_ranked_events(ranked)
    winners = {}
    for item in clustered:
        key = item.get("event_cluster_id") or item.get("canonical")
        old = winners.get(key)
        if old is None:
            winners[key] = item
            continue
        # Preserve the highest editorial rank, then newest story.
        item_key = (
            item.get("editor_rank", 9999),
            -(parse_datetime(item.get("published_date")).timestamp() if parse_datetime(item.get("published_date")) else 0),
        )
        old_key = (
            old.get("editor_rank", 9999),
            -(parse_datetime(old.get("published_date")).timestamp() if parse_datetime(old.get("published_date")) else 0),
        )
        if item_key < old_key:
            winners[key] = item
    return sorted(winners.values(), key=lambda x: x.get("editor_rank", 9999))


def persist_event_cluster_state(ranked):
    clusters = STATE.setdefault("event_clusters", {})
    for item in ranked:
        event_id = item.get("event_cluster_id")
        if not event_id:
            continue
        clusters[event_id] = {
            "event_id": event_id,
            "topic": item.get("topic", ""),
            "region": item.get("region", ""),
            "sources": item.get("event_sources", []),
            "source_count": item.get("event_source_count", 0),
            "confidence": item.get("event_confidence", 0),
            "last_seen": now_iso(),
            "headline": item.get("title", ""),
        }


def remember_posted_event(story):
    event_id = story.get("event_cluster_id") or make_event_id(story)
    ids = STATE.setdefault("posted_event_ids", [])
    if event_id and event_id not in ids:
        ids.append(event_id)
    STATE["posted_event_ids"] = ids[-500:]
    return event_id


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def find_image_candidates(
    url,
    page_html=None,
    final_url=None,
    preferred_image="",
):
    """Return ordered article-image candidates from RSS and page metadata.

    The caller must validate candidates by actually downloading them. This
    prevents one broken RSS URL from blocking a perfectly valid og:image or
    JSON-LD image later in the pipeline.
    """
    candidates = []

    def add(value):
        value = safe_text(value).strip()
        if not value or value.startswith("data:"):
            return
        try:
            value = urljoin(final_url or url, value)
        except Exception:
            return
        if value not in candidates:
            candidates.append(value)

    add(preferred_image)

    try:
        base_url = final_url or url
        if page_html is None:
            response = session.get(
                url,
                headers={**HEADERS, "Referer": url},
                timeout=20,
            )
            if response.status_code >= 400:
                return candidates
            page_html = response.text
            base_url = response.url

        soup = BeautifulSoup(page_html, "html.parser")

        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                add(urljoin(base_url, safe_text(tag.get("content", ""))))

        for link in soup.find_all("link"):
            rel = {safe_text(x).lower() for x in (link.get("rel") or [])}
            href = safe_text(link.get("href", ""))
            if "preload" in rel and safe_text(link.get("as", "")).lower() == "image":
                add(urljoin(base_url, href))
            elif rel & {"image_src", "image"}:
                add(urljoin(base_url, href))

        # JSON-LD often has the cleanest article image URL.
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                obj = stack.pop()
                if isinstance(obj, dict):
                    img = obj.get("image")
                    if isinstance(img, str):
                        add(img)
                    elif isinstance(img, list):
                        for val in img:
                            add(val if isinstance(val, str) else (val or {}).get("url", "") if isinstance(val, dict) else "")
                    elif isinstance(img, dict):
                        add(img.get("url", "") or img.get("contentUrl", ""))
                    for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                        child = obj.get(key)
                        if isinstance(child, (dict, list)):
                            stack.extend(child if isinstance(child, list) else [child])

        # Lazy-loaded and srcset images. Keep only plausible image URLs.
        for tag in soup.find_all(["img", "source"]):
            for attr in ("data-src", "data-lazy-src", "data-original", "data-image", "src"):
                add(tag.get(attr, ""))
            srcset = safe_text(tag.get("srcset", ""))
            if srcset:
                for part in srcset.split(","):
                    add(part.strip().split(" ")[0])
            data_srcset = safe_text(tag.get("data-srcset", ""))
            if data_srcset:
                for part in data_srcset.split(","):
                    add(part.strip().split(" ")[0])

    except Exception as exc:
        logger.debug("Image candidate discovery failed %s: %s", url, exc)

    return candidates


def find_og_image(url, page_html=None, final_url=None):
    candidates = find_image_candidates(url, page_html, final_url)
    return candidates[0] if candidates else ""


def extract_article(item):
    global EXA_CONTENT_FALLBACKS_USED
    url = item["url"]
    page_posted = None
    apply_url = safe_text(item.get("apply_url"))
    combined_image_candidates = []
    page_text = ""
    try:
        response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=12)
        if response.status_code < 400:
            page_html = response.text
            final_url = response.url
            page_posted = posted_at_from_html(page_html)
            if page_posted:
                item["page_posted_date"] = page_posted.isoformat()
                if not item.get("published_date") or item.get("date_estimated"):
                    item["published_date"] = page_posted.isoformat()
                    item["date_estimated"] = False
            if not apply_url:
                apply_url = extract_apply_url(final_url, page_html)
            if apply_url:
                item["apply_url"] = apply_url
            deadline_dt = choose_deadline(page_html)
            if deadline_dt:
                item["deadline_hint"] = deadline_dt.strftime("%d %B %Y")
            item["deadline_evidence"] = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)[:24000]
            extract_page_structured_hints(page_html, final_url, item)
            combined_image_candidates = find_image_candidates(url, page_html, final_url, preferred_image=item.get("image", ""))
            page_text = trafilatura.extract(page_html, include_comments=False, include_tables=False, favor_precision=True) or ""
            if page_text and len(safe_text(page_text)) >= 220:
                return safe_text(page_text), combined_image_candidates
            excerpt_fallback = safe_text(item.get("excerpt"))
            if len(excerpt_fallback) >= 500:
                return excerpt_fallback[:14000], combined_image_candidates
    except Exception as exc:
        logger.debug("Local career extraction failed %s: %s", url, exc)

    with EXA_CONTENT_LOCK:
        if EXA_CONTENT_FALLBACKS_USED >= MAX_EXA_CONTENT_FALLBACKS:
            logger.info("Exa content fallback cap reached; using available candidate evidence: %s", item.get("title"))
            excerpt_fallback = safe_text(item.get("excerpt"))
            return (excerpt_fallback[:14000] if len(excerpt_fallback) >= 500 else ""), combined_image_candidates
        EXA_CONTENT_FALLBACKS_USED += 1
    try:
        result_set = get_exa().get_contents([url], text={"max_characters": 14000})
        if getattr(result_set, "results", None):
            result = result_set.results[0]
            text = safe_text(getattr(result, "text", ""))
            exa_image = safe_text(getattr(result, "image", ""))
            combined_image_candidates = find_image_candidates(url, preferred_image=item.get("image", "") or exa_image)
            if exa_image and exa_image not in combined_image_candidates:
                combined_image_candidates.append(exa_image)
            if text:
                # Exa text may contain markdown/application links. Never use source URL as fallback.
                if not apply_url:
                    for candidate_url in _extract_urls_from_text(text):
                        if canonical_url(candidate_url) == canonical_url(url):
                            continue
                        label_match = re.search(r"(?:apply|application|submit|আবেদন)[^\n]{0,120}", text, re.I)
                        if label_match and candidate_url in label_match.group(0):
                            apply_url = candidate_url; break
                if apply_url:
                    item["apply_url"] = apply_url
                deadline_dt = choose_deadline(text)
                if deadline_dt:
                    item["deadline_hint"] = deadline_dt.strftime("%d %B %Y")
                    item["deadline_evidence"] = text[:18000]
                return text, combined_image_candidates
    except Exception as exc:
        logger.debug("Exa career extraction failed %s: %s", url, exc)
    return "", [item.get("image", "")] if item.get("image") else []

FIELD_PATTERNS = {
    "company": [
        r"(?:company|employer|organization|প্রতিষ্ঠান|নিয়োগকারী)\s*[:\-]\s*([^\n|•]+)",
    ],
    "location": [
        r"(?:location|job location|work location|স্থান|কর্মস্থল)\s*[:\-]\s*([^\n|•]+)",
    ],
    "job_type": [
        r"(?:job type|employment type|type|চাকরির ধরন|নিয়োগের ধরন)\s*[:\-]\s*([^\n|•]+)",
    ],
    "education": [
        r"(?:education|educational qualification|academic qualification|যোগ্যতা|শিক্ষাগত যোগ্যতা)\s*[:\-]\s*([^\n|•]+)",
    ],
    "experience": [
        r"(?:experience|work experience|অভিজ্ঞতা)\s*[:\-]\s*([^\n|•]+)",
    ],
    "salary": [
        r"(?:salary|salary range|compensation|বেতন|বেতন ভাতা)\s*[:\-]\s*([^\n|•]+)",
    ],
    "vacancies": [
        r"(?:vacancies?|number of vacancies|no\.? of vacancies|পদসংখ্যা|শূন্যপদ)\s*[:\-]\s*([^\n|•]+)",
    ],
    "age_limit": [
        r"(?:age limit|age|বয়স|বয়সসীমা)\s*[:\-]\s*([^\n|•]+)",
    ],
    "application_fee": [
        r"(?:application fee|fee|আবেদন ফি|আবেদন ফি)\s*[:\-]\s*([^\n|•]+)",
    ],
    "application_method": [
        r"(?:application|application method|how to apply|apply|আবেদন|আবেদন পদ্ধতি)\s*[:\-]\s*([^\n|•]+)",
    ],
    "application_period": [
        r"(?:application period|application time|আবেদনের সময়|আবেদনের সময়কাল)\s*[:\-]\s*([^\n|•]+)",
    ],
    "selection_process": [
        r"(?:selection process|selection procedure|পরীক্ষার ধরণ|নির্বাচন প্রক্রিয়া)\s*[:\-]\s*([^\n|•]+)",
    ],
}


def _label_value(text, patterns):
    clean = re.sub(r"[ \t]+", " ", safe_text(text))
    for pattern in patterns:
        m = re.search(pattern, clean, re.I)
        if m:
            value = clean_generated_text(m.group(1)).strip(" -|•:")
            if value:
                return trim_source_text(value, 320)
    return ""


def _jsonld_objects(page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    objects = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = safe_text(script.string or script.get_text(" ", strip=True))
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                objects.append(obj)
                for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                    child = obj.get(key)
                    if isinstance(child, (list, dict)):
                        stack.extend(child if isinstance(child, list) else [child])
    return objects


def extract_page_structured_hints(page_html, page_url, item):
    """Extract cheap structured facts before any AI call."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    item["company_hint"] = item.get("company_hint") or _label_value(text, FIELD_PATTERNS["company"])
    item["location_hint"] = item.get("location_hint") or _label_value(text, FIELD_PATTERNS["location"])
    item["job_type_hint"] = item.get("job_type_hint") or _label_value(text, FIELD_PATTERNS["job_type"])
    for obj in _jsonld_objects(page_html):
        if not item.get("company_hint"):
            org = obj.get("hiringOrganization") or obj.get("hiringorganisation")
            if isinstance(org, dict):
                item["company_hint"] = safe_text(org.get("name"))
        if not item.get("job_title_hint") and obj.get("title"):
            item["job_title_hint"] = safe_text(obj.get("title"))
        if not item.get("location_hint") and obj.get("jobLocation"):
            loc = obj.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            if isinstance(loc, dict):
                addr = loc.get("address") or loc
                if isinstance(addr, dict):
                    parts = [safe_text(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if safe_text(addr.get(k))]
                    item["location_hint"] = ", ".join(parts)
        if not item.get("salary_hint") and isinstance(obj.get("baseSalary"), dict):
            bs = obj["baseSalary"].get("value")
            if isinstance(bs, dict):
                lo, hi, cur = bs.get("minValue"), bs.get("maxValue"), obj["baseSalary"].get("currency")
                if lo and hi: item["salary_hint"] = f"{cur or ''} {lo}-{hi}".strip()
                elif lo: item["salary_hint"] = f"{cur or ''} {lo}".strip()
        if not item.get("deadline_hint") and obj.get("validThrough"):
            dt = parse_date_text(safe_text(obj.get("validThrough")))
            if dt: item["deadline_hint"] = dt.strftime("%d %B %Y")
    return text


def local_job_record(item, article_text):
    """Build a JobRecord locally. AI is only needed when critical facts remain ambiguous."""
    text = safe_text(article_text)
    title = safe_text(item.get("job_title_hint") or item.get("title"))
    company = safe_text(item.get("company_hint")) or _label_value(text, FIELD_PATTERNS["company"])
    location = safe_text(item.get("location_hint")) or _label_value(text, FIELD_PATTERNS["location"])
    job_type = safe_text(item.get("job_type_hint")) or _label_value(text, FIELD_PATTERNS["job_type"])
    record = {
        "job_title": trim_source_text(title, 120),
        "company": trim_source_text(company, 160),
        "location": trim_source_text(location, 180),
        "job_type": trim_source_text(job_type, 120),
        "education": _label_value(text, FIELD_PATTERNS["education"]),
        "experience": _label_value(text, FIELD_PATTERNS["experience"]),
        "salary": safe_text(item.get("salary_hint")) or _label_value(text, FIELD_PATTERNS["salary"]),
        "vacancies": _label_value(text, FIELD_PATTERNS["vacancies"]),
        "age_limit": _label_value(text, FIELD_PATTERNS["age_limit"]),
        "application_fee": _label_value(text, FIELD_PATTERNS["application_fee"]),
        "application_method": _application_label_value(text),
        "application_period": _label_value(text, FIELD_PATTERNS["application_period"]),
        "selection_process": _label_value(text, FIELD_PATTERNS["selection_process"]),
        "deadline": safe_text(item.get("deadline_hint")),
        "apply_url": safe_text(item.get("apply_url")),
        "source_url": safe_text(item.get("url")),
    }
    if not record["job_type"]:
        gov_domains = {"smartjob.portal.gov.bd", "alljobs.teletalk.com.bd", "jobs.teletalk.com.bd", "bpsc.gov.bd", "erecruitment.bcc.gov.bd"}
        if normalized_domain(item.get("url", "")) in gov_domains or item.get("source_type") == "official_job_portal":
            record["job_type"] = "Government"
    if not record["deadline"]:
        dt = choose_deadline(text)
        if dt: record["deadline"] = dt.strftime("%d %B %Y")
    if not record["application_period"] and record["deadline"]:
        period = _application_period_from_text(text, record["deadline"])
        if period: record["application_period"] = period
    return record


def _application_label_value(text):
    value = _label_value(text, FIELD_PATTERNS["application_method"])
    if value:
        return _application_display(value)
    markers = re.compile(r"(?:apply|application|আবেদন)[^\n]{0,220}", re.I)
    for m in markers.finditer(text):
        value = _application_display(m.group(0))
        if value and not re.fullmatch(r"(?:apply|application|আবেদন)", value, re.I):
            return trim_source_text(value, 220)
    return ""


def _application_period_from_text(text, deadline):
    # Only build a period when a source explicitly gives a start date near the deadline marker.
    dt = parse_date_text(deadline)
    if not dt: return ""
    start_patterns = [
        r"(?:application (?:starts|begins|opens|start|begin)|applications? open(?:s)?|apply from|আবেদন শুরু|আবেদন শুরু হবে|আবেদন শুরু হয়)\s*[:\-]?\s*([^\n]{4,80})",
    ]
    for pat in start_patterns:
        m = re.search(pat, text, re.I)
        if m:
            start = parse_date_text(m.group(1))
            if start:
                return f"{start.strftime('%d %B %Y')} – {dt.strftime('%d %B %Y')}"
    return ""


JOB_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "job_title": {"type": "string"}, "company": {"type": "string"}, "location": {"type": "string"}, "job_type": {"type": "string"},
        "education": {"type": "string"}, "experience": {"type": "string"}, "salary": {"type": "string"},
        "vacancies": {"type": "string"}, "age_limit": {"type": "string"},
        "application_fee": {"type": "string"}, "application_method": {"type": "string"}, "application_period": {"type": "string"},
        "selection_process": {"type": "string"}, "deadline": {"type": "string"}, "apply_url": {"type": "string"},
        "bangladesh_relevance": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "job_title","company","location","job_type","education","experience","salary","vacancies","age_limit",
        "application_fee","application_method","application_period","selection_process","deadline","apply_url","bangladesh_relevance","confidence",
    ],
    "additionalProperties": False,
}

def extract_locked_job_record(item, article_text):
    global AI_RECORD_FALLBACKS_USED
    local = local_job_record(item, article_text)
    source_url = canonical_url(item.get("url", ""))
    apply_url = safe_text(local.get("apply_url"))
    if apply_url and canonical_url(apply_url) == source_url:
        apply_url = ""
    local["apply_url"] = apply_url

    # Local record is sufficient for strong cases. Missing optional fields are fine.
    critical_ok = bool(local.get("job_title") and local.get("company") and local.get("location") and local.get("apply_url"))
    if critical_ok:
        dt = parse_date_text(local.get("deadline", "")) if local.get("deadline") else None
        if dt and deadline_is_expired(dt):
            return None
        local["bangladesh_relevance"] = 90 if (BANGLADESH_RE.search(f"{local['location']} {article_text}") or normalized_domain(source_url) in set(CAREER_JOB_PORTAL_DOMAINS)) else 70
        local["confidence"] = 90
        return local

    # Bounded AI fallback for only the few strongest candidates where local extraction is ambiguous.
    with AI_RECORD_LOCK:
        if AI_RECORD_FALLBACKS_USED >= MAX_AI_RECORD_FALLBACKS:
            return None
        AI_RECORD_FALLBACKS_USED += 1
        fallback_no = AI_RECORD_FALLBACKS_USED
    logger.info("AI record fallback %d/%d: %s", fallback_no, MAX_AI_RECORD_FALLBACKS, item.get("title", ""))

    prompt = """Extract exactly ONE Bangladesh job vacancy from the provided source evidence.
Use the SOURCE TITLE and SOURCE URL as the vacancy identity. Never merge or substitute another vacancy.
Return empty strings for absent fields. Never guess.
CRITICAL: apply_url must be a real application destination different from source_url. Never use source_url as apply_url.
Missing optional fields are allowed.
"""
    user = f"SOURCE={item.get('source','')}\nSOURCE TITLE={item.get('title','')}\nSOURCE URL={item.get('url','')}\nDISCOVERED APPLY URL={item.get('apply_url','')}\nPAGE:\n{article_text[:13000]}"
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role":"system","content":prompt},{"role":"user","content":user}],
            response_format={"type":"json_schema","json_schema":{"name":f"career_job_record_fallback_{fallback_no}","strict":True,"schema":JOB_RECORD_SCHEMA}},
            reasoning_effort="low", temperature=0.0, max_completion_tokens=1400,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
    except Exception as exc:
        logger.warning("AI job record fallback failed: %s", exc)
        return None

    def f(name, limit): return trim_source_text(clean_generated_text(data.get(name)), limit)
    candidate_apply = safe_text(item.get("apply_url"))
    model_apply = f("apply_url", 500)
    article_urls = {canonical_url(u):u for u in _extract_urls_from_text(article_text)}
    if not candidate_apply and model_apply and canonical_url(model_apply) in article_urls:
        candidate_apply = article_urls[canonical_url(model_apply)]
    if canonical_url(candidate_apply) == source_url:
        candidate_apply = ""

    record = {
        "job_title": safe_text(item.get("title")) or f("job_title",120) or local.get("job_title"),
        "company": f("company",160) or local.get("company"),
        "location": f("location",180) or local.get("location"),
        "job_type": f("job_type",120) or local.get("job_type"),
        "education": f("education",260) or local.get("education"),
        "experience": f("experience",180) or local.get("experience"),
        "salary": f("salary",140) or local.get("salary"),
        "vacancies": f("vacancies",80) or local.get("vacancies"),
        "age_limit": f("age_limit",100) or local.get("age_limit"),
        "application_fee": f("application_fee",120) or local.get("application_fee"),
        "application_method": _application_display(f("application_method",220) or local.get("application_method")),
        "application_period": f("application_period",160) or local.get("application_period"),
        "selection_process": f("selection_process",220) or local.get("selection_process"),
        "deadline": f("deadline",80) or local.get("deadline"),
        "apply_url": candidate_apply,
        "source_url": safe_text(item.get("url")),
        "bangladesh_relevance": max(0,min(100,int(data.get("bangladesh_relevance",85)))),
        "confidence": max(0,min(100,int(data.get("confidence",75)))),
    }
    if not record["job_title"] or not record["company"] or not record["location"] or not record["apply_url"]:
        return None
    if record["deadline"]:
        dt = parse_date_text(record["deadline"])
        if dt and deadline_is_expired(dt):
            return None
    return record

def generate_story(item, article_text, locked_record=None):
    """Deterministic editorial wrapper.

    No second AI call is needed after the authoritative JobRecord is extracted:
    the channel card contains source-backed facts only, so the editor simply
    chooses the canonical topic and derives emphasis terms locally.
    """
    record=locked_record or {}
    locked_title=safe_text(record.get("job_title")) or safe_text(item.get("title")) or "Job Vacancy"
    story={
        **item,
        "headline": locked_title,
        "topic": canonical_topic(item.get("topic"),"Career"),
        "bold_terms": [],
    }
    story["bold_terms"]=derive_bold_terms({**story, **record})[:12]
    return story


# ============================================================
# NUMERIC GROUNDING
# ============================================================

NUMBER_RE = re.compile(
    r"""
    (?:
        (?:US|U\.S\.|HK|HK\$|Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥)
        \s*
    )?
    \d[\d,]*(?:\.\d+)?
    \s*
    (?:
        million|billion|trillion|
        crore|lakh|bn|mn|b|m|k|%
    )?
    (?![A-Za-z])
    """,
    re.I | re.X,
)

YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_NUMERIC_SCALES = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
    "t": 1e12,
    "crore": 1e7,
    "lakh": 1e5,
}
_NUMERIC_CURRENCIES = {
    "$": "usd",
    "usd": "usd",
    "tk": "bdt",
    "bdt": "bdt",
    "৳": "bdt",
    "€": "eur",
    "eur": "eur",
    "£": "gbp",
    "gbp": "gbp",
    "¥": "jpy",
    "jpy": "jpy",
    "cny": "cny",
    "inr": "inr",
    "₹": "inr",
    "hk$": "hkd",
    "hk": "hkd",
}

def _numeric_signature(raw):
    text = safe_text(raw).strip().lower()
    if not text:
        return None

    currency = None
    for symbol in sorted(_NUMERIC_CURRENCIES, key=len, reverse=True):
        if text.startswith(symbol):
            currency = _NUMERIC_CURRENCIES[symbol]
            text = text[len(symbol):].strip()
            break

    if text.endswith("%"):
        unit = "%"
        text = text[:-1].strip()
    else:
        m = re.search(r"(trillion|billion|million|crore|lakh|bn|mn|[kmbt])$", text)
        unit = m.group(1) if m else ""
        if m:
            text = text[:m.start()].strip()

    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None

    if unit == "%":
        scale = 1.0
        percent = True
    else:
        scale = _NUMERIC_SCALES.get(unit, 1.0)
        percent = False

    return {
        "value": value * scale,
        "percent": percent,
        "currency": currency,
    }

def normalize_number(raw):
    sig = _numeric_signature(raw)
    if not sig:
        return ""
    value = sig["value"]
    value_key = f"{value:.12g}"
    currency = sig["currency"] or ""
    percent = "%" if sig["percent"] else ""
    return f"{currency}|{percent}|{value_key}"

def numeric_tokens(text):
    tokens = []
    for match in NUMBER_RE.finditer(safe_text(text)):
        token = safe_text(match.group(0)).strip()
        sig = _numeric_signature(token)
        if not sig:
            continue

        # Ignore bare years, but retain years with a financial/percent/unit marker.
        numeric_value = sig["value"]
        if (
            numeric_value.is_integer()
            and YEAR_RE.match(str(int(numeric_value)))
            and not sig["percent"]
            and not sig["currency"]
        ):
            continue

        if token:
            tokens.append(token)
    return tokens

def _numeric_equivalent(source_sig, generated_sig):
    if not source_sig or not generated_sig:
        return False
    if source_sig["percent"] != generated_sig["percent"]:
        return False

    # If both explicitly name currencies, they must agree.
    # If only one names a currency, accept the numeric equivalent because
    # source extraction often drops currency symbols around abbreviated forms.
    if (
        source_sig["currency"]
        and generated_sig["currency"]
        and source_sig["currency"] != generated_sig["currency"]
    ):
        return False

    return abs(source_sig["value"] - generated_sig["value"]) <= max(
        1e-9, abs(source_sig["value"]) * 1e-9
    )

def numeric_grounded(story, article_text):
    source_sigs = [_numeric_signature(x) for x in numeric_tokens(article_text)]
    source_sigs = [x for x in source_sigs if x]
    fields = [
        story.get("headline", ""), story.get("company", ""), story.get("location", ""),
        story.get("job_type", ""), story.get("education", ""), story.get("experience", ""),
        story.get("salary", ""), story.get("deadline", ""),
        story.get("vacancies", ""), story.get("age_limit", ""), story.get("application_fee", ""), story.get("application_method", ""), story.get("application_period", ""), story.get("selection_process", ""), story.get("apply_url", ""),
    ]
    generated_text = " ".join(safe_text(x) for x in fields if safe_text(x))
    for token in numeric_tokens(generated_text):
        generated_sig = _numeric_signature(token)
        if not generated_sig:
            continue
        if not any(_numeric_equivalent(source_sig, generated_sig) for source_sig in source_sigs):
            return False, token
    return True, ""



# ============================================================
# BOLD TERMS
# ============================================================

def derive_bold_terms(story, max_terms=16):
    terms = []
    for value in story.get("bold_terms", []) or []:
        value = safe_text(value).strip()
        if value and value not in terms:
            terms.append(value)

    candidates = [
        story.get("headline", ""), story.get("company", ""), story.get("location", ""),
        story.get("job_type", ""), story.get("education", ""), story.get("salary", ""),
        story.get("deadline", ""),
    ]
    for key in ("vacancies","age_limit","application_fee","application_method","application_period","selection_process"):
        if story.get(key): candidates.append(story.get(key))

    for value in candidates:
        value = safe_text(value).strip()
        if not value:
            continue
        if value not in terms and len(value) <= 80:
            terms.append(value)

    return terms[:max_terms]



def escape_rich_html(
    text,
):
    return html.escape(
        clean_generated_text(text),
        quote=False,
    )


def bold_terms_html(
    text,
    terms,
):
    text = clean_generated_text(
        text
    )

    if not text:
        return ""

    result = text

    # Use letter-only markers to avoid collisions with numeric terms.
    replacements = []

    for index, term in enumerate(
        sorted(
            {
                safe_text(x)
                for x in terms
                if safe_text(x)
            },
            key=len,
            reverse=True,
        )
    ):
        marker = (
            f"__RICHBOLD_{chr(65 + (index % 26))}"
            f"{index // 26}__"
        )

        pattern = re.compile(
            re.escape(term),
            re.I,
        )

        match = pattern.search(
            result
        )

        if match:
            original = match.group(
                0
            )
            result = (
                result[:match.start()]
                + marker
                + result[match.end():]
            )
            replacements.append(
                (
                    marker,
                    original,
                )
            )

    escaped = html.escape(
        result,
        quote=False,
    )

    for marker, original in replacements:
        escaped = escaped.replace(
            marker,
            "<b>"
            + html.escape(
                original,
                quote=False,
            )
            + "</b>",
        )

    return escaped


# ============================================================
# DYNAMIC RICH MESSAGE HTML
# ============================================================

def _display_field(value):
    """Return a displayable source-backed value; omit unknown placeholders."""
    value = clean_generated_text(safe_text(value)).strip()
    if not value:
        return ""
    if value.lower() in {
        "not specified", "not available", "n/a", "na", "unknown",
        "not mentioned", "not stated", "not provided", "-", "--",
    }:
        return ""
    return value


def _format_posted_date(story):
    raw = safe_text(story.get("posted_at") or story.get("published_date"))
    dt = parse_datetime(raw)
    if dt:
        return dt.astimezone(BD_TZ).strftime("%d %B %Y")
    return _display_field(raw)


def _application_display(value):
    """Hide raw application URLs from the body while retaining the useful method text."""
    value = _display_field(value)
    if not value:
        return ""
    value = re.sub(r"https?://[^\s<>)\"]+", "", value, flags=re.I)
    value = re.sub(r"\s{2,}", " ", value).strip(" -•|:")
    return value


def _snapshot_rows(story):
    fields = [
        ("📍 Location", story.get("location")),
        ("💼 Type", story.get("job_type")),
        ("🎓 Education", story.get("education")),
        ("👨‍💼 Experience", story.get("experience")),
        ("💰 Salary", story.get("salary")),
        ("👥 Vacancies", story.get("vacancies")),
        ("🎂 Age Limit", story.get("age_limit")),
        ("💳 Application Fee", story.get("application_fee")),
        ("📝 Application", _application_display(story.get("application_method"))),
        ("🗓️ Application Period", story.get("application_period")),
        ("🧪 Selection Process", story.get("selection_process")),
        ("📅 Deadline", story.get("deadline")),
        ("🕒 Posted", _format_posted_date(story)),
    ]
    rows=[]
    for label,value in fields:
        value=_display_field(value)
        if value:
            rows.append((label,value))
    return rows


def _html_table(rows):
    parts=["<table bordered striped compact><tr><th><b>FIELD</b></th><th><b>DETAILS</b></th></tr>"]
    for label,value in rows:
        parts.append("<tr><td>"+escape_rich_html(label)+"</td><td>"+bold_terms_html(value,[])+"</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _inline_keyboard(story):
    # Apply URL is a distinct field. Never fall back to the source/details URL.
    apply_url = safe_text(
        story.get("apply_url")
        or story.get("application_url")
        or ""
    ).strip()
    source_url = canonical_url(story.get("source_url") or story.get("url") or "")
    if not apply_url or not re.match(r"^https?://", apply_url, re.I):
        return None
    if canonical_url(apply_url) == source_url:
        return None
    return {"inline_keyboard": [[{"text": "APPLY NOW", "url": apply_url}]]}


def dynamic_rich_html(story):
    """Compact CareerNewsroom card: facts in a table, hashtags → source → one Apply button."""
    terms=derive_bold_terms(story)
    company=_display_field(story.get("company"))
    source=_display_field(story.get("source")) or "Official Source"
    source_url=safe_text(story.get("source_url") or story.get("url") or "")
    rows=_snapshot_rows(story)

    parts=["<h1>📣 "+escape_rich_html(story.get("headline","Job Vacancy"))+"</h1>"]
    if company:
        parts.append("<p>🏢 <b>Company:</b> "+bold_terms_html(company,terms)+"</p>")
    if rows:
        parts.append("<h2>JOB SNAPSHOT</h2>")
        parts.append(_html_table(rows))

    hashtags=" ".join(category_hashtags(story))
    if hashtags:
        parts.append("<p>"+escape_rich_html(hashtags)+"</p>")

    if source_url and re.match(r"^https?://",source_url,re.I):
        parts.append("<p>🔎 <b>Official Source:</b> "+f'<a href="{html.escape(source_url,quote=True)}">{escape_rich_html(source)}</a></p>')
    else:
        parts.append("<p>🔎 <b>Official Source:</b> "+escape_rich_html(source)+"</p>")

    # The Apply control is deliberately NOT embedded in Rich HTML.
    # It is attached as a native Telegram InlineKeyboardMarkup so it renders
    # below the message bubble like a normal inline keyboard button.
    return "\n".join(parts)

def rich_visible_length(text):
    """Return Telegram-visible character count for Rich HTML text.

    Telegram limits the rendered text, not the raw HTML markup, so strip
    tags and decode HTML entities before counting characters.
    """
    no_tags = re.sub(r"<[^>]+>", "", text)
    no_attrs = re.sub(
        r"\[[^\]]+\]\([^)]+\)",
        lambda m: m.group(0).split("]")[0][1:],
        no_tags,
    )
    return len(html.unescape(no_attrs))


def fit_rich_html(story):
    variants=[
        {"education":240,"experience":150,"application_method":220,"application_period":160,"selection_process":220,"location":140},
        {"education":210,"experience":130,"application_method":180,"application_period":140,"selection_process":180,"location":120},
        {"education":180,"experience":110,"application_method":150,"application_period":120,"selection_process":150,"location":110},
    ]
    for limits in variants:
        candidate=dict(story)
        for key,limit in limits.items():
            candidate[key]=trim_source_text(story.get(key,""),limit)
        rendered=dynamic_rich_html(candidate)
        if rich_visible_length(rendered)<=MAX_RICH_CHARACTERS:
            return rendered
    return dynamic_rich_html(story)



# ============================================================
# IMAGE BRANDING: ONLY @CareerNewsroom
# ============================================================

def find_font(
    bold=False,
):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def download_image(
    url,
    referer,
):
    if not url:
        return None

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": referer,
            },
            timeout=12,
            stream=True,
        )

        if response.status_code >= 400:
            return None

        content_type = (
            response.headers.get(
                "content-type",
                "",
            )
            .lower()
        )

        if (
            content_type
            and not content_type.startswith(
                "image/"
            )
        ):
            return None

        buf = BytesIO()

        for chunk in response.iter_content(
            65536
        ):
            if not chunk:
                continue

            buf.write(
                chunk
            )

            if buf.tell() > 8_000_000:
                return None

        buf.seek(0)

        image = Image.open(
            buf
        )
        image.load()

        if (
            image.width < 400
            or image.height < 250
        ):
            return None

        return image.convert(
            "RGB"
        )

    except Exception as exc:
        logger.warning(
            "Image download failed: %s",
            exc,
        )
        return None


def crop_cover(
    image,
    size=(1200, 675),
):
    target_w, target_h = size

    ratio = max(
        target_w / image.width,
        target_h / image.height,
    )

    resized = image.resize(
        (
            int(
                image.width
                * ratio
            ),
            int(
                image.height
                * ratio
            ),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (
        resized.width
        - target_w
    ) // 2

    top = (
        resized.height
        - target_h
    ) // 2

    return resized.crop(
        (
            left,
            top,
            left + target_w,
            top + target_h,
        )
    )


def image_average_brightness(
    image,
):
    small = image.resize(
        (1, 1)
    ).convert(
        "L"
    )
    return small.getpixel(
        (0, 0)
    )


def display_source_name(source):
    return safe_text(source).strip() or "Source"



def source_homepage(source, article_url=""):
    host = ""
    try:
        host = urlparse(article_url).netloc.lower().removeprefix("www.")
    except Exception:
        pass
    domain_map = {
        "Bdjobs": "bdjobs.com", "Dohaj": "dohaj.com", "Job.com.bd": "job.com.bd",
        "Smart Job": "smartjob.portal.gov.bd", "Alljobs Teletalk": "alljobs.teletalk.com.bd",
        "BPSC": "bpsc.gov.bd", "BCC e-Recruitment": "erecruitment.bcc.gov.bd",
        "JobsNoticeBD": "jobsnoticebd.com", "JobsInfo": "jobsinfo.bd", "JobFeeds": "jobfeeds.online",
        "CircularBD": "circularbd.com", "Bangladesher Khabor": "bangladesherkhabor.net",
        "Dhaka Post": "dhakapost.com", "Dhaka Tribune": "dhakatribune.com",
        "Bangla Tribune": "banglatribune.com", "JagoNews24": "jagonews24.com",
        "Prothom Alo": "prothomalo.com", "RisingBD": "risingbd.com", "BD24Live": "bd24live.com",
        "Bangladesh Journal": "bd-journal.com", "Jugantor": "jugantor.com", "Kaler Kantho": "kalerkantho.com",
        "The Daily Star": "thedailystar.net", "The Daily Ittefaq": "ittefaq.com.bd", "ProjobsBD": "projobsbd.com",
        "BD Govt Jobs": "bdgovtjobs.com", "JobPagol": "jobpagol.com", "Bangladesh Pratidin Jobs": "bdpratidin.net",
        "Bangladesh Pratidin": "bdpratidin.net",
    }
    domain = domain_map.get(source, host)
    return f"https://{domain}" if domain else ""



def download_source_logo(source, article_url=""):
    """Download a reasonably large source logo/icon from the publisher site."""
    homepage = source_homepage(source, article_url)
    if not homepage:
        return None

    candidates = []
    try:
        response = session.get(
            homepage,
            headers={**HEADERS, "Referer": article_url or homepage},
            timeout=15,
        )
        if response.status_code < 400:
            soup = BeautifulSoup(response.text, "html.parser")
            base = response.url
            for rel in ("apple-touch-icon", "icon", "shortcut icon"):
                for tag in soup.find_all("link", rel=lambda x: x and rel in [str(v).lower() for v in (x if isinstance(x, list) else [x])]):
                    href = safe_text(tag.get("href", ""))
                    if href:
                        candidates.append(urljoin(base, href))
            for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
                for tag in soup.find_all("meta", attrs=attrs):
                    value = safe_text(tag.get("content", ""))
                    if value:
                        candidates.append(urljoin(base, value))
    except Exception as exc:
        logger.debug("Source-logo page discovery failed %s: %s", source, exc)

    # Direct favicon is a strong last-resort source-site identity image.
    parsed = urlparse(homepage)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates.extend([
        f"{origin}/apple-touch-icon.png",
        f"{origin}/favicon.ico",
        f"{origin}/favicon.png",
    ])

    # Google favicon proxy is only the last fallback, not the primary source.
    candidates.append(
        "https://www.google.com/s2/favicons?domain="
        + parsed.netloc
        + "&sz=256"
    )

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        image = download_image(candidate, homepage)
        if image is not None:
            return image
    return None


def _contrast_palette(image):
    brightness = image_average_brightness(image)
    if brightness < 120:
        return (245, 245, 245, 235), (18, 22, 28, 255)
    return (24, 29, 35, 225), (250, 250, 250, 255)


def _draw_channel_chip(draw, font, canvas_size):
    channel_text = "@CareerNewsroom"
    bbox = draw.textbbox((0, 0), channel_text, font=font)
    padding_x, padding_y = 18, 9
    margin_x, margin_y = 28, 24
    chip_w = (bbox[2] - bbox[0]) + padding_x * 2
    chip_h = (bbox[3] - bbox[1]) + padding_y * 2
    x2 = canvas_size[0] - margin_x
    y2 = canvas_size[1] - margin_y
    x1 = x2 - chip_w
    y1 = y2 - chip_h
    return (x1, y1, x2, y2), (x1 + padding_x, y1 + padding_y - 1), channel_text



def branded_card(photo, source, source_position="left"):
    """Brand a real article image with only @CareerNewsroom bottom-right."""
    base = crop_cover(photo).convert("RGBA")
    chip_bg, chip_fg = _contrast_palette(base)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    rect, text_pos, channel_text = _draw_channel_chip(draw, font, base.size)
    draw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    draw.text(text_pos, channel_text, font=font, fill=chip_fg)
    return Image.alpha_composite(base, overlay).convert("RGB")


def fallback_logo_card(logo, source):
    """Large centered source logo + channel chip, with contrast-aware background."""
    canvas = Image.new("RGB", (1200, 675), (238, 240, 243))
    # Sample source logo average to choose a contrasting neutral background.
    logo_brightness = image_average_brightness(logo)
    bg = (28, 34, 42) if logo_brightness > 150 else (238, 240, 243)
    canvas.paste(bg, (0, 0, canvas.width, canvas.height))
    logo = logo.convert("RGBA")
    # Preserve logo aspect ratio and make it visually large without touching edges.
    max_w, max_h = 780, 430
    scale = min(max_w / logo.width, max_h / logo.height)
    new_size = (max(1, int(logo.width * scale)), max(1, int(logo.height * scale)))
    logo = logo.resize(new_size, Image.Resampling.LANCZOS)
    x = (1200 - logo.width) // 2
    y = (675 - logo.height) // 2
    # Keep transparency where possible; add a subtle neutral plate only when needed.
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(logo, (x, y))

    overlay = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    chip_bg, chip_fg = _contrast_palette(canvas_rgba)
    rect, text_pos, channel_text = _draw_channel_chip(draw, font, canvas_rgba.size)
    draw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    draw.text(text_pos, channel_text, font=font, fill=chip_fg)
    return Image.alpha_composite(canvas_rgba, overlay).convert("RGB")


def fallback_source_name_card(source):
    """Source name centered in bold + channel chip bottom-right."""
    canvas = Image.new("RGB", (1200, 675), (30, 39, 51))
    draw = ImageDraw.Draw(canvas)
    font_path = find_font(bold=True)
    name = display_source_name(source)
    if font_path:
        size = 110
        while size >= 48:
            font = ImageFont.truetype(font_path, size)
            bbox = draw.textbbox((0, 0), name, font=font)
            if bbox[2] - bbox[0] <= 1000:
                break
            size -= 4
    else:
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), name, font=font)
    bbox = draw.textbbox((0, 0), name, font=font)
    x = (1200 - (bbox[2] - bbox[0])) // 2
    y = (675 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), name, font=font, fill="white")

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    chip_font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    chip_bg, chip_fg = _contrast_palette(canvas.convert("RGBA"))
    rect, text_pos, channel_text = _draw_channel_chip(odraw, chip_font, canvas.size)
    odraw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    odraw.text(text_pos, channel_text, font=chip_font, fill=chip_fg)
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def prepare_image(story, index):
    """Use a real article image when available. Missing imagery never blocks publishing."""
    candidates=[]
    for value in story.get("image_candidates",[]):
        if value and value not in candidates:
            candidates.append(value)
    if story.get("image_url") and story.get("image_url") not in candidates:
        candidates.append(story.get("image_url"))

    for candidate in candidates:
        image=download_image(candidate, story.get("url",""))
        if image is not None:
            logger.info("Article image recovered: %s", candidate)
            path=f"/tmp/news_{index}.jpg"
            branded=branded_card(image, story.get("source","Source"))
            branded.save(path,"JPEG",quality=88,optimize=True)
            return path

    logger.info("No usable article image; publishing text-only: %s", story.get("headline",""))
    return None



# ============================================================
# TELEGRAM RICH MESSAGES
# ============================================================

def telegram_call(
    method,
    data=None,
    files=None,
):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    last = {
        "ok": False,
        "description": "Unknown error",
    }

    for attempt in range(
        1,
        6,
    ):
        try:
            response = session.post(
                url,
                data=data or {},
                files=files,
                timeout=90,
            )

            result = response.json()

            if result.get(
                "ok"
            ):
                return result

            last = result

            if response.status_code == 429:
                retry_after = int(
                    result.get(
                        "parameters",
                        {},
                    ).get(
                        "retry_after",
                        5,
                    )
                )

                logger.warning(
                    "Telegram 429; waiting %ss",
                    retry_after,
                )

                time.sleep(
                    max(
                        1,
                        retry_after,
                    )
                )
                continue

            if response.status_code >= 500:
                time.sleep(
                    2 * attempt
                )
                continue

            break

        except Exception as exc:
            last = {
                "ok": False,
                "description": str(exc),
            }

            time.sleep(
                2 * attempt
            )

    return last



def _rich_html_to_plain_text(rich_html):
    text=rich_html
    text=re.sub(r"<br\s*/?>","\n",text,flags=re.I)
    text=re.sub(r"</(p|h1|h2|h3|footer|summary|details|tr|td|th)>","\n",text,flags=re.I)
    text=re.sub(r"<[^>]+>","",text)
    text=html.unescape(text)
    text=re.sub(r"\n{3,}","\n\n",text).strip()
    return text


def send_bot_api_fallback(image_path, rich_html, reply_markup=None):
    text=_rich_html_to_plain_text(rich_html)
    if len(text)>3900:
        text=text[:3897].rsplit(" ",1)[0].rstrip()+"..."
    try:
        if image_path:
            url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(image_path,"rb") as photo:
                response=session.post(url,data={"chat_id":TELEGRAM_CHANNEL,"caption":text,**({"reply_markup":json.dumps(reply_markup,ensure_ascii=False)} if reply_markup else {})},files={"photo":photo},timeout=90)
        else:
            url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            response=session.post(url,data={"chat_id":TELEGRAM_CHANNEL,"text":text,"disable_web_page_preview":"true",**({"reply_markup":json.dumps(reply_markup,ensure_ascii=False)} if reply_markup else {})},timeout=90)
        return response.json()
    except Exception as exc:
        return {"ok":False,"description":str(exc)}


def send_rich_message(image_path, rich_html, reply_markup=None):
    """Send the Rich Message and attach a native inline keyboard outside the HTML body."""
    rich_message={"html":rich_html,"skip_entity_detection":False}
    data={
        "chat_id":TELEGRAM_CHANNEL,
        "rich_message":json.dumps(rich_message,ensure_ascii=False),
    }
    if reply_markup:
        data["reply_markup"]=json.dumps(reply_markup,ensure_ascii=False)

    if image_path:
        rich_message["media"]=[{"id":"newsphoto","media":{"type":"photo","media":"attach://photo"}}]
        data["rich_message"]=json.dumps(rich_message,ensure_ascii=False)
        with open(image_path,"rb") as photo:
            return telegram_call("sendRichMessage",data=data,files={"photo":photo})
    return telegram_call("sendRichMessage",data=data)


# ============================================================
# KNOWLEDGE / EVENT RECORD
# ============================================================

def make_event_id(
    story,
):
    event_key = safe_text(
        story.get(
            "event_key"
        )
    )

    if event_key:
        return (
            re.sub(
                r"[^a-z0-9]+",
                "_",
                event_key.lower(),
            ).strip("_")
        )

    stable = normalize_title(
        f"{story.get('company','')} {story.get('headline','')} {story.get('location','')}"
    )
    if stable:
        return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]
    return canonical_url(story.get("url", ""))


def store_event(story, published=False, message_id=None):
    event_id = make_event_id(story)
    event = {
        "event_id": event_id,
        "event_key": story.get("event_key", ""),
        "canonical_url": story.get("canonical", canonical_url(story.get("url", ""))),
        "source_url": story.get("source_url") or story.get("url", ""),
        "apply_url": story.get("apply_url", ""),
        "original_url": story.get("url", ""),
        "source": story.get("source", ""),
        "region": "Career",
        "topic": story.get("topic", ""),
        "institution": story.get("company", ""),
        "event_cluster_id": story.get("event_cluster_id", event_id),
        "event_confidence": story.get("event_confidence", 0),
        "event_source_count": story.get("event_source_count", 0),
        "headline": story.get("headline", ""),
        "company": story.get("company", ""),
        "location": story.get("location", ""),
        "job_type": story.get("job_type", ""),
        "education": story.get("education", ""),
        "experience": story.get("experience", ""),
        "salary": story.get("salary", ""),
        "vacancies": story.get("vacancies", ""),
        "age_limit": story.get("age_limit", ""),
        "application_fee": story.get("application_fee", ""),
        "application_method": story.get("application_method", ""),
        "application_period": story.get("application_period", ""),
        "selection_process": story.get("selection_process", ""),
        "deadline": story.get("deadline", ""),
        "deadline_iso": story.get("deadline_iso", ""),
        "published_at": story.get("published_date", ""),
        "selected_at": now_iso(),
        "status": "published" if published else "selected",
        "message_id": message_id,
    }
    STATE["events"][event_id] = event
    return event_id



# ============================================================
# ============================================================



# ============================================================
# VERSION 1 FALLBACK POOLS
# ============================================================

def build_candidate_pool(ranked, needed):
    if not ranked:
        return []
    normalized = []
    for raw in ranked:
        item = dict(raw)
        item["canonical"] = safe_text(item.get("canonical")) or canonical_url(item.get("url", ""))
        normalized.append(item)
    target = min(MAX_PROCESS_CANDIDATES, max(24, needed * 3), len(normalized))
    buckets = defaultdict(list)
    for item in normalized:
        buckets[safe_text(item.get("source")) or "Unknown Source"].append(item)
    selected = []
    source_names = list(buckets)
    # First pass: one strong candidate from each source.
    for source in sorted(source_names, key=lambda s: -float(buckets[s][0].get("importance_score", 0))):
        if len(selected) >= target:
            break
        selected.append(dict(buckets[source].pop(0)))
    # Fill by global rank, but cap a source at four in the processing pool.
    source_used = Counter(safe_text(x.get("source")) or "Unknown Source" for x in selected)
    for item in normalized:
        if len(selected) >= target:
            break
        canonical = item.get("canonical")
        if any(x.get("canonical") == canonical for x in selected):
            continue
        source = safe_text(item.get("source")) or "Unknown Source"
        if source_used[source] >= 4:
            continue
        selected.append(dict(item)); source_used[source] += 1
    logger.info("DIVERSE PROCESS POOL: %d candidates across %d sources", len(selected), len(source_used))
    return selected

def claims_grounded(story: Dict[str, Any], evidence: str) -> Tuple[bool, List[str]]:
    # Career facts are source-locked before this point. Keep this check local and cheap.
    issues: List[str] = []
    if not story.get("source_url"):
        issues.append("missing source URL")
    apply_url = str(story.get("apply_url") or "").strip()
    source_url = str(story.get("source_url") or "").strip()
    if not apply_url:
        issues.append("missing apply URL")
    if apply_url and source_url and canonical_url(apply_url) == canonical_url(source_url):
        issues.append("apply URL equals source URL")
    return (not issues), issues

def cache_job_record(item, record):
    canonical = safe_text(item.get("canonical"))
    if canonical and canonical in STATE.get("queue", {}):
        STATE["queue"][canonical]["job_record"] = record
        if record.get("apply_url"):
            STATE["queue"][canonical]["apply_url"] = record["apply_url"]
        if record.get("deadline"):
            STATE["queue"][canonical]["deadline_hint"] = record["deadline"]


def process_story_candidate(item, verify_claims=False):
    article_text, image_candidates = extract_article(item)
    if not article_text:
        logger.warning("DROP extraction: %s", item.get("title"))
        return None

    page_posted_date = parse_datetime(item.get("page_posted_date"))
    if page_posted_date:
        item["published_date"] = page_posted_date.isoformat()
    if item.get("published_date"):
        published_dt = parse_datetime(item.get("published_date"))
        if published_dt and not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
            return None

    if not candidate_basic_allowed(item):
        return None

    record = item.get("job_record")
    if not record:
        record = extract_locked_job_record(item, article_text)
    if not record:
        logger.info("DROP job record: %s", item.get("title"))
        return None

    # Re-resolve apply URL from page/article when available, never from source URL.
    apply_url = safe_text(record.get("apply_url") or item.get("apply_url"))
    if not apply_url or canonical_url(apply_url) == canonical_url(item.get("url", "")):
        return None
    if not re.match(r"^https?://", apply_url, re.I):
        return None
    record["apply_url"] = apply_url
    record["source_url"] = safe_text(item.get("url"))

    deadline_dt = parse_date_text(record.get("deadline", "")) if record.get("deadline") else None
    if deadline_dt and deadline_is_expired(deadline_dt):
        return None

    story = generate_story(item, article_text, locked_record=record)
    if not story:
        return None
    for key in ("headline","company","location","job_type","education","experience","salary","vacancies","age_limit","application_fee","application_method","application_period","selection_process","deadline"):
        story[key] = record.get("job_title") if key == "headline" else record.get(key, "")
    story["source"] = safe_text(item.get("source")) or source_name(item.get("url", ""))
    story["source_url"] = safe_text(item.get("url"))
    story["apply_url"] = apply_url
    story["job_record"] = record
    story["deadline_iso"] = deadline_dt.date().isoformat() if deadline_dt else ""
    story["published_date"] = (page_posted_date or parse_datetime(item.get("published_date")) or career_now()).isoformat()
    story["canonical"] = safe_text(item.get("canonical")) or canonical_url(item.get("url", ""))
    story["url"] = safe_text(item.get("url"))
    story["event_key"] = item.get("event_key") or normalize_title(f"{record.get('company','')} {record.get('job_title','')} {record.get('location','')}")
    story["event_cluster_id"] = item.get("event_cluster_id", "")
    story["event_source_count"] = item.get("event_source_count", 1)
    story["image_candidates"] = list(image_candidates or [])
    story["image_url"] = story["image_candidates"][0] if story["image_candidates"] else ""
    story["topic"] = canonical_topic(story.get("topic") or item.get("topic"))
    story["category_hashtags"] = category_hashtags(story)

    grounded, bad_number = numeric_grounded(story, article_text)
    if not grounded:
        logger.info("DROP numeric grounding: %s (%s)", story.get("headline"), bad_number)
        return None

    if ENABLE_AI_CLAIM_VERIFY and verify_claims:
        verified, unsupported = claims_grounded(story, article_text)
        if not verified:
            return None
    else:
        verified, unsupported = claims_grounded(story, article_text)
        if not verified:
            logger.info("DROP local claim grounding: %s | %s", story.get("headline"), unsupported)
            return None

    cache_job_record(item, record)
    return story

def is_already_published_candidate(item):
    canonical = safe_text(item.get("canonical"))
    if canonical and canonical in POSTED_URLS:
        return True

    title = safe_text(item.get("title"))
    if not title:
        return False

    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        if event.get("region") != item.get("region"):
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or (NOW_BD - published_at).total_seconds() > EVENT_RETENTION_DAYS * 86400:
            continue
        previous_title = safe_text(event.get("headline"))
        if previous_title and title_similarity(title, previous_title) >= 0.90:
            return True
    return False


def available_candidates(region, source_pool=None):
    candidates = []
    seen = set()
    for item in STATE.get("queue", {}).values():
        if item.get("region") != region or item.get("status") not in {"pending", "selected"}:
            continue
        published = parse_datetime(item.get("published_date"))
        estimated = bool(item.get("date_estimated"))
        anchor = published or parse_datetime(item.get("first_seen"))
        if not anchor or not (DISCOVERY_START <= anchor <= DISCOVERY_END):
            continue
        url = safe_text(item.get("url")); canonical = safe_text(item.get("canonical"))
        if not canonical or canonical in seen:
            continue
        if source_pool == "primary" and not primary_domain_allowed(url, region):
            continue
        if source_pool == "fallback" and not fallback_domain_allowed(url, region):
            continue
        if source_pool is None and not allowed_source_for_region(url, region):
            continue
        if is_already_published_candidate(item):
            continue
        item_copy = dict(item)
        item_copy["date_estimated"] = estimated
        candidates.append(item_copy); seen.add(canonical)
    candidates.sort(key=lambda x: (
        local_job_score(x)[0],
        freshness_score(parse_datetime(x.get("published_date") or x.get("first_seen")), bool(x.get("date_estimated"))),
    ), reverse=True)
    return candidates[:MAX_RSS_CANDIDATES]

def prepare_ranked_region(region,candidates):
    ranked=rank_candidates(candidates,region)
    logger.info("%s RANKED RETURNED: %d",region,len(ranked))
    clustered=collapse_event_clusters(ranked)
    logger.info("%s AFTER EVENT DEDUP: %d",region,len(clustered))
    persist_event_cluster_state(clustered)
    return clustered


def process_ranked_region(region, ranked):
    if not ranked:
        return []
    pool = build_candidate_pool(ranked, MAX_STORIES_PER_RUN)
    candidates = pool[:MAX_PROCESS_CANDIDATES]
    valid = []
    rejected = 0
    attempted = set()

    def try_process(items):
        nonlocal rejected
        for item in items:
            if len(valid) >= MAX_STORIES_PER_RUN:
                break
            canonical = safe_text(item.get("canonical")) or canonical_url(item.get("url", ""))
            if canonical in attempted:
                continue
            attempted.add(canonical)
            score = float(item.get("importance_score", item.get("local_score", 0)) or 0)
            if score < MIN_PUBLISH_SCORE:
                continue
            story = process_story_candidate(item, verify_claims=False)
            if not story:
                rejected += 1
                continue
            story["publish_score"] = score
            valid.append(story)

    # Fast primary pass.
    try_process(candidates[:PRIMARY_PROCESS_CANDIDATES])
    # Bounded reserve pass only when the minimum has not been reached. This pass uses
    # the same deterministic extraction path and does not expand the AI budget.
    if len(valid) < MIN_STORIES_PER_RUN:
        logger.info("MINIMUM RESERVE PASS: valid=%d minimum=%d; checking up to %d additional candidates", len(valid), MIN_STORIES_PER_RUN, max(0, MAX_PROCESS_CANDIDATES - PRIMARY_PROCESS_CANDIDATES))
        try_process(candidates[PRIMARY_PROCESS_CANDIDATES:MAX_PROCESS_CANDIDATES])

    # Publication diversity: first one per source, then highest scoring fill, cap 3/source.
    valid.sort(key=lambda x: -float(x.get("publish_score", 0)))
    available_sources = sorted({safe_text(x.get("source")) or "Unknown Source" for x in valid})
    chosen = []
    chosen_canonicals = set()
    final_counts = Counter()
    for source in available_sources:
        for story in valid:
            if len(chosen) >= MAX_STORIES_PER_RUN:
                break
            if story.get("canonical") in chosen_canonicals or (safe_text(story.get("source")) or "Unknown Source") != source:
                continue
            chosen.append(story); chosen_canonicals.add(story.get("canonical")); final_counts[source] += 1
            break
    for story in valid:
        if len(chosen) >= MAX_STORIES_PER_RUN:
            break
        canonical = story.get("canonical")
        source = safe_text(story.get("source")) or "Unknown Source"
        if canonical in chosen_canonicals or final_counts[source] >= MAX_POSTS_PER_SOURCE_PER_RUN:
            continue
        chosen.append(story); chosen_canonicals.add(canonical); final_counts[source] += 1

    logger.info(
        "%s FINAL VALID: %d | target_min=%d target_max=%d | pool=%d processed=%d rejected=%d | sources=%s",
        region, len(chosen), MIN_STORIES_PER_RUN, MAX_STORIES_PER_RUN, len(pool), len(attempted), rejected, dict(final_counts)
    )
    if len(chosen) < MIN_STORIES_PER_RUN:
        logger.warning("%s below minimum target: valid=%d minimum=%d; no fabricated vacancies.", region, len(chosen), MIN_STORIES_PER_RUN)
    return chosen[:MAX_STORIES_PER_RUN]

def career_now():
    return datetime.now(BD_TZ)


def refresh_career_window():
    global NOW_BD, TODAY_START, YESTERDAY_START, DISCOVERY_START, DISCOVERY_END
    NOW_BD = career_now()
    TODAY_START = NOW_BD.replace(hour=0, minute=0, second=0, microsecond=0)
    YESTERDAY_START = TODAY_START - timedelta(days=1)
    DISCOVERY_START = NOW_BD - timedelta(hours=ROLLING_DISCOVERY_HOURS)
    DISCOVERY_END = NOW_BD + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)


def parse_date_text(raw):
    text = safe_text(raw).strip()
    if not text:
        return None
    text = text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    dt = parse_datetime(text)
    if dt:
        return dt.astimezone(BD_TZ)

    for name, month in EN_MONTHS.items():
        m = re.search(rf"\b(\d{{1,2}})\s+{re.escape(name)}(?:\s*,?\s*(\d{{4}}))?\b", text, re.I)
        if m:
            year = int(m.group(2) or career_now().year)
            try:
                return datetime(year, month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None
        m = re.search(rf"\b{re.escape(name)}\s+(\d{{1,2}}),?\s*(20\d{{2}})\b", text, re.I)
        if m:
            try:
                return datetime(int(m.group(2)), month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None

    for name, month in BANGLA_MONTHS.items():
        m = re.search(rf"(\d{{1,2}})\s*{re.escape(name)}\s*(\d{{4}})?", text)
        if m:
            year = int(m.group(2) or career_now().year)
            try:
                return datetime(year, month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None

    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if m:
        a, b, c = map(int, m.groups())
        year = c + 2000 if c < 100 else c
        for day, month in ((a, b), (b, a)):
            if 1 <= month <= 12:
                try:
                    return datetime(year, month, day, tzinfo=BD_TZ)
                except ValueError:
                    pass
    return None


def extract_deadline_candidates(text):
    text = safe_text(text)
    date_patterns = [
        r"\b(?:0?[1-9]|[12]\d|3[01])[\s./-](?:0?[1-9]|1[0-2])[\s./-](?:20\d{2}|\d{2})\b",
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember|t)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+20\d{2}\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember|t)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s*(?:জানুয়ারি|জানুয়ারি|ফেব্রুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর)\s*20\d{2}\b",
    ]
    date_re = re.compile("|".join(f"(?:{x})" for x in date_patterns), re.I)
    marker_re = re.compile(r"(deadline|last date|closing date|application deadline|apply by|আবেদনের শেষ|শেষ তারিখ|শেষ সময়|আবেদনের শেষ সময়|আবেদন শেষ)", re.I)
    found = []
    for m in date_re.finditer(text):
        raw = m.group(0)
        dt = parse_date_text(raw)
        if not dt:
            continue
        lo = max(0, m.start() - 180)
        hi = min(len(text), m.end() + 180)
        context = text[lo:hi]
        date_pos = m.start() - lo
        distances = [abs(date_pos - mm.start()) for mm in marker_re.finditer(context)]
        score = 2 if distances and min(distances) <= 150 else (1 if marker_re.search(context) else 0)
        found.append((score, dt, raw))
    found.sort(key=lambda x: (-x[0], x[1].timestamp()))
    return found


def choose_deadline(text):
    candidates = extract_deadline_candidates(text)
    return candidates[0][1] if candidates else None


def deadline_is_eligible(deadline_dt):
    """Compatibility helper. Deadline duration is scored, not hard-gated."""
    return bool(deadline_dt) and not deadline_is_expired(deadline_dt)


def deadline_grounded(deadline_text, article_text):
    out_dt = parse_date_text(deadline_text)
    if not out_dt:
        return False
    return any(dt.date() == out_dt.date() for _, dt, _ in extract_deadline_candidates(article_text))


def posted_at_from_html(page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    values = []
    for tag_name, attrs in [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"itemprop": "datePublished"}),
        ("meta", {"name": "datePublished"}),
        ("time", {"itemprop": "datePublished"}),
    ]:
        for tag in soup.find_all(tag_name, attrs=attrs):
            value = tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True)
            dt = parse_datetime(value)
            if dt:
                values.append(dt.astimezone(BD_TZ))
    for script in soup.find_all("script", type="application/ld+json"):
        raw = safe_text(script.string or script.get_text(" ", strip=True))
        if not raw:
            continue
        try:
            data = json.loads(raw)
            stack = data if isinstance(data, list) else [data]
            for obj in stack:
                if isinstance(obj, dict):
                    for key in ("datePosted", "datePublished", "dateCreated", "dateModified"):
                        dt = parse_datetime(obj.get(key, ""))
                        if dt:
                            values.append(dt.astimezone(BD_TZ))
        except Exception:
            continue
    return min(values) if values else None


def _extract_urls_from_text(text):
    urls = []
    for raw in re.findall(r"https?://[^\s<>)\"]+", safe_text(text)):
        url = raw.rstrip(".,;:)")
        if url not in urls:
            urls.append(url)
    return urls


def extract_apply_url(page_url, page_html):
    """Find a real application destination without ever using the source page as fallback."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    source_canonical = canonical_url(page_url)
    signals = re.compile(
        r"(apply now|apply here|apply online|submit application|application form|apply|application|আবেদন|আবেদন করুন|এখনই আবেদন|অনলাইনে আবেদন|আবেদনপত্র)",
        re.I,
    )
    scored = []

    def consider(candidate, score, label=""):
        href = safe_text(candidate).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        absolute = urljoin(page_url, href)
        if not re.match(r"^https?://", absolute, re.I):
            return
        if canonical_url(absolute) == source_canonical:
            return
        context = f"{label} {absolute}"
        extra = 2 if re.search(r"(apply|application|form|submit|career|recruit)", context, re.I) else 0
        scored.append((score + extra, absolute))

    # Explicit links and buttons.
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = safe_text(a.get("href"))
        parent_context = ""
        parent = getattr(a, "parent", None)
        if parent is not None:
            parent_context = safe_text(parent.get_text(" ", strip=True))[:700]
        score = 0
        if signals.search(label):
            score += 12
        if signals.search(parent_context):
            score += 7
        if re.search(r"(apply|application|form|submit|career|recruit|ats|workday|greenhouse|lever|smartrecruiters)", href, re.I):
            score += 6
        if re.search(r"(google\.com/forms|docs\.google\.com/forms|typeform|jotform)", href, re.I):
            score += 8
        if score:
            consider(href, score, f"{label} {parent_context}")

    # JavaScript application controls sometimes keep the destination in onclick/data attributes.
    for tag in soup.find_all(True):
        context = safe_text(tag.get_text(" ", strip=True))[:700]
        if not signals.search(context) and not any(safe_text(tag.get(k)) for k in ("data-apply-url", "data-application-url", "data-url", "data-href", "onclick")):
            continue
        for attr in ("data-apply-url", "data-application-url", "data-url", "data-href", "onclick"):
            raw = safe_text(tag.get(attr))
            if not raw:
                continue
            for embedded in re.findall(r"https?://[^\"'\s)]+", raw):
                consider(embedded, 11 if attr.startswith("data-") else 9, context)

    # Forms with an explicit submission action are strong evidence.
    for form in soup.find_all("form", action=True):
        label = form.get_text(" ", strip=True)
        action = safe_text(form.get("action"))
        score = 7 if signals.search(label) else 3
        consider(action, score, label)

    # Data attributes commonly used by JS application controls.
    for tag in soup.find_all(True):
        label = tag.get_text(" ", strip=True)
        if not signals.search(label):
            continue
        for attr in ("data-href", "data-url", "data-apply-url", "data-application-url", "data-action"):
            value = safe_text(tag.get(attr))
            if value:
                consider(value, 7, label)

    # Visible text can contain the actual application destination.
    for raw in _extract_urls_from_text(soup.get_text(" ", strip=True)):
        consider(raw, 6, "application")

    unique = {}
    for score, url in scored:
        unique[url] = max(score, unique.get(url, -1))
    ranked = sorted(unique.items(), key=lambda x: (-x[1], len(x[0])))
    return ranked[0][0] if ranked else ""


def parse_relative_listing_date(text, base=None):
    base = base or career_now()
    raw = safe_text(text).strip()
    normalized = raw.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    if re.search(r"\b(just now|now|এইমাত্র|এই মাত্র)\b", normalized, re.I):
        return base
    m = re.search(r"(\d+)\s*(minute|minutes|min|mins|ঘণ্টা|ঘন্টা|hour|hours|hr|hrs|day|days|দিন)\s*(ago|আগে)?", normalized, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith(("minute", "min")):
            return base - timedelta(minutes=n)
        if unit in {"ঘণ্টা", "ঘন্টা"} or unit.startswith(("hour", "hr")):
            return base - timedelta(hours=n)
        return base - timedelta(days=n)
    if re.search(r"\b(yesterday|গতকাল)\b", normalized, re.I):
        return base - timedelta(days=1)
    return parse_date_text(normalized)


def listing_published_date(text):
    raw = safe_text(text)
    for pattern in [
        r"(?:posted|published|date|added|প্রকাশিত|প্রকাশ|যোগ করা হয়েছে|পোস্ট করা হয়েছে)\s*[:\-]?\s*([^|•]+)",
        r"(?:posted on|published on)\s+([^|•]+)",
    ]:
        for match in re.finditer(pattern, raw, re.I):
            dt = parse_relative_listing_date(match.group(1), career_now())
            if dt:
                return dt
    return parse_relative_listing_date(raw, career_now())


def discover_direct_source(source):
    items = []
    try:
        response = session.get(source["url"], headers=HEADERS, timeout=8)
        if response.status_code >= 400:
            logger.warning("Direct portal %s returned %s", source["name"], response.status_code)
            return items
        soup = BeautifulSoup(response.text, "html.parser")
        anchors = soup.find_all("a", href=True)
        scored_links = []
        for anchor in anchors[:800]:
            title = safe_text(anchor.get_text(" ", strip=True))
            href = safe_text(anchor.get("href"))
            link = urljoin(response.url, href)
            if not title or len(title) < 6 or len(title) > 240 or not link or not primary_domain_allowed(link, "Career"):
                continue
            score = job_detail_link_score(link, title, source["name"])
            if score < 45:
                continue
            if any(link == x[1] for x in scored_links):
                continue
            container = anchor
            listing_text = title
            for _ in range(4):
                parent = getattr(container, "parent", None)
                if parent is None: break
                candidate_text = safe_text(parent.get_text(" ", strip=True))
                if len(candidate_text) >= 120:
                    listing_text = candidate_text[:4000]
                    break
                container = parent
            published_dt = listing_published_date(listing_text)
            deadline_dt = choose_deadline(listing_text)
            # For portals without per-card timestamps, use first_seen later instead of pretending the job was published now.
            item = {
                "title": title, "url": link, "canonical": canonical_url(link),
                "published_dt": published_dt.isoformat() if published_dt else "",
                "published_date": published_dt.isoformat() if published_dt else "",
                "source": source["name"], "source_type": source["type"], "region": "Career",
                "excerpt": listing_text, "image": "", "discovery": "direct_portal",
                "date_estimated": not bool(published_dt),
                "deadline_hint": deadline_dt.strftime("%d %B %Y") if deadline_dt else "",
                "job_detail_score": score,
            }
            if not candidate_basic_allowed(item):
                continue
            scored_links.append((score, link, item))
        scored_links.sort(key=lambda x: (-x[0], -len(x[2].get("excerpt", ""))))
        for _, _, item in scored_links[:35]:
            items.append(item)
    except Exception as exc:
        logger.warning("Direct portal discovery failed %s: %s", source["name"], exc)
    return items


def direct_portal_gap_fill():
    added = 0
    source_counts = Counter()
    # Sequential source access keeps the session predictable; fail-fast HTTP policy keeps it bounded.
    for source in DIRECT_JOB_SOURCES:
        items = discover_direct_source(source)
        for item in items:
            canonical = item["canonical"]
            if canonical in POSTED_URLS:
                continue
            before = canonical in STATE["queue"]
            queue_candidate(item)
            if not before:
                added += 1
            source_counts[source["name"]] += 1
    logger.info("DIRECT SOURCE INVENTORY: %s", dict(source_counts))
    return added

def run():
    global NOW_BD, TODAY_START, YESTERDAY_START, DISCOVERY_START, DISCOVERY_END, AI_RECORD_FALLBACKS_USED, EXA_CONTENT_FALLBACKS_USED
    refresh_career_window()
    AI_RECORD_FALLBACKS_USED = 0
    EXA_CONTENT_FALLBACKS_USED = 0
    logger.info("%s | channel=%s | window=%s -> %s | target=%d-%d", VERSION_NAME, TELEGRAM_CHANNEL, DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat(), MIN_STORIES_PER_RUN, MAX_STORIES_PER_RUN)

    prune_state()
    rss_count = collect_rss()
    direct_count = direct_portal_gap_fill()
    google_count = google_news_gap_fill("Career", 0, MIN_STORIES_PER_RUN)
    exa_count = exa_gap_fill("Career", 0, MIN_STORIES_PER_RUN)
    logger.info("DISCOVERY rss=%d direct=%d google=%d exa=%d", rss_count, direct_count, google_count, exa_count)
    save_state(STATE)

    candidates = available_candidates("Career")
    logger.info("CAREER INVENTORY=%d", len(candidates))
    ranked = prepare_ranked_region("Career", candidates)
    logger.info("CAREER UNIQUE/RANKED=%d", len(ranked))
    for item in ranked[:15]:
        logger.info("RANK #%s score=%s source=%s title=%s", item.get("editor_rank","?"), item.get("importance_score",0), item.get("source"), item.get("title",""))

    stories = process_ranked_region("Career", ranked)
    logger.info("CAREER FINAL STORIES=%d", len(stories))

    published_count = 0
    for index, story in enumerate(stories, start=1):
        rich_html = fit_rich_html(story)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.error("Rich message exceeds Telegram limit: %s", story.get("headline")); continue
        image_path = prepare_image(story, index)
        reply_markup = _inline_keyboard(story)
        if not reply_markup:
            logger.error("SKIP publish: no verified Apply URL for %s", story.get("headline")); continue
        result = send_rich_message(image_path, rich_html, reply_markup=reply_markup)
        if not result.get("ok"):
            logger.warning("Rich Message publish failed; using Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, rich_html, reply_markup=reply_markup)
        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            canonical = story.get("canonical") or canonical_url(story.get("source_url", ""))
            if canonical:
                POSTED_URLS.add(canonical); save_posted_url(canonical)
                queue_item = STATE["queue"].get(canonical)
                if queue_item:
                    queue_item["status"] = "posted"; queue_item["posted_at"] = now_iso()
            store_event(story, published=True, message_id=message_id)
            remember_posted_event(story)
            update_category_coverage(story)
            STATE["recent_titles"].append(normalize_title(story.get("headline", "")))
            logger.info("Published %d/%d: %s | source=%s", published_count, len(stories), story.get("headline",""), story.get("source",""))
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)
    save_state(STATE)
    logger.info("Finished. Published=%d/%d | AI record fallbacks=%d", published_count, len(stories), AI_RECORD_FALLBACKS_USED)

def self_test() -> bool:
    print("[SELF-TEST] CareerNewsroom validation starting...")
    assert TELEGRAM_CHANNEL == "@CareerNewsroom"
    assert MIN_STORIES_PER_RUN == 5
    assert MAX_STORIES_PER_RUN == 15
    assert DISCOVERY_LOOKBACK_HOURS == 72
    assert MAX_POSTS_PER_SOURCE_PER_RUN == 3
    assert AI_RANK_INPUT_LIMIT == 30
    assert AI_RANK_BATCH_SIZE == 30
    assert AI_RANK_MAX_BATCHES == 1
    assert ENABLE_AI_CLAIM_VERIFY is False

    now = datetime(2026, 9, 18, 12, 0, tzinfo=BD_TZ)
    assert deadline_is_eligible(datetime(2020, 1, 1, tzinfo=BD_TZ)) is False
    assert deadline_is_eligible(now + timedelta(days=1)) is True

    bdjobs_url = "https://jobs.bdjobs.com/jobdetails.asp?id=123456"
    assert job_detail_link_score(bdjobs_url, "Management Trainee Officer", "Bdjobs") >= 70
    utility_url = "https://jobs.bdjobs.com/jobsearch.asp?fcatid=1"
    assert job_detail_link_score(utility_url, "Career Tools", "Bdjobs") < 70

    sample_html = '''
    <html><head>
      <meta property="og:site_name" content="Example Bank PLC"/>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee Officer","hiringOrganization":{"@type":"Organization","name":"Example Bank PLC"},"jobLocation":{"address":{"addressLocality":"Dhaka","addressCountry":"BD"}},"validThrough":"2026-10-10T23:59:00+06:00","datePosted":"2026-09-17T10:00:00+06:00"}
      </script>
    </head><body>
      <h1>Management Trainee Officer</h1>
      <div>Company: Example Bank PLC</div>
      <div>Location: Gulshan, Dhaka</div>
      <div>Education: BBA / MBA</div>
      <div>Experience: Fresh graduates may apply</div>
      <div>Application: Online</div>
      <div>Deadline: 10 October 2026</div>
      <a href="https://jobs.examplebank.com/apply/mt-2026" aria-label="Apply Now">Apply Now</a>
      <a href="https://careers.examplebank.com/jobs/mt-2026">Job Details</a>
    </body></html>
    '''
    article = {
        "url": bdjobs_url, "headline": "Management Trainee Officer",
        "text": "Management Trainee Officer\nCompany: Example Bank PLC\nLocation: Gulshan, Dhaka\nEducation: BBA / MBA\nExperience: Fresh graduates may apply\nApplication: Online\nDeadline: 10 October 2026",
        "html": sample_html, "source": "Bdjobs", "site_name": "Bdjobs",
        "published_date": now - timedelta(days=1), "date_estimated": False,
        "first_seen": now - timedelta(days=1), "image_url": ""
    }
    apply_url = extract_apply_url(bdjobs_url, sample_html)
    assert apply_url == "https://jobs.examplebank.com/apply/mt-2026"
    assert canonical_url(apply_url) != canonical_url(bdjobs_url)
    hints = dict(article)
    hint_text = extract_page_structured_hints(sample_html, bdjobs_url, hints)
    assert hints.get("job_title_hint") == "Management Trainee Officer"
    assert hints.get("company_hint") == "Example Bank PLC"
    assert hints.get("deadline_hint")
    hints["apply_url"] = apply_url
    record = local_job_record(hints, hint_text)
    assert record["job_title"] == "Management Trainee Officer"
    assert record["company"] == "Example Bank PLC"
    assert record["apply_url"] == apply_url
    assert record["source_url"] == bdjobs_url
    assert record["apply_url"] != record["source_url"]
    assert "gender" not in record

    story = dict(record)
    story.update({"headline": record["job_title"], "hashtags": "#CareerNewsroom #BankingJob", "deadline": "10 October 2026", "application_method": "Online", "vacancies": "01", "source_name": "Bdjobs", "location": "Gulshan, Dhaka"})
    html = dynamic_rich_html(story)
    assert "<tg-button" not in html and "<table" in html
    assert "APPLY NOW" not in html
    assert apply_url not in html
    assert "Gender" not in html and "gender" not in html
    assert "JOB SNAPSHOT" in html and "Official Source" in html
    markup = _inline_keyboard(story)
    assert markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    assert markup["inline_keyboard"][0][0]["url"] == apply_url

    q = []
    for i, source in enumerate(["Bdjobs", "Dohaj", "BDJobs Live", "ProjobsBD", "Smart Job"] * 3):
        q.append({"id": f"e{i}", "title": f"Management Trainee {i}", "company": f"Company {i}", "url": f"https://example.com/job/{i}", "source": source, "domain": "example.com", "published_date": now - timedelta(hours=min(i, 48)), "deadline": now + timedelta(days=10+i), "first_seen": now - timedelta(hours=min(i, 48)), "job_relevance": 90-i, "source_confidence": 90, "career_value": 90, "data_completeness": 90, "deadline_score": 80, "freshness_score": 90, "local_score": 90-i})
    selected = build_candidate_pool(q, 15)
    counts = Counter(x.get("source") for x in selected)
    assert max(counts.values()) <= 3 and len(selected) >= 10

    ok, issues = claims_grounded(story, article["text"])
    assert ok and not issues
    print("[SELF-TEST] PASS")
    return True

def visible_text_for_test(
    rendered,
):
    text = re.sub(
        r"<[^>]+>",
        "",
        rendered,
    )
    return html.unescape(
        text
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        run()
