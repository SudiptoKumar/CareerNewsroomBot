from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import BD_TZ, EVENT_RETENTION_DAYS, POSTED_FILE, QUEUE_RETENTION_DAYS, STATE_FILE
from .utils import canonical_url, parse_datetime, safe_text


def default_state() -> dict:
    return {
        "version": 2,
        "feeds": {},
        "queue": {},
        "events": {},
        "source_health": {},
        "posted_event_ids": [],
        "recent_titles": [],
    }


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw=json.load(f)
        base=default_state()
        if isinstance(raw,dict): base.update(raw)
        return base
    except Exception:
        return default_state()


def save_state(state: dict):
    tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(state,f,ensure_ascii=False,indent=2)
    import os
    os.replace(tmp,STATE_FILE)


def load_posted_urls() -> set[str]:
    try:
        with open(POSTED_FILE,"r",encoding="utf-8") as f:
            return {canonical_url(x) for x in f if safe_text(x)}
    except FileNotFoundError:
        return set()


def append_posted_url(url: str):
    can=canonical_url(url)
    if not can:return
    with open(POSTED_FILE,"a",encoding="utf-8") as f:f.write(can+"\n")


def prune_state(state: dict, now: datetime):
    qcut=now-timedelta(days=QUEUE_RETENTION_DAYS)
    ecut=now-timedelta(days=EVENT_RETENTION_DAYS)
    q=state.get("queue",{})
    state["queue"]={k:v for k,v in q.items() if (parse_datetime(v.get("last_seen") or v.get("published_date")) or now)>=qcut}
    e=state.get("events",{})
    state["events"]={k:v for k,v in e.items() if (parse_datetime(v.get("published_at")) or now)>=ecut}
    state["recent_titles"]=state.get("recent_titles",[])[-500:]


def remember_event(state: dict, story: dict, published: bool=False, message_id=None):
    eid=safe_text(story.get("event_key")) or canonical_url(story.get("source_url") or story.get("url"))
    if not eid:return
    event={
        "event_id":eid,"headline":story.get("job_title") or story.get("headline"),"company":story.get("company"),
        "source":story.get("source"),"source_url":story.get("source_url") or story.get("url"),
        "deadline":story.get("deadline"),"status":"published" if published else story.get("status","verified"),
        "published_at":datetime.now(BD_TZ).isoformat(),"message_id":message_id,
    }
    state.setdefault("events",{})[eid]=event
    if published:
        ids=state.setdefault("posted_event_ids",[])
        if eid not in ids:ids.append(eid)
        state["posted_event_ids"]=ids[-500:]


def event_already_published(state: dict, item: dict) -> bool:
    can=canonical_url(item.get("source_url") or item.get("url"))
    if can and can in {canonical_url(x) for x in load_posted_urls()}:return True
    title=safe_text(item.get("job_title") or item.get("title"))
    company=safe_text(item.get("company"))
    for ev in state.get("events",{}).values():
        if ev.get("status")!="published":continue
        if company and safe_text(ev.get("company")).lower()!=company.lower():continue
        from .utils import title_similarity
        if title and title_similarity(title,safe_text(ev.get("headline")))>=0.90:return True
    return False
