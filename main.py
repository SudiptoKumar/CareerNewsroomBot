"""
Career Newsroom Bot
Bangladesh job discovery/extraction foundation.

V1 implementation focus:
- Source registry loading
- Repository state management
- Direct HTML/RSS/PDF discovery
- Google News RSS discovery
- Exa discovery
- Deterministic candidate extraction
- Cerebras structured extraction
- Safe, failure-isolated orchestration

Publishing/intelligence hooks are kept explicit so later steps can extend them
without changing the repository contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup

try:
    from exa_py import Exa
except Exception:
    Exa = None  # type: ignore

try:
    from cerebras.cloud.sdk import Cerebras
except Exception:
    Cerebras = None  # type: ignore

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None  # type: ignore

try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore


ROOT = Path(__file__).resolve().parent
REGISTRY_FILE = ROOT / "source_registry.json"
STATE_FILE = ROOT / "job_state.json"
POSTED_URLS_FILE = ROOT / "posted_urls.txt"

DEFAULT_CHANNEL = "@CareerNewsroom"
DEFAULT_TIMEOUT = 20
MAX_CANDIDATES = 250

BANGLADESH_TERMS = (
    "bangladesh", "dhaka", "chattogram", "chittagong", "sylhet",
    "khulna", "rajshahi", "barishal", "barisal", "rangpur", "mymensingh",
    "gov.bd", "edu.bd", "org.bd", "teletalk.com.bd",
    "নিয়োগ", "চাকরি", "চাকরির", "বিজ্ঞপ্তি", "আবেদন", "নিয়োগ বিজ্ঞপ্তি",
)

JOB_TERMS = (
    "job", "jobs", "career", "careers", "vacancy", "vacancies", "recruit",
    "recruitment", "hiring", "position", "employment", "internship",
    "চাকরি", "নিয়োগ", "পদ", "শূন্যপদ", "আবেদন", "নিয়োগ বিজ্ঞপ্তি",
)


class ConfigError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict[str, str]:
    config = {
        "EXA_API_KEY": os.getenv("EXA_API_KEY", ""),
        "CEREBRAS_API_KEY": os.getenv("CEREBRAS_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "CEREBRAS_MODEL": os.getenv("CEREBRAS_MODEL", ""),
        "TELEGRAM_CHANNEL": os.getenv("TELEGRAM_CHANNEL", DEFAULT_CHANNEL),
        "TELEGRAM_ADMIN_CHAT_ID": os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""),
    }
    return config


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    json.loads(temp.read_text(encoding="utf-8"))
    temp.replace(path)


def load_registry() -> dict[str, Any]:
    registry = load_json(REGISTRY_FILE, {"version": 1, "sources": []})
    registry.setdefault("sources", [])
    return registry


def load_state() -> dict[str, Any]:
    state = load_json(
        STATE_FILE,
        {"version": 1, "updated_at": None, "jobs": {}, "sources": {}},
    )
    state.setdefault("jobs", {})
    state.setdefault("sources", {})
    return state


def load_posted_urls() -> set[str]:
    if not POSTED_URLS_FILE.exists():
        return set()
    return {
        line.strip()
        for line in POSTED_URLS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_posted_urls(urls: set[str]) -> None:
    temp = POSTED_URLS_FILE.with_suffix(".tmp")
    temp.write_text("\n".join(sorted(urls)) + ("\n" if urls else ""), encoding="utf-8")
    temp.replace(POSTED_URLS_FILE)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        return url

    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
    }
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in tracking]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    cleaned = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        query=urlencode(query),
        fragment="",
    )
    return urlunparse(cleaned)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "CareerNewsroomBot/0.1 "
                "(+https://t.me/CareerNewsroom; Bangladesh job aggregation)"
            )
        }
    )
    return session


def fetch_url(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = 3,
) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response.content, response.headers.get("Content-Type", ""), response.url
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    raise RuntimeError(f"Fetch failed: {url} ({last_error})")


def likely_bangladesh(text: str, url: str = "") -> bool:
    haystack = f"{url}\n{text}".lower()
    return any(term in haystack for term in BANGLADESH_TERMS)


def likely_job(text: str, url: str = "") -> bool:
    haystack = f"{url}\n{text}".lower()
    return any(term in haystack for term in JOB_TERMS)


def extract_links(base_url: str, html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = urljoin(base_url, tag.get("href", ""))
        normalized = normalize_url(href)
        if normalized:
            links.append(normalized)
    return list(dict.fromkeys(links))


def html_to_text(html: bytes, url: str = "") -> str:
    extracted = trafilatura.extract(
        html,
        url=url or None,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if extracted:
        return extracted.strip()

    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    return soup.get_text("\n", strip=True)


def pdf_to_text(content: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")
    from io import BytesIO

    reader = PdfReader(BytesIO(content))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages).strip()


def deterministic_extract(text: str, source_url: str = "") -> dict[str, Any]:
    compact = re.sub(r"[ \t]+", " ", text or "").strip()

    date_patterns = [
        r"(?i)(?:deadline|last date|last date of application|closing date)"
        r".{0,50}?(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
        r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"(?i)(?:আবেদনের শেষ তারিখ|শেষ তারিখ).{0,50}?"
        r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    ]

    deadline_raw = None
    for pattern in date_patterns:
        match = re.search(pattern, compact)
        if match:
            deadline_raw = match.group(0)
            break

    salary = re.search(
        r"(?i)(?:salary|বেতন).{0,40}?"
        r"(?:(?:৳|tk\.?|bdt)\s*)?[\d,]+(?:\s*[-–]\s*[\d,]+)?",
        compact,
    )

    vacancy = re.search(
        r"(?i)(?:vacanc(?:y|ies)|no\.?\s*of\s*(?:post|posts)|পদসংখ্যা|শূন্যপদ)"
        r".{0,30}?\d{1,5}",
        compact,
    )

    return {
        "source_url": normalize_url(source_url),
        "text_hash": content_hash(compact),
        "deadline_hint": deadline_raw,
        "salary_hint": salary.group(0) if salary else None,
        "vacancy_hint": vacancy.group(0) if vacancy else None,
        "bangladesh_hint": likely_bangladesh(compact, source_url),
        "job_hint": likely_job(compact, source_url),
    }


def google_news_rss_url(query: str) -> str:
    from urllib.parse import quote_plus

    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-BD&gl=BD&ceid=BD:en"
    )


def discover_google_news(query: str, session: requests.Session) -> list[dict[str, Any]]:
    url = google_news_rss_url(query)
    parsed = feedparser.parse(session.get(url, timeout=DEFAULT_TIMEOUT).content)
    candidates: list[dict[str, Any]] = []
    for entry in parsed.entries[:30]:
        link = normalize_url(getattr(entry, "link", ""))
        if not link:
            continue
        candidates.append(
            {
                "url": link,
                "source_id": "google_news",
                "discovery_method": "google_news_rss",
                "title_hint": getattr(entry, "title", ""),
                "published_hint": getattr(entry, "published", ""),
            }
        )
    return candidates


def discover_rss(source: dict[str, Any], session: requests.Session) -> list[dict[str, Any]]:
    feed_urls = source.get("rss_urls") or []
    results: list[dict[str, Any]] = []
    for feed_url in feed_urls:
        try:
            parsed = feedparser.parse(
                session.get(feed_url, timeout=DEFAULT_TIMEOUT).content
            )
        except requests.RequestException:
            continue
        for entry in parsed.entries[:50]:
            link = normalize_url(getattr(entry, "link", ""))
            if link:
                results.append(
                    {
                        "url": link,
                        "source_id": source["id"],
                        "discovery_method": "rss",
                        "title_hint": getattr(entry, "title", ""),
                    }
                )
    return results


def discover_direct(source: dict[str, Any], session: requests.Session) -> list[dict[str, Any]]:
    url = normalize_url(source.get("url", ""))
    if not url:
        return []

    content, content_type, final_url = fetch_url(session, url)
    if "rss" in content_type.lower() or final_url.lower().endswith((".xml", ".rss")):
        parsed = feedparser.parse(content)
        return [
            {
                "url": normalize_url(getattr(entry, "link", "")),
                "source_id": source["id"],
                "discovery_method": "direct_rss",
                "title_hint": getattr(entry, "title", ""),
            }
            for entry in parsed.entries[:50]
            if getattr(entry, "link", "")
        ]

    links = extract_links(final_url, content)
    base_domain = urlparse(final_url).netloc
    candidates = []
    for link in links:
        if urlparse(link).netloc != base_domain:
            continue
        path_text = urlparse(link).path.lower()
        if likely_job(path_text, link):
            candidates.append(
                {
                    "url": link,
                    "source_id": source["id"],
                    "discovery_method": "direct_html",
                    "title_hint": "",
                }
            )
    return candidates[:50]


def discover_exa(
    queries: Iterable[str],
    api_key: str,
    num_results: int = 10,
) -> list[dict[str, Any]]:
    if not api_key or Exa is None:
        return []

    exa = Exa(api_key=api_key)
    results: list[dict[str, Any]] = []

    for query in queries:
        try:
            response = exa.search_and_contents(
                query,
                type="auto",
                num_results=num_results,
                contents={"text": {"max_characters": 5000}},
            )
        except Exception as exc:
            logging.warning("Exa query failed: %s", exc)
            continue

        for item in getattr(response, "results", []) or []:
            url = normalize_url(getattr(item, "url", ""))
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "source_id": "exa_discovery",
                    "discovery_method": "exa",
                    "title_hint": getattr(item, "title", ""),
                    "exa_text": getattr(item, "text", "") or "",
                }
            )

    return results


def extract_candidate(
    candidate: dict[str, Any],
    session: requests.Session,
) -> dict[str, Any] | None:
    url = normalize_url(candidate.get("url", ""))
    if not url:
        return None

    try:
        content, content_type, final_url = fetch_url(session, url)
    except Exception as exc:
        logging.warning("Candidate fetch failed: %s (%s)", url, exc)
        return None

    if "pdf" in content_type.lower() or final_url.lower().endswith(".pdf"):
        try:
            text = pdf_to_text(content)
        except Exception as exc:
            logging.warning("PDF extraction failed: %s (%s)", final_url, exc)
            return None
    else:
        text = html_to_text(content, final_url)

    hints = deterministic_extract(text, final_url)
    if not hints["job_hint"] or not hints["bangladesh_hint"]:
        # Exa text/title may still contain useful evidence, but do not flood the queue.
        combined = f"{candidate.get('title_hint', '')}\n{candidate.get('exa_text', '')}"
        if not likely_job(combined, final_url) or not likely_bangladesh(combined, final_url):
            return None

    return {
        "url": final_url,
        "source_id": candidate.get("source_id", ""),
        "discovery_method": candidate.get("discovery_method", ""),
        "title_hint": candidate.get("title_hint", ""),
        "content_type": content_type,
        "text": text[:20000],
        "hints": hints,
    }


def cerebras_extract_job(
    text: str,
    source_name: str,
    source_url: str,
    api_key: str,
    model: str,
) -> dict[str, Any] | None:
    if not api_key or Cerebras is None or not model:
        return None

    client = Cerebras(api_key=api_key)

    system = (
        "You are a structured Bangladesh job-data extraction engine. "
        "Extract only information explicitly supported by the supplied source. "
        "Never invent salary, vacancy, deadline, eligibility, benefits, or employer data. "
        "Return valid JSON only. Use null for missing scalar values and [] for missing arrays."
    )

    user = {
        "source_name": source_name,
        "source_url": source_url,
        "text": text[:16000],
        "schema": {
            "is_job": "boolean",
            "company": "string|null",
            "title": "string|null",
            "organization_type": "string|null",
            "sector": "string|null",
            "job_function": "string|null",
            "job_level": "string|null",
            "employment_type": "string|null",
            "location": "string|null",
            "vacancy": "string|null",
            "salary": "string|null",
            "education": "array",
            "experience": "string|null",
            "skills": "array",
            "age_limit": "string|null",
            "gender": "string|null",
            "published_date": "string|null",
            "application_start": "string|null",
            "deadline": "string|null",
            "application_method": "string|null",
            "application_url": "string|null",
            "requirements": "array",
            "responsibilities": "array",
            "confidence": "number 0..1",
        },
    }

    try:
        completion = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        )
        content = completion.choices[0].message.content
        if not content:
            return None
        return json.loads(content)
    except Exception as exc:
        logging.warning("Cerebras extraction failed for %s: %s", source_url, exc)
        return None


def source_queries(registry: dict[str, Any]) -> list[str]:
    queries = [
        '"Bangladesh" "job circular"',
        '"Bangladesh" recruitment vacancy',
        '"নিয়োগ বিজ্ঞপ্তি" Bangladesh',
        '"চাকরির বিজ্ঞপ্তি" Bangladesh',
        'site:gov.bd "নিয়োগ বিজ্ঞপ্তি"',
        'site:gov.bd recruitment Bangladesh',
        'site:edu.bd job circular',
        'Bangladesh bank recruitment',
        'Bangladesh NGO jobs',
        'Bangladesh pharmaceutical jobs',
        'Bangladesh healthcare jobs',
        'Bangladesh university recruitment',
        'Bangladesh software engineer jobs',
        'Bangladesh garments jobs',
        'Bangladesh manufacturing jobs',
    ]
    # Add employer-focused discovery queries from registry when available.
    for source in registry.get("sources", []):
        if source.get("discovery_queries"):
            queries.extend(source["discovery_queries"][:3])
    return list(dict.fromkeys(queries))


def select_due_sources(registry: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    due = []
    now = time.time()
    for source in registry.get("sources", []):
        if not source.get("enabled", source.get("status") == "active"):
            continue

        source_state = state["sources"].get(source["id"], {})
        last_crawl = source_state.get("last_crawl_epoch", 0)
        interval_minutes = int(source.get("crawl_minutes", 60) or 60)

        if now - last_crawl >= interval_minutes * 60:
            due.append(source)

    # Always prefer P0, then P1, then the rest.
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return sorted(due, key=lambda s: (order.get(s.get("priority", "P3"), 3), s["id"]))


def record_source_result(
    state: dict[str, Any],
    source_id: str,
    success: bool,
    jobs_found: int = 0,
) -> None:
    entry = state["sources"].setdefault(source_id, {})
    entry["last_crawl_epoch"] = time.time()
    entry["last_success"] = utc_now() if success else entry.get("last_success")
    entry["last_failure"] = None if success else utc_now()
    entry["consecutive_failures"] = 0 if success else int(entry.get("consecutive_failures", 0)) + 1
    entry["jobs_found_last_run"] = jobs_found


def make_event_id(job: dict[str, Any]) -> str:
    pieces = [
        str(job.get("company") or "").strip().lower(),
        str(job.get("title") or "").strip().lower(),
        str(job.get("deadline") or "").strip().lower(),
    ]
    raw = "|".join(pieces)
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def process_run(config: dict[str, str]) -> dict[str, Any]:
    registry = load_registry()
    state = load_state()
    posted_urls = load_posted_urls()
    session = request_session()

    candidates: list[dict[str, Any]] = []

    due_sources = select_due_sources(registry, state)
    for source in due_sources:
        try:
            found = discover_direct(source, session)
            candidates.extend(found)
            record_source_result(state, source["id"], True, len(found))
        except Exception as exc:
            logging.warning("Source failed: %s (%s)", source["id"], exc)
            record_source_result(state, source["id"], False)

    # Google News RSS discovery.
    google_queries = [
        '"Bangladesh job circular"',
        '"নিয়োগ বিজ্ঞপ্তি"',
        '"চাকরির বিজ্ঞপ্তি"',
        '"Bangladesh recruitment"',
    ]
    for query in google_queries:
        try:
            candidates.extend(discover_google_news(query, session))
        except Exception as exc:
            logging.warning("Google News RSS failed: %s", exc)

    # Exa discovery is intentionally bounded.
    exa_queries = source_queries(registry)[:25]
    candidates.extend(discover_exa(exa_queries, config["EXA_API_KEY"], num_results=6))

    # Normalize and remove URL duplicates.
    unique_candidates: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        url = normalize_url(candidate.get("url", ""))
        if not url:
            continue
        unique_candidates.setdefault(url, candidate)

    candidate_list = list(unique_candidates.values())[:MAX_CANDIDATES]

    extracted_jobs: list[dict[str, Any]] = []
    for candidate in candidate_list:
        if candidate["url"] in posted_urls:
            continue

        extracted = extract_candidate(candidate, session)
        if not extracted:
            continue

        source_name = candidate.get("source_id", "")
        ai_job = cerebras_extract_job(
            extracted["text"],
            source_name,
            extracted["url"],
            config["CEREBRAS_API_KEY"],
            config["CEREBRAS_MODEL"],
        )

        if ai_job is not None and ai_job.get("is_job") is False:
            continue

        job = {
            **(ai_job or {}),
            "source_url": extracted["url"],
            "source_id": source_name,
            "discovery_method": candidate.get("discovery_method", ""),
            "first_seen": utc_now(),
            "last_seen": utc_now(),
        }

        if job.get("company") or job.get("title"):
            job["event_id"] = make_event_id(job)
            extracted_jobs.append(job)

    # Persist raw normalized events in repository state.
    for job in extracted_jobs:
        event_id = job["event_id"]
        existing = state["jobs"].get(event_id)
        if existing:
            existing.update(
                {
                    "last_seen": utc_now(),
                    "latest_source_url": job.get("source_url"),
                    "latest_record": job,
                }
            )
        else:
            state["jobs"][event_id] = {
                "event_id": event_id,
                "status": "candidate",
                "first_seen": utc_now(),
                "last_seen": utc_now(),
                "latest_source_url": job.get("source_url"),
                "latest_record": job,
                "telegram": {
                    "published": False,
                    "message_id": None,
                },
            }

    state["updated_at"] = utc_now()
    atomic_write_json(STATE_FILE, state)

    # Do NOT add URLs to posted_urls.txt here.
    # A URL belongs in posted_urls.txt only after Telegram publication succeeds.
    # This prevents a failed/unpublished job from becoming permanently skipped.

    return {
        "sources_due": len(due_sources),
        "candidates": len(candidate_list),
        "jobs_extracted": len(extracted_jobs),
        "state_events": len(state["jobs"]),
    }


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def main() -> int:
    configure_logging()
    config = load_config()

    try:
        result = process_run(config)
    except Exception:
        logging.exception("Fatal run error")
        return 1

    logging.info(
        "Run complete | sources_due=%s candidates=%s jobs_extracted=%s state_events=%s",
        result["sources_due"],
        result["candidates"],
        result["jobs_extracted"],
        result["state_events"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
