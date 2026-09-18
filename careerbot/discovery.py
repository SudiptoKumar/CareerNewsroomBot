from __future__ import annotations

import logging
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
try:
    import feedparser
except ImportError:
    feedparser = None
import xml.etree.ElementTree as ET

from .config import DIRECT_SOURCES, GOOGLE_QUERIES, MAX_DISCOVERY_CANDIDATES, PRIMARY_DOMAINS, RSS_FEEDS, Window
from .utils import canonical_url, parse_datetime, safe_text, source_name

logger = logging.getLogger("career-news-bot")


def _allowed(url: str) -> bool:
    domain = url.split("/", 3)[2].lower().removeprefix("www.") if "://" in url else ""
    return any(domain == d or domain.endswith("." + d) for d in PRIMARY_DOMAINS)


def _parse_feed(content):
    if feedparser is not None:
        return feedparser.parse(content).entries
    root=ET.fromstring(content)
    rows=[]
    for node in root.iter():
        if node.tag.lower().endswith("item") or node.tag.lower().endswith("entry"):
            row={}
            for child in list(node):
                key=child.tag.rsplit("}",1)[-1].lower()
                if key=="link": row["link"]=child.attrib.get("href") or child.text or ""
                else: row[key]=child.text or ""
            rows.append(row)
    return rows


def _rss_date(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                from datetime import datetime, timezone
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for key in ("published", "updated", "created"):
        dt = parse_datetime(entry.get(key))
        if dt:
            return dt
    return None


def _candidate(title, url, published_dt, source, source_type, excerpt="", discovery="rss", image=""):
    return {
        "title": safe_text(title), "url": url, "canonical": canonical_url(url),
        "published_date": published_dt.isoformat() if published_dt else "", "source": source,
        "source_type": source_type, "excerpt": safe_text(excerpt)[:3000], "image": safe_text(image),
        "discovery": discovery, "status": "pending",
    }


def collect_rss(session, window: Window, state: dict, posted: set[str]) -> list[dict]:
    found=[]
    for feed in RSS_FEEDS:
        old=state.setdefault("feeds", {}).get(feed["url"], {})
        headers={}
        if old.get("etag"): headers["If-None-Match"]=old["etag"]
        if old.get("last_modified"): headers["If-Modified-Since"]=old["last_modified"]
        try:
            r=session.get(feed["url"], headers=headers, timeout=20)
            if r.status_code == 304: continue
            if r.status_code >= 400:
                logger.warning("RSS %s -> HTTP %s", feed["name"], r.status_code)
                continue
            state["feeds"][feed["url"]]={"etag":r.headers.get("ETag",old.get("etag")),"last_modified":r.headers.get("Last-Modified",old.get("last_modified"))}
            entries=_parse_feed(r.content)
            for entry in entries:
                dt=_rss_date(entry)
                if not dt or not (window.start <= dt <= window.end): continue
                url=urljoin(feed["url"], safe_text(entry.get("link")))
                title=safe_text(entry.get("title"))
                if not url or not title or not _allowed(url): continue
                can=canonical_url(url)
                if not can or can in posted: continue
                summary=BeautifulSoup(safe_text(entry.get("summary") or entry.get("description")),"html.parser").get_text(" ",strip=True)
                found.append(_candidate(title,url,dt.astimezone(window.now.tzinfo),feed["name"],feed["type"],summary,"rss",safe_text(entry.get("media_thumbnail", ""))))
        except Exception as exc:
            logger.warning("RSS %s failed: %s", feed["name"], exc)
    return found


def collect_direct(session, window: Window, posted: set[str]) -> list[dict]:
    found=[]
    for source in DIRECT_SOURCES:
        try:
            r=session.get(source["url"], timeout=15)
            if r.status_code >= 400: continue
            soup=BeautifulSoup(r.text,"html.parser")
            for a in soup.find_all("a",href=True)[:500]:
                title=safe_text(a.get_text(" ",strip=True))
                if not (8 <= len(title) <= 220): continue
                url=urljoin(r.url,safe_text(a.get("href")))
                if not _allowed(url): continue
                parent=a
                context=""
                for _ in range(4):
                    parent=getattr(parent,"parent",None)
                    if parent is None: break
                    context=parent.get_text(" ",strip=True)
                    if len(context)>=120: break
                from .config import VACANCY_TERMS
                blob=(title+" "+context).lower()
                if not any(term in blob for term in VACANCY_TERMS): continue
                can=canonical_url(url)
                if not can or can in posted: continue
                found.append(_candidate(title,url,window.now,source["name"],source["type"],context,"direct_portal"))
        except Exception as exc:
            logger.debug("Direct discovery %s failed: %s", source["name"], exc)
    return found


def collect_google_news(session, window: Window, posted: set[str]) -> list[dict]:
    found=[]
    for query in GOOGLE_QUERIES:
        feed_url=f"https://news.google.com/rss/search?q={quote(query+' when:3d')}&hl=en-US&gl=BD&ceid=BD:en"
        try:
            r=session.get(feed_url,timeout=20)
            if r.status_code>=400: continue
            entries=_parse_feed(r.content)
            for entry in entries[:12]:
                dt=_rss_date(entry)
                if not dt or not (window.start <= dt <= window.end): continue
                redirect=safe_text(entry.get("link"))
                if not redirect: continue
                rr=session.get(redirect,timeout=12,allow_redirects=True,stream=True)
                real=safe_text(rr.url); rr.close()
                if not real or not _allowed(real): continue
                can=canonical_url(real)
                if not can or can in posted: continue
                title=safe_text(entry.get("title"))
                summary=BeautifulSoup(safe_text(entry.get("summary")),"html.parser").get_text(" ",strip=True)
                found.append(_candidate(title,real,dt.astimezone(window.now.tzinfo),source_name(real),"google_news",summary,"google_news"))
        except Exception as exc:
            logger.debug("Google News query failed: %s", exc)
    return found


def collect_exa_search(exa_client, window: Window, posted: set[str]) -> list[dict]:
    if exa_client is None:
        return []
    found=[]
    queries=GOOGLE_QUERIES
    for query in queries:
        try:
            result=exa_client.search(
                query,
                type="auto",
                num_results=10,
                start_published_date=window.start.isoformat(),
                end_published_date=window.end.isoformat(),
                include_domains=PRIMARY_DOMAINS,
                contents={"highlights": {"max_characters": 1200}},
            )
            for row in (getattr(result,"results",None) or []):
                url=safe_text(getattr(row,"url","")); title=safe_text(getattr(row,"title",""));
                if not url or not title or not _allowed(url): continue
                can=canonical_url(url)
                if not can or can in posted: continue
                dt=parse_datetime(getattr(row,"published_date","")) or window.now
                highlights=getattr(row,"highlights",None) or []
                if not isinstance(highlights,list): highlights=[highlights]
                excerpt=" ".join(safe_text(x) for x in highlights if safe_text(x))
                found.append(_candidate(title,url,dt,source_name(url),"exa",excerpt,"exa"))
        except Exception as exc:
            logger.warning("Exa discovery failed: %s", exc)
    return found


def merge_candidates(*groups) -> list[dict]:
    merged={}
    for group in groups:
        for item in group:
            can=item.get("canonical")
            if not can: continue
            old=merged.get(can)
            if old is None:
                merged[can]=dict(item)
            else:
                for k,v in item.items():
                    if v and not old.get(k): old[k]=v
                old["discovery_methods"]=sorted(set(old.get("discovery_methods",[])) | {item.get("discovery","")})
    return list(merged.values())[:MAX_DISCOVERY_CANDIDATES]
