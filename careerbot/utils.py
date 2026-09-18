from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from difflib import SequenceMatcher

from .config import BDJOBS_HOSTS

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}

def safe_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def canonical_url(url: str) -> str:
    raw = safe_text(url)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("http://", "https://")):
        return raw
    p = urlparse(raw)
    host = p.hostname.lower() if p.hostname else ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    path = path.rstrip("/") or "/"
    query = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl in TRACKING_KEYS or any(kl.startswith(x) for x in TRACKING_PREFIXES):
            continue
        query.append((k, v))
    query.sort()
    return urlunparse(("https", host, path, "", urlencode(query), ""))

def is_bdjobs_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return host == "bdjobs.com" or host.endswith(".bdjobs.com")
    except Exception:
        return False

def short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]

def normalize_title(value: str) -> str:
    x = safe_text(value).casefold()
    x = re.sub(r"[^\w\u0980-\u09ff]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()

def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()

def parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = safe_text(value)
    if not raw:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%d %B %Y",
        "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None

def clamp(value: float, low=0, high=100) -> int:
    return int(max(low, min(high, round(value))))

def event_key(job: dict) -> str:
    return normalize_title("|".join([
        safe_text(job.get("company")),
        safe_text(job.get("job_title")),
        safe_text(job.get("location")),
    ]))

def looks_like_target_title(title: str) -> bool:
    from .config import TARGET_TITLE_TERMS
    t = normalize_title(title)
    return any(normalize_title(term) in t for term in TARGET_TITLE_TERMS)

def contains_any(text: str, terms) -> bool:
    x = safe_text(text).casefold()
    return any(safe_text(term).casefold() in x for term in terms)
