from __future__ import annotations

import json
import logging
import re
import time
import threading
from dataclasses import dataclass, field
from urllib.request import Request, urlopen

import requests
try:
    import trafilatura
except ImportError:
    trafilatura = None
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import JINA_READER_BASE
from .utils import safe_text

logger = logging.getLogger("career-news-bot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CareerNewsBot/2.0; +https://github.com/)",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}


@dataclass
class RetrievalResult:
    url: str
    final_url: str = ""
    content: str = ""
    html: str = ""
    backend: str = ""
    success: bool = False
    quality: float = 0.0
    links: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: str = ""


class RetrievalRouter:
    """Agent-Reach-inspired ordered retrieval: direct HTTP -> Jina -> Exa Contents."""

    def __init__(self, exa_client=None):
        self.exa_client = exa_client
        self._local = threading.local()
        self._session_template = HEADERS.copy()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        self._retry = retry

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._session_template)
            adapter = HTTPAdapter(max_retries=self._retry, pool_connections=4, pool_maxsize=4)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    @staticmethod
    def _content_quality(text: str) -> float:
        t = safe_text(text)
        if not t:
            return 0.0
        score = min(70.0, len(t) / 80.0)
        markers = len(re.findall(r"\b(application|deadline|vacancy|position|education|experience|salary|responsibilities|requirements|apply)\b", t, re.I))
        score += min(30.0, markers * 3.0)
        return min(100.0, score)

    @staticmethod
    def _is_antibot(text: str) -> bool:
        sample = safe_text(text)[:5000].casefold()
        return any(marker in sample for marker in (
            "just a moment...", "performing security verification", "attention required! | cloudflare",
            "requiring captcha", "access denied", "verify you are human",
        ))

    def direct(self, url: str) -> RetrievalResult:
        try:
            response = self._session().get(url, headers={**HEADERS, "Referer": url}, timeout=25, allow_redirects=True)
            if response.status_code >= 400:
                return RetrievalResult(url=url, final_url=response.url, backend="direct_http", error=f"HTTP {response.status_code}")
            content_type = safe_text(response.headers.get("content-type")).lower()
            if content_type and "html" not in content_type and "text" not in content_type:
                return RetrievalResult(url=url, final_url=response.url, backend="direct_http", error=f"unsupported content-type {content_type}")
            html_text = response.text
            if self._is_antibot(html_text):
                return RetrievalResult(url=url, final_url=response.url, backend="direct_http", error="anti-bot challenge")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_text, "html.parser")
            extracted = trafilatura.extract(html_text, include_comments=False, include_tables=True, favor_precision=True) if trafilatura else ""
            text = extracted or soup.get_text(" ", strip=True)
            links = []
            for a in soup.find_all("a", href=True):
                label = a.get_text(" ", strip=True) or safe_text(a.get("aria-label")) or safe_text(a.get("title"))
                href = safe_text(a.get("href"))
                if href:
                    links.append({"url": href, "label": label, "kind": "anchor", "context": safe_text(a.parent.get_text(" ", strip=True))[:240] if getattr(a, "parent", None) else ""})
            return RetrievalResult(
                url=url, final_url=response.url, html=html_text, content=text,
                backend="direct_http", success=True, quality=self._content_quality(text), links=links,
                metadata={"status_code": response.status_code, "content_type": content_type},
            )
        except Exception as exc:
            return RetrievalResult(url=url, backend="direct_http", error=str(exc))

    def jina(self, url: str) -> RetrievalResult:
        jina_url = f"{JINA_READER_BASE}/{url}"
        try:
            req = Request(jina_url, headers={**HEADERS, "Accept": "text/plain"})
            with urlopen(req, timeout=35) as response:
                body = response.read(6 * 1024 * 1024)
            text = body.decode("utf-8", errors="replace")
            if self._is_antibot(text):
                raise RuntimeError("Jina returned an anti-bot challenge page")
            links = []
            for label, href in re.findall(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)", text):
                links.append({"url": href, "label": label, "kind": "markdown", "context": f"Jina markdown link: {label}"})
            quality = self._content_quality(text)
            return RetrievalResult(url=url, final_url=url, content=text, backend="jina", success=quality >= 35, quality=quality, links=links)
        except Exception as exc:
            return RetrievalResult(url=url, backend="jina", error=str(exc))

    def exa_contents(self, url: str) -> RetrievalResult:
        if self.exa_client is None:
            return RetrievalResult(url=url, backend="exa_contents", error="Exa client unavailable")
        try:
            result = self.exa_client.get_contents([url], text=True, max_age_hours=24)
            rows = getattr(result, "results", None) or getattr(result, "documents", None) or []
            if not rows:
                return RetrievalResult(url=url, backend="exa_contents", error="no content returned")
            row = rows[0]
            text = safe_text(getattr(row, "text", "") or (row.get("text") if isinstance(row, dict) else ""))
            return RetrievalResult(url=url, final_url=safe_text(getattr(row, "url", "")) or url, content=text, backend="exa_contents", success=bool(text), quality=self._content_quality(text), metadata={"title": safe_text(getattr(row, "title", ""))})
        except Exception as exc:
            return RetrievalResult(url=url, backend="exa_contents", error=str(exc))

    def read(self, url: str, min_quality: float = 55.0) -> RetrievalResult:
        attempts = [self.direct(url), self.jina(url)]
        for result in attempts:
            if result.success and result.quality >= min_quality:
                return result
        exa = self.exa_contents(url)
        if exa.success and exa.quality >= 35:
            return exa
        for result in attempts:
            if result.success:
                return result
        return exa if exa.success else next((r for r in attempts if r.error), RetrievalResult(url=url, backend="router", error="all retrieval backends failed"))

    def health(self) -> dict:
        return {
            "direct_http": {"status": "ready", "backend": "requests"},
            "jina": {"status": "ready", "backend": "Jina Reader"},
            "exa_contents": {"status": "ready" if self.exa_client is not None else "optional_unavailable", "backend": "Exa Contents"},
        }
