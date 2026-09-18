from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import (
    BDJOBS_INDEX_URLS, HTTP_TIMEOUT, INDEX_URL_PARTS, INDEX_TITLE_TERMS,
    NOISE_TERMS, TARGET_TITLE_TERMS, GENERAL_JOB_TERMS, NON_TARGET_TITLE_TERMS,
    BBA_MBA_TERMS, EARLY_CAREER_TERMS, SENIOR_TERMS,
)
from .utils import safe_text, canonical_url, is_bdjobs_url, normalize_title, contains_any, looks_like_target_title, parse_datetime, clamp

logger = logging.getLogger("career-news-bot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CareerNewsBot-BDjobs/3.0)",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}

MONTHS = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,"apr":4,"april":4,"may":5,
    "jun":6,"june":6,"jul":7,"july":7,"aug":8,"august":8,"sep":9,"sept":9,"september":9,
    "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12,
    "জানুয়ারি":1,"ফেব্রুয়ারি":2,"মার্চ":3,"এপ্রিল":4,"মে":5,"জুন":6,"জুলাই":7,
    "আগস্ট":8,"সেপ্টেম্বর":9,"অক্টোবর":10,"নভেম্বর":11,"ডিসেম্বর":12,
}

def is_index(url: str, title: str = "") -> bool:
    u = safe_text(url).lower()
    t = normalize_title(title)
    if any(part in u for part in INDEX_URL_PARTS):
        return True
    if any(normalize_title(term) in t for term in INDEX_TITLE_TERMS):
        return True
    return "jobdetails" not in u and any(x in u for x in ("jobs", "companyoffered", "locationwise"))

def hard_noise(title: str, url: str, snippet: str = "") -> bool:
    blob = " ".join((title, url, snippet)).casefold()
    return any(safe_text(term).casefold() in blob for term in NOISE_TERMS)

def quick_filter(item: dict) -> tuple[bool, int, list[str]]:
    title = safe_text(item.get("title"))
    url = safe_text(item.get("url"))
    snippet = safe_text(item.get("snippet"))
    if not title or not url or not is_bdjobs_url(url):
        return False, 0, ["invalid"]
    if hard_noise(title, url, snippet):
        return False, 0, ["hard_noise"]
    if is_index(url, title):
        return True, 25, ["index"]
    blob = f"{title} {snippet}".casefold()
    general = sum(1 for t in GENERAL_JOB_TERMS if safe_text(t).casefold() in blob)
    target = sum(1 for t in TARGET_TITLE_TERMS if safe_text(t).casefold() in blob)
    non_target = sum(1 for t in NON_TARGET_TITLE_TERMS if safe_text(t).casefold() in normalize_title(title))
    early = sum(1 for t in EARLY_CAREER_TERMS if safe_text(t).casefold() in blob)
    score = 30 + min(20, general * 4) + min(30, target * 5) + min(10, early * 3) - min(25, non_target * 8)
    # Details pages can pass on weaker title snippets, but must have some job signal.
    return score >= 38, clamp(score), ["target" if target else "general"]

def extract_bdjobs_links(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = safe_text(a.get("href"))
        if not href:
            continue
        url = canonical_url(urljoin(base_url, href))
        if not is_bdjobs_url(url):
            continue
        label = safe_text(a.get_text(" ", strip=True)) or safe_text(a.get("aria-label")) or safe_text(a.get("title"))
        if not label:
            continue
        if "jobdetails" not in url.lower() and "companyofferedjobs" not in url.lower() and "jobsearch" not in url.lower():
            continue
        key = url
        if key in seen:
            continue
        seen.add(key)
        parent_text = ""
        if getattr(a, "parent", None):
            parent_text = safe_text(a.parent.get_text(" ", strip=True))[:700]
        rows.append({"url": url, "label": label[:220], "context": parent_text})
    return rows

def discover_index_pages(session: requests.Session, limit=10) -> list[dict]:
    out = []
    for url in BDJOBS_INDEX_URLS[:limit]:
        try:
            r = session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code >= 400:
                logger.warning("BDJOBS INDEX HTTP=%s url=%s", r.status_code, url)
                continue
            links = extract_bdjobs_links(r.text, r.url)
            detail_links = [x for x in links if "jobdetails" in x["url"].lower()]
            logger.info("BDJOBS INDEX url=%s links=%d detail_links=%d", url, len(links), len(detail_links))
            for row in detail_links[:250]:
                out.append({
                    "url": row["url"],
                    "canonical": canonical_url(row["url"]),
                    "title": row["label"],
                    "snippet": row["context"],
                    "published_date": "",
                    "source": "Bdjobs.com",
                    "source_type": "bdjobs",
                    "discovery": "bdjobs_index",
                })
        except Exception as exc:
            logger.warning("BDJOBS INDEX FAILED url=%s error=%s", url, exc)
    dedup = {}
    for item in out:
        dedup[item["canonical"]] = item
    return list(dedup.values())

def fetch_html(session: requests.Session, url: str) -> tuple[str, str, int]:
    try:
        r = session.get(url, headers={**HEADERS, "Referer":"https://jobs.bdjobs.com/"}, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return "", r.url, r.status_code
        return r.text, r.url, r.status_code
    except Exception:
        return "", url, 0

def _parse_date_token(raw: str, now: datetime) -> datetime | None:
    s = safe_text(raw)
    if not s:
        return None
    d = parse_datetime(s)
    if d:
        return d.astimezone(now.tzinfo)
    m = re.search(r"(\d{1,2})\s+([A-Za-z\u0980-\u09ff]+)\s+(20\d{2})", s)
    if m:
        mon = MONTHS.get(m.group(2).casefold())
        if mon:
            return datetime(int(m.group(3)), mon, int(m.group(1)), tzinfo=now.tzinfo)
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](20\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=now.tzinfo)
        except ValueError:
            pass
    return None

def extract_labeled(text: str, labels, max_chars=500) -> str:
    compact = safe_text(text)
    for label in labels:
        pat = re.compile(
            rf"{re.escape(label)}\s*[:\-]?\s*(.+?)(?=\s+(?:{'|'.join(re.escape(x) for x in labels + ['Deadline','Experience','Education','Location','Job Location','Salary','Vacancies','No. of Vacancies','How to Apply','Application'] )})\s*[:\-]|\s{{2,}}|$)",
            re.I,
        )
        m = pat.search(compact)
        if m:
            return safe_text(m.group(1))[:max_chars]
    return ""

def extract_dates(text: str, now: datetime) -> tuple[datetime|None, str, int, datetime|None, str]:
    # Prefer explicit labels for publication and deadline.
    deadline = None
    deadline_text = ""
    for label in ("Deadline for apply the job", "Application Deadline", "Deadline", "আবেদনের শেষ তারিখ"):
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([A-Za-z\u0980-\u09ff0-9./\- ]{{6,40}})", text, re.I)
        if m:
            deadline = _parse_date_token(m.group(1), now)
            if deadline:
                deadline_text = m.group(1).strip()
                break
    posted = None
    posted_source = ""
    conf = 0
    for label in ("Published", "Published Date", "Posted", "Posted Date", "Date Posted", "প্রকাশের তারিখ", "প্রকাশিত"):
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([A-Za-z\u0980-\u09ff0-9./\- ]{{6,40}})", text, re.I)
        if m:
            posted = _parse_date_token(m.group(1), now)
            if posted:
                posted_source = label
                conf = 100
                break
    return posted, posted_source, conf, deadline, deadline_text

def extract_job_record(candidate: dict, text: str, html: str, now: datetime) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")
    # Use visible text while preserving some table text.
    visible = safe_text(soup.get_text(" ", strip=True)) if html else safe_text(text)
    combined = " ".join(x for x in (visible, text) if x)
    title = extract_labeled(combined, ["Job Title", "Position", "পদের নাম"], 250) or safe_text(candidate.get("title"))
    company = extract_labeled(combined, ["Company Name", "Company", "Employer", "Organization", "প্রতিষ্ঠানের নাম", "প্রতিষ্ঠান"], 200)
    location = extract_labeled(combined, ["Job Location", "Location", "কর্মস্থল"], 220)
    education = extract_labeled(combined, ["Educational Qualification", "Education", "Qualification", "শিক্ষাগত যোগ্যতা"], 900)
    experience = extract_labeled(combined, ["Experience", "Work Experience", "অভিজ্ঞতা"], 500)
    salary = extract_labeled(combined, ["Salary", "Compensation", "Remuneration", "বেতন"], 300)
    vacancies = extract_labeled(combined, ["No. of Vacancies", "Vacancies", "Number of Vacancy", "Vacancy", "শূন্যপদ"], 120)
    job_type = extract_labeled(combined, ["Job Nature", "Job Type", "Employment Type", "Type", "চাকরির ধরন"], 160)
    age_limit = extract_labeled(combined, ["Age", "Age Limit", "বয়সসীমা"], 160)
    app_fee = extract_labeled(combined, ["Application Fee", "Fee", "আবেদন ফি"], 160)
    app_method = extract_labeled(combined, ["How to Apply", "Apply Instruction", "Application Procedure", "আবেদনের নিয়ম", "আবেদন পদ্ধতি"], 1000)
    app_period = extract_labeled(combined, ["Application Period", "Application Time", "আবেদনের সময়"], 350)
    selection = extract_labeled(combined, ["Selection Process", "Selection Procedure", "নিয়োগ প্রক্রিয়া"], 450)
    posted, posted_source, date_conf, deadline_dt, deadline_text = extract_dates(combined, now)
    if deadline_dt and deadline_dt.hour == 0:
        deadline_dt = deadline_dt.replace(hour=23, minute=59, second=59)
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = safe_text(a.get("href"))
        url = canonical_url(urljoin(candidate.get("url",""), href))
        if not url:
            continue
        label = safe_text(a.get_text(" ", strip=True)) or safe_text(a.get("aria-label")) or safe_text(a.get("title"))
        if not label:
            continue
        blob = f"{label} {safe_text(a.get('title'))} {safe_text(a.get('aria-label'))}".casefold()
        score = 0
        if any(x in blob for x in ("apply now", "apply online", "application form", "submit application", "apply")):
            score += 80
        if any(x in blob for x in ("application", "আবেদন")):
            score += 30
        if "jobdetails" in url.casefold():
            score -= 40
        if "login" in url.casefold() or "register" in url.casefold():
            score -= 30
        if score > 0 and url not in seen and is_bdjobs_url(url) or (score >= 80 and url not in seen):
            seen.add(url)
            links.append({"url": url, "label": label[:200], "deterministic_score": score})
    links.sort(key=lambda x: (-x["deterministic_score"], x["url"]))
    # Images from OpenGraph/JSON-LD only.
    images = []
    for tag in soup.find_all("meta"):
        prop = safe_text(tag.get("property") or tag.get("name")).casefold()
        content = safe_text(tag.get("content"))
        if content and prop in ("og:image", "twitter:image"):
            images.append(content)
    # Exa content can carry an image in the search result candidate.
    if candidate.get("image"):
        images.append(candidate["image"])
    images = list(dict.fromkeys(images))[:8]
    return {
        **candidate,
        "source_url": candidate.get("url"),
        "job_title": title,
        "company": company,
        "location": location,
        "education": education,
        "experience": experience,
        "salary": salary,
        "vacancies": vacancies,
        "job_type": job_type,
        "age_limit": age_limit,
        "application_fee": app_fee,
        "application_method": app_method,
        "application_period": app_period,
        "selection_process": selection,
        "deadline": deadline_text,
        "deadline_iso": deadline_dt.isoformat() if deadline_dt else "",
        "posted_date": posted.isoformat() if posted else safe_text(candidate.get("published_date")),
        "date_source": posted_source or ("exa_published_date" if candidate.get("published_date") else ""),
        "date_confidence": date_conf or (55 if candidate.get("published_date") else 0),
        "apply_link_candidates": links[:20],
        "image_candidates": images,
        "source_text": combined[:24000],
    }

def validate_active(job: dict, now: datetime) -> tuple[bool, list[str]]:
    reasons = []
    if not safe_text(job.get("job_title")):
        reasons.append("missing_title")
    if not safe_text(job.get("company")):
        reasons.append("missing_company")
    blob = " ".join([
        safe_text(job.get("job_title")), safe_text(job.get("company")),
        safe_text(job.get("education")), safe_text(job.get("experience")),
        safe_text(job.get("source_text"))[:8000],
    ]).casefold()
    vacancy_signals = sum(1 for x in ("vacancy", "hiring", "recruit", "apply", "application", "position", "job", "নিয়োগ", "চাকরি", "আবেদন") if x in blob)
    if vacancy_signals < 2:
        reasons.append("weak_vacancy_evidence")
    deadline = parse_datetime(job.get("deadline_iso"))
    if deadline and deadline < now:
        reasons.append("expired_deadline")
    # Because discovery is BDjobs-only, no outside-domain source can enter.
    return not reasons, reasons

def audience_score(job: dict) -> tuple[int, list[str]]:
    title = normalize_title(job.get("job_title"))
    blob = normalize_title(" ".join([
        safe_text(job.get("job_title")), safe_text(job.get("education")),
        safe_text(job.get("experience")), safe_text(job.get("job_type")),
        safe_text(job.get("source_text"))[:9000],
    ]))
    score = 35
    reasons = []
    target_hits = sum(1 for term in TARGET_TITLE_TERMS if normalize_title(term) in title)
    target_body = sum(1 for term in BBA_MBA_TERMS if normalize_title(term) in blob)
    early_hits = sum(1 for term in EARLY_CAREER_TERMS if normalize_title(term) in blob)
    senior_hits = sum(1 for term in SENIOR_TERMS if normalize_title(term) in blob or normalize_title(term) in title)
    score += min(30, target_hits * 8)
    score += min(18, target_body * 3)
    score += min(18, early_hits * 4)
    score -= min(30, senior_hits * 8)
    if any(x in title for x in ("management trainee","graduate trainee","intern","business analyst","financial analyst","credit analyst","relationship officer","relationship manager")):
        score += 12
        reasons.append("direct early-career role")
    if any(x in blob for x in ("bba","mba","business administration")):
        score += 10
        reasons.append("BBA/MBA signal")
    if any(x in title for x in ("finance","account","bank","marketing","sales","hr","human resources","business","management","procurement","supply chain","operations")):
        reasons.append("target business function")
    if any(normalize_title(x) in title for x in NON_TARGET_TITLE_TERMS) and target_hits == 0:
        score -= 25
    return clamp(score), reasons[:6]
