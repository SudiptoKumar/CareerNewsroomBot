from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

from .config import Config

logger = logging.getLogger("career-news-v2.http")

@dataclass
class FetchResult:
    ok: bool
    url: str
    text: str = ""
    status: int = 0
    backend: str = ""
    error: str = ""

class HttpClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        retry = Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "CareerNewsroom/2.0 (+https://t.me/CareerNewsroom)",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })

    def fetch(self, url: str, *, referer: str = "", timeout: int | None = None,
              allow_browser: bool = True, wait_for: str = "") -> FetchResult:
        timeout = timeout or self.config.detail_timeout
        headers = {"Referer": referer} if referer else {}
        # Direct first. curl_cffi is optional and only used when installed.
        if curl_requests:
            for impersonate in ("chrome", "safari_ios"): 
                try:
                    r = curl_requests.get(url, headers=headers, timeout=timeout, impersonate=impersonate)
                    if r.status_code < 500 and r.text:
                        result = FetchResult(r.ok, str(r.url), r.text, r.status_code, f"curl_cffi:{impersonate}")
                        if self._looks_useful(result.text, url):
                            return result
                except Exception as exc:
                    logger.debug("curl_cffi failed %s: %s", url, exc)
        try:
            r = self.session.get(url, headers=headers, timeout=timeout)
            result = FetchResult(r.ok, r.url, r.text, r.status_code, "requests")
            if result.ok and self._looks_useful(result.text, url):
                return result
            direct_error = f"status={result.status} shell_or_empty={not self._looks_useful(result.text, url)}"
        except Exception as exc:
            result = FetchResult(False, url, error=str(exc), backend="requests")
            direct_error = str(exc)

        if allow_browser and self.config.browser_enabled:
            browser = self._browser_fetch(url, referer=referer, wait_for=wait_for)
            if browser.ok:
                return browser

        return FetchResult(False, url, status=result.status, backend=result.backend, error=direct_error)

    @staticmethod
    def _looks_useful(text: str, url: str) -> bool:
        if not text or len(text) < 250:
            return False
        low = text.lower()
        if "just a moment" in low or "cf-chl" in low or "enable javascript" in low and len(text) < 2000:
            return False
        host = url.lower()
        if "bdjobs-live" in host or "bdjobslive.com" in host:
            return "/bdjobs-details/" in low or "/bdjobs-circular/" in low or "application deadline" in low
        if "bdjobs.com" in host:
            return "/h/details/" in low or "app-job-list" in low or "job-title" in low or "application deadline" in low
        return True

    def _browser_fetch(self, url: str, *, referer: str = "", wait_for: str = "") -> FetchResult:
        if sync_playwright is None:
            return FetchResult(False, url, backend="browser", error="playwright not installed")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = browser.new_context(user_agent="Mozilla/5.0 CareerNewsroom/2.0", locale="en-US")
                page = context.new_page()
                if referer:
                    page.set_extra_http_headers({"Referer": referer})
                page.goto(url, wait_until="domcontentloaded", timeout=self.config.browser_timeout_ms)
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=min(self.config.browser_timeout_ms, 15000))
                    except Exception:
                        pass
                page.wait_for_timeout(self.config.browser_wait_ms)
                html = page.content()
                final_url = page.url
                browser.close()
                if html and len(html) > 250:
                    return FetchResult(True, final_url, html, 200, "playwright")
        except Exception as exc:
            logger.warning("Browser fetch failed | url=%s | error=%s", url, exc)
        return FetchResult(False, url, backend="browser", error="browser fetch failed")
