"""
Career Newsroom Bot - production-oriented V1

Architecture:
  discovery -> retrieval -> extraction -> normalization -> event matching
  -> verification -> quality/importance scoring -> Telegram publishing -> state

No external database. Persistent state lives in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

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
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None  # type: ignore


ROOT = Path(__file__).resolve().parent
REGISTRY_FILE = ROOT / "source_registry.json"
STATE_FILE = ROOT / "job_state.json"
POSTED_URLS_FILE = ROOT / "posted_urls.txt"
TMP_DIR = ROOT / ".runtime"

DEFAULT_CHANNEL = "@CareerNewsroom"
DEFAULT_TIMEOUT = 20
MAX_CANDIDATES = 180
MAX_EXA_QUERIES = 20
MAX_JOBS_PER_RUN = 12
DEFAULT_CEREBRAS_MODEL = "gpt-oss-120b"

LOG = logging.getLogger("career_newsroom")

BANGLADESH_TERMS = (
    "bangladesh", "dhaka", "chattogram", "chittagong", "sylhet",
    "khulna", "rajshahi", "barishal", "barisal", "rangpur", "mymensingh",
    "gazipur", "narayanganj", "cumilla", "comilla", "jessore", "jashore",
    "gov.bd", "edu.bd", "org.bd", "teletalk.com.bd", "jobs.gov.bd",
    "নিয়োগ", "চাকরি", "চাকরির", "বিজ্ঞপ্তি", "আবেদন", "নিয়োগ বিজ্ঞপ্তি",
)

JOB_TERMS = (
    "job", "jobs", "career", "careers", "vacancy", "vacancies", "recruit",
    "recruitment", "hiring", "position", "employment", "internship",
    "job circular", "career opportunity", "apply now", "application",
    "চাকরি", "নিয়োগ", "পদ", "শূন্যপদ", "আবেদন", "নিয়োগ বিজ্ঞপ্তি",
)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}

SCAM_TERMS = (
    "pay fee", "registration fee", "processing fee", "security deposit",
    "send money", "bikash", "bkash", "nagad", "rocket", "personal account",
    "upfront payment", "application fee",
)

FIELD_LABELS = {
    "deadline": (
        r"(?i)(?:application\s+)?(?:deadline|last\s+date|closing\s+date)"
        r".{0,80}?(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
        r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        r"(?i)(?:আবেদনের শেষ তারিখ|শেষ তারিখ).{0,80}?"
        r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    ),
    "salary": (
        r"(?i)(?:salary|বেতন).{0,50}?"
        r"(?:(?:৳|tk\.?|bdt)\s*)?[\d,]+(?:\s*[-–]\s*[\d,]+)?",
    ),
    "vacancy": (
        r"(?i)(?:vacanc(?:y|ies)|no\.?\s*of\s*(?:post|posts)|"
        r"পদসংখ্যা|শূন্যপদ).{0,40}?\d{1,6}",
    ),
}


class RetryableError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
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
        {"version": 2, "updated_at": None, "jobs": {}, "sources": {}, "runs": []},
    )
    state.setdefault("jobs", {})
    state.setdefault("sources", {})
    state.setdefault("runs", [])
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
    temp.write_text(
        "\n".join(sorted(urls)) + ("\n" if urls else ""),
        encoding="utf-8",
    )
    temp.replace(POSTED_URLS_FILE)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
            query=urlencode(query),
            fragment="",
        )
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/128 Safari/537.36 "
                "CareerNewsroomBot/1.0"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/pdf;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "en-US,en;q=0.8,bn;q=0.7",
            "Cache-Control": "no-cache",
        }
    )
    return session


def fetch(
    session: requests.Session,
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = 3,
) -> tuple[bytes, str, str, int]:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            status = r.status_code
            if status >= 400:
                raise RetryableError(f"HTTP {status}")
            return r.content, r.headers.get("Content-Type", ""), r.url, status
        except (requests.RequestException, RetryableError, ValueError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(min(attempt * 1.5, 4))
    raise RuntimeError(f"Fetch failed: {url} ({last_exc})")


def looks_like_bangladesh(text: str, url: str = "") -> bool:
    haystack = f"{url}\n{text}".lower()
    return any(term in haystack for term in BANGLADESH_TERMS)


def looks_like_job(text: str, url: str = "") -> bool:
    haystack = f"{url}\n{text}".lower()
    return any(term in haystack for term in JOB_TERMS)


def html_to_text(content: bytes, url: str = "") -> str:
    extracted = trafilatura.extract(
        content,
        url=url or None,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if extracted and len(extracted.strip()) >= 120:
        return extracted.strip()

    soup = BeautifulSoup(content, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    return soup.get_text("\n", strip=True)


def pdf_to_text(content: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks).strip()


def extract_meta(html_bytes: bytes, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_bytes, "html.parser")
    meta: dict[str, Any] = {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "description": "",
        "image": "",
        "logo": "",
        "job_url": base_url,
    }

    og = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            og[key.lower()] = value.strip()

    meta["description"] = (
        og.get("og:description")
        or og.get("description")
        or ""
    )
    image = og.get("og:image") or ""
    if image:
        meta["image"] = urljoin(base_url, image)

    # JSON-LD logo/image/job posting hints.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "JobPosting":
                meta["jobposting"] = item
            logo = item.get("logo")
            if isinstance(logo, dict):
                logo = logo.get("url")
            if isinstance(logo, str) and not meta["logo"]:
                meta["logo"] = urljoin(base_url, logo)
            image_val = item.get("image")
            if isinstance(image_val, dict):
                image_val = image_val.get("url")
            if isinstance(image_val, str) and not meta["image"]:
                meta["image"] = urljoin(base_url, image_val)
    return meta


def extract_links(base_url: str, html_bytes: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(html_bytes, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = normalize_url(urljoin(base_url, a.get("href", "")))
        if not href or href in seen:
            continue
        seen.add(href)
        text = a.get_text(" ", strip=True)
        results.append({"url": href, "text": text})
    return results


def deterministic_hints(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"[ \t]+", " ", text or "").strip()
    hints: dict[str, Any] = {
        "text_hash": sha256_text(cleaned),
        "bangladesh_hint": looks_like_bangladesh(cleaned),
        "job_hint": looks_like_job(cleaned),
        "deadline": None,
        "salary": None,
        "vacancy": None,
        "scam_signals": [],
    }

    for pattern in FIELD_LABELS["deadline"]:
        match = re.search(pattern, cleaned)
        if match:
            hints["deadline"] = match.group(0)
            break

    for pattern in FIELD_LABELS["salary"]:
        match = re.search(pattern, cleaned)
        if match:
            hints["salary"] = match.group(0)
            break

    for pattern in FIELD_LABELS["vacancy"]:
        match = re.search(pattern, cleaned)
        if match:
            hints["vacancy"] = match.group(0)
            break

    lower = cleaned.lower()
    hints["scam_signals"] = [term for term in SCAM_TERMS if term in lower]
    return hints


def direct_source_candidates(
    source: dict[str, Any],
    session: requests.Session,
) -> list[dict[str, Any]]:
    url = normalize_url(source.get("url", ""))
    if not url:
        return []

    content, content_type, final_url, _ = fetch(session, url)

    if "pdf" in content_type.lower() or final_url.lower().endswith(".pdf"):
        text = pdf_to_text(content)
        return [{
            "url": final_url,
            "source_id": source["id"],
            "discovery_method": "direct_pdf",
            "title_hint": source.get("name", ""),
            "text_hint": text[:12000],
            "meta": {},
        }] if text else []

    if "xml" in content_type.lower() or final_url.lower().endswith((".rss", ".xml")):
        feed = feedparser.parse(content)
        return [
            {
                "url": normalize_url(getattr(e, "link", "")),
                "source_id": source["id"],
                "discovery_method": "direct_rss",
                "title_hint": getattr(e, "title", ""),
                "text_hint": BeautifulSoup(
                    getattr(e, "summary", "") or "",
                    "html.parser",
                ).get_text(" ", strip=True),
                "meta": {},
            }
            for e in feed.entries[:60]
            if getattr(e, "link", "")
        ]

    text = html_to_text(content, final_url)
    meta = extract_meta(content, final_url)

    candidates: list[dict[str, Any]] = []

    # Important: the source landing page itself can be the job page.
    if looks_like_job(text, final_url) and (
        "job" in text.lower()
        or "vacancy" in text.lower()
        or "career" in text.lower()
        or "recruit" in text.lower()
        or "নিয়োগ" in text
        or "চাকরি" in text
    ):
        candidates.append(
            {
                "url": final_url,
                "source_id": source["id"],
                "discovery_method": "direct_page",
                "title_hint": meta.get("title", ""),
                "text_hint": text[:16000],
                "meta": meta,
            }
        )

    for link in extract_links(final_url, content):
        link_hay = f"{link['text']} {link['url']}".lower()
        # Use both URL and anchor text. Many career systems have opaque URLs.
        score = 0
        if looks_like_job(link_hay):
            score += 2
        if any(k in link["text"].lower() for k in ("apply", "vacancy", "career", "position", "নিয়োগ", "চাকরি")):
            score += 2
        if source.get("official", False):
            score += 1
        if score >= 2:
            candidates.append(
                {
                    "url": link["url"],
                    "source_id": source["id"],
                    "discovery_method": "direct_link",
                    "title_hint": link["text"],
                    "text_hint": "",
                    "meta": {},
                }
            )

    # Deduplicate while preserving order.
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["url"], candidate)
    return list(unique.values())[:60]


def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-BD&gl=BD&ceid=BD:en"
    )


def discover_google_news(
    queries: Iterable[str],
    session: requests.Session,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for query in queries:
        try:
            r = session.get(google_news_url(query), timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as exc:
            LOG.warning("Google News RSS failed: %s", exc)
            continue

        for e in parsed.entries[:30]:
            link = normalize_url(getattr(e, "link", ""))
            if not link:
                continue
            out.append(
                {
                    "url": link,
                    "source_id": "google_news",
                    "discovery_method": "google_news_rss",
                    "title_hint": getattr(e, "title", ""),
                    "text_hint": BeautifulSoup(
                        getattr(e, "summary", "") or "",
                        "html.parser",
                    ).get_text(" ", strip=True),
                    "meta": {},
                }
            )
    return out


def exa_search(
    queries: Iterable[str],
    api_key: str,
    num_results: int = 8,
) -> list[dict[str, Any]]:
    if not api_key or Exa is None:
        if not api_key:
            LOG.warning("EXA_API_KEY is not configured")
        else:
            LOG.warning("exa-py is unavailable")
        return []

    exa = Exa(api_key=api_key)
    out: list[dict[str, Any]] = []

    for query in queries:
        try:
            # Current SDK path. Fall back for older installed SDKs.
            if hasattr(exa, "search"):
                response = exa.search(
                    query,
                    type="auto",
                    num_results=num_results,
                    contents={"text": {"max_characters": 8000}},
                )
            else:
                response = exa.search_and_contents(
                    query,
                    type="auto",
                    num_results=num_results,
                    contents={"text": {"max_characters": 8000}},
                )
        except Exception as exc:
            LOG.warning("Exa query failed: %s", exc)
            continue

        for result in getattr(response, "results", []) or []:
            url = normalize_url(getattr(result, "url", ""))
            if not url:
                continue
            text = getattr(result, "text", "") or ""
            highlights = getattr(result, "highlights", None)
            if isinstance(highlights, list):
                text = "\n".join(str(x) for x in highlights) + "\n" + text
            out.append(
                {
                    "url": url,
                    "source_id": "exa_discovery",
                    "discovery_method": "exa",
                    "title_hint": getattr(result, "title", "") or "",
                    # Critical fix: retain Exa content even if direct fetch blocks us.
                    "text_hint": text[:16000],
                    "meta": {},
                }
            )
    return out


def fetch_candidate_content(
    candidate: dict[str, Any],
    session: requests.Session,
) -> dict[str, Any] | None:
    url = candidate.get("url", "")
    text_hint = candidate.get("text_hint", "") or ""
    meta = candidate.get("meta", {}) or {}

    # Use already available source/Exa content first.
    if len(text_hint.strip()) >= 100:
        hints = deterministic_hints(text_hint)
        if hints["job_hint"] or looks_like_job(candidate.get("title_hint", ""), url):
            return {
                "url": url,
                "source_id": candidate.get("source_id", ""),
                "discovery_method": candidate.get("discovery_method", ""),
                "title_hint": candidate.get("title_hint", ""),
                "text": text_hint[:20000],
                "meta": meta,
                "hints": hints,
            }

    try:
        content, content_type, final_url, _ = fetch(session, url)
    except Exception as exc:
        LOG.warning("Candidate fetch failed: %s (%s)", url, exc)
        # Exa/content hint fallback even when HTTP fails.
        if len(text_hint.strip()) >= 100:
            return {
                "url": url,
                "source_id": candidate.get("source_id", ""),
                "discovery_method": candidate.get("discovery_method", ""),
                "title_hint": candidate.get("title_hint", ""),
                "text": text_hint[:20000],
                "meta": meta,
                "hints": deterministic_hints(text_hint),
            }
        return None

    if "pdf" in content_type.lower() or final_url.lower().endswith(".pdf"):
        text = pdf_to_text(content)
    else:
        text = html_to_text(content, final_url)
        if not meta:
            meta = extract_meta(content, final_url)

    if not text:
        text = text_hint

    hints = deterministic_hints(text)
    if not (
        hints["job_hint"]
        or looks_like_job(candidate.get("title_hint", ""), final_url)
    ):
        return None

    if not (
        hints["bangladesh_hint"]
        or looks_like_bangladesh(
            f"{candidate.get('title_hint', '')}\n{text}",
            final_url,
        )
    ):
        return None

    return {
        "url": final_url,
        "source_id": candidate.get("source_id", ""),
        "discovery_method": candidate.get("discovery_method", ""),
        "title_hint": candidate.get("title_hint", ""),
        "text": text[:20000],
        "meta": meta,
        "hints": hints,
    }


def parse_json_response(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def cerebras_json(
    system_prompt: str,
    payload: dict[str, Any],
    api_key: str,
    model: str,
) -> dict[str, Any] | None:
    if not api_key or Cerebras is None:
        return None
    model = model or DEFAULT_CEREBRAS_MODEL

    client = Cerebras(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
        )
        content = response.choices[0].message.content
        return parse_json_response(content)
    except Exception as exc:
        LOG.warning("Cerebras request failed: %s", exc)
        return None


def extract_job_with_ai(
    document: dict[str, Any],
    config: dict[str, str],
) -> dict[str, Any] | None:
    payload = {
        "source_name": document.get("source_id", ""),
        "source_url": document.get("url", ""),
        "title_hint": document.get("title_hint", ""),
        "deterministic_hints": document.get("hints", {}),
        "text": document.get("text", "")[:16000],
    }
    system = (
        "You are a strict Bangladesh job-listing extraction engine. "
        "Return JSON only. Extract only facts supported by the supplied text. "
        "Never invent salary, vacancy, dates, education, experience, benefits, "
        "company identity, or eligibility. Missing scalar fields must be null; "
        "missing arrays must be []. Set is_job false when this is not a job listing."
    )
    result = cerebras_json(
        system,
        {
            **payload,
            "schema": {
                "is_job": "boolean",
                "company": "string|null",
                "title": "string|null",
                "organization_type": "string|null",
                "sector": "string|null",
                "job_function": "string|null",
                "job_level": "string|null",
                "job_type": "string|null",
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
                "summary": "string|null",
                "confidence": "number 0..1",
            },
        },
        config["CEREBRAS_API_KEY"],
        config["CEREBRAS_MODEL"],
    )

    if result is not None and result.get("is_job") is False:
        return None

    # Deterministic fallback keeps the pipeline alive if Cerebras is temporarily unavailable.
    if result is None:
        title = document.get("title_hint") or ""
        if not title and document.get("meta", {}).get("title"):
            title = document["meta"]["title"]
        if not title:
            return None
        result = {
            "is_job": True,
            "company": None,
            "title": title[:250],
            "organization_type": None,
            "sector": None,
            "job_function": None,
            "job_level": None,
            "job_type": None,
            "employment_type": None,
            "location": None,
            "vacancy": document["hints"].get("vacancy"),
            "salary": document["hints"].get("salary"),
            "education": [],
            "experience": None,
            "skills": [],
            "age_limit": None,
            "gender": None,
            "published_date": None,
            "application_start": None,
            "deadline": document["hints"].get("deadline"),
            "application_method": None,
            "application_url": document["url"],
            "requirements": [],
            "responsibilities": [],
            "summary": "",
            "confidence": 0.55,
        }

    result.setdefault("source_url", document["url"])
    result.setdefault("source_id", document["source_id"])
    result.setdefault("discovery_method", document["discovery_method"])
    result.setdefault("meta", document.get("meta", {}))
    result.setdefault("text_hash", document["hints"].get("text_hash"))
    return result


def clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(job)

    for key in (
        "company", "title", "organization_type", "sector", "job_function",
        "job_level", "job_type", "employment_type", "location", "vacancy",
        "salary", "experience", "age_limit", "gender", "published_date",
        "application_start", "deadline", "application_method", "application_url",
        "summary",
    ):
        normalized[key] = clean_value(normalized.get(key))

    normalized["education"] = [
        clean_value(x) for x in (normalized.get("education") or []) if clean_value(x)
    ]
    normalized["skills"] = [
        clean_value(x) for x in (normalized.get("skills") or []) if clean_value(x)
    ]
    normalized["requirements"] = [
        clean_value(x) for x in (normalized.get("requirements") or []) if clean_value(x)
    ]
    normalized["responsibilities"] = [
        clean_value(x) for x in (normalized.get("responsibilities") or []) if clean_value(x)
    ]

    normalized["application_url"] = normalize_url(
        normalized.get("application_url") or normalized.get("source_url") or ""
    )
    normalized["source_url"] = normalize_url(normalized.get("source_url") or "")

    # Infer company from obvious official-source metadata only.
    if not normalized.get("company"):
        title = str(normalized.get("title") or "")
        if " - " in title and normalized.get("source_id"):
            normalized["company"] = title.split(" - ")[-1][:180].strip()

    return normalized


def norm_identity(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9\u0980-\u09ff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def event_fingerprint(job: dict[str, Any]) -> str:
    strong = "|".join(
        [
            norm_identity(str(job.get("company") or "")),
            norm_identity(str(job.get("title") or "")),
            norm_identity(str(job.get("deadline") or "")),
            normalize_url(str(job.get("application_url") or "")),
        ]
    )
    return sha256_text(strong)[:24]


def semantic_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    fields = ("company", "title", "location", "deadline", "application_url")
    scores = []
    for field in fields:
        av = norm_identity(str(a.get(field) or ""))
        bv = norm_identity(str(b.get(field) or ""))
        if not av or not bv:
            continue
        if av == bv:
            scores.append(1.0)
        elif av in bv or bv in av:
            scores.append(0.75)
        else:
            at = set(av.split())
            bt = set(bv.split())
            union = at | bt
            scores.append(len(at & bt) / len(union) if union else 0.0)

    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def is_same_event(a: dict[str, Any], b: dict[str, Any]) -> bool:
    au = normalize_url(str(a.get("application_url") or ""))
    bu = normalize_url(str(b.get("application_url") or ""))
    if au and bu and au == bu:
        return True

    af = event_fingerprint(a)
    bf = event_fingerprint(b)
    if af == bf:
        return True

    company_a = norm_identity(str(a.get("company") or ""))
    company_b = norm_identity(str(b.get("company") or ""))
    title_a = norm_identity(str(a.get("title") or ""))
    title_b = norm_identity(str(b.get("title") or ""))

    if company_a and company_b and company_a == company_b and title_a and title_b:
        title_tokens_a = set(title_a.split())
        title_tokens_b = set(title_b.split())
        title_overlap = (
            len(title_tokens_a & title_tokens_b) / len(title_tokens_a | title_tokens_b)
            if (title_tokens_a | title_tokens_b) else 0
        )
        if title_overlap >= 0.7:
            da = norm_identity(str(a.get("deadline") or ""))
            db = norm_identity(str(b.get("deadline") or ""))
            if da == db or not da or not db:
                return True

    return semantic_similarity(a, b) >= 0.78


def parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None

    value = value.strip()

    formats = [
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
        "%d %B %Y", "%d %b %Y",
    ]

    candidates = re.findall(
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
        r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
        value,
    )
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).replace(
                    tzinfo=timezone.utc,
                    hour=23,
                    minute=59,
                    second=59,
                )
            except ValueError:
                continue

    return None


def deadline_status(deadline: str | None) -> tuple[str, int | None]:
    dt = parse_deadline(deadline)
    if dt is None:
        return "unknown", None

    now = datetime.now(timezone.utc)
    delta = dt - now
    days = delta.days

    if delta.total_seconds() < 0:
        return "expired", days
    if delta.total_seconds() <= 24 * 3600:
        return "deadline_today", 0
    if delta.total_seconds() <= 3 * 24 * 3600:
        return "deadline_soon", days
    return "active", days


def calculate_quality(job: dict[str, Any], source: dict[str, Any] | None) -> int:
    score = 0
    official = bool(source and source.get("official"))
    priority = source.get("priority", "P3") if source else "P3"

    if official or priority == "P0":
        score += 25
    elif priority == "P1":
        score += 22
    elif priority == "P2":
        score += 18
    else:
        score += 10

    field_weights = {
        "title": 18,
        "company": 15,
        "deadline": 15,
        "application_url": 15,
        "location": 7,
        "education": 3,
        "experience": 2,
    }

    for field, weight in field_weights.items():
        value = job.get(field)
        if isinstance(value, list):
            if value:
                score += weight
        elif value:
            score += weight

    confidence = float(job.get("confidence") or 0.5)
    score += int(max(0, min(10, confidence * 10)))

    if job.get("verification_suspicious"):
        score -= 35

    status, _ = deadline_status(job.get("deadline"))
    if status == "expired":
        score -= 60

    return max(0, min(100, score))


def calculate_importance(job: dict[str, Any], source: dict[str, Any] | None) -> int:
    score = 0

    priority = source.get("priority", "P3") if source else "P3"
    score += {"P0": 20, "P1": 17, "P2": 12, "P3": 6}.get(priority, 6)

    title = norm_identity(str(job.get("title") or ""))
    company = norm_identity(str(job.get("company") or ""))
    text = f"{title} {company}"

    if any(k in text for k in ("intern", "internship", "trainee", "graduate", "fresher")):
        score += 12

    if any(k in text for k in ("manager", "director", "head", "officer", "engineer", "executive")):
        score += 5

    vacancy_raw = str(job.get("vacancy") or "")
    numbers = re.findall(r"\d+", vacancy_raw.replace(",", ""))
    if numbers:
        try:
            count = int(numbers[-1])
            if count >= 100:
                score += 15
            elif count >= 20:
                score += 10
            elif count >= 5:
                score += 6
        except ValueError:
            pass

    status, days = deadline_status(job.get("deadline"))
    if status == "deadline_today":
        score += 15
    elif status == "deadline_soon":
        score += 12
    elif days is not None and days <= 7:
        score += 7
    elif days is not None and days <= 14:
        score += 4

    if job.get("salary"):
        score += 5
    if job.get("education"):
        score += 4
    if job.get("application_url"):
        score += 6

    return max(0, min(100, score))


def source_map(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in registry.get("sources", []) if s.get("id")}


def choose_canonical_source(
    source_ids: list[str],
    registry_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        registry_map[sid] for sid in source_ids
        if sid in registry_map
    ]
    if not candidates:
        return None
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return sorted(
        candidates,
        key=lambda s: (order.get(s.get("priority", "P3"), 3), not s.get("official", False)),
    )[0]


def source_queries(registry: dict[str, Any]) -> list[str]:
    base = [
        '"Bangladesh" "job circular"',
        '"Bangladesh" recruitment vacancy',
        '"Bangladesh" "career" jobs',
        '"নিয়োগ বিজ্ঞপ্তি"',
        '"চাকরির বিজ্ঞপ্তি"',
        'site:gov.bd "নিয়োগ বিজ্ঞপ্তি"',
        'site:gov.bd recruitment',
        'site:edu.bd job circular',
        'Bangladesh bank recruitment',
        'Bangladesh NGO jobs',
        'Bangladesh pharma jobs',
        'Bangladesh healthcare jobs',
        'Bangladesh university recruitment',
        'Bangladesh IT jobs',
        'Bangladesh garments jobs',
        'Bangladesh manufacturing jobs',
        'Bangladesh internship jobs',
        'Bangladesh fresher jobs',
    ]

    # Only add a small rotating sample of source names to bound each run.
    for source in registry.get("sources", []):
        name = source.get("name")
        if not name:
            continue
        base.append(f'"{name}" Bangladesh career jobs')
        base.append(f'"{name}" Bangladesh recruitment')
    return list(dict.fromkeys(base))[:MAX_EXA_QUERIES]


def rotate_sources(
    registry: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    now_epoch = time.time()
    due: list[dict[str, Any]] = []

    for source in registry.get("sources", []):
        if not source.get("enabled", source.get("status") == "active"):
            continue

        sid = source["id"]
        sstate = state["sources"].get(sid, {})
        last = float(sstate.get("last_crawl_epoch") or 0)
        interval = int(source.get("crawl_minutes") or 60)

        if now_epoch - last >= interval * 60:
            due.append(source)

    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return sorted(
        due,
        key=lambda s: (
            priority_order.get(s.get("priority", "P3"), 3),
            float(state["sources"].get(s["id"], {}).get("last_crawl_epoch") or 0),
        ),
    )


def update_source_health(
    state: dict[str, Any],
    source_id: str,
    success: bool,
    candidates: int,
) -> None:
    entry = state["sources"].setdefault(source_id, {})
    entry["last_crawl_epoch"] = time.time()
    entry["last_run_at"] = utc_now()
    entry["last_success"] = utc_now() if success else entry.get("last_success")
    entry["last_failure"] = None if success else utc_now()
    entry["consecutive_failures"] = 0 if success else int(entry.get("consecutive_failures") or 0) + 1
    entry["candidates_last_run"] = candidates


def verification(job: dict[str, Any], source: dict[str, Any] | None) -> dict[str, Any]:
    official = bool(source and source.get("official"))
    status, _ = deadline_status(job.get("deadline"))
    suspicious = bool(job.get("verification_suspicious"))

    checks = {
        "source_known": source is not None,
        "company_identified": bool(job.get("company")),
        "title_identified": bool(job.get("title")),
        "application_url": bool(job.get("application_url")),
        "active_deadline": status != "expired",
        "bangladesh_relevant": True,
        "suspicious_signals": not suspicious,
    }

    passed = sum(1 for v in checks.values() if v)
    if suspicious:
        level = "rejected"
        status_value = "rejected"
    elif passed >= 6 and official:
        level = "official"
        status_value = "verified"
    elif passed >= 5:
        level = "trusted"
        status_value = "verified"
    elif passed >= 3:
        level = "discovered"
        status_value = "unverified"
    else:
        level = "unverified"
        status_value = "rejected"

    return {
        "status": status_value,
        "level": level,
        "checks": checks,
        "checked_at": utc_now(),
    }


def build_fallback_image(job: dict[str, Any], path: Path) -> Path | None:
    if Image is None or ImageDraw is None:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 1200, 675
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    title = str(job.get("title") or "JOB OPPORTUNITY").strip()
    company = str(job.get("company") or job.get("source_name") or "Career Opportunity").strip()
    source = str(job.get("source_name") or "Career Newsroom").strip()

    font = None
    bold = None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 42)
            break
    for candidate in bold_candidates:
        if Path(candidate).exists():
            bold = ImageFont.truetype(candidate, 58)
            break

    font = font or ImageFont.load_default()
    bold = bold or font

    def wrap(value: str, max_chars: int) -> list[str]:
        words = value.split()
        lines, current = [], []
        for word in words:
            test = " ".join(current + [word])
            if len(test) > max_chars and current:
                lines.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(" ".join(current))
        return lines

    draw.text((80, 70), company[:70], font=font, fill="black")
    y = 220
    for line in wrap(title, 28)[:4]:
        draw.text((80, y), line, font=bold, fill="black")
        y += 70

    draw.text((80, 540), "Career Opportunity", font=font, fill="black")
    draw.text((940, 615), "@CareerNewsroom", font=font, fill="black")
    draw.text((80, 615), source[:45], font=font, fill="black")

    image.save(path, format="JPEG", quality=92)
    return path


def image_url_from_job(job: dict[str, Any]) -> str:
    meta = job.get("meta") or {}
    for key in ("image", "logo"):
        url = normalize_url(str(meta.get(key) or ""))
        if url:
            return url
    return ""


def escape_html(value: str) -> str:
    return html.escape(value or "", quote=False)


def build_caption(job: dict[str, Any]) -> str:
    title = escape_html(str(job.get("title") or "Job Vacancy"))
    company = escape_html(str(job.get("company") or "Organization"))
    parts = [
        f"<b>📌 {title}</b>",
        "",
        f"🏢 <b>Organization:</b> {company}",
    ]

    def add(icon: str, label: str, value: Any) -> None:
        if value:
            parts.append(f"{icon} <b>{label}:</b> {escape_html(str(value))}")

    add("📍", "Location", job.get("location"))
    add("💼", "Type", job.get("employment_type") or job.get("job_type"))
    add("🎯", "Level", job.get("job_level"))
    add("🎓", "Education", "; ".join(job.get("education") or []))
    add("🧑‍💼", "Experience", job.get("experience"))
    add("💰", "Salary", job.get("salary"))
    add("👥", "Vacancy", job.get("vacancy"))
    add("📅", "Deadline", job.get("deadline"))

    requirements = [
        str(x).strip() for x in (job.get("requirements") or []) if str(x).strip()
    ][:4]

    if requirements:
        parts += ["", "<b>KEY REQUIREMENTS</b>"]
        parts.extend(f"• {escape_html(x)}" for x in requirements)

    summary = str(job.get("summary") or "").strip()
    if summary:
        parts += ["", escape_html(summary[:500])]

    parts += [
        "",
        f"Source: {escape_html(str(job.get('source_name') or 'Career Newsroom'))}",
    ]

    caption = "\n".join(parts)
    # Telegram photo captions are limited to 1024 chars in the current Bot API.
    return caption[:1000]


def telegram_request(
    token: str,
    method: str,
    data: dict[str, Any],
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/{method}"
    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=30,
    )

    payload: dict[str, Any]
    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "description": response.text[:500]}

    if not response.ok or not payload.get("ok"):
        raise RuntimeError(
            f"Telegram {method} failed: HTTP {response.status_code} "
            f"{payload.get('description', response.text[:300])}"
        )
    return payload["result"]


def send_telegram_post(
    job: dict[str, Any],
    config: dict[str, str],
    dry_run: bool = False,
) -> dict[str, Any]:
    caption = build_caption(job)
    application_url = normalize_url(
        str(job.get("application_url") or job.get("source_url") or "")
    )

    reply_markup = None
    if application_url:
        reply_markup = json.dumps(
            {
                "inline_keyboard": [
                    [{"text": "APPLY NOW", "url": application_url}]
                ]
            }
        )

    if dry_run:
        return {
            "published": False,
            "dry_run": True,
            "caption": caption,
            "application_url": application_url,
        }

    token = config["TELEGRAM_BOT_TOKEN"]
    channel = config["TELEGRAM_CHANNEL"] or DEFAULT_CHANNEL

    image_url = image_url_from_job(job)
    TMP_DIR.mkdir(exist_ok=True)

    if image_url:
        try:
            result = telegram_request(
                token,
                "sendPhoto",
                {
                    "chat_id": channel,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup or "",
                },
            )
            return {
                "published": True,
                "mode": "photo_url",
                "message_id": result.get("message_id"),
                "caption": caption,
            }
        except Exception as exc:
            LOG.warning("Telegram photo-by-URL failed: %s", exc)

    # Download image and upload as multipart if URL-based send fails.
    if image_url:
        try:
            session = request_session()
            content, content_type, _, _ = fetch(session, image_url, timeout=15, attempts=2)
            if "image" in content_type.lower() or image_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                image_path = TMP_DIR / f"{sha256_text(image_url)[:12]}.jpg"
                image_path.write_bytes(content)
                with image_path.open("rb") as fh:
                    result = telegram_request(
                        token,
                        "sendPhoto",
                        {
                            "chat_id": channel,
                            "caption": caption,
                            "parse_mode": "HTML",
                            "reply_markup": reply_markup or "",
                        },
                        files={"photo": fh},
                    )
                return {
                    "published": True,
                    "mode": "photo_upload",
                    "message_id": result.get("message_id"),
                    "caption": caption,
                }
        except Exception as exc:
            LOG.warning("Telegram image upload failed: %s", exc)

    # Final image fallback.
    fallback_path = build_fallback_image(
        job,
        TMP_DIR / f"fallback_{sha256_text(job.get('event_id', ''))[:12]}.jpg",
    )
    if fallback_path:
        try:
            with fallback_path.open("rb") as fh:
                result = telegram_request(
                    token,
                    "sendPhoto",
                    {
                        "chat_id": channel,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup or "",
                    },
                    files={"photo": fh},
                )
            return {
                "published": True,
                "mode": "fallback_card",
                "message_id": result.get("message_id"),
                "caption": caption,
            }
        except Exception as exc:
            LOG.warning("Telegram fallback card failed: %s", exc)

    # Never lose a good job solely because media failed.
    result = telegram_request(
        token,
        "sendMessage",
        {
            "chat_id": channel,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup or "",
        },
    )
    return {
        "published": True,
        "mode": "text",
        "message_id": result.get("message_id"),
        "caption": caption,
    }


def send_admin_alert(message: str, config: dict[str, str]) -> None:
    chat_id = config.get("TELEGRAM_ADMIN_CHAT_ID", "")
    token = config.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    try:
        telegram_request(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"CareerNewsroomBot alert\n\n{message[:3500]}",
            },
        )
    except Exception as exc:
        LOG.warning("Admin alert failed: %s", exc)


def _source_priority(source_id: str, source_lookup: dict[str, dict[str, Any]] | None) -> int:
    if not source_lookup or not source_id:
        return 3
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(
        source_lookup.get(source_id, {}).get("priority", "P3"), 3
    )


def upsert_event(
    state: dict[str, Any],
    job: dict[str, Any],
    source: dict[str, Any] | None,
    source_lookup: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, bool, bool]:
    """
    Returns:
      event_id, is_new, is_material_update
    """
    # Search existing events conservatively.
    for event_id, event in state["jobs"].items():
        existing = event.get("canonical", {})
        if is_same_event(existing, job):
            material = False
            old_deadline = existing.get("deadline")
            new_deadline = job.get("deadline")
            if old_deadline != new_deadline and new_deadline:
                material = True

            old_url = existing.get("application_url")
            new_url = job.get("application_url")
            if old_url != new_url and new_url:
                material = True

            old_vacancy = existing.get("vacancy")
            new_vacancy = job.get("vacancy")
            if old_vacancy != new_vacancy and new_vacancy:
                material = True

            old_salary = existing.get("salary")
            new_salary = job.get("salary")
            if old_salary != new_salary and new_salary:
                material = True

            existing_source_id = event.get("canonical", {}).get("source_id") or ""
            # Preserve the highest-trust canonical source. Secondary sources only fill gaps.
            current_priority = _source_priority(existing_source_id, source_lookup)
            incoming_priority = _source_priority(str(job.get("source_id") or ""), source_lookup)
            if incoming_priority < current_priority:
                event["canonical"] = {**event.get("canonical", {}), **job}
            else:
                merged = dict(event.get("canonical", {}))
                for k, v in job.items():
                    if v not in (None, "", [], {}) and not merged.get(k):
                        merged[k] = v
                event["canonical"] = merged
            event["last_seen"] = utc_now()
            event["last_updated"] = utc_now() if material else event.get("last_updated")
            event.setdefault("sources", [])
            source_record = {
                "source_id": job.get("source_id"),
                "source_url": job.get("source_url"),
                "discovery_method": job.get("discovery_method"),
                "seen_at": utc_now(),
            }
            event["sources"].append(source_record)
            event["sources"] = event["sources"][-20:]
            if material:
                event.setdefault("update_history", []).append(
                    {
                        "timestamp": utc_now(),
                        "type": "material_update",
                    }
                )
                event["update_history"] = event["update_history"][-20:]
            return event_id, False, material

    event_id = "evt_" + event_fingerprint(job)
    event = {
        "event_id": event_id,
        "status": "active",
        "canonical": job,
        "sources": [
            {
                "source_id": job.get("source_id"),
                "source_url": job.get("source_url"),
                "discovery_method": job.get("discovery_method"),
                "seen_at": utc_now(),
            }
        ],
        "verification": {},
        "quality_score": 0,
        "importance_score": 0,
        "first_seen": utc_now(),
        "last_seen": utc_now(),
        "last_updated": None,
        "update_history": [],
        "telegram": {
            "published": False,
            "message_id": None,
            "published_at": None,
            "last_update_message_id": None,
        },
    }
    state["jobs"][event_id] = event
    return event_id, True, False


def prune_state(state: dict[str, Any], days: int = 90) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    remove: list[str] = []

    for event_id, event in state["jobs"].items():
        status = event.get("status")
        if status not in ("expired", "rejected"):
            continue
        last_seen = event.get("last_seen")
        try:
            dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < cutoff and not event.get("telegram", {}).get("published"):
            remove.append(event_id)

    for event_id in remove:
        del state["jobs"][event_id]


def save_run_summary(state: dict[str, Any], summary: dict[str, Any]) -> None:
    state["runs"].append({"timestamp": utc_now(), **summary})
    state["runs"] = state["runs"][-20:]


def config_from_env() -> dict[str, str]:
    return {
        "EXA_API_KEY": os.getenv("EXA_API_KEY", ""),
        "CEREBRAS_API_KEY": os.getenv("CEREBRAS_API_KEY", ""),
        "CEREBRAS_MODEL": os.getenv("CEREBRAS_MODEL", DEFAULT_CEREBRAS_MODEL),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHANNEL": os.getenv("TELEGRAM_CHANNEL", DEFAULT_CHANNEL),
        "TELEGRAM_ADMIN_CHAT_ID": os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""),
    }


def validate_config(config: dict[str, str], dry_run: bool = False) -> None:
    required = ["EXA_API_KEY", "CEREBRAS_API_KEY"]
    if not dry_run:
        required.append("TELEGRAM_BOT_TOKEN")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


def run_self_test() -> dict[str, Any]:
    sample = {
        "company": "ABC Bank PLC",
        "title": "Management Trainee Officer",
        "deadline": "30-09-2026",
        "application_url": "https://example.com/apply?id=1&utm_source=test",
        "location": "Dhaka",
        "education": ["Bachelor's degree"],
        "salary": "BDT 30,000",
        "source_id": "test",
        "source_url": "https://example.com/job/1",
    }
    normalized_url = normalize_url(sample["application_url"])
    assert "utm_source" not in normalized_url
    assert parse_deadline(sample["deadline"]) is not None

    a = dict(sample)
    b = dict(sample)
    b["source_id"] = "bdjobs"
    b["source_url"] = "https://bdjobs.example/123"
    assert is_same_event(a, b)

    q = calculate_quality(sample, {"official": True, "priority": "P1"})
    i = calculate_importance(sample, {"official": True, "priority": "P1"})
    assert 0 <= q <= 100
    assert 0 <= i <= 100

    caption = build_caption(sample)
    assert "Management Trainee Officer" in caption
    assert len(caption) <= 1000

    return {
        "status": "PASS",
        "url_normalization": "PASS",
        "deadline_parser": "PASS",
        "deduplication": "PASS",
        "scoring": "PASS",
        "telegram_caption": "PASS",
    }


def process_run(
    config: dict[str, str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    validate_config(config, dry_run=dry_run)

    registry = load_registry()
    state = load_state()
    posted_urls = load_posted_urls()
    registry_map = source_map(registry)
    session = request_session()

    summary = {
        "sources_scheduled": 0,
        "sources_failed": 0,
        "candidates": 0,
        "documents": 0,
        "jobs_extracted": 0,
        "new_events": 0,
        "updates": 0,
        "rejected": 0,
        "published": 0,
        "publish_failures": 0,
    }

    candidates: list[dict[str, Any]] = []

    due_sources = rotate_sources(registry, state)
    summary["sources_scheduled"] = len(due_sources)

    for source in due_sources:
        try:
            found = direct_source_candidates(source, session)
            candidates.extend(found)
            update_source_health(state, source["id"], True, len(found))
        except Exception as exc:
            summary["sources_failed"] += 1
            update_source_health(state, source["id"], False, 0)
            LOG.warning("Source failed: %s (%s)", source.get("id"), exc)

    google_queries = [
        '"Bangladesh job circular"',
        '"নিয়োগ বিজ্ঞপ্তি"',
        '"চাকরির বিজ্ঞপ্তি"',
        '"Bangladesh recruitment"',
    ]
    candidates.extend(discover_google_news(google_queries, session))

    candidates.extend(
        exa_search(
            source_queries(registry),
            config["EXA_API_KEY"],
            num_results=6,
        )
    )

    unique_candidates: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        url = normalize_url(candidate.get("url", ""))
        if not url:
            continue
        if url in posted_urls and candidate.get("discovery_method") != "exa":
            continue
        unique_candidates.setdefault(url, candidate)

    candidate_list = list(unique_candidates.values())[:MAX_CANDIDATES]
    summary["candidates"] = len(candidate_list)

    processed_events: list[dict[str, Any]] = []

    for candidate in candidate_list:
        document = fetch_candidate_content(candidate, session)
        if not document:
            continue

        summary["documents"] += 1

        # Prefer registry source. Exa/Google News may have no registry source.
        source = registry_map.get(document.get("source_id", ""))
        ai_job = extract_job_with_ai(document, config)
        if not ai_job:
            continue

        job = normalize_job(ai_job)
        job["source_name"] = source.get("name") if source else document.get("source_id", "")
        job["source_type"] = source.get("source_type") if source else document.get("discovery_method", "")
        job["official_source"] = bool(source and source.get("official"))
        job["meta"] = document.get("meta", {})
        job["source_url"] = document["url"]
        job["source_id"] = document.get("source_id")
        job["discovery_method"] = document.get("discovery_method")
        job["verification_suspicious"] = bool(
            document.get("hints", {}).get("scam_signals")
        )

        # Hard Bangladesh relevance check.
        if not (
            looks_like_bangladesh(
                f"{job.get('company','')} {job.get('title','')} {document.get('text','')}",
                document["url"],
            )
            or (source and (
                source.get("priority") in ("P0", "P1")
                or source.get("categories")
            ))
        ):
            summary["rejected"] += 1
            continue

        status, _ = deadline_status(job.get("deadline"))
        if status == "expired":
            job["status"] = "expired"
            summary["rejected"] += 1
            continue

        job["status"] = status if status != "unknown" else "active"

        quality = calculate_quality(job, source)
        importance = calculate_importance(job, source)

        event_id, is_new, is_update = upsert_event(state, job, source)
        event = state["jobs"][event_id]
        event["quality_score"] = quality
        event["importance_score"] = importance
        event["verification"] = verification(job, source)
        event["status"] = job["status"]

        if is_new:
            summary["new_events"] += 1
        elif is_update:
            summary["updates"] += 1

        processed_events.append(event)

        # Keep in-memory state current.
        state["jobs"][event_id] = event
        summary["jobs_extracted"] += 1

    # Decide publishing only after all candidates have been processed.
    publish_candidates: list[dict[str, Any]] = []
    for event in processed_events:
        if event.get("status") not in ("active", "deadline_today", "deadline_soon"):
            continue

        if event.get("verification", {}).get("status") != "verified":
            continue

        if event.get("quality_score", 0) < 70:
            continue

        if event.get("importance_score", 0) < 70:
            continue

        telegram = event.setdefault("telegram", {})
        if telegram.get("published"):
            # Material updates only.
            if event.get("last_updated") and event.get("last_updated") != telegram.get("published_at"):
                publish_candidates.append(event)
            continue

        publish_candidates.append(event)

    publish_candidates = sorted(
        publish_candidates,
        key=lambda e: (
            int(e.get("importance_score", 0)),
            int(e.get("quality_score", 0)),
        ),
        reverse=True,
    )[:MAX_JOBS_PER_RUN]

    for event in publish_candidates:
        publish_job = dict(event["canonical"])
        publish_job["event_id"] = event["event_id"]
        publish_job["source_name"] = (
            event["canonical"].get("source_name")
            or event.get("sources", [{}])[0].get("source_id")
            or "Career Newsroom"
        )
        try:
            result = send_telegram_post(
                publish_job,
                config,
                dry_run=dry_run,
            )

            if dry_run:
                continue

            event["telegram"]["published"] = True
            event["telegram"]["message_id"] = result.get("message_id")
            event["telegram"]["published_at"] = utc_now()
            if event.get("last_updated"):
                event["telegram"]["last_update_message_id"] = result.get("message_id")

            posted_urls.add(normalize_url(str(event["canonical"].get("source_url") or "")))
            summary["published"] += 1

        except Exception as exc:
            summary["publish_failures"] += 1
            LOG.exception("Telegram publication failed for %s", event["event_id"])
            send_admin_alert(
                f"Publish failed for {event.get('event_id')}\n{exc}",
                config,
            )

    prune_state(state)

    state["updated_at"] = utc_now()
    save_run_summary(state, summary)

    atomic_write_json(STATE_FILE, state)

    # Only successfully published URLs are added.
    if not dry_run:
        posted_urls.discard("")
        save_posted_urls(posted_urls)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if args.self_test:
        result = run_self_test()
        print(json.dumps(result, indent=2))
        return 0

    config = config_from_env()

    try:
        result = process_run(config, dry_run=args.dry_run)
        LOG.info(
            "Run complete | scheduled=%s candidates=%s documents=%s extracted=%s "
            "new=%s updates=%s published=%s failures=%s",
            result["sources_scheduled"],
            result["candidates"],
            result["documents"],
            result["jobs_extracted"],
            result["new_events"],
            result["updates"],
            result["published"],
            result["publish_failures"],
        )
        return 0
    except Exception:
        LOG.exception("Fatal run error")
        send_admin_alert("Fatal run error. Check GitHub Actions logs.", config)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
