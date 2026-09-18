from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BD_TZ = ZoneInfo("Asia/Dhaka")

MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 15
DISCOVERY_LOOKBACK_HOURS = 72
FUTURE_TOLERANCE_MINUTES = 15
MAX_DISCOVERY_CANDIDATES = 300
MAX_RETRIEVE_CANDIDATES = 90
MIN_RETRIEVE_CANDIDATES = 60
MAX_AI_RANK_CANDIDATES = 48
MIN_FINAL_QUALITY_SCORE = 62
QUEUE_RETENTION_DAYS = 5
EVENT_RETENTION_DAYS = 30
MAX_SOURCE_POSTS_PER_RUN = 3
SOURCE_DIVERSITY_TARGET = 5
POST_DELAY_SECONDS = 3.0
STATE_FILE = "news_state.json"
POSTED_FILE = "posted_urls.txt"
MAX_RICH_CHARACTERS = 32768
BOT_API_TEXT_LIMIT = 4096
BOT_API_CAPTION_LIMIT = 1024

TELEGRAM_CHANNEL = (os.getenv("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
JINA_READER_BASE = (os.getenv("JINA_READER_BASE") or "https://r.jina.ai").rstrip("/")

RSS_FEEDS = [
    {"name": "ProjobsBD", "url": "https://projobsbd.com/feed/", "type": "job_publisher"},
    {"name": "ProjobsBD Running Jobs", "url": "https://projobsbd.com/category/running-job-circular/feed/", "type": "job_publisher"},
    {"name": "BD Govt Jobs", "url": "https://bdgovtjobs.com/feed/", "type": "job_publisher"},
    {"name": "JobPagol", "url": "https://jobpagol.com/feed/", "type": "job_publisher"},
    {"name": "Bangladesh Pratidin Jobs", "url": "https://bdpratidin.net/rss/category/job", "type": "news_jobs"},
    {"name": "JagoNews24", "url": "https://www.jagonews24.com/rss/rss.xml", "type": "news_jobs"},
    {"name": "Bangla Tribune", "url": "https://www.banglatribune.com/feed", "type": "news_jobs"},
    {"name": "BD24Live", "url": "https://www.bd24live.com/bangla/feed", "type": "news_jobs"},
    {"name": "RisingBD", "url": "https://risingbd.com/rss/rss.xml", "type": "news_jobs"},
    {"name": "Bangladesh Journal", "url": "https://www.bd-journal.com/feed/latest-rss.xml", "type": "news_jobs"},
    {"name": "Prothom Alo", "url": "https://www.prothomalo.com/feed/", "type": "news_jobs"},
    {"name": "Jugantor", "url": "https://www.jugantor.com/feed/rss.xml", "type": "news_jobs"},
    {"name": "Kaler Kantho", "url": "https://www.kalerkantho.com/rss.xml", "type": "news_jobs"},
    {"name": "The Daily Star", "url": "https://www.thedailystar.net/jobs/rss.xml", "type": "news_jobs"},
    {"name": "The Daily Ittefaq", "url": "https://www.ittefaq.com.bd/rss.xml", "type": "news_jobs"},
]

DIRECT_SOURCES = [
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
    {"name": "Dhaka Post", "url": "https://www.dhakapost.com/jobs-career/", "type": "news_jobs"},
    {"name": "Dhaka Tribune", "url": "https://bangla.dhakatribune.com/jobs", "type": "news_jobs"},
    {"name": "Bangla Tribune", "url": "https://www.banglatribune.com/jobs", "type": "news_jobs"},
    {"name": "JagoNews24", "url": "https://www.jagonews24.com/topic/%E0%A6%9A%E0%A6%BE%E0%A6%95%E0%A6%B0%E0%A6%BF", "type": "news_jobs"},
    {"name": "Prothom Alo", "url": "https://www.prothomalo.com/collection/chakri-all", "type": "news_jobs"},
]

PRIMARY_DOMAINS = sorted({
    "jobs.bdjobs.com", "bdjobs.com", "bdjobslive.com", "dohaj.com", "job.com.bd",
    "smartjob.portal.gov.bd", "alljobs.teletalk.com.bd", "jobs.teletalk.com.bd", "bpsc.gov.bd",
    "erecruitment.bcc.gov.bd", "jobsnoticebd.com", "jobsinfo.bd", "jobfeeds.online", "circularbd.com",
    "bangladesherkhabor.net", "dhakapost.com", "dhakatribune.com", "banglatribune.com", "jagonews24.com",
    "prothomalo.com", "risingbd.com", "bd24live.com", "bd-journal.com", "jugantor.com", "kalerkantho.com",
    "thedailystar.net", "ittefaq.com.bd", "projobsbd.com", "bdgovtjobs.com", "jobpagol.com", "bdpratidin.net",
})

SOURCE_NAMES = {
    "jobs.bdjobs.com": "Bdjobs", "bdjobs.com": "Bdjobs", "bdjobslive.com": "BDJobs Live", "dohaj.com": "Dohaj",
    "job.com.bd": "Job.com.bd", "smartjob.portal.gov.bd": "Smart Job", "alljobs.teletalk.com.bd": "Alljobs Teletalk",
    "jobs.teletalk.com.bd": "Alljobs Teletalk", "bpsc.gov.bd": "BPSC", "erecruitment.bcc.gov.bd": "BCC e-Recruitment",
    "jobsnoticebd.com": "JobsNoticeBD", "jobsinfo.bd": "JobsInfo", "jobfeeds.online": "JobFeeds", "circularbd.com": "CircularBD",
    "bangladesherkhabor.net": "Bangladesher Khabor", "dhakapost.com": "Dhaka Post", "dhakatribune.com": "Dhaka Tribune",
    "banglatribune.com": "Bangla Tribune", "jagonews24.com": "JagoNews24", "prothomalo.com": "Prothom Alo",
    "risingbd.com": "RisingBD", "bd24live.com": "BD24Live", "bd-journal.com": "Bangladesh Journal", "jugantor.com": "Jugantor",
    "kalerkantho.com": "Kaler Kantho", "thedailystar.net": "The Daily Star", "ittefaq.com.bd": "The Daily Ittefaq",
    "projobsbd.com": "ProjobsBD", "bdgovtjobs.com": "BD Govt Jobs", "jobpagol.com": "JobPagol", "bdpratidin.net": "Bangladesh Pratidin Jobs",
}

GOOGLE_QUERIES = [
    'site:bdjobs.com Bangladesh "management trainee" OR "internship"',
    'site:dohaj.com Bangladesh BBA MBA job vacancy',
    'Bangladesh private company hiring "BBA" "MBA" vacancy',
    'Bangladesh "management trainee" "deadline" job',
    'Bangladesh "graduate trainee" OR "internship" finance banking marketing HR',
    'Bangladesh bank job vacancy BBA MBA deadline',
    'Bangladesh corporate job "business development" OR "relationship manager" deadline',
    'Bangladesh government recruitment BBA MBA officer vacancy',
]

TARGET_ROLE_TERMS = {
    "bba": 10, "mba": 12, "business administration": 10, "management trainee": 16,
    "graduate trainee": 16, "graduate program": 15, "internship": 16, "intern": 14,
    "finance": 9, "accounting": 9, "accounts": 8, "audit": 7, "bank": 8, "banking": 10,
    "marketing": 9, "sales": 8, "human resources": 9, "hr": 8, "business development": 10,
    "operations": 7, "supply chain": 8, "procurement": 7, "commercial": 7, "relationship manager": 10,
    "business analyst": 10, "financial analyst": 10, "credit analyst": 9, "merchandising": 7,
    "customer relationship": 7, "management": 6, "administration": 6,
}

EARLY_CAREER_TERMS = [
    "fresher", "fresh graduate", "fresh graduates", "graduate", "0 year", "0-1", "0–1", "0-2", "0–2",
    "0-3", "0–3", "entry level", "entry-level", "trainee", "internship", "intern", "graduate program",
]

SENIORITY_TERMS = [
    "senior", "lead", "head of", "director", "vice president", "vp", "chief", "principal", "specialist",
    "manager", "5+ years", "6+ years", "7+ years", "8+ years", "10+ years", "experienced professional",
]

NON_TARGET_TECH_TERMS = [
    "software engineer", "frontend developer", "backend developer", "full stack developer", "devops engineer",
    "network engineer", "civil engineer", "electrical engineer", "mechanical engineer", "doctor", "physician",
    "nurse", "medical officer", "pharmacist", "lab technologist",
]

BAD_CONTENT_TERMS = [
    "career advice", "career guide", "job tips", "interview tips", "cv tips", "resume tips", "job fair",
    "workshop", "seminar", "training course", "admission", "scholarship", "exam result", "salary guide",
]

VACANCY_TERMS = [
    "vacancy", "vacancies", "hiring", "recruitment", "recruit", "apply", "application", "job circular",
    "job vacancy", "career opportunity", "position", "post name", "নিয়োগ", "চাকরি", "শূন্যপদ", "আবেদন",
]

APPLICATION_TERMS = [
    "apply online", "apply now", "submit application", "application form", "apply here", "apply",
    "career apply", "start application", "official application", "submit your cv", "teletalk apply",
]

DEADLINE_TERMS = ["deadline", "last date", "closing date", "apply by", "application deadline", "আবেদনের শেষ", "শেষ তারিখ"]

@dataclass(frozen=True)
class Window:
    now: datetime
    start: datetime
    end: datetime


def current_window() -> Window:
    now = datetime.now(BD_TZ)
    return Window(now=now, start=now - timedelta(hours=DISCOVERY_LOOKBACK_HOURS), end=now + timedelta(minutes=FUTURE_TOLERANCE_MINUTES))
