from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .config import BD_TZ, SOURCE_NAMES


TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref", "source"}


def safe_text(value) -> str:
    return "" if value is None else str(value).strip()


def canonical_url(url: str) -> str:
    raw = safe_text(url)
    if not raw:
        return ""
    p = urlparse(raw)
    if p.scheme and p.scheme.lower() not in {"http", "https"}:
        return ""
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", p.path or "/").rstrip("/")
    path = re.sub(r"/amp$", "", path, flags=re.I)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]
    query.sort()
    return urlunparse((p.scheme.lower() or "https", host, path or "/", "", urlencode(query), ""))[len("https://"):]


def normalize_title(text: str) -> str:
    text = safe_text(text).lower()
    text = re.sub(r"[^a-z0-9\u0980-\u09ff\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text: str) -> set[str]:
    return {x for x in normalize_title(text).split() if len(x) >= 3}


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    aa, bb = title_tokens(na), title_tokens(nb)
    jac = len(aa & bb) / max(1, len(aa | bb)) if aa and bb else 0.0
    return max(seq, jac)


def normalize_domain(url: str) -> str:
    try:
        return urlparse(safe_text(url)).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def source_name(url: str) -> str:
    domain = normalize_domain(url)
    return SOURCE_NAMES.get(domain, domain or "Source")


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BD_TZ)


def clean_text(text: str) -> str:
    text = safe_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_html(text: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def extract_json(value: str):
    try:
        return json.loads(value)
    except Exception:
        return None


def short_hash(*values: str) -> str:
    raw = "|".join(safe_text(v).lower() for v in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def absolute_url(base: str, href: str) -> str:
    href = safe_text(href)
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    return urljoin(base, href)


def visible_text(rich_html: str) -> str:
    rich_html = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    rich_html = re.sub(r"</(p|h1|h2|h3|table|tr|td|th)>", "\n", rich_html, flags=re.I)
    rich_html = re.sub(r"<[^>]+>", "", rich_html)
    return html.unescape(re.sub(r"\n{3,}", "\n\n", rich_html).strip())
