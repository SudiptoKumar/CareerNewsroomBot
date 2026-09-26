from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Iterable

from .ai import AIJudge
from .config import Config, BD_TZ
from .models import JobRecord
from .publisher import TelegramPublisher
from .rules import hard_eligibility, deterministic_score, classify_internship
from .state import StateStore

from sources.bdjobs import BdjobsAdapter
from sources.bdjobslive import BdjobsLiveAdapter
from sources.teletalk import TeletalkAdapter

logger = logging.getLogger("career-news-v2.pipeline")

class Pipeline:
    def __init__(self, config: Config, state: StateStore, http):
        self.config = config
        self.state = state
        self.adapters = [BdjobsAdapter(http, config), BdjobsLiveAdapter(http, config), TeletalkAdapter(http, config)]
        self.ai = AIJudge(config)
        self.publisher = TelegramPublisher(config)

    def discover(self) -> list[JobRecord]:
        all_jobs: list[JobRecord] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            future_map = {pool.submit(adapter.discover): adapter.name for adapter in self.adapters}
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    jobs = future.result()
                except Exception as exc:
                    logger.exception("DISCOVERY FAILED | source=%s | error=%s", name, exc)
                    jobs = []
                logger.info("DISCOVERY | source=%s | count=%d", name, len(jobs))
                all_jobs.extend(jobs)
        return self._dedup_discovered(all_jobs)

    @staticmethod
    def _dedup_discovered(jobs: Iterable[JobRecord]) -> list[JobRecord]:
        seen = set()
        out = []
        for job in jobs:
            key = (job.source, job.source_job_id) if job.source_job_id else job.canonical_url
            if key in seen:
                continue
            seen.add(key); out.append(job)
        return out

    def enrich(self, jobs: list[JobRecord]) -> list[JobRecord]:
        adapter_map = {adapter.name: adapter for adapter in self.adapters}
        out: list[JobRecord] = []
        with ThreadPoolExecutor(max_workers=self.config.detail_workers) as pool:
            futures = {pool.submit(adapter_map[job.source].enrich, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    out.append(future.result())
                except Exception as exc:
                    job.extraction_status = "detail_fetch_failed"
                    logger.warning("DETAIL ERROR | source=%s | id=%s | error=%s", job.source, job.source_job_id, exc)
                    out.append(job)
        return out

    def filter(self, jobs: list[JobRecord], today: date) -> tuple[list[JobRecord], dict[str, int]]:
        eligible = []
        counts: dict[str, int] = {}
        published = list(self.state.data.get("events", {}).values())
        for job in jobs:
            ok, reason, details = hard_eligibility(job, today=today, config=self.config, published_events=published)
            counts[reason] = counts.get(reason, 0) + 1
            if not ok:
                self.state.record_rejection(job.to_dict(), "eligibility", reason, details)
                continue
            job.eligibility = {"eligible_for_ai": True, **details}
            eligible.append(job)
        return eligible, counts

    def ai_rank(self, jobs: list[JobRecord]) -> list[JobRecord]:
        # Absolute pre-AI invariant. It should be impossible for an unknown date/deadline job to enter here.
        for job in jobs:
            if not job.published_date or not job.deadline or job.deadline_status != "active" or job.eligibility.get("eligible_for_ai") is not True:
                raise RuntimeError(f"PRE_AI_INVARIANT_FAILED:{job.source}:{job.source_job_id}")
        judged = self.ai.judge(jobs)
        for job in judged:
            job.ai["deterministic_score"] = deterministic_score(job)
            job.ai["final_score"] = round(job.ai.get("deterministic_score", 0) * 0.85 + float(job.ai.get("score", 0)) * 0.15, 3)
        ranked = [j for j in judged if j.ai.get("publish", True)]
        ranked.sort(key=lambda x: (-float(x.ai.get("final_score", 0)), -int(x.age_days or 0), x.canonical_url))
        return ranked

    def select(self, ranked: list[JobRecord]) -> list[JobRecord]:
        government = [j for j in ranked if j.is_government]
        internships = [j for j in ranked if not j.is_government and classify_internship(j)]
        private_regular = [j for j in ranked if not j.is_government and not classify_internship(j)]
        selected: list[JobRecord] = []
        used = set()
        for pool, target in ((private_regular, self.config.private_target), (government, self.config.government_target), (internships, self.config.internship_target)):
            for job in pool[:target]:
                if job.canonical_url not in used:
                    selected.append(job); used.add(job.canonical_url)
        target_total = min(self.config.hard_max_posts, self.config.private_target + self.config.government_target + self.config.internship_target + self.config.flexible_extras)
        if len(selected) < target_total:
            for job in ranked:
                if job.canonical_url in used:
                    continue
                selected.append(job); used.add(job.canonical_url)
                if len(selected) >= target_total:
                    break
        return selected[:self.config.hard_max_posts]

    def publish(self, selected: list[JobRecord], dry_run: bool = False) -> int:
        published = 0
        for job in selected:
            if dry_run:
                logger.info("DRY RUN PUBLISH | %s | %s | %s", job.source, job.source_job_id, job.title)
                continue
            ok, message_id, error = self.publisher.publish(job)
            if ok:
                published += 1
                job.publication = {"published": True, "message_id": message_id}
                event = job.to_dict(); event["event_id"] = f"{job.source}:{job.source_job_id}"
                self.state.mark_posted(event, message_id)
            else:
                logger.error("PUBLISH FAILED | %s | %s | %s", job.source, job.source_job_id, error)
        self.state.save()
        return published

    def expire(self) -> int:
        count = 0
        now = datetime.now(BD_TZ)
        for event_id, event in list(self.state.data.get("events", {}).items()):
            if event.get("status") != "published" or not event.get("deadline"):
                continue
            from core.utils import parse_date
            d = parse_date(event.get("deadline"))
            if d and d < now.date():
                ok, error = self.publisher.expire(event)
                if ok:
                    event["status"] = "expired"; event["expired_at"] = now.isoformat(); count += 1
                else:
                    logger.warning("EXPIRY UPDATE FAILED | event=%s | error=%s", event_id, error)
        self.state.save()
        return count
