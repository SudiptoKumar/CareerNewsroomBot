from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BD_TZ = ZoneInfo("Asia/Dhaka")
UTC = ZoneInfo("UTC")

TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@CareerNewsroom")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
EXA_API_URL = os.getenv("EXA_API_URL", "https://api.exa.ai")
EXA_CACHE_HOURS = int(os.getenv("EXA_CACHE_HOURS", "24"))

# BDjobs is the only research/source domain.
BDJOBS_DOMAINS = ("bdjobs.com", "*.bdjobs.com")
BDJOBS_HOSTS = ("bdjobs.com", "jobs.bdjobs.com", "www.bdjobs.com", "corporate3.bdjobs.com")

# Direct BDjobs pages are used for cheap index expansion/link verification.
BDJOBS_INDEX_URLS = (
    "https://jobs.bdjobs.com/jobsearch-cache.asp",
    "https://jobs.bdjobs.com/bn/otherjobsbn.asp?JobType=new",
)

# Targeted searches reduce noise and keep Exa focused on the channel audience.
EXA_QUERIES = (
    "BDjobs Bangladesh BBA MBA Management Trainee",
    "BDjobs Bangladesh Graduate Trainee fresh graduate",
    "BDjobs Bangladesh BBA finance accounting jobs",
    "BDjobs Bangladesh MBA banking financial institution jobs",
    "BDjobs Bangladesh BBA marketing sales business development",
    "BDjobs Bangladesh BBA HR human resources jobs",
    "BDjobs Bangladesh BBA operations supply chain procurement",
    "BDjobs Bangladesh business analyst relationship officer executive",
    "BDjobs Bangladesh internship BBA MBA business finance marketing",
    "BDjobs Bangladesh entry level officer executive associate",
)

DISCOVERY_DAYS = 7
INVENTORY_MAX_DAYS = 45
MAX_EXA_RESULTS_PER_QUERY = 15
MAX_DISCOVERY = 180
MAX_INDEX_PAGES = 10
MAX_INDEX_LINKS = 250
MAX_CONTENTS_URLS = 90
MAX_CEREBRAS_BATCH = 20

MIN_FINAL_SCORE = 65
MIN_AUDIENCE_SCORE = 62
MIN_FACT_CONFIDENCE = 72

MIN_POSTS = 5
MAX_POSTS = 12
MAX_COMPANY_POSTS = 3

POST_DELAY_SECONDS = 1.5
HTTP_TIMEOUT = 25
EXA_TIMEOUT = 60
CEREBRAS_TIMEOUT = 60

NOISE_TERMS = (
    "calculator", "quiz", "mcq", "model test", "question bank",
    "exam result", "result news", "exam schedule", "syllabus",
    "career advice", "career guide", "cv tips", "resume builder",
    "cover letter", "salary calculator", "gpa calculator",
    "admission", "scholarship", "course", "training course",
    "workshop", "seminar", "webinar", "deadline tracker",
    "typing speed", "practice set", "answer key", "question solution",
    "চাকরির পরীক্ষা", "ফলাফল", "প্রশ্ন সমাধান", "ক্যারিয়ার পরামর্শ",
    "বয়স ক্যালকুলেটর", "জিপিএ", "স্কলারশিপ",
)

INDEX_URL_PARTS = (
    "jobsearch", "jobsearch-cache", "otherjobs", "companyofferedjobs",
    "company_list", "company-list", "locationwisejobs", "category",
    "/search", "/page/", "/tag/", "/tags/",
)

INDEX_TITLE_TERMS = (
    "all jobs", "new jobs", "latest jobs", "job circulars",
    "company offered jobs", "location wise jobs", "new job",
    "সকল নতুন চাকরি", "সব নতুন চাকরি", "চাকরির খবর",
    "চাকরির তালিকা", "নিয়োগ বিজ্ঞপ্তি সমগ্র",
)

TARGET_TITLE_TERMS = (
    "management trainee", "graduate trainee", "trainee", "intern",
    "internship", "business analyst", "financial analyst", "credit analyst",
    "relationship officer", "relationship manager", "banking", "finance",
    "account", "accounting", "audit", "commercial", "procurement",
    "supply chain", "marketing", "sales", "business development",
    "human resources", "hr", "management", "operations", "admin",
    "customer service", "merchandising", "mis", "corporate sales",
    "brand", "product executive", "executive", "officer", "associate",
    "coordinator", "probationary officer", "branch", "credit",
    "treasury", "trade", "import", "export",
    "ব্যাংক", "হিসাব", "ফিনান্স", "বিক্রয়", "মার্কেটিং", "ব্যবস্থাপনা",
    "মানবসম্পদ", "প্রকিউরমেন্ট", "সাপ্লাই চেইন", "কমার্শিয়াল",
    "অফিসার", "এক্সিকিউটিভ", "ম্যানেজমেন্ট ট্রেইনি", "ইন্টার্ন",
)

GENERAL_JOB_TERMS = (
    "job", "vacancy", "hiring", "recruit", "recruitment", "apply",
    "position", "officer", "executive", "assistant", "associate",
    "manager", "trainee", "intern", "coordinator", "specialist",
    "চাকরি", "নিয়োগ", "পদ", "আবেদন", "কর্মকর্তা", "এক্সিকিউটিভ",
)

NON_TARGET_TITLE_TERMS = (
    "software engineer", "web developer", "app developer", "network engineer",
    "civil engineer", "electrical engineer", "mechanical engineer",
    "doctor", "medical officer", "nurse", "pharmacist", "lab technician",
    "security guard", "driver", "chef", "waiter", "housekeeper",
    "machine operator", "electrician", "plumber", "welder",
    "সিকিউরিটি গার্ড", "ড্রাইভার", "নার্স", "ইঞ্জিনিয়ার",
)

# Synonyms useful for BBA/MBA audience relevance.
BBA_MBA_TERMS = (
    "bba", "mba", "business administration", "business studies",
    "management", "finance", "accounting", "marketing", "hr",
    "human resources", "banking", "commerce", "commercial",
    "supply chain", "procurement", "business development",
    "operations", "management trainee", "graduate trainee",
    "internship", "intern", "ব্যবসায় প্রশাসন", "ব্যবস্থাপনা",
    "হিসাব", "ফিনান্স", "মার্কেটিং", "ব্যাংক", "মানবসম্পদ",
)

EARLY_CAREER_TERMS = (
    "fresher", "fresh graduate", "entry level", "graduate",
    "0-1 year", "0 to 1 year", "0-2 year", "0 to 2 year",
    "1 year", "1-2 year", "1 to 2 year", "below 2 year",
    "intern", "internship", "trainee", "management trainee",
    "graduate trainee", "probationary officer",
    "ফ্রেশার", "নবীন", "অভিজ্ঞতা ছাড়াই",
)

SENIOR_TERMS = (
    "senior manager", "general manager", "head of", "chief", "director",
    "vp ", "vice president", "assistant general manager",
    "8-10 year", "8 to 10 year", "10+ year", "over 10 year",
    "৭-১০ বছর", "৮-১০ বছর", "১০ বছর",
)

@dataclass(frozen=True)
class RunWindow:
    now: datetime
    discovery_start: datetime

def current_window() -> RunWindow:
    now = datetime.now(BD_TZ)
    return RunWindow(now=now, discovery_start=now - timedelta(days=DISCOVERY_DAYS))

def env_required_ok() -> dict[str, bool]:
    return {
        "EXA_API_KEY": bool(os.getenv("EXA_API_KEY")),
        "CEREBRAS_API_KEY": bool(os.getenv("CEREBRAS_API_KEY")),
        "TELEGRAM_BOT_TOKEN": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
    }
