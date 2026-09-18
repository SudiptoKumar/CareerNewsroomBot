from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from .config import INVENTORY_MAX_DAYS, BD_TZ
from .utils import canonical_url, event_key, parse_datetime

STATE_PATH = Path("news_state.json")
POSTED_PATH = Path("posted_urls.txt")

def default_state() -> dict:
    return {
        "version": 3,
        "inventory": {},
        "published_events": {},
        "retrieval_cache": {},
        "search_runs": [],
    }

def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        base = default_state()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return default_state()

def save_state(state: dict):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)

def load_posted() -> set[str]:
    try:
        return {canonical_url(x) for x in POSTED_PATH.read_text(encoding="utf-8").splitlines() if canonical_url(x)}
    except FileNotFoundError:
        return set()

def mark_posted(url: str):
    can = canonical_url(url)
    if not can:
        return
    with POSTED_PATH.open("a", encoding="utf-8") as f:
        f.write(can + "\n")

def prune(state: dict, now):
    cut = now - timedelta(days=INVENTORY_MAX_DAYS)
    inventory = {}
    for key, item in state.get("inventory", {}).items():
        if not isinstance(item, dict):
            continue
        last = parse_datetime(item.get("last_seen"))
        deadline = parse_datetime(item.get("deadline_iso"))
        if deadline and deadline >= now:
            inventory[key] = item
        elif last and last >= cut:
            inventory[key] = item
    state["inventory"] = inventory
    # Keep cache short enough for repository state.
    cache = {}
    cache_cut = now - timedelta(hours=26)
    for key, item in state.get("retrieval_cache", {}).items():
        ts = parse_datetime(item.get("retrieved_at"))
        if ts and ts >= cache_cut:
            cache[key] = item
    state["retrieval_cache"] = cache
    state["search_runs"] = state.get("search_runs", [])[-100:]

def upsert_inventory(state: dict, job: dict):
    key = canonical_url(job.get("source_url")) or event_key(job)
    if not key:
        return
    state.setdefault("inventory", {})[key] = {
        k: v for k, v in job.items()
        if k != "source_text"
    }

def already_published(state: dict, job: dict) -> bool:
    ek = event_key(job)
    return bool(ek and ek in state.get("published_events", {}))

def remember_published(state: dict, job: dict, message_id=None):
    ek = event_key(job)
    if not ek:
        return
    state.setdefault("published_events", {})[ek] = {
        "job_title": job.get("job_title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "source_url": job.get("source_url"),
        "published_at": __import__("datetime").datetime.now(BD_TZ).isoformat(),
        "message_id": message_id,
    }
