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

VALID_NEWS_MODES = {"update"}
if NEWS_MODE not in VALID_NEWS_MODES:
    raise ValueError(f"Invalid NEWS_MODE={NEWS_MODE!r}; expected one of {sorted(VALID_NEWS_MODES)}")

CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")

POSTED_FILE = "posted_urls.txt"
SOURCE_HEALTH_FILE = "source_health.json"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# Same runtime/selection framework as the reference bot.
MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 15
RANKING_POOL_SIZE = 60
MAX_PROCESS_CANDIDATES = 60
RESCUE_PROCESS_CANDIDATES = 36
MAX_POSTS_PER_SOURCE_PER_RUN = 3
DISCOVERY_LOOKBACK_HOURS = 72
DEADLINE_SOFT_TARGET_DAYS = 7
DEADLINE_URGENT_DAYS = 3
MIN_PUBLISH_SCORE = 58

POST_DELAY_SECONDS = 3.5
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 5
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES = 240
MAX_EXA_CANDIDATES = 60
MAX_GOOGLE_NEWS_CANDIDATES = 40
MAX_EXCERPT_ENRICH = 10
THIN_EXCERPT_CHARS = 150
MAX_RICH_CHARACTERS = 32768
MAX_SOURCE_PER_RUN = 99
SOURCE_DIVERSITY_TARGET = 4
MIN_JOB_INTENT_SCORE = 34
MAX_LOCAL_PREFILTER = 110
MAX_DETAILED_CANDIDATES = 42
CEREBRAS_RANK_BATCH_SIZE = 20
CEREBRAS_MIN_INTERVAL_SECONDS = 0.75
REQUIRE_DISTINCT_APPLY_URL = True
SOURCE_FAILURE_COOLDOWN_MINUTES = 90
TARGETED_LANE_LIMIT = 4

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
    # High-value primary portals. Their listing pages are parsed only for real detail links.
    {"name": "Bdjobs", "url": "https://jobs.bdjobs.com/jobsearch-cache.asp", "type": "job_portal", "mode": "bdjobs_listing", "new_feed": True},
    {"name": "BDJobs Live", "url": "https://www.bdjobslive.com/", "type": "job_portal", "mode": "bdjobslive_listing", "new_feed": True},
    {"name": "BDJobs Live", "url": "https://www.bdjobslive.com/bdjobs-circular/new-job-circular-in-bangladesh", "type": "job_portal", "mode": "bdjobslive_listing", "new_feed": True},
    {"name": "BDJobs Live", "url": "https://www.bdjobslive.com/bdjobs-circular/internship-opportunity", "type": "job_portal", "mode": "bdjobslive_listing", "new_feed": True},
    {"name": "BDJobs Live", "url": "https://www.bdjobslive.com/bdjobs-circular/fresher-jobs", "type": "job_portal", "mode": "bdjobslive_listing", "new_feed": True},
    {"name": "Dohaj", "url": "https://dohaj.com/jobs", "type": "job_portal", "mode": "generic_listing", "new_feed": True},
    {"name": "Job.com.bd", "url": "https://job.com.bd/jobs/new_jobs/", "type": "job_portal", "mode": "jobcombd_listing", "new_feed": True},
    {"name": "Smart Job", "url": "https://smartjob.portal.gov.bd/", "type": "official_job_portal", "mode": "generic_listing", "new_feed": True},
    {"name": "Alljobs Teletalk", "url": "https://alljobs.teletalk.com.bd/", "type": "official_job_portal", "mode": "generic_listing", "new_feed": True},
    {"name": "BPSC", "url": "https://bpsc.gov.bd/", "type": "official_job_portal", "mode": "generic_listing", "new_feed": True},
    {"name": "BCC e-Recruitment", "url": "https://erecruitment.bcc.gov.bd/", "type": "official_job_portal", "mode": "generic_listing", "new_feed": True},
    {"name": "JobsNoticeBD", "url": "https://jobsnoticebd.com/", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "JobsInfo", "url": "https://jobsinfo.bd/", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "JobFeeds", "url": "https://jobfeeds.online/", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "CircularBD", "url": "https://www.circularbd.com/alljobs", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "Dhaka Post", "url": "https://www.dhakapost.com/jobs-career/", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "Dhaka Tribune", "url": "https://bangla.dhakatribune.com/jobs", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "Bangla Tribune", "url": "https://www.banglatribune.com/jobs", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "JagoNews24", "url": "https://www.jagonews24.com/topic/%E0%A6%9A%E0%A6%BE%E0%A6%95%E0%A6%B0%E0%A6%BF", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
    {"name": "Prothom Alo", "url": "https://www.prothomalo.com/collection/chakri-all", "type": "news_jobs", "mode": "news_listing", "new_feed": True},
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

retry_policy = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)

adapter = HTTPAdapter(
    max_retries=retry_policy,
    pool_connections=20,
    pool_maxsize=20,
)

session.mount("https://", adapter)
session.mount("http://", adapter)


# ============================================================
# HELPERS
# ============================================================

# Audience-targeted discovery lanes. These are discovery queries, not facts.
TARGETED_JOB_LANES = [
    ("BBA/MBA & Business", "BBA MBA business administration management jobs Bangladesh"),
    ("Freshers & Graduate", "fresher fresh graduate entry level jobs Bangladesh"),
    ("Internship", "internship intern jobs Bangladesh students graduates"),
    ("Management Trainee", "management trainee graduate trainee jobs Bangladesh"),
    ("Accounting & Finance", "accounting finance accounts audit jobs Bangladesh"),
    ("Banking", "banking bank relationship officer jobs Bangladesh"),
    ("HR", "human resources HR jobs Bangladesh"),
    ("Marketing & Sales", "marketing sales brand jobs Bangladesh"),
    ("Business Development", "business development BD executive jobs Bangladesh"),
    ("Management & Admin", "management administration executive officer jobs Bangladesh"),
    ("Operations & Supply Chain", "operations supply chain procurement jobs Bangladesh"),
    ("NGO & Development", "NGO development project jobs Bangladesh"),
    ("Customer Service", "customer service relationship officer jobs Bangladesh"),
]


def safe_text(value):
    return "" if value is None else str(value).strip()


def canonical_url(url):
    raw = safe_text(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("amp.")
    path = parsed.path or "/"
    path = re.sub(r"/amp$", "", path, flags=re.I)
    path = re.sub(r"\.amp$", "", path, flags=re.I)
    path = re.sub(r"/{2,}", "/", path)
    path = path.rstrip("/") or "/"
    tracking = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","gclid","fbclid","mc_cid","mc_eid","ref","source"}
    keep=[]
    try:
        from urllib.parse import parse_qsl, urlencode
        for key,val in parse_qsl(parsed.query, keep_blank_values=False):
            lk=key.lower()
            if lk in tracking or lk.startswith("utm_"):
                continue
            keep.append((key,val))
        query=urlencode(sorted(keep), doseq=True)
    except Exception:
        query=parsed.query
    return f"{host}{path}" + (f"?{query}" if query else "")

def normalize_title(title):
    text = safe_text(title).casefold()
    # Keep Unicode letters/numbers so Bangla titles remain comparable.
    text = re.sub(r"[^\w\s\u0980-\u09FF]+", " ", text, flags=re.UNICODE)
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
        "source_health_file": SOURCE_HEALTH_FILE,
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


# ============================================================
# SOURCE HEALTH
# ============================================================

def load_source_health():
    try:
        with open(SOURCE_HEALTH_FILE, "r", encoding="utf-8") as f:
            data=json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_source_health(data):
    tmp=SOURCE_HEALTH_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
    os.replace(tmp,SOURCE_HEALTH_FILE)


SOURCE_HEALTH = load_source_health()


def source_health_key(source, url=""):
    return safe_text(source) or source_name(url)


def source_is_in_cooldown(source, url=""):
    key=source_health_key(source,url)
    row=SOURCE_HEALTH.get(key,{})
    until=parse_datetime(row.get("cooldown_until"))
    return bool(until and until > career_now())


def record_source_health(source, url, status, error=""):
    key=source_health_key(source,url)
    row=SOURCE_HEALTH.setdefault(key,{"source":key,"successes":0,"failures":0})
    row["last_checked"]=now_iso()
    code = int(status) if isinstance(status,(int,float)) else 0
    if code and 200 <= code < 400:
        row["successes"]=int(row.get("successes",0))+1
        row["last_status"]=code
        row["consecutive_failures"]=0
        row["cooldown_until"]=""
    else:
        row["failures"]=int(row.get("failures",0))+1
        row["consecutive_failures"]=int(row.get("consecutive_failures",0))+1
        row["last_status"]=code
        row["last_error"]=safe_text(error)[:300]
        if row["consecutive_failures"] >= 2:
            row["cooldown_until"]=(career_now()+timedelta(minutes=SOURCE_FAILURE_COOLDOWN_MINUTES)).isoformat()
    save_source_health(SOURCE_HEALTH)


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

DISCOVERY_TARGET_PER_REGION = 60


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
# CANDIDATE FILTERING
# ============================================================

BAD_PATH_RE = re.compile(
    r"/(opinion|editorial|sponsored|tag|topic|live-blog|liveblog|photo|photos|video|help|login|signin|sign-in|register|employer|profile|account|about|contact|service|faq|career-hub|blog)(/|$)",
    re.I,
)

BAD_TITLE_RE = re.compile(
    r"\b(sponsored|advertisement|promo|opinion|editorial|help center|employer login|candidate login|sign in|sign up|register|profile|showcase your company|bdjobs (?:ios|android) app|ios app|android app|download app|post a job|my account|career hub|resume writing|cover letter|interview tips)\b",
    re.I,
)

GENERIC_UI_RE = re.compile(
    r"\b(navigation menu|filter by category|filter by location|search for|keywords|list your skills|your experience|and more|one place|has become easier|showcase your company|download.*app|post a job|employer login|help center|career guide|resume writing|cover letter|interviewing tips)\b",
    re.I,
)

JOB_DETAIL_PATTERNS = {
    "Bdjobs": re.compile(r"/jobdetails(?:bn)?(?:\.asp|/?)\?(?:[^#]*?&)?id=\d+", re.I),
    "BDJobs Live": re.compile(r"/bdjobs-details/[^/?#]+", re.I),
    "Job.com.bd": re.compile(r"/jobs/details/\?i=\d+", re.I),
}

JOB_SIGNAL_RE = re.compile(
    r"(job|jobs|vacancy|vacancies|career|careers|recruit|recruitment|hiring|apply|internship|intern|trainee|management trainee|fresher|fresh graduate|graduate|circular|নিয়োগ|চাকরি|শূন্যপদ|আবেদন|নিয়োগ বিজ্ঞপ্তি|চাকরির বিজ্ঞপ্তি|ইন্টার্নশিপ)",
    re.I,
)
BAD_JOB_RE = re.compile(
    r"(job tips|career advice|how to prepare|exam result|admission|scholarship|job fair|training course|workshop|seminar|career guide|salary guide|interview tips|cv tips|resume tips|career resources|career hub|help center|employer login|app download|ios app|android app)",
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


def is_generic_navigation_candidate(title, url, excerpt=""):
    blob = f"{title} {excerpt}".strip()
    path = urlparse(url).path.lower()
    if BAD_TITLE_RE.search(title or ""):
        return True
    if GENERIC_UI_RE.search(blob):
        return True
    if BAD_PATH_RE.search(path):
        return True
    return False


def looks_like_job_detail_url(source_name_value, url):
    source = safe_text(source_name_value)
    pattern = JOB_DETAIL_PATTERNS.get(source)
    if pattern:
        return bool(pattern.search(url))
    path = urlparse(url).path.lower()
    if source in {"Dohaj"}:
        return path.startswith("/jobs/") and path.count("/") >= 2 and not path.rstrip("/").endswith("jobs")
    return False


def job_intent_score(item):
    title = safe_text(item.get("title"))
    excerpt = safe_text(item.get("excerpt"))
    blob = f"{title} {excerpt}"
    if is_generic_navigation_candidate(title, item.get("url", ""), excerpt):
        return 0
    score = 0
    if JOB_SIGNAL_RE.search(title): score += 42
    if re.search(r"(management trainee|trainee|internship|intern|fresher|fresh graduate|vacancy|position|officer|executive|manager|assistant|analyst|coordinator)", title, re.I):
        score += 18
    for pat in (
        r"company|organization|employer|প্রতিষ্ঠান|সংস্থা",
        r"deadline|last date|application deadline|আবেদনের শেষ|শেষ তারিখ",
        r"salary|বেতন|vacancy|vacancies|খালি পদ|শূন্যপদ",
        r"education|qualification|experience|যোগ্যতা|অভিজ্ঞতা",
        r"apply|আবেদন",
    ):
        if re.search(pat, blob, re.I):
            score += 7
    if BAD_JOB_RE.search(blob): score -= 60
    return max(0, min(100, score))


def candidate_basic_allowed(item):
    title = safe_text(item.get("title"))
    url = safe_text(item.get("url"))
    excerpt = safe_text(item.get("excerpt"))
    if not title or not url or not canonical_url(url):
        return False
    if is_generic_navigation_candidate(title, url, excerpt):
        return False

    published_dt = parse_datetime(item.get("published_date", item.get("published_dt", "")))
    estimated = bool(item.get("date_estimated"))
    if not published_dt:
        return False
    if not estimated and not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
        return False
    if estimated:
        # Estimated timestamps are accepted only for curated current job-list pages.
        if item.get("source_type") not in {"job_portal", "official_job_portal", "news_jobs"} or not item.get("new_feed"):
            return False
        if published_dt < DISCOVERY_START:
            return False
    if not allowed_source_for_region(url, "Career"):
        return False

    blob = f"{title} {excerpt}"
    intent = job_intent_score(item)
    item["job_intent_score"] = intent
    if intent < MIN_JOB_INTENT_SCORE:
        return False

    overseas = OVERSEAS_RE.search(blob)
    bangladesh = BANGLADESH_RE.search(blob)
    official = item.get("source_type") == "official_job_portal"
    known_bd_portal = normalized_domain(url) in set(CAREER_JOB_PORTAL_DOMAINS)
    title_overseas = bool(OVERSEAS_RE.search(title))
    location_foreign = bool(re.search(r"(location|workplace|কর্মস্থল|job location|based in).{0,110}" + OVERSEAS_RE.pattern, blob, re.I | re.S))
    if title_overseas and not bangladesh:
        return False
    if location_foreign and not bangladesh:
        return False
    if overseas and not (bangladesh or official or known_bd_portal):
        return False
    if not (bangladesh or official or known_bd_portal):
        return False

    # For portal candidates, only accept actual job detail links, never navigation links.
    if item.get("discovery") == "direct_portal" and item.get("source_type") in {"job_portal", "official_job_portal"}:
        if not looks_like_job_detail_url(item.get("source", ""), url):
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
    canonical = item["canonical"]

    existing = STATE["queue"].get(
        canonical
    )

    if existing:
        existing.update(
            {
                "last_seen": now_iso(),
                "source_url": item.get("source_url") or existing.get("source_url") or item.get("url"),
                "apply_url": item.get("apply_url") or existing.get("apply_url", ""),
                "image": (
                    item.get("image")
                    or existing.get("image", "")
                ),
            }
        )
        return

    STATE["queue"][canonical] = {
        **item,
        "source_url": item.get("source_url") or item.get("url"),
        "status": "pending",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
        "attempt_count": 0,
        "verification_state": "pending",
        "published_to_channel": False,
        "score": float(item.get("importance_score", item.get("local_score", 0)) or 0),
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
            timeout=20,
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


def google_news_gap_fill(region, existing_count, needed):
    if existing_count >= max(6, needed * 3):
        return 0

    queries = [
        "Bangladesh job circular recruitment vacancy",
        "Bangladesh government job circular recruitment",
        "Bangladesh private company hiring vacancy",
        "Bangladesh bank NGO education IT jobs",
        "Bangladesh internship management trainee graduate job",
        "Bangladesh recruitment notice application deadline",
    ]
    hl, gl, ceid = ("en-US", "BD", "BD:en")
    added = 0

    for query in queries:
        try:
            feed_url = (
                "https://news.google.com/rss/search?q="
                + quote(f"{query} when:3d")
                + f"&hl={hl}&gl={gl}&ceid={ceid}"
            )
            response = session.get(feed_url, timeout=15, headers=HEADERS)
            if response.status_code >= 400:
                continue
            parsed = feedparser.parse(response.content)

            for entry in parsed.entries[:8]:
                title = safe_text(entry.get("title"))
                link = safe_text(entry.get("link"))
                if not title or not link:
                    continue
                real_url = resolve_google_news_url(link)
                if not real_url or not primary_domain_allowed(real_url, region):
                    continue
                published_dt = feed_entry_datetime(entry)
                if not published_dt:
                    continue

                raw_summary = BeautifulSoup(safe_text(entry.get("summary")), "html.parser").get_text(" ", strip=True)
                item = {
                    "title": title,
                    "url": real_url,
                    "canonical": canonical_url(real_url),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(real_url),
                    "source_type": "google_news",
                    "region": "Career",
                    "excerpt": raw_summary[:2200],
                    "image": "",
                    "discovery": "google_news",
                    "date_estimated": False,
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Google News career gap fill failed: %s", exc)
    return added



def targeted_portal_lane_gap_fill(region="Career", max_total=36):
    """Use Exa for audience-specific discovery lanes, then keep only resolvable Bangladesh job detail URLs."""
    if max_total <= 0:
        return 0
    added=0
    for lane,query in TARGETED_JOB_LANES:
        if added >= max_total: break
        try:
            results=get_exa().search_and_contents(
                f"{query} latest within 3 days",
                type="auto", category="news", num_results=TARGETED_LANE_LIMIT,
                include_domains=PRIMARY_CAREER_DOMAINS,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights":{"max_characters":900}},
            )
            for result in getattr(results,"results",[]) or []:
                url=safe_text(getattr(result,"url","")); title=safe_text(getattr(result,"title",""))
                if not url or not title: continue
                final=url
                # If Exa resolved a generic publisher/news page, its URL is still acceptable only when it passes normal job filters.
                published=parse_datetime(getattr(result,"published_date","")) or career_now()
                highlights=getattr(result,"highlights",[]) or []
                excerpt=" ".join(safe_text(x) for x in highlights)[:2200]
                item={"title":title,"url":final,"source_url":final,"canonical":canonical_url(final),"published_date":published.isoformat(),"published_dt":published.isoformat(),"source":source_name(final),"source_type":"exa","region":region,"excerpt":excerpt,"image":safe_text(getattr(result,"image","")),"discovery":"exa_lane","discovery_lane":lane,"date_estimated":False}
                # For direct portals, enforce detail paths. For news publishers, job-intent + source extraction will decide.
                if normalized_domain(final) in {"jobs.bdjobs.com","bdjobslive.com","www.bdjobslive.com","job.com.bd"} and not looks_like_job_detail_url(item["source"],final):
                    continue
                if not candidate_basic_allowed(item): continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]: continue
                queue_candidate(item); added+=1
                if added>=max_total: break
        except Exception as exc:
            logger.warning("Targeted lane failed [%s]: %s",lane,exc)
    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(12, needed * 3):
        return 0

    domains = FALLBACK_CAREER_DOMAINS if fallback else PRIMARY_CAREER_DOMAINS
    if not domains:
        return 0

    queries = [
        "latest Bangladesh job circular recruitment vacancy deadline",
        "latest Bangladesh government recruitment job circular vacancy",
        "latest Bangladesh private company hiring vacancy job",
        "latest Bangladesh bank NGO education IT recruitment vacancy",
        "latest Bangladesh internship management trainee graduate vacancy",
        "latest Bangladesh job recruitment notice application deadline",
    ]

    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query,
                type="auto",
                category="news",
                num_results=8,
                include_domains=domains,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 1000}},
            )
            for result in results.results:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt:
                    continue
                if fallback:
                    if not fallback_domain_allowed(url, region):
                        continue
                elif not primary_domain_allowed(url, region):
                    continue

                raw_highlights = getattr(result, "highlights", None) or []
                if not isinstance(raw_highlights, list):
                    raw_highlights = [raw_highlights]
                excerpt = " ".join(
                    safe_text(value) for value in raw_highlights if safe_text(value)
                )[:2200]

                item = {
                    "title": title,
                    "url": url,
                    "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(url),
                    "source_type": "exa",
                    "region": "Career",
                    "excerpt": excerpt,
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_fallback" if fallback else "exa",
                    "source_pool": "fallback" if fallback else "primary",
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa %s discovery failed: %s", "fallback" if fallback else "primary", exc)
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

def freshness_score(published_dt):
    if not published_dt: return 20
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
       accounting|finance|accounts|audit|bank|banking|marketing|sales|hr|human resources|
       business development|operations|supply chain|procurement|customer service|analyst|admin|management)""",
    re.I | re.X,
)

CAREER_AUDIENCE_WEAK_RE = re.compile(
    r"(executive|officer|assistant|associate|coordinator|relationship|support)",
    re.I,
)

def audience_fit_score(item):
    raw = f"{item.get('title','')} {item.get('excerpt','')}"
    strong = len(CAREER_AUDIENCE_STRONG_RE.findall(raw))
    weak = len(CAREER_AUDIENCE_WEAK_RE.findall(raw))
    priority = 0
    if re.search(r"(management trainee|graduate trainee|internship|intern|fresher|fresh graduate)", raw, re.I): priority += 18
    if re.search(r"(bba|mba|business administration|accounting|finance|bank|banking|marketing|sales|hr|human resources|business development|operations|supply chain|procurement)", raw, re.I): priority += 15
    if strong >= 3: base=90
    elif strong == 2: base=84
    elif strong == 1: base=76 if weak else 74
    else: base=58 if weak else 42
    return min(100, base+priority)

def local_job_score(item):
    published=parse_datetime(item.get("published_date"))
    deadline=parse_date_text(item.get("deadline_hint","")) or choose_deadline(item.get("excerpt",""))
    raw = f"{item.get('title','')} {item.get('excerpt','')}"
    parts={
        "freshness":freshness_score(published),
        "deadline":deadline_status_score(deadline),
        "source":source_reliability_score(item),
        "completeness":completeness_score(item),
        "job_signal":job_intent_score(item),
        "audience_fit":audience_fit_score(item),
    }
    score=round(
        .20*parts["freshness"]
        + .15*parts["deadline"]
        + .17*parts["source"]
        + .12*parts["completeness"]
        + .18*parts["job_signal"]
        + .18*parts["audience_fit"]
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
    topic_list=", ".join(TOPICS["Career"])
    return f"""You rank real Bangladesh job vacancies for @CareerNewsroom. Return every candidate. Do not invent facts.
Deadline and freshness are ranking factors, not hard gates. Only clearly expired vacancies are rejected later.
Score 0-100 using applicant relevance, career value, source quality, freshness, deadline usefulness, completeness, vacancy quality and fit for early-career users aged roughly 20-30. Give extra weight to BBA/MBA, business, finance/accounting, banking, marketing/sales, HR, management trainee, graduate and internship roles. Prefer official recruitment pages and original employer/job-portal listings. Reject advice, exam results, scholarships without vacancies, training-only offers and generic commentary. Do not treat nationality wording as a requirement; the channel already serves its Bangladesh audience. Return id, rank, score, relevance, career_value, reason, topic, institution, event_key. Allowed topics: {topic_list}"""


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
            max_completion_tokens=3500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []


def rank_candidates(candidates, region):
    if not candidates:
        return []
    # Stage 1: cheap local pre-rank. This removes generic/low-fit portal noise before AI.
    scored=[]
    for item in candidates:
        local,parts=local_job_score(item)
        item=dict(item)
        item.update({"local_score":local,"score_components":parts})
        scored.append(item)
    scored.sort(key=lambda x:(-x["local_score"],-(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0)))
    regional=scored[:MAX_LOCAL_PREFILTER]
    logger.info("LOCAL PREFILTER: %d/%d",len(regional),len(candidates))

    rows_by_url={}
    last_cerebras_call=0.0
    for offset in range(0,len(regional),CEREBRAS_RANK_BATCH_SIZE):
        batch=regional[offset:offset+CEREBRAS_RANK_BATCH_SIZE]
        wait=max(0.0,CEREBRAS_MIN_INTERVAL_SECONDS-(time.monotonic()-last_cerebras_call))
        if wait: time.sleep(wait)
        rows=_rank_batch(batch,region,offset//CEREBRAS_RANK_BATCH_SIZE+1)
        last_cerebras_call=time.monotonic()
        by_id={i:x for i,x in enumerate(batch,1)}
        for row in rows:
            try: idx=int(row.get("id"))
            except Exception: continue
            if idx not in by_id: continue
            item=dict(by_id[idx]); local=item["local_score"]; parts=item["score_components"]
            ai=max(0,min(100,int(row.get("score",local))))
            item.update({
                "importance_score":round(.74*ai+.26*local),"ai_score":ai,
                "topic":canonical_topic(safe_text(row.get("topic")),region),
                "institution":safe_text(row.get("institution")),
                "event_key":safe_text(row.get("event_key")) or normalize_title(f"{item.get('title','')} {item.get('source','')}"),
                "rank_reason":safe_text(row.get("reason")),
                "relevance_score":int(row.get("relevance",local)),
                "career_value_score":int(row.get("career_value",local)),
                "batch_rank":int(row.get("rank",9999)),
            })
            rows_by_url[item.get("canonical")]=item

    # Complete AI failures with deterministic ranking so one 429 cannot erase the run.
    for original in regional:
        if original.get("canonical") in rows_by_url:
            continue
        item=dict(original); local=item["local_score"]
        item.update({
            "importance_score":local,"ai_score":None,
            "topic":canonical_topic(item.get("topic"),region),
            "institution":safe_text(item.get("institution")),
            "event_key":normalize_title(f"{item.get('title','')} {item.get('source','')}"),
            "rank_reason":"Deterministic ranking fallback; AI ranking unavailable or incomplete.",
            "relevance_score":local,"career_value_score":local,"batch_rank":9999,
        })
        rows_by_url[item.get("canonical")]=item

    # Candidates outside the local prefilter are retained only as reserve inventory.
    ranked=list(rows_by_url.values())
    ranked.sort(key=lambda x:(-x.get("importance_score",0),-x.get("local_score",0),-(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0)))
    for i,item in enumerate(ranked,1): item["editor_rank"]=i
    logger.info("%s RANKED RETURNED: %d/%d",region,len(ranked),len(regional))
    return ranked


# ============================================================
# VERSION 1 EVENT DEDUPLICATION
# ============================================================

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



def distinct_http_url(candidate, source_url):
    candidate = safe_text(candidate).strip()
    source_url = safe_text(source_url).strip()
    if not re.match(r"^https?://", candidate, re.I):
        return ""
    try:
        c = canonical_url(candidate)
        s = canonical_url(source_url)
        if not c or c == s:
            return ""
        return candidate
    except Exception:
        return ""


def extract_urls_near_apply(text):
    raw = safe_text(text)
    urls = re.findall(r"https?://[^\s<>()\"']+", raw, re.I)
    scored = []
    for url in urls:
        clean = url.rstrip(".,);]}>")
        pos = raw.lower().find(clean.lower())
        window = raw[max(0, pos-260):pos+len(clean)+260] if pos >= 0 else raw
        score = 0
        if re.search(r"(apply|application|career|recruit|আবেদন|নিয়োগ)", window, re.I): score += 8
        if re.search(r"(login|register|profile|account|help|blog|download|facebook|instagram|linkedin|youtube)", clean, re.I): score -= 8
        scored.append((score, len(clean), clean))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [u for _,__,u in scored]


def extract_jobposting_jsonld(page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    records = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = safe_text(script.string or script.get_text(" ", strip=True))
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        # Expand @graph containers.
        expanded=[]
        for obj in stack:
            if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
                expanded.extend(obj["@graph"])
            else:
                expanded.append(obj)
        for obj in expanded:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type")
            if typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ):
                records.append(obj)
    if not records:
        return {}
    obj = records[0]
    org = obj.get("hiringOrganization") or {}
    loc = obj.get("jobLocation") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    address = loc.get("address") if isinstance(loc, dict) else {}
    if not isinstance(address, dict):
        address = {}
    salary = obj.get("baseSalary") or {}
    if isinstance(salary, dict):
        value = salary.get("value") or {}
        if isinstance(value, dict):
            salary_text = value.get("value") or value.get("minValue") or value.get("maxValue") or ""
        else:
            salary_text = value or ""
        currency = salary.get("currency") or ""
        salary_text = f"{currency} {salary_text}".strip()
    else:
        salary_text = safe_text(salary)
    edu = obj.get("educationRequirements") or obj.get("qualifications") or ""
    exp = obj.get("experienceRequirements") or ""
    if isinstance(edu, list): edu = "; ".join(safe_text(x.get("name") if isinstance(x,dict) else x) for x in edu if safe_text(x.get("name") if isinstance(x,dict) else x))
    if isinstance(exp, dict): exp = exp.get("value") or exp.get("description") or exp.get("name") or ""
    result = {
        "job_title": safe_text(obj.get("title")),
        "company": safe_text(org.get("name") if isinstance(org, dict) else org),
        "location": ", ".join(x for x in [safe_text(address.get("streetAddress")), safe_text(address.get("addressLocality")), safe_text(address.get("addressRegion")), safe_text(address.get("addressCountry"))] if x),
        "job_type": safe_text(obj.get("employmentType")),
        "education": safe_text(edu),
        "experience": safe_text(exp),
        "salary": salary_text,
        "deadline": safe_text(obj.get("validThrough")),
        "posted": safe_text(obj.get("datePosted")),
        "description": safe_text(obj.get("description")),
    }
    # JSON-LD often exposes the job page URL, not the actual application route, so never treat it as apply URL.
    return {k:v for k,v in result.items() if safe_text(v)}


def extract_labelled_job_fields(text):
    raw = safe_text(text)
    if not raw:
        return {}
    flat = re.sub(r"\s+", " ", raw).strip()
    labels = {
        "location": r"(?:job location|location|কর্মস্থল|কর্মস্থান|চাকরির স্থান)",
        "job_type": r"(?:job type|employment type|employment status|চাকরির ধরন)",
        "education": r"(?:education|educational requirement|শিক্ষাগত যোগ্যতা)",
        "experience": r"(?:experience|experience required|অভিজ্ঞতা)",
        "salary": r"(?:salary|salary & benefits|বেতন)",
        "vacancies": r"(?:vacancy|vacancies|খালি পদ|শূন্যপদ)",
        "age_limit": r"(?:age|age limit|বয়স|বয়স)",
        "application_fee": r"(?:application fee|আবেদন ফি)",
        "application_method": r"(?:apply method|apply procedure|application method|apply instruction|আবেদন প্রক্রিয়া|আবেদন প্রক্রিয়া|রিজিউমি পাঠানোর উপায়)",
        "application_period": r"(?:application period|আবেদনকাল|আবেদন চলবে)",
        "selection_process": r"(?:selection procedure|selection process|নির্বাচন প্রক্রিয়া|বাছাই প্রক্রিয়া)",
        "deadline": r"(?:application deadline|deadline|last date|closing date|আবেদনের শেষ তারিখ|আবেদনের শেষ|শেষ তারিখ)",
        "posted": r"(?:published|posted|date posted|প্রকাশিত|প্রকাশের তারিখ)",
    }
    out={}
    for key,label in labels.items():
        m = re.search(label + r"\s*[:\-]?\s*(.{1,420}?)(?=(?:\b(?:" + "|".join(labels.values()) + r")\b)\s*[:\-]|$)", flat, re.I)
        if m:
            value = m.group(1).strip(" \t:;|•")
            if value and not GENERIC_UI_RE.search(value):
                out[key]=value
    # Education is frequently a multiline section between headings.
    if not out.get("education"):
        m = re.search(r"(?:###\s*)?(?:Education|শিক্ষাগত যোগ্যতা)\s*(.+?)(?=(?:###\s*)?(?:Experience|অভিজ্ঞতা|Skills|দক্ষতা|Responsibilities|দায়িত্ব|Experience Required))", raw, re.I | re.S)
        if m:
            value = re.sub(r"\s+", " ", m.group(1)).strip(" \t:;|•")
            if value and not GENERIC_UI_RE.search(value): out["education"]=value[:400]
    return out


def sanitize_fact_field(name, value, source_text=""):
    value = clean_generated_text(safe_text(value)).strip()
    # Raw URLs are never rendered inside the job card body. Application links
    # belong only in the native APPLY NOW button.
    value = re.sub(r"https?://\S+", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" :;-|•")
    if not value:
        return ""
    if value.lower() in {"not specified","not available","n/a","na","unknown","not mentioned","not stated","not provided","-","--"}:
        return ""
    if GENERIC_UI_RE.search(value):
        return ""
    if name in {"application_method", "application_fee", "selection_process"} and len(value) > 260:
        value = re.sub(r"\s+", " ", value)[:260].rstrip(" ,.;:")
    if name in {"education","experience","location","salary","job_type","age_limit"} and len(value) > 320:
        value = re.sub(r"\s+", " ", value)[:320].rstrip(" ,.;:")
    if name == "salary" and value and not re.search(r"\d|negotiable|competitive|undisclosed|not disclosed|টাকা|বেতন", value, re.I):
        return ""
    if name == "experience" and value and not re.search(r"\d|fresh|fresher|entry|no experience|years?|মাস|বছর|অভিজ্ঞতা", value, re.I):
        return ""
    if name == "age_limit" and value and not re.search(r"\d|age|বয়স|বয়স", value, re.I):
        return ""
    return value


def normalize_application_method(value, source_text=""):
    value = sanitize_fact_field("application_method", value, source_text)
    if value and len(value) <= 150:
        return value
    source = safe_text(source_text)
    patterns = [
        r"(apply online[^.]{0,140})",
        r"(apply directly[^.]{0,140})",
        r"(send (?:your )?(?:updated )?cv[^.]{0,140})",
        r"(submit[^.]{0,140}application[^.]{0,140})",
        r"(অনলাইনে আবেদন[^।]{0,120})",
        r"(ই-মেইলে[^।]{0,120})",
        r"(ইমেইলে[^।]{0,120})",
        r"(ডাকযোগে[^।]{0,120})",
    ]
    for pattern in patterns:
        m=re.search(pattern, source, re.I)
        if m:
            result=re.sub(r"\s+"," ",m.group(1)).strip(" :;-|•")
            if result: return result[:170]
    if value:
        return re.sub(r"\s+"," ",value).split(".")[0][:170].rstrip(" ,;:")
    return ""


def extract_apply_url_from_attributes(page_url, soup):
    candidates=[]
    attr_names=("data-apply-url","data-apply","data-href","data-url","data-link","data-target","href","action")
    for tag in soup.find_all(["a","button","input","form"]):
        label=safe_text(tag.get_text(" ",strip=True))+" "+safe_text(tag.get("aria-label"))+" "+safe_text(tag.get("title"))+" "+safe_text(tag.get("value"))
        blob=" ".join(safe_text(tag.get(a)) for a in attr_names)
        score=0
        if re.search(r"(apply now|apply here|apply online|apply|application|submit|আবেদন|আবেদন করুন|এখনই আবেদন)",label,re.I): score+=12
        if re.search(r"(apply|application|submit|recruit|candidate|career|vacancy)",blob,re.I): score+=6
        for a in attr_names:
            raw=safe_text(tag.get(a))
            if not raw: continue
            for u in re.findall(r"https?://[^\"'\s)]+",raw,re.I):
                u=distinct_http_url(urljoin(page_url,u),page_url)
                if u: candidates.append((score+8,len(u),u))
            if a in {"href","data-apply-url","data-href","data-url","data-link","action"}:
                u=distinct_http_url(urljoin(page_url,raw),page_url)
                if u: candidates.append((score,len(u),u))
        onclick=safe_text(tag.get("onclick"))
        for u in re.findall(r"https?://[^\"'\s)]+",onclick,re.I):
            u=distinct_http_url(urljoin(page_url,u),page_url)
            if u: candidates.append((score+8,len(u),u))
    candidates.sort(key=lambda x:(-x[0],x[1]))
    return candidates[0][2] if candidates else ""

def extract_article(item):
    url = item["url"]
    page_posted = None
    apply_url = safe_text(item.get("apply_url"))
    combined_image_candidates = []
    structured_fields = dict(item.get("structured_fields") or {})

    try:
        response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=25)
        if response.status_code < 400:
            page_html = response.text
            final_url = response.url
            page_posted = posted_at_from_html(page_html)
            if page_posted:
                item["page_posted_date"] = page_posted.isoformat()
                item["published_date"] = page_posted.isoformat()
            # Parse structured JobPosting first. It is authoritative when present.
            jsonld = extract_jobposting_jsonld(page_html)
            if jsonld:
                structured_fields.update({k:v for k,v in jsonld.items() if v})
                for key,item_key in (("job_title","title"),("company","company"),("location","location"),("job_type","job_type"),("education","education"),("experience","experience"),("salary","salary")):
                    if jsonld.get(key): item[item_key]=jsonld[key]
                if jsonld.get("deadline"):
                    item["deadline_hint"] = jsonld["deadline"]
                if jsonld.get("posted") and not item.get("published_date"):
                    dt=parse_datetime(jsonld["posted"])
                    if dt: item["published_date"]=dt.isoformat()

            visible_text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
            labelled = extract_labelled_job_fields(visible_text)
            for key,value in labelled.items():
                if value and not structured_fields.get(key): structured_fields[key]=value
                if value and key in {"location","job_type","education","experience","salary","vacancies","age_limit","application_fee","application_method","application_period","selection_process"} and not item.get(key):
                    item[key]=value
            apply_url = resolve_apply_url(final_url, page_html, apply_url)
            item["source_url"] = final_url
            item["url"] = final_url
            item["canonical"] = canonical_url(final_url)
            item["structured_fields"] = structured_fields
            if apply_url:
                item["apply_url"] = distinct_http_url(apply_url, url)
            deadline_dt = choose_deadline(page_html)
            if deadline_dt:
                item["deadline_hint"] = deadline_dt.strftime("%d %B %Y")
                item["deadline_evidence"] = visible_text[:22000]
            else:
                item["deadline_evidence"] = visible_text[:16000]

            text = trafilatura.extract(page_html, include_comments=False, include_tables=False, favor_precision=True)
            combined_image_candidates = find_image_candidates(url, page_html, final_url, preferred_image=item.get("image", ""))
            if text and len(safe_text(text)) >= 500:
                item["article_text_source"] = "local"
                return safe_text(text), combined_image_candidates
    except Exception as exc:
        logger.warning("Local career extraction failed %s: %s", url, exc)

    try:
        result_set = get_exa().get_contents([url], text={"max_characters": 14000})
        if result_set.results:
            result = result_set.results[0]
            text = safe_text(getattr(result, "text", ""))
            exa_image = safe_text(getattr(result, "image", ""))
            combined_image_candidates = find_image_candidates(url, preferred_image=item.get("image", "") or exa_image)
            if exa_image and exa_image not in combined_image_candidates:
                combined_image_candidates.append(exa_image)
            if text:
                labelled = extract_labelled_job_fields(text)
                item["structured_fields"] = {**structured_fields, **{k:v for k,v in labelled.items() if v}}
                text_apply = ""
                for candidate_url in extract_urls_near_apply(text):
                    text_apply = distinct_http_url(candidate_url, url)
                    if text_apply:
                        break
                item["apply_url"] = resolve_apply_url(url, text, apply_url or text_apply)
                deadline_dt = choose_deadline(text)
                if deadline_dt:
                    item["deadline_hint"] = deadline_dt.strftime("%d %B %Y")
                    item["deadline_evidence"] = text[:16000]
                return text, combined_image_candidates
    except Exception as exc:
        logger.warning("Exa career extraction failed %s: %s", url, exc)

    return "", [item.get("image", "")] if item.get("image") else []


# ============================================================
# STORY + KNOWLEDGE GENERATION
# ============================================================


JOB_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "job_title": {"type": "string"}, "company": {"type": "string"}, "location": {"type": "string"}, "job_type": {"type": "string"},
        "education": {"type": "string"}, "experience": {"type": "string"}, "salary": {"type": "string"},
        "application_fee": {"type": "string"}, "application_method": {"type": "string"}, "application_period": {"type": "string"},
        "selection_process": {"type": "string"}, "deadline": {"type": "string"}, "apply_url": {"type": "string"},
        "bangladesh_relevance": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "application_fee","application_method","application_period","selection_process","deadline","bangladesh_relevance","confidence",
    ],
    "additionalProperties": False,
}

def extract_locked_job_record(item, article_text):
    structured = dict(item.get("structured_fields") or {})
    labelled = extract_labelled_job_fields(article_text)
    for key,value in labelled.items():
        structured.setdefault(key,value)
    source_url=safe_text(item.get("source_url") or item.get("url"))
    discovered_apply=distinct_http_url(item.get("apply_url"),source_url)
    base_fields={
        "job_title": structured.get("job_title") or item.get("title"),
        "company": structured.get("company") or item.get("company"),
        "location": structured.get("location") or item.get("location"),
        "job_type": structured.get("job_type") or item.get("job_type"),
        "education": structured.get("education") or item.get("education"),
        "experience": structured.get("experience") or item.get("experience"),
        "salary": structured.get("salary") or item.get("salary"),
        "vacancies": structured.get("vacancies") or item.get("vacancies"),
        "age_limit": structured.get("age_limit") or item.get("age_limit"),
        "application_fee": structured.get("application_fee") or item.get("application_fee"),
        "application_method": structured.get("application_method") or item.get("application_method"),
        "application_period": structured.get("application_period") or item.get("application_period"),
        "selection_process": structured.get("selection_process") or item.get("selection_process"),
        "deadline": structured.get("deadline") or item.get("deadline_hint"),
        "apply_url": discovered_apply,
    }
    deterministic={k:sanitize_fact_field(k,v,article_text) for k,v in base_fields.items()}
    deterministic["job_title"]=deterministic.get("job_title") or safe_text(item.get("title"))
    deterministic["application_method"]=normalize_application_method(deterministic.get("application_method"),article_text)
    if deterministic.get("deadline"):
        dt=parse_date_text(deterministic["deadline"])
        if dt: deterministic["deadline"]=dt.strftime("%d %B %Y")
    if deterministic.get("apply_url") and not is_actionable_apply_url(deterministic["apply_url"]):
        deterministic["apply_url"]=""
    record={
        "job_title":deterministic.get("job_title",""),
        "company":deterministic.get("company",""),
        "location":deterministic.get("location",""),
        "job_type":deterministic.get("job_type",""),
        "education":deterministic.get("education",""),
        "experience":deterministic.get("experience",""),
        "salary":deterministic.get("salary",""),
        "vacancies":deterministic.get("vacancies",""),
        "age_limit":deterministic.get("age_limit",""),
        "application_fee":deterministic.get("application_fee",""),
        "application_method":deterministic.get("application_method",""),
        "application_period":deterministic.get("application_period",""),
        "selection_process":deterministic.get("selection_process",""),
        "deadline":deterministic.get("deadline",""),
        "apply_url":deterministic.get("apply_url",""),
        "bangladesh_relevance":85,
        "confidence":100,
    }
    if not record["job_title"] or not record["company"] or record["confidence"] < 60:
        return None
    if REQUIRE_DISTINCT_APPLY_URL and not record["apply_url"]:
        logger.info("DROP no distinct actionable apply URL: %s",record["job_title"])
        return None
    if record["deadline"]:
        dt=parse_date_text(record["deadline"])
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
        ("📝 Application", story.get("application_method")),
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
    apply_url=safe_text(story.get("apply_url"))
    source_url=safe_text(story.get("source_url") or story.get("url"))
    if not apply_url or not is_actionable_apply_url(apply_url) or not distinct_http_url(apply_url,source_url):
        return None
    return {"inline_keyboard":[[{"text":"APPLY NOW","url":apply_url}]]}


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
            timeout=20,
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

    return canonical_url(
        story["url"]
    )


def store_event(story, published=False, message_id=None):
    event_id = make_event_id(story)
    event = {
        "event_id": event_id,
        "event_key": story.get("event_key", ""),
        "canonical_url": story.get("canonical", canonical_url(story.get("url", ""))),
        "original_url": story.get("source_url") or story.get("url", ""),
        "source_url": story.get("source_url") or story.get("url", ""),
        "apply_url": story.get("apply_url", ""),
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
    target=min(max(RANKING_POOL_SIZE, needed*6, 40), MAX_PROCESS_CANDIDATES, len(ranked))
    ordered=sorted(ranked, key=lambda x:(-float(x.get("importance_score",x.get("local_score",0))),-float(x.get("local_score",0))))
    selected=[]; seen=set(); source_counts={}; sources=[]
    # First pass: strongest candidate from each source.
    for item in ordered:
        can=safe_text(item.get("canonical"))
        if not can or can in seen:
            continue
        src=safe_text(item.get("source")) or "Unknown Source"
        if src not in sources:
            sources.append(src)
            selected.append(dict(item)); seen.add(can); source_counts[src]=1
        if len(selected)>=target:
            return selected
    # Second pass: continue in quality order. Diversity is a tie-breaker, not a quality replacement.
    for item in ordered:
        if len(selected)>=target: break
        can=safe_text(item.get("canonical"))
        if not can or can in seen: continue
        selected.append(dict(item)); seen.add(can)
        src=safe_text(item.get("source")) or "Unknown Source"
        source_counts[src]=source_counts.get(src,0)+1
    logger.info("QUALITY-FIRST CANDIDATE POOL: %d candidates across %d sources distribution=%s",len(selected),len(source_counts),source_counts)
    return selected

def claims_grounded(story, article_text):
    claims = [
        story.get("headline", ""), story.get("company", ""), story.get("location", ""),
        story.get("job_type", ""), story.get("education", ""), story.get("experience", ""),
        story.get("salary", ""), story.get("deadline", ""),
        story.get("vacancies", ""), story.get("age_limit", ""), story.get("application_fee", ""), story.get("application_method", ""), story.get("application_period", ""), story.get("selection_process", ""),
    ]
    claims = [safe_text(x) for x in claims if safe_text(x)]
    prompt = """
You are a strict fact-checking editor for a Bangladesh job-news channel.
Compare every generated field with the source article/page.
Mark supported=true only if material factual claims in the job title, employer, location, job type,
selection process, and deadline are directly supported by the source or are faithful concise normalizations.
Reject invented facts, identity changes, unsupported eligibility, incorrect employer/location, wrong dates, unsupported
salary, fabricated vacancy counts, or application instructions stronger than the source. Missing fields are allowed;
they should simply be omitted from the final card.
Return only the JSON schema.
"""
    user = "SOURCE JOB PAGE:\n" + article_text[:14000] + "\n\nGENERATED JOB CARD:\n- " + "\n- ".join(claims)
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "career_claim_verification", "strict": True, "schema": VERIFY_SCHEMA},
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return bool(data.get("supported")), data.get("unsupported_claims", [])
    except Exception as exc:
        logger.warning("Career claim verification failed: %s", exc)
        # Same fail-open verifier-outage policy as the reference bot.
        return True, []



def process_story_candidate(item, verify_claims=True):
    article_text,image_candidates=extract_article(item)
    if not article_text:
        logger.warning("DROP extraction: %s",item.get("title")); item["verification_state"]="extraction_failed"; return None
    page_posted_date=parse_datetime(item.get("page_posted_date"))
    if page_posted_date and not (DISCOVERY_START<=page_posted_date<=DISCOVERY_END):
        item["verification_state"]="stale_source"; return None
    if not candidate_basic_allowed(item):
        item["verification_state"]="basic_filter_failed"; return None
    record=extract_locked_job_record(item,article_text)
    if not record:
        item["verification_state"]="job_record_failed"; return None
    source_url=safe_text(item.get("source_url") or item.get("url"))
    apply_url=distinct_http_url(record.get("apply_url"),source_url)
    if REQUIRE_DISTINCT_APPLY_URL and not apply_url:
        item["verification_state"]="apply_url_failed"; return None
    deadline_dt=parse_date_text(record.get("deadline","")) if record.get("deadline") else None
    if deadline_dt and deadline_is_expired(deadline_dt):
        item["verification_state"]="expired"; return None
    story=generate_story(item,article_text,locked_record=record)
    if not story:
        item["verification_state"]="editorial_failed"; return None
    factual={
        "headline":record["job_title"],"company":record["company"],"location":record["location"],"job_type":record["job_type"],
        "education":record["education"],"experience":record["experience"],"salary":record["salary"],"vacancies":record["vacancies"],
        "age_limit":record["age_limit"],"application_fee":record["application_fee"],"application_method":record["application_method"],
        "application_period":record["application_period"],"selection_process":record["selection_process"],"deadline":record["deadline"],
        "apply_url":apply_url,"source":item.get("source") or story.get("source"),
    }
    story.update(factual)
    story["source_url"]=source_url
    story["url"]=source_url
    story["apply_url"]=apply_url
    if story.get("deadline") and not deadline_grounded(story["deadline"],article_text+"\n"+safe_text(item.get("deadline_evidence",""))):
        item["verification_state"]="deadline_grounding_failed"; return None
    grounded,bad_number=numeric_grounded(story,article_text+"\n"+safe_text(item.get("deadline_evidence","")))
    if not grounded:
        item["verification_state"]="numeric_grounding_failed"; logger.warning("DROP numeric grounding: %s (%s)",story.get("headline"),bad_number); return None
    story["image_candidates"]=list(image_candidates or []); story["image_url"]=story["image_candidates"][0] if story["image_candidates"] else ""
    story["topic"]=canonical_topic(story.get("topic") or item.get("topic")); story["category_hashtags"]=category_hashtags(story)
    story["job_record"]=record
    story["deadline_iso"]=deadline_dt.date().isoformat() if deadline_dt else ""
    published=page_posted_date or parse_datetime(item.get("published_date"))
    story["published_date"]=published.isoformat() if published else item.get("published_date","")
    story["posted_at"]=story["published_date"]
    story["canonical"]=canonical_url(source_url)
    story["job_id"]=hashlib.sha1(f"{story['canonical']}|{record['company']}|{record['job_title']}".encode("utf-8")).hexdigest()[:16]
    story["event_key"]=item.get("event_key") or normalize_title(f"{record['company']} {record['job_title']} {record['location']}")
    story["event_id"]=hashlib.sha1(story["event_key"].encode("utf-8")).hexdigest()[:16]
    story["event_cluster_id"]=item.get("event_cluster_id",""); story["event_source_count"]=item.get("event_source_count",1)
    story["source_reliability"]=source_reliability_score(item); story["audience_fit"]=audience_fit_score(item)
    story["verification_state"]="verified"
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
        if item.get("region") != region:
            continue
        if item.get("status") not in {"pending", "selected"}:
            continue

        published = parse_datetime(item.get("published_date"))
        if not published or not (DISCOVERY_START <= published <= DISCOVERY_END):
            continue

        url = safe_text(item.get("url"))
        canonical = safe_text(item.get("canonical"))
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
        if title_duplicate_against_list(item.get("title", ""), candidates, threshold=0.94):
            continue

        candidates.append(dict(item))
        seen.add(canonical)

    candidates.sort(
        key=lambda x: parse_datetime(x.get("published_date"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return candidates[:MAX_RSS_CANDIDATES]


def prepare_ranked_region(region,candidates):
    ranked=rank_candidates(candidates,region)
    logger.info("%s RANKED RETURNED: %d",region,len(ranked))
    clustered=collapse_event_clusters(ranked)
    logger.info("%s AFTER EVENT DEDUP: %d",region,len(clustered))
    persist_event_cluster_state(clustered)
    return clustered


def process_ranked_region(region, ranked):
    """Process a broad candidate pool while preserving quality and source diversity."""
    pool=build_candidate_pool(ranked,MAX_STORIES_PER_RUN)
    valid=[]; attempted=0; rejected=0; source_counts={}; processed=set()
    deferred=[]
    distinct_sources=len({safe_text(x.get("source")) for x in pool if safe_text(x.get("source"))})

    # Dynamic source cap: strong quality first, but avoid one publisher dominating the feed.
    if distinct_sources <= 1: source_cap=MAX_STORIES_PER_RUN
    elif distinct_sources == 2: source_cap=8
    elif distinct_sources == 3: source_cap=6
    elif distinct_sources == 4: source_cap=5
    elif distinct_sources <= 7: source_cap=4
    else: source_cap=3

    def source_allowed(source):
        return source_counts.get(source,0) < source_cap

    def accept_story(item,story):
        nonlocal rejected
        if not story:
            rejected+=1; return False
        canonical=safe_text(story.get("canonical") or item.get("canonical"))
        if not canonical or canonical in processed: return False
        processed.add(canonical)
        if is_already_published_candidate({**item,"title":story.get("headline",item.get("title"))}):
            rejected+=1; return False
        source=safe_text(story.get("source")) or safe_text(item.get("source")) or "Unknown Source"
        if not source_allowed(source):
            deferred.append(item); return False
        story["topic"]=canonical_topic(story.get("topic"),region)
        story["category_hashtags"]=category_hashtags(story)
        story["publish_score"]=item.get("importance_score",item.get("local_score",0))
        valid.append(story); source_counts[source]=source_counts.get(source,0)+1
        logger.info("ACCEPT %s #%d score=%s source=%s title=%s",region,len(valid),story.get("publish_score",0),source,story.get("headline",""))
        return True

    # Normal pass.
    for item in pool:
        if len(valid)>=MAX_STORIES_PER_RUN: break
        attempted+=1
        story=process_story_candidate(item,verify_claims=False)
        if not accept_story(item,story):
            deferred.append(item)

    # Rebalance from deferred/ranked pool to hit the minimum target without relaxing fact integrity.
    if len(valid)<MAX_STORIES_PER_RUN:
        reserve=list(deferred)
        seen={safe_text(x.get("canonical")) for x in reserve}
        for item in ranked:
            if safe_text(item.get("canonical")) in seen: continue
            if safe_text(item.get("canonical")) in processed: continue
            if not candidate_basic_allowed(item): continue
            reserve.append(item); seen.add(safe_text(item.get("canonical")))
            if len(reserve)>=RESCUE_PROCESS_CANDIDATES: break
        for item in reserve:
            if len(valid)>=MAX_STORIES_PER_RUN: break
            attempted+=1
            story=process_story_candidate(item,verify_claims=False)
            accept_story(item,story)

    valid.sort(key=lambda x:(-float(x.get("publish_score",0)),-deadline_sort_key(parse_date_text(x.get("deadline",""))) if x.get("deadline") else 0,-(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0)))
    logger.info("%s FINAL VALID: %d | target_min=%d target_max=%d | pool=%d attempted=%d rejected=%d source_cap=%d sources=%s",region,len(valid),MIN_STORIES_PER_RUN,MAX_STORIES_PER_RUN,len(pool),attempted,rejected,source_cap,source_counts)
    if len(valid)<MIN_STORIES_PER_RUN:
        logger.warning("%s could not reach minimum publish target: valid=%d minimum=%d. No fabricated jobs will be created.",region,len(valid),MIN_STORIES_PER_RUN)
    return valid[:MAX_STORIES_PER_RUN]



# ============================================================
# CAREER-SPECIFIC DATE / JOB EXTRACTION HELPERS
# ============================================================

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
                    for key in ("datePublished", "dateCreated"):
                        dt = parse_datetime(obj.get(key, ""))
                        if dt:
                            values.append(dt.astimezone(BD_TZ))
        except Exception:
            continue
    return min(values) if values else None


def is_actionable_apply_url(url):
    raw=safe_text(url)
    if not re.match(r"^https?://",raw,re.I):
        return False
    path=urlparse(raw).path.lower()
    if re.search(r"/(help|about|contact|faq|login|signin|register|employer|post-a-job)(/|$)",path,re.I):
        return False
    return True


def resolve_apply_url(page_url, page_html, known_apply_url=""):
    source_url=safe_text(page_url)
    candidates=[]
    known=distinct_http_url(known_apply_url,source_url)
    if known and is_actionable_apply_url(known):
        candidates.append((30,known))
    discovered=extract_apply_url(page_url,page_html)
    if discovered and is_actionable_apply_url(discovered):
        candidates.append((20,discovered))
    for url in extract_urls_near_apply(BeautifulSoup(page_html or "","html.parser").get_text(" ",strip=True)):
        candidate=distinct_http_url(url,source_url)
        if candidate and is_actionable_apply_url(candidate):
            candidates.append((10,candidate))
    if not candidates:
        return ""
    candidates.sort(key=lambda x:(-x[0],len(x[1])))
    return candidates[0][1]

def extract_apply_url(page_url, page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    candidates=[]
    # Forms / buttons / data attributes first.
    attr_url = extract_apply_url_from_attributes(page_url, soup)
    if attr_url:
        candidates.append((12, len(attr_url), attr_url))

    signals = re.compile(r"(apply now|apply here|apply online|apply|application|submit|আবেদন|আবেদন করুন|এখনই আবেদন|প্রয়োগ করুন)", re.I)
    negative = re.compile(r"(login|sign in|sign up|register|profile|account|post a job|employer|resume builder|career tips|download app)", re.I)
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = safe_text(a.get("href"))
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute=urljoin(page_url,href)
        if not re.match(r"^https?://", absolute, re.I):
            continue
        if canonical_url(absolute)==canonical_url(page_url):
            continue
        score=0
        if signals.search(label): score += 10
        if re.search(r"(apply|application|submit|recruit|candidate|career)", absolute, re.I): score += 4
        if negative.search(label + " " + absolute): score -= 10
        candidates.append((score,len(absolute),absolute))

    # Text can explicitly contain an external application URL.
    for url in extract_urls_near_apply(BeautifulSoup(page_html or "","html.parser").get_text(" ",strip=True)):
        absolute=distinct_http_url(url,page_url)
        if absolute:
            candidates.append((9,len(absolute),absolute))

    candidates.sort(key=lambda x:(-x[0],x[1]))
    return candidates[0][2] if candidates else ""

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


def _extract_listing_anchor(source, anchor, page_url, listing_text, page_new_feed=False):
    title = safe_text(anchor.get_text(" ", strip=True))
    href = safe_text(anchor.get("href"))
    link = urljoin(page_url, href)
    if not title or len(title) < 5 or len(title) > 220:
        return None
    if is_generic_navigation_candidate(title, link, listing_text):
        return None

    # Source-specific detail URL gate. This is the primary protection against
    # Help Center, Employer Login, iOS/Android app and navigation pages.
    source_name_value=source["name"]
    mode=source.get("mode")
    if mode == "bdjobs_listing" and not JOB_DETAIL_PATTERNS["Bdjobs"].search(link):
        return None
    if mode == "bdjobslive_listing" and not JOB_DETAIL_PATTERNS["BDJobs Live"].search(link):
        return None
    if mode == "jobcombd_listing" and not JOB_DETAIL_PATTERNS["Job.com.bd"].search(link):
        return None
    if mode == "generic_listing" and source_name_value == "Dohaj":
        if not looks_like_job_detail_url(source_name_value, link):
            return None
    if mode == "news_listing" and (BAD_JOB_RE.search(title) or BAD_PATH_RE.search(urlparse(link).path)):
        return None
    if not primary_domain_allowed(link,"Career"):
        return None

    published_dt = listing_published_date(listing_text)
    estimated = False
    if not published_dt and page_new_feed:
        published_dt=career_now()
        estimated=True
    if not published_dt:
        return None
    deadline_dt=choose_deadline(listing_text)
    item={
        "title":title,
        "url":link,
        "source_url":link,
        "canonical":canonical_url(link),
        "published_dt":published_dt.isoformat(),
        "published_date":published_dt.isoformat(),
        "source":source_name_value,
        "source_type":source["type"],
        "region":"Career",
        "excerpt":listing_text[:5000],
        "image":"",
        "discovery":"direct_portal",
        "date_estimated":estimated,
        "new_feed":bool(page_new_feed),
        "date_source":"listing_page" if not estimated else "curated_new_feed",
        "deadline_hint":deadline_dt.strftime("%d %B %Y") if deadline_dt else "",
        "job_intent_score":job_intent_score({"title":title,"url":link,"excerpt":listing_text}),
        "discovery_lane":mode,
    }
    if item["job_intent_score"] < MIN_JOB_INTENT_SCORE:
        return None
    if not candidate_basic_allowed(item):
        return None
    if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
        return None
    return item


def direct_portal_gap_fill():
    added=0
    per_source={}
    seen_links=set()
    for source in DIRECT_JOB_SOURCES:
        source_key=source["name"]
        per_source.setdefault(source_key,0)
        if source_is_in_cooldown(source_key, source["url"]):
            logger.info("Skipping source in cooldown: %s", source_key)
            continue
        try:
            response=session.get(source["url"],headers=HEADERS,timeout=15)
            record_source_health(source["name"],source["url"],response.status_code,response.reason if response.status_code>=400 else "")
            if response.status_code>=400:
                logger.warning("Direct portal %s returned %s",source["name"],response.status_code)
                continue
            soup=BeautifulSoup(response.text,"html.parser")
            anchors=soup.find_all("a",href=True)
            # Detail-link patterns are far safer than arbitrary keyword anchors.
            max_links=180 if source["mode"] in {"bdjobs_listing","bdjobslive_listing"} else 120
            for anchor in anchors[:max_links*4]:
                href=safe_text(anchor.get("href"))
                link=urljoin(response.url,href)
                if link in seen_links:
                    continue
                if source["mode"] == "bdjobs_listing" and not JOB_DETAIL_PATTERNS["Bdjobs"].search(link):
                    continue
                if source["mode"] == "bdjobslive_listing" and not JOB_DETAIL_PATTERNS["BDJobs Live"].search(link):
                    continue
                if source["mode"] == "jobcombd_listing" and not JOB_DETAIL_PATTERNS["Job.com.bd"].search(link):
                    continue
                container=anchor
                listing_text=""
                for _ in range(6):
                    parent=getattr(container,"parent",None)
                    if parent is None: break
                    candidate_text=safe_text(parent.get_text(" ",strip=True))
                    if len(candidate_text)>=140:
                        listing_text=candidate_text
                        break
                    container=parent
                if not listing_text:
                    listing_text=safe_text(anchor.get_text(" ",strip=True))
                item=_extract_listing_anchor(source,anchor,response.url,listing_text,source.get("new_feed",False))
                if not item:
                    continue
                seen_links.add(link)
                queue_candidate(item)
                per_source[source_key]+=1
                added+=1
                if per_source[source_key]>=60:
                    break
                if added>=MAX_RSS_CANDIDATES:
                    return added
        except Exception as exc:
            record_source_health(source["name"],source["url"],0,str(exc))
            logger.warning("Direct portal discovery failed %s: %s",source["name"],exc)
    logger.info("DIRECT PORTAL SOURCES: %s",per_source)
    return added


def run():
    global NOW_BD, TODAY_START, YESTERDAY_START, DISCOVERY_START, DISCOVERY_END
    NOW_BD = career_now()
    TODAY_START = NOW_BD.replace(hour=0, minute=0, second=0, microsecond=0)
    YESTERDAY_START = TODAY_START - timedelta(days=1)
    DISCOVERY_START = NOW_BD - timedelta(hours=ROLLING_DISCOVERY_HOURS)
    DISCOVERY_END = NOW_BD + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)

    logger.info("CAREER NEWSROOM V1 UPDATE-ONLY")
    logger.info(
        "Channel=%s | LOOKBACK=%dh | %s -> %s | deadline soft-target=%dd",
        TELEGRAM_CHANNEL,
        DISCOVERY_LOOKBACK_HOURS,
        DISCOVERY_START.isoformat(),
        DISCOVERY_END.isoformat(),
        DEADLINE_SOFT_TARGET_DAYS,
    )

    prune_state()
    refresh_category_coverage()
    rss_count = collect_rss()
    direct_count = direct_portal_gap_fill()
    queued_count = queue_candidates_for_region("Career")
    targeted_count = targeted_portal_lane_gap_fill("Career", max_total=36)
    google_count = google_news_gap_fill("Career", queued_count + direct_count + targeted_count, DISCOVERY_TARGET_PER_REGION)
    exa_count = exa_gap_fill("Career", queued_count + direct_count + targeted_count + google_count, DISCOVERY_TARGET_PER_REGION)
    logger.info("DISCOVERY rss=%d direct=%d targeted=%d google=%d exa=%d", rss_count, direct_count, targeted_count, google_count, exa_count)
    save_state(STATE)

    candidates = available_candidates("Career", source_pool=None)
    filtered = []
    for item in candidates:
        if not candidate_basic_allowed(item):
            continue
        if is_already_published_candidate(item):
            continue
        if any(title_similarity(item.get("title", ""), x.get("title", "")) >= 0.92 for x in filtered):
            continue
        filtered.append(item)

    logger.info("STAGE COUNTS | discovered=%d queue=%d filtered_basic=%d", rss_count + direct_count + targeted_count + google_count + exa_count, queued_count, len(filtered))
    # Keep a large live inventory, then let local pre-ranking reduce AI cost.
    ranked = prepare_ranked_region("Career", filtered[:MAX_RSS_CANDIDATES])
    logger.info("STAGE COUNTS | event_dedup_ranked=%d audience_top=%d", len(ranked), min(len(ranked), MAX_LOCAL_PREFILTER))

    for item in ranked[:12]:
        logger.info(
            "RANK CAREER #%s | score=%s | %s | %s",
            item.get("editor_rank", "?"), item.get("importance_score", 0), item.get("title", ""), item.get("rank_reason", ""),
        )

    stories = process_ranked_region("Career", ranked)
    logger.info("STAGE COUNTS | verified=%d publish_ready=%d source_diversity=%d", len(stories), len(stories), len({safe_text(x.get("source")) for x in stories}))
    logger.info("CAREER FINAL=%d MAX=%d", len(stories), MAX_STORIES_PER_RUN)

    published_count = 0
    for index, story in enumerate(stories, start=1):
        rich_html = fit_rich_html(story)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.error("Rich message exceeds Telegram limit: %s", story.get("headline"))
            continue

        image_path = prepare_image(story, index)
        reply_markup = _inline_keyboard(story)
        result = send_rich_message(image_path, rich_html, reply_markup=reply_markup)
        if not result.get("ok"):
            logger.warning("Rich Message publish failed; using Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, rich_html, reply_markup=reply_markup)

        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            canonical = story.get("canonical") or canonical_url(story.get("url", ""))
            if canonical:
                POSTED_URLS.add(canonical)
                save_posted_url(canonical)
                queue_item = STATE["queue"].get(canonical)
                if queue_item:
                    queue_item["status"] = "posted"
                    queue_item["posted_at"] = now_iso()
                    queue_item["published_to_channel"] = True
                    queue_item["verification_state"] = "published"

            store_event(story, published=True, message_id=message_id)
            remember_posted_event(story)
            update_category_coverage(story)
            STATE["recent_titles"].append(normalize_title(story.get("headline", "")))
            logger.info("Published %d/%d: %s", published_count, len(stories), story.get("headline", ""))
        else:
            logger.error("Telegram failed: %s", result.get("description"))

        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)

    save_state(STATE)
    save_source_health(SOURCE_HEALTH)
    logger.info("Finished. Published=%d/%d", published_count, len(stories))



# ============================================================
# SELF TEST
# ============================================================

def self_test():
    refresh_career_window()
    assert TELEGRAM_CHANNEL == "@CareerNewsroom"
    assert DISCOVERY_LOOKBACK_HOURS == 72
    assert MIN_STORIES_PER_RUN == 5
    assert MAX_STORIES_PER_RUN == 15
    assert RANKING_POOL_SIZE == 60
    assert MAX_PROCESS_CANDIDATES == 60
    assert MAX_POSTS_PER_SOURCE_PER_RUN == 3

    # URL normalization must preserve vacancy IDs while dropping tracking.
    assert canonical_url("https://jobs.bdjobs.com/jobdetails/?id=1534808&ln=1&utm_source=x") == "jobs.bdjobs.com/jobdetails?id=1534808&ln=1"
    assert canonical_url("https://www.example.com/job/?utm_source=x") == "example.com/job"
    assert normalize_title("এইচএসসি পাসে চাকরি — ACI PLC") and "এইচএসসি" in normalize_title("এইচএসসি পাসে চাকরি — ACI PLC")

    # Discovery/domain/date constraints.
    assert primary_domain_allowed("https://jobs.bdjobs.com/jobdetails/?id=1", "Career")
    assert primary_domain_allowed("https://smartjob.portal.gov.bd/", "Career")
    assert not primary_domain_allowed("https://example.com/job", "Career")
    assert parse_date_text("25 September 2026").date() == datetime(2026,9,25,tzinfo=BD_TZ).date()
    inside=career_now()-timedelta(hours=71,minutes=59)
    outside=career_now()-timedelta(hours=72,minutes=1)
    base_item={"title":"Management Trainee Internship BBA MBA","url":"https://www.banglatribune.com/jobs/test","source":"Bangla Tribune","region":"Career","excerpt":"Dhaka Bangladesh company recruitment management trainee internship BBA MBA vacancy","date_estimated":False,"published_date":inside.isoformat()}
    assert candidate_basic_allowed(base_item)
    assert not candidate_basic_allowed({**base_item,"published_date":outside.isoformat()})
    assert not candidate_basic_allowed({**base_item,"title":"India Jobs Open Now","excerpt":"India recruitment jobs"})
    a,_=local_job_score(base_item); b,_=local_job_score({**base_item,"title":"Generic Vacancy","excerpt":"Dhaka recruitment vacancy"}); assert a>b

    # Detail URL and navigation guards.
    assert looks_like_job_detail_url("Bdjobs","https://jobs.bdjobs.com/jobdetails/?id=1534808&ln=1")
    assert looks_like_job_detail_url("BDJobs Live","https://www.bdjobslive.com/bdjobs-details/management-trainee-13344")
    assert looks_like_job_detail_url("Job.com.bd","https://job.com.bd/jobs/details/?i=1865")
    assert not looks_like_job_detail_url("Job.com.bd","https://job.com.bd/jobs/new_jobs/")
    assert not candidate_basic_allowed({**base_item,"title":"Help Center","url":"https://job.com.bd/help-center","excerpt":"Filter by category, location, and keywords"})

    # Output: dynamic rows, no placeholders or retired sections, no raw URLs.
    sample={"headline":"Management Trainee","company":"Example Company Ltd.","location":"Dhaka","job_type":"Management Trainee","education":"BBA / MBA","experience":"Fresh graduates may apply","salary":"","vacancies":"02","age_limit":"18-30","application_fee":"BDT 200","application_method":"Online application","application_period":"17 September 2026 – 25 September 2026","selection_process":"Written and viva","deadline":"25 September 2026","apply_url":"https://example.com/apply","source_url":"https://example.com/job","source":"Example Source","topic":"Management Trainee","published_date":"2026-09-17T10:00:00+06:00"}
    rendered=fit_rich_html(sample)
    for marker in ("📣 Management Trainee","🏢 <b>Company:</b>","JOB SNAPSHOT","Location","Education","Experience","Vacancies","Application Fee","Application","Deadline","Posted","#CareerNewsroom","Official Source"):
        assert marker in rendered
    assert "Suitable For" not in rendered and "Key Highlights" not in rendered and "More Details" not in rendered
    visible=visible_text_for_test(rendered)
    assert "https://example.com/apply" not in visible and "https://example.com/job" not in visible
    assert rendered.index("JOB SNAPSHOT") < rendered.index("#CareerNewsroom") < rendered.index("Official Source")
    kb=_inline_keyboard(sample)
    assert kb=={"inline_keyboard":[[{"text":"APPLY NOW","url":"https://example.com/apply"}]]}
    assert "emoji" not in kb["inline_keyboard"][0][0]["text"].lower() and kb["inline_keyboard"][0][0]["text"]=="APPLY NOW"
    assert "<tg-" + "button" not in rendered and "<tg-" + "button-row" not in rendered

    missing=dict(sample); missing["salary"]=""; missing["vacancies"]=""; missing["application_fee"]=""; missing["application_period"]=""; missing["selection_process"]=""; missing["education"]=""; missing["experience"]=""
    missing_render=fit_rich_html(missing)
    assert "Salary" not in missing_render and "Vacancies" not in missing_render and "Application Fee" not in missing_render

    # Native markup is attached separately to sendRichMessage.
    original_call=globals()["telegram_call"]
    try:
        captured={}
        def capture(method,data=None,files=None):
            captured["method"]=method; captured["data"]=dict(data or {}); return {"ok":True,"result":{"message_id":1}}
        globals()["telegram_call"]=capture
        send_rich_message(None,rendered,reply_markup=kb)
        assert captured["method"]=="sendRichMessage"
        assert json.loads(captured["data"]["reply_markup"])==kb
        assert "<tg-" + "button" not in json.loads(captured["data"]["rich_message"])["html"]
    finally:
        globals()["telegram_call"]=original_call

    # Apply resolver must find a distinct action and never fall back to source.
    html_page='<html><form action="https://careers.example.com/apply/123"><button type="submit">Apply Now</button></form></html>'
    assert resolve_apply_url("https://example.com/job/123",html_page)=="https://careers.example.com/apply/123"
    assert distinct_http_url("https://example.com/job/123","https://example.com/job/123")==""
    assert is_actionable_apply_url("https://careers.example.com/apply/123")
    assert not is_actionable_apply_url("https://example.com/help")

    # Deadline is a scoring signal, not a seven-day hard reject.
    future_6=career_now()+timedelta(days=6); assert deadline_is_eligible(future_6)
    future_20=career_now()+timedelta(days=20); assert deadline_status_score(future_6) < deadline_status_score(future_20)
    assert deadline_is_expired(career_now()-timedelta(days=1))

    # Quality-first pool still preserves source diversity.
    ranked=[]
    for i in range(12):
        src=["Bdjobs","Dohaj","JobPagol","ProjobsBD","BDJobs Live","Smart Job"][i%6]
        ranked.append({"canonical":f"c{i}","source":src,"importance_score":100-i,"local_score":100-i})
    pool=build_candidate_pool(ranked,10)
    assert len(pool)==12 and len({x["source"] for x in pool})>=6

    # Queue retains rolling inventory metadata.
    original_queue=dict(STATE["queue"]); STATE["queue"]={}
    q={"canonical":"jobs.bdjobs.com/jobdetails?id=999","title":"Demo","url":"https://jobs.bdjobs.com/jobdetails/?id=999","source":"Bdjobs","region":"Career"}
    queue_candidate(q)
    assert STATE["queue"][q["canonical"]]["attempt_count"]==0
    assert STATE["queue"][q["canonical"]]["published_to_channel"] is False
    STATE["queue"]=original_queue

    # Fact-lock: source-backed identity cannot be rewritten by editorial layer.
    original_extract=globals()["extract_article"]
    original_record=globals()["extract_locked_job_record"]
    try:
        globals()["extract_article"]=lambda item:("Example Bank PLC Management Trainee Dhaka BBA deadline 30 September 2026 salary BDT 35000",[])
        locked={"job_title":"Bank Management Trainee","company":"Example Bank PLC","location":"Dhaka","job_type":"Management Trainee","education":"BBA / MBA","experience":"Fresh graduates","salary":"BDT 35,000","vacancies":"03","age_limit":"","application_fee":"BDT 200","application_method":"Online","application_period":"","selection_process":"Written and viva","deadline":"30 September 2026","apply_url":"https://careers.example.com/apply","confidence":100,"bangladesh_relevance":95}
        globals()["extract_locked_job_record"]=lambda item,text:dict(locked)
        story=process_story_candidate({**base_item,"title":"Bank Management Trainee","source":"Bdjobs","source_url":"https://jobs.bdjobs.com/jobdetails/?id=100","url":"https://jobs.bdjobs.com/jobdetails/?id=100","canonical":"jobs.bdjobs.com/jobdetails?id=100"})
        assert story["headline"]==locked["job_title"] and story["company"]==locked["company"] and story["source_url"]!=story["apply_url"]
    finally:
        globals()["extract_article"]=original_extract; globals()["extract_locked_job_record"]=original_record

    # Image absence must not block publication pipeline.
    original_img=globals()["download_image"]
    try:
        globals()["download_image"]=lambda url,referer="":None
        assert prepare_image({"source":"Example Source","url":"https://example.com/job","image_candidates":[]},990) is None
    finally:
        globals()["download_image"]=original_img

    logger.info("CareerNewsBot V1 self-test passed.")


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
