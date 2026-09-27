"""BDJobs Live source adapter.

This module owns only BDJobs Live source discovery, validation, and detail
extraction. It deliberately has no homepage/global-search discovery path.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
import threading
from bs4 import BeautifulSoup

try:
    import curl_cffi
except ImportError:  # pragma: no cover
    curl_cffi = None

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:  # pragma: no cover
    StealthyFetcher = None

BASE = "https://www.bdjobslive.com"
DOMAIN = "bdjobslive.com"
DETAIL_RE = re.compile(r"/bdjobs-details/[^/?#]+-(\d+)(?:[/?#]|$)", re.I)

# These are the allowed BBA/MBA career lanes for BDJobs Live.
# Each category has explicit route variants because the site has used more than
# one routing style during its current/legacy migrations. No homepage discovery
# is performed.
CATEGORIES = (
    {"name": "Accounting / Finance", "slug": "accounting-finance", "priority": 1},
    {"name": "Bank / Financial Institution", "slug": "bank-financial-institution", "priority": 1},
    {"name": "Commercial", "slug": "commercial", "priority": 1},
    {"name": "Company Secretary / Regulatory Affairs", "slug": "company-secretary-regulatory-affairs", "priority": 1},
    {"name": "Customer Service / Call Centre", "slug": "customer-service-call-centre", "priority": 2},
    {"name": "E-commerce / Digital Marketing", "slug": "e-commerce-digital-marketing", "priority": 1},
    {"name": "General Management / Admin", "slug": "general-management-admin", "priority": 1},
    {"name": "HR / Organizational Development", "slug": "hr-organizational-development", "priority": 1},
    {"name": "Marketing / Sales", "slug": "marketing-sales", "priority": 1},
    {"name": "Media / Advertising / Event Management", "slug": "media-advertising-event-management", "priority": 2},
    {"name": "NGO / Development", "slug": "ngo-development", "priority": 2},
    {"name": "Production / Operation", "slug": "production-operation", "priority": 2},
    {"name": "Research / Consultancy", "slug": "research-consultancy", "priority": 1},
    {"name": "Supply Chain / Procurement", "slug": "supply-chain-procurement", "priority": 1},
)

INTERNSHIP_KEYWORDS = ("intern", "internship")
NOISE_TITLES = {
    "jobs", "home", "bdjobs live", "filter", "quick filter", "career hub", "companies",
    "post a job", "sign in", "register", "view all", "next", "prev",
}
PLACEHOLDER_VALUES = {"", "-", "--", "n/a", "na", "not specified", "not available", "null", "none"}
BROWSER_SLOTS = threading.Semaphore(2)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", normalize_space(value).lower()).strip()


def canonical_url(url: str) -> str:
    raw = _text(url)
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parsed.path or "/")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}:
            query_pairs.append((key, value))
    query = urlencode(query_pairs)
    return urlunparse((parsed.scheme.lower() or "https", host, path, "", query, ""))


def is_domain_allowed(url: str) -> bool:
    host = urlparse(_text(url)).netloc.lower().removeprefix("www.")
    return host == DOMAIN or host.endswith("." + DOMAIN)


def extract_job_id(url: str) -> str:
    m = DETAIL_RE.search(urlparse(_text(url)).path)
    return m.group(1) if m else ""


def is_detail_url(url: str) -> bool:
    raw = _text(url)
    return is_domain_allowed(raw) and bool(DETAIL_RE.search(urlparse(raw).path))


def category_urls(category: dict[str, Any]) -> list[str]:
    slug = _text(category.get("slug"))
    # Known/current and legacy route families used by BDJobs Live. Keep the list
    # small and deterministic. A failed route never triggers homepage fallback.
    variants = [
        f"{BASE}/bdjobs-circular/{slug}-jobs",
        f"{BASE}/bdjobs-industry/{slug}-jobs",
        f"{BASE}/bdjobs/{slug}",
    ]
    aliases = {
        "e-commerce-digital-marketing": [
            f"{BASE}/bdjobs-circular/ecommerce-digital-marketing-jobs",
            f"{BASE}/bdjobs-industry/e-commerce-digital-marketing-jobs",
            f"{BASE}/bdjobs/e-commerce-digital-marketing",
            f"{BASE}/bdjobs/ecommerce-digital-marketing",
        ],
        "media-advertising-event-management": [
            f"{BASE}/bdjobs-circular/media-advertisement-event-management-jobs",
            f"{BASE}/bdjobs-circular/media-advertising-event-mgt-jobs",
            f"{BASE}/bdjobs-industry/media-advertising-event-mgt-jobs",
            f"{BASE}/bdjobs/media-advertising-event-mgt",
        ],
        "hr-organizational-development": [
            f"{BASE}/bdjobs-industry/hr-organizational-development-jobs",
            f"{BASE}/bdjobs/hr-organizational-development",
        ],
        "customer-service-call-centre": [
            f"{BASE}/bdjobs-industry/customer-service-call-centre",
            f"{BASE}/bdjobs/customer-service-call-centre",
        ],
    }
    result = variants[:]
    result[0:0] = aliases.get(slug, [])
    seen = set()
    return [u for u in result if not (u in seen or seen.add(u))]


def _looks_like_error_page(text: str) -> bool:
    blob = normalize_space(text).lower()
    if not blob:
        return True
    bad = (
        "just a moment", "access denied", "captcha", "verify you are human",
        "404 - not found", "page not found", "internal server error",
    )
    return any(x in blob for x in bad)


def _clean_value(value: Any) -> str:
    value = normalize_space(value)
    if value.lower() in PLACEHOLDER_VALUES:
        return ""
    return value


def _extract_first(patterns: list[str], text: str, flags=re.I) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            value = _clean_value(m.group(1))
            if value:
                return value
    return ""


def _parse_date(value: str) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    raw = re.sub(r"\bSept\b", "Sep", raw, flags=re.I)
    formats = (
        "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _parse_experience(value: str) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    # Preserve source semantics, including the site's occasional "-1 Year".
    m = re.search(r"-?\d+(?:\.\d+)?\s*Year(?:s)?\s*Experience", raw, re.I)
    return m.group(0) if m else raw


def _nearest_card(anchor):
    title = normalize_space(anchor.get_text(" ", strip=True))
    markers = ("deadline", "experience", "education", "location", "salary", "job type")
    # Use the nearest valid ancestor. Choosing the ancestor with the most metadata
    # can accidentally swallow several adjacent job cards into one record.
    for parent in anchor.parents:
        if getattr(parent, "name", "") in {"body", "html"}:
            break
        text = normalize_space(parent.get_text(" ", strip=True))
        if not text or len(text) > 2500 or title and title not in text:
            continue
        marker_count = sum(1 for marker in markers if marker in text.lower())
        if marker_count >= 2:
            return parent
    return anchor.parent


def _card_title(anchor, card) -> str:
    heading = anchor.find("h3")
    if heading:
        value = _clean_value(heading.get_text(" ", strip=True))
        if value and normalize_title(value) not in NOISE_TITLES:
            return value
    for heading_tag in ("h2", "h4"):
        heading = anchor.find(heading_tag) or card.find(heading_tag)
        if heading:
            value = _clean_value(heading.get_text(" ", strip=True))
            if value and normalize_title(value) not in NOISE_TITLES:
                return value
    return _clean_value(anchor.get_text(" ", strip=True))


def _card_lines(card) -> list[str]:
    values = []
    for node in card.find_all(["span", "div", "p", "li", "small", "strong"]):
        text = _clean_value(node.get_text(" ", strip=True))
        if not text or len(text) > 220:
            continue
        if text not in values:
            values.append(text)
    if not values:
        values = [_clean_value(x) for x in card.get_text("\n", strip=True).splitlines() if _clean_value(x)]
    return values


def _extract_company(anchor, card, title: str) -> str:
    title_norm = normalize_title(title)
    # Company is commonly the first span immediately after the title block.
    for span in card.find_all("span"):
        value = _clean_value(span.get_text(" ", strip=True))
        if not value or normalize_title(value) == title_norm or normalize_title(value) in NOISE_TITLES:
            continue
        if any(token in value.lower() for token in ("year experience", "deadline", "full time", "contract", "internship")):
            continue
        if len(value) <= 160:
            return value

    lines = _card_lines(card)
    for i, line in enumerate(lines):
        if normalize_title(line) == title_norm and i + 1 < len(lines):
            candidate = lines[i + 1]
            if candidate and normalize_title(candidate) != title_norm:
                return candidate
    return ""


def _field_from_lines(lines: list[str], label_patterns: list[str]) -> str:
    for i, line in enumerate(lines):
        low = line.lower()
        if any(re.search(p, low, re.I) for p in label_patterns):
            # Label and value may be in the same line or the next line.
            for pattern in label_patterns:
                m = re.search(pattern + r"\s*[:：-]?\s*(.+)$", line, re.I)
                if m:
                    value = _clean_value(m.group(1))
                    if value and normalize_title(value) != normalize_title(line):
                        return value
            if i + 1 < len(lines):
                candidate = _clean_value(lines[i + 1])
                if candidate:
                    return candidate
    return ""


def parse_listing_card(anchor, category: dict[str, Any], category_url: str) -> dict[str, Any] | None:
    href = urljoin(category_url, _text(anchor.get("href")))
    if not is_detail_url(href):
        return None
    job_id = extract_job_id(href)
    if not job_id:
        return None
    card = _nearest_card(anchor)
    title = _card_title(anchor, card)
    if not title or normalize_title(title) in NOISE_TITLES:
        return None
    lines = _card_lines(card)
    card_text = normalize_space(card.get_text(" ", strip=True))
    company = _extract_company(anchor, card, title)
    employment = _extract_first([
        r"(?:Job Type|Employment Type)\s*[:：-]\s*(.+?)(?=\s+(?:\w+\s+)?(?:Deadline|Experience|Education|Location)\b|$)",
    ], card_text)
    if not employment:
        employment = next((x for x in lines if re.fullmatch(r"(?:Full Time/Permanent|Full Time|Part Time|Contract|Internship|Temporary)", x, re.I)), "")
    location = _field_from_lines(lines, [r"^location$", r"^job location$"])
    experience = _field_from_lines(lines, [r"^experience$", r"experience$"])
    education = _field_from_lines(lines, [r"^education$", r"education$"])
    deadline_raw = _field_from_lines(lines, [r"^deadline$", r"deadline$"])
    deadline = _parse_date(deadline_raw)
    if not deadline:
        deadline = _parse_date(_extract_first([r"deadline\s*[:：-]\s*([A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4})"], card_text))
    logo_url = ""
    logo_alt = ""
    for img in card.find_all("img"):
        alt = _text(img.get("alt"))
        src = _text(img.get("src") or img.get("data-src"))
        if alt and company and company.lower() in alt.lower() and src:
            logo_url = urljoin(category_url, src)
            logo_alt = alt
            break
    is_hot = bool(re.search(r"\bHOT\b", card_text, re.I))
    is_internship = any(k in (title + " " + employment).lower() for k in INTERNSHIP_KEYWORDS)
    return {
        "title": title,
        "company": company,
        "location": location,
        "experience": _parse_experience(experience),
        "education": education,
        "employment_type": employment,
        "deadline": deadline,
        "listing_deadline": deadline,
        "company_logo_url": logo_url,
        "company_logo_alt": logo_alt,
        "is_hot": is_hot,
        "source": "BDJobs Live",
        "source_url": canonical_url(href),
        "url": canonical_url(href),
        "canonical": canonical_url(href),
        "source_job_id": job_id,
        "source_category_name": _text(category.get("name")),
        "source_category_url": category_url,
        "discovery": "bdjobslive_category",
        "is_government": False,
        "is_internship": is_internship,
        "listing_fields": {
            "company": company,
            "location": location,
            "experience": _parse_experience(experience),
            "education": education,
            "employment_type": employment,
            "deadline": deadline,
            "salary": "",
            "vacancy": "",
        },
        "listing_posted": "",
        "raw_text": trim_card_text(card_text),
    }


def trim_card_text(text: str, limit: int = 3000) -> str:
    text = normalize_space(text)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")


def parse_listing_page(html: str, category: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
    if not html or _looks_like_error_page(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)):
        return []
    soup = BeautifulSoup(html, "html.parser")
    result = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = _text(anchor.get("href"))
        if "/bdjobs-details/" not in href.lower():
            continue
        item = parse_listing_card(anchor, category, page_url)
        if not item or item["canonical"] in seen:
            continue
        seen.add(item["canonical"])
        result.append(item)
    return result


def pagination_urls(html: str, page_url: str, max_page: int = 4) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    result = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, _text(anchor.get("href")))
        if not is_domain_allowed(href):
            continue
        text = normalize_space(anchor.get_text(" ", strip=True))
        m = re.fullmatch(r"(?:page\s*)?(\d+)", text, re.I)
        if m and 1 <= int(m.group(1)) <= max_page:
            if href not in seen:
                seen.add(href); result.append(href)
        elif text.lower() in {"next", "›", "»"} and href not in seen:
            seen.add(href); result.append(href)
    # Generic current-site pattern fallback. Still constrained to the configured
    # category URL by only adding ?page=2..max_page.
    parsed = urlparse(page_url)
    for n in range(2, max_page + 1):
        q = dict(parse_qsl(parsed.query, keep_blank_values=True)); q["page"] = str(n)
        href = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(q), ""))
        if href not in seen:
            result.append(href); seen.add(href)
    return result


def _curl_fetch(url: str, timeout: int = 20, impersonate: str = "safari18_0_ios"):
    if curl_cffi is None:
        return None
    try:
        r = curl_cffi.requests.get(
            url,
            impersonate=impersonate,
            headers={"Accept-Language": "en-US,en;q=0.9", "Referer": BASE + "/"},
            timeout=timeout,
            allow_redirects=True,
        )
        return r
    except Exception:
        return None


def _detail_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    lines = []
    for value in soup.stripped_strings:
        value = normalize_space(value)
        if value and value not in lines:
            lines.append(value)
    return lines


def _label_value_lines(lines: list[str], labels: tuple[str, ...]) -> str:
    wanted = {x.casefold() for x in labels}
    for i, line in enumerate(lines):
        label = re.sub(r"\s*[:：]\s*$", "", line).strip().casefold()
        if label in wanted and i + 1 < len(lines):
            value = _clean_value(lines[i + 1])
            if value.casefold() not in wanted:
                return value
        for label_text in labels:
            m = re.match(rf"^{re.escape(label_text)}\s*[:：-]\s*(.+)$", line, re.I)
            if m:
                return _clean_value(m.group(1))
    return ""


def _inline_field(text: str, labels: tuple[str, ...], stop_labels: tuple[str, ...]) -> str:
    if not text:
        return ""
    label_pattern = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
    stop_pattern = "|".join(re.escape(x) for x in sorted((*labels, *stop_labels), key=len, reverse=True))
    m = re.search(
        rf"(?:{label_pattern})\s*[:：-]?\s*(.*?)(?=\s+(?:{stop_pattern})\s*[:：-]?\s*|$)",
        text,
        re.I,
    )
    return _clean_value(m.group(1)) if m else ""


def _section_text(lines: list[str], headings: tuple[str, ...]) -> str:
    starts = {x.casefold() for x in headings}
    for i, line in enumerate(lines):
        if line.casefold() in starts:
            values = []
            for value in lines[i + 1:i + 18]:
                if value.casefold() in {"all", "skills", "requirements", "responsibilities", "salary & benefits", "company information", "additional requirements"}:
                    break
                values.append(value)
            return normalize_space(" ".join(values))
    return ""


def parse_detail(html: str, url: str, expected_title: str = "") -> dict[str, Any] | None:
    lines = _detail_text_lines(html)
    if len(lines) < 5 or _looks_like_error_page(" ".join(lines)):
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = _label_value_lines(lines, ("Job Title", "Position", "Post Name"))
    if not title:
        for tag in soup.find_all(["h1", "h2"]):
            candidate = _clean_value(tag.get_text(" ", strip=True))
            if candidate and normalize_title(candidate) not in {"job list", "company information", "all", "skills", "requirements", "responsibilities", "filters"}:
                title = candidate
                break

    company = ""
    h1 = soup.find("h1")
    if h1:
        prev = h1.find_previous("a")
        if prev:
            candidate = _clean_value(prev.get_text(" ", strip=True))
            if candidate and normalize_title(candidate) != normalize_title(title):
                company = candidate
    company = company or _label_value_lines(lines, ("Company", "Company Name", "Employer", "Organization"))

    flat_text = normalize_space(" ".join(lines))
    deadline = _inline_field(
        flat_text,
        ("Application Deadline", "Deadline", "Last Date"),
        ("Vacancy", "Age", "Location", "Salary", "Experience", "Job Type", "Published", "Education"),
    ) or _label_value_lines(lines, ("Application Deadline", "Deadline", "Last Date"))
    posted = _inline_field(
        flat_text,
        ("Published", "Posted", "Date Posted", "Publication Date"),
        ("Application Deadline", "Vacancy", "Age", "Location", "Salary", "Experience", "Job Type", "Education"),
    ) or _label_value_lines(lines, ("Published", "Posted", "Date Posted", "Publication Date"))
    vacancy = _inline_field(flat_text, ("Vacancy", "No. of Vacancy", "Number of Vacancy"), ("Age", "Location", "Salary", "Experience", "Job Type", "Published", "Education")) or _label_value_lines(lines, ("Vacancy", "No. of Vacancy", "Number of Vacancy"))
    age = _inline_field(flat_text, ("Age", "Age Limit"), ("Location", "Salary", "Experience", "Job Type", "Published", "Education")) or _label_value_lines(lines, ("Age", "Age Limit"))
    location = _inline_field(flat_text, ("Location", "Job Location"), ("Salary", "Experience", "Job Type", "Published", "Education")) or _label_value_lines(lines, ("Location", "Job Location"))
    salary = _inline_field(flat_text, ("Salary", "Salary Range", "Compensation"), ("Experience", "Job Type", "Published", "Education")) or _label_value_lines(lines, ("Salary", "Salary Range", "Compensation"))
    experience = _inline_field(flat_text, ("Experience", "Experience Requirement", "Experience Requirements"), ("Job Type", "Published", "Education")) or _label_value_lines(lines, ("Experience", "Experience Requirement", "Experience Requirements"))
    gender = _inline_field(flat_text, ("Gender", "Gender Preference"), ("Job Type", "Published", "Education")) or _label_value_lines(lines, ("Gender", "Gender Preference"))
    employment = _inline_field(flat_text, ("Job Type", "Employment Type", "Employment Status"), ("Published", "Education")) or _label_value_lines(lines, ("Job Type", "Employment Type", "Employment Status"))
    shift = _inline_field(flat_text, ("Job Shift", "Shift"), ("Industry", "Education")) or _label_value_lines(lines, ("Job Shift", "Shift"))
    industry = _inline_field(flat_text, ("Industry", "Category"), ("Published", "Education")) or _label_value_lines(lines, ("Industry", "Category"))
    education = _section_text(lines, ("Education", "Educational Qualification", "Educational Requirements"))
    if not education:
        education = _label_value_lines(lines, ("Education", "Educational Qualification", "Educational Requirements"))

    if "Apply Now" in education:
        education = education.split("Apply Now", 1)[0].strip()

    if not title or not company:
        chain = [x for x in lines[:20] if normalize_title(x) not in {"job list", "bdjobs live", "home", "filter", "filters"}]
        if not company and chain:
            company = chain[0] if normalize_title(chain[0]) != normalize_title(title) else (chain[1] if len(chain) > 1 else "")
        if not title:
            title = next((x for x in chain if len(x) > 3), "")

    expected = normalize_title(expected_title)
    actual = normalize_title(title)
    identity_status = "matched" if not expected or expected == actual or (expected and actual and (expected in actual or actual in expected)) else "mismatch"
    if identity_status == "mismatch":
        return {"identity_status": "mismatch", "title": title, "company": company, "is_job_page": False}

    apply_url = ""
    for anchor in soup.find_all("a", href=True):
        label = normalize_space(anchor.get_text(" ", strip=True)).lower()
        href = urljoin(url, _text(anchor.get("href")))
        if "apply now" in label and href.startswith(("http://", "https://")):
            apply_url = href
            break

    return {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
        "experience": _parse_experience(experience),
        "education": education,
        "vacancy": vacancy,
        "age": age,
        "employment_type": employment,
        "workplace": "",
        "application_method": "Online" if apply_url else "",
        "selection_process": "",
        "application_period": "",
        "posted_date": _parse_date(posted),
        "deadline": _parse_date(deadline),
        "apply_url": apply_url,
        "industry": industry,
        "job_shift": shift,
        "gender": gender,
        "responsibilities": _section_text(lines, ("Responsibilities & Context", "Responsibilities", "Job Responsibilities")),
        "description": _section_text(lines, ("Job Context", "Job Description", "Description")),
        "skills": _section_text(lines, ("Skills", "Skills Requirements")),
        "raw_text": trim_card_text(" ".join(lines), 16000),
        "detail_identity_status": identity_status,
        "detail_quality": "direct_valid",
        "detail_is_job_page": True,
        "detail_dom_used": True,
        "source": "BDJobs Live",
        "source_url": canonical_url(url),
        "source_job_id": extract_job_id(url),
    }


def _is_category_url(url: str) -> bool:
    path = urlparse(_text(url)).path.lower()
    return "/bdjobs-circular/" in path or "/bdjobs-industry/" in path or re.search(r"/bdjobs/[^/]+/?$", path) is not None


def _has_expected_content(html: str, url: str) -> bool:
    if not html or _looks_like_error_page(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)):
        return False
    path = urlparse(_text(url)).path.lower()
    if "/bdjobs-details/" in path:
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find(["h1", "h2"])
        text = normalize_space(soup.get_text(" ", strip=True)).lower()
        return bool(heading and "application deadline" in text and "published" in text)
    if _is_category_url(url):
        return bool(re.search(r"/bdjobs-details/[^\"'<>\s]+", html, re.I))
    return False


def fetch_document(url: str, *, timeout: int = 20, browser_timeout: int = 60000, wait_ms: int = 1200):
    """Fetch a BDJobs Live page with content-aware direct -> browser fallbacks.

    A 200 response containing only the Next.js shell is not treated as a valid
    listing/detail document. This is critical because current category pages are
    client-rendered and may return no job anchors until browser execution.
    """
    if not is_domain_allowed(url):
        return None

    if curl_cffi is not None:
        for fp in ("safari18_0_ios", "safari184_ios", "safari_ios"):
            r = _curl_fetch(url, timeout=timeout, impersonate=fp)
            if not r:
                continue
            body = r.text or ""
            if r.ok and len(body) >= 500 and _has_expected_content(body, r.url or url):
                return {"ok": True, "status": r.status_code, "text": body, "html": body, "url": r.url or url, "backend": "curl_cffi"}

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
            allow_redirects=True,
        )
        body = r.text or ""
        if r.ok and len(body) >= 500 and _has_expected_content(body, r.url or url):
            return {"ok": True, "status": r.status_code, "text": body, "html": body, "url": r.url or url, "backend": "requests"}
    except Exception:
        pass

    if StealthyFetcher is not None:
        with BROWSER_SLOTS:
            try:
                page = StealthyFetcher.fetch(
                    url,
                    headless=True,
                    disable_resources=False,
                    load_dom=True,
                    network_idle=True,
                    wait=wait_ms,
                    wait_selector=None,
                    timeout=browser_timeout,
                    retries=1,
                    retry_delay=0.5,
                    google_search=False,
                    solve_cloudflare=True,
                    block_webrtc=True,
                    hide_canvas=True,
                )
                rendered_html = _text(getattr(page, "html_content", ""))
                if not rendered_html:
                    body = getattr(page, "body", b"")
                    rendered_html = body.decode("utf-8", "ignore") if isinstance(body, bytes) else _text(body)
                final_url = _text(getattr(page, "url", "")) or url
                if rendered_html and len(rendered_html) >= 500 and _has_expected_content(rendered_html, final_url):
                    return {
                        "ok": True,
                        "status": 200,
                        "text": BeautifulSoup(rendered_html, "html.parser").get_text(" ", strip=True),
                        "html": rendered_html,
                        "url": final_url,
                        "backend": "scrapling_stealthy",
                    }
            except Exception:
                pass
    return None
