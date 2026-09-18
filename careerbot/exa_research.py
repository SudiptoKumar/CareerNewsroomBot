from __future__ import annotations

import logging
import time
from datetime import timedelta
from urllib.parse import quote

import requests

from .config import EXA_API_URL, EXA_CACHE_HOURS, BDJOBS_DOMAINS, EXA_TIMEOUT, MAX_EXA_RESULTS_PER_QUERY
from .utils import safe_text, canonical_url, is_bdjobs_url

logger = logging.getLogger("career-news-bot")

class ExaError(RuntimeError):
    pass

class ExaResearcher:
    def __init__(self, api_key: str, now):
        self.api_key = api_key
        self.now = now
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "CareerNewsBot-BDjobs/3.0",
        })

    def _post(self, path, payload):
        url = EXA_API_URL.rstrip("/") + path
        last = ""
        for attempt in range(3):
            try:
                r = self.session.post(url, json=payload, timeout=EXA_TIMEOUT)
                if r.status_code == 429:
                    wait = min(8, 2 ** attempt)
                    time.sleep(wait)
                    last = f"HTTP 429 after attempt {attempt+1}"
                    continue
                if r.status_code >= 400:
                    raise ExaError(f"HTTP {r.status_code}: {r.text[:300]}")
                return r.json()
            except requests.RequestException as exc:
                last = str(exc)
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise ExaError(last or "Exa request failed")

    def search(self, query: str, start, num_results=10):
        payload = {
            "query": query,
            "type": "auto",
            "numResults": min(100, max(1, num_results)),
            "includeDomains": list(BDJOBS_DOMAINS),
            "startPublishedDate": start.astimezone(__import__("datetime").timezone.utc).isoformat(),
            "endPublishedDate": (self.now + timedelta(hours=2)).astimezone(__import__("datetime").timezone.utc).isoformat(),
            "contents": {
                "highlights": {"maxCharacters": 1400},
            },
        }
        data = self._post("/search", payload)
        return data.get("results", []) or []

    def discover(self, queries, start, max_results=180):
        merged = {}
        for query in queries:
            try:
                rows = self.search(query, start, MAX_EXA_RESULTS_PER_QUERY)
                logger.info("EXA SEARCH query=%s results=%d", query, len(rows))
            except Exception as exc:
                logger.warning("EXA SEARCH FAILED query=%s error=%s", query, exc)
                continue
            for row in rows:
                url = safe_text(row.get("url"))
                title = safe_text(row.get("title"))
                if not url or not title or not is_bdjobs_url(url):
                    continue
                can = canonical_url(url)
                if not can:
                    continue
                highlights = row.get("highlights") or []
                if not isinstance(highlights, list):
                    highlights = [highlights]
                snippet = " ".join(safe_text(x) for x in highlights if safe_text(x))
                item = {
                    "url": url,
                    "canonical": can,
                    "title": title[:240],
                    "published_date": safe_text(row.get("publishedDate")),
                    "exa_id": safe_text(row.get("id")) or url,
                    "exa_score": row.get("score"),
                    "snippet": snippet[:2500],
                    "image": safe_text(row.get("image")),
                    "discovery": "exa",
                    "source": "Bdjobs.com",
                    "source_type": "bdjobs",
                }
                old = merged.get(can)
                if not old:
                    merged[can] = item
                else:
                    if len(item["snippet"]) > len(old.get("snippet", "")):
                        old["snippet"] = item["snippet"]
                    old["discovery_count"] = int(old.get("discovery_count", 1)) + 1
        rows = list(merged.values())
        rows.sort(key=lambda x: (-int(x.get("discovery_count", 1)), -(float(x.get("exa_score") or 0))))
        return rows[:max_results]

    def get_contents(self, urls_or_ids: list[str]) -> dict[str, dict]:
        if not urls_or_ids:
            return {}
        output = {}
        # Exa permits up to 100 ids/URLs per Contents call.
        for start in range(0, len(urls_or_ids), 100):
            batch = urls_or_ids[start:start + 100]
            payload = {
                "ids": batch,
                "text": True,
                "maxAgeHours": EXA_CACHE_HOURS,
            }
            try:
                data = self._post("/contents", payload)
            except Exception as exc:
                logger.warning("EXA CONTENTS BATCH FAILED size=%d error=%s", len(batch), exc)
                continue
            for row in data.get("results", []) or []:
                key = safe_text(row.get("id")) or safe_text(row.get("url"))
                output[key] = {
                    "id": key,
                    "url": safe_text(row.get("url")),
                    "title": safe_text(row.get("title")),
                    "published_date": safe_text(row.get("publishedDate")),
                    "text": safe_text(row.get("text")),
                    "summary": safe_text(row.get("summary")),
                }
        return output
