from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config, BD_TZ
from .utils import canonical_url, parse_date

class StateStore:
    def __init__(self, config: Config, root: Path):
        self.config = config
        self.root = root
        self.state_path = root / "news_state.json"
        self.posted_path = root / "posted_urls.txt"
        self.data = self._load()
        self.posted_urls = self._load_posted()

    def _default(self) -> dict[str, Any]:
        return {
            "format_version": self.config.state_format_version,
            "schedule_guard": {},
            "queue": {},
            "events": {},
            "rejections": {},
            "run_history": [],
            "last_run": "",
            "pipeline_version": self.config.pipeline_version,
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            base = self._default()
            if isinstance(data, dict):
                # Preserve every existing key. V2 only adds keys, never wipes old history.
                base.update(data)
            base["pipeline_version"] = self.config.pipeline_version
            base["format_version"] = max(int(base.get("format_version", 0) or 0), self.config.state_format_version)
            return base
        except Exception:
            # Never destroy a potentially valuable state file because of malformed JSON.
            return self._default()

    def _load_posted(self) -> set[str]:
        if not self.posted_path.exists():
            return set()
        return {canonical_url(x) for x in self.posted_path.read_text(encoding="utf-8").splitlines() if x.strip()}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def mark_posted(self, job: dict[str, Any], message_id: int | None) -> None:
        canonical = canonical_url(job.get("source_url", ""))
        if canonical:
            self.posted_urls.add(canonical)
            with self.posted_path.open("a", encoding="utf-8") as f:
                f.write(canonical + "\n")
        event_id = job.get("event_id") or f"{job.get('source')}:{job.get('source_job_id')}"
        self.data.setdefault("events", {})[event_id] = {
            **job,
            "status": "published",
            "published_at": datetime.now(BD_TZ).isoformat(),
            "message_id": message_id,
        }

    def record_rejection(self, job: dict[str, Any], stage: str, reason: str, extra: dict[str, Any] | None = None) -> None:
        key = job.get("event_id") or f"{job.get('source')}:{job.get('source_job_id')}:{job.get('canonical_url')}"
        self.data.setdefault("rejections", {})[key] = {
            "source": job.get("source"),
            "source_job_id": job.get("source_job_id"),
            "title": job.get("title"),
            "stage": stage,
            "reason": reason,
            "checked_at": datetime.now(BD_TZ).isoformat(),
            **(extra or {}),
        }

    def schedule_allowed(self, session: str, now: datetime, manual: bool) -> bool:
        if manual:
            return True
        today = now.date().isoformat()
        guard = self.data.setdefault("schedule_guard", {})
        if guard.get(session) == today:
            return False
        guard[session] = today
        self.save()
        return True

    def prune(self, days: int = 90) -> None:
        cutoff = datetime.now(BD_TZ) - timedelta(days=days)
        events = self.data.get("events", {})
        self.data["events"] = {
            k: v for k, v in events.items()
            if _record_date(v.get("published_at")) is None or _record_date(v.get("published_at")) >= cutoff.date()
        }
        self.data["run_history"] = self.data.get("run_history", [])[-100:]


def _record_date(value: str):
    try:
        return datetime.fromisoformat(value).date()
    except Exception:
        return None
