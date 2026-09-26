from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, unquote

TRACKING_PARAMS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid"}
BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_title(value: object) -> str:
    s = clean_text(value).lower().translate(BENGALI_DIGITS)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_company(value: object) -> str:
    s = normalize_title(value)
    for token in ("limited", "ltd", "company", "co", "inc"):
        s = re.sub(rf"\b{re.escape(token)}\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def canonical_url(url: str) -> str:
    raw = clean_text(url)
    if not raw:
        return ""
    p = urlparse(raw)
    host = p.netloc.lower().removeprefix("www.")
    path = unquote(p.path or "/").rstrip("/")
    query_pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query = urlencode(query_pairs)
    return urlunparse(("https", host, path, "", query, ""))


def parse_date(value: object) -> date | None:
    s = clean_text(value).translate(BENGALI_DIGITS)
    if not s:
        return None
    s = re.sub(r"^(published|posted|deadline|application deadline)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"\bSept\.?\b", "Sep", s, flags=re.I)
    formats = [
        "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
        "%b %d, %Y", "%B %d, %Y", "%d-%m-%Y", "%d/%m/%Y",
        "%d Sept %Y", "%d Sep %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})\b", s)
    if m:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(m.group(0), fmt).date()
            except ValueError:
                continue
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def iso_date(value: object) -> str:
    d = value if isinstance(value, date) else parse_date(value)
    return d.isoformat() if d else ""


def date_age_days(published: str, today: date) -> int | None:
    d = parse_date(published)
    if not d:
        return None
    return (today - d).days


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def is_placeholder(value: object) -> bool:
    return clean_text(value).lower() in {"", "-", "--", "na", "n/a", "none", "null", "not specified", "not available"}


def first_number(value: object) -> int | None:
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", clean_text(value).replace(",", ""))
    return int(float(m.group(1))) if m else None
