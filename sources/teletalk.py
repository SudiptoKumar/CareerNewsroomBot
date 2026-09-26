from __future__ import annotations

import re
from urllib.parse import quote

from core.config import Config
from core.models import JobRecord
from core.utils import clean_text, canonical_url, iso_date
from .base import SourceAdapter

class TeletalkAdapter(SourceAdapter):
    name = "Teletalk"

    def __init__(self, http, config: Config):
        super().__init__(http)
        self.config = config

    def discover(self) -> list[JobRecord]:
        try:
            import requests
            response = self.http.session.get(self.config.teletalk_api, params={"searchKeyword": ""}, timeout=self.config.discovery_timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []
        records = []
        if isinstance(payload, dict):
            for key in ("govtJobs", "data", "jobs", "results"):
                if isinstance(payload.get(key), list):
                    records = payload[key]
                    break
        out: list[JobRecord] = []
        seen: set[str] = set()
        for raw in records:
            if not isinstance(raw, dict):
                continue
            job = self._from_api(raw)
            if not job or job.canonical_url in seen:
                continue
            seen.add(job.canonical_url)
            out.append(job)
            if len(out) >= self.config.teletalk_discovery_cap:
                break
        return out

    def _from_api(self, raw: dict) -> JobRecord | None:
        def pick(*keys):
            for k in keys:
                value = raw.get(k)
                if value not in (None, ""):
                    return value
            return ""
        job_id = clean_text(pick("job_primary_id", "jobPrimaryId", "id"))
        title = clean_text(pick("job_title", "jobTitle", "title"))
        if not job_id or not title:
            return None
        company = clean_text(pick("org_name", "orgName", "organization", "company"))
        location = clean_text(pick("location", "job_location", "jobLocation"))
        vacancy = clean_text(pick("vacancy", "number_of_vacancy", "numberOfVacancy"))
        deadline = iso_date(pick("deadline_date", "deadlineDate", "deadline"))
        published = iso_date(pick("published_date", "publish_date", "posted_date", "postedDate"))
        apply_url = clean_text(pick("application_site_url", "applicationSiteUrl", "apply_url", "applyUrl"))
        education = clean_text(re.sub(r"<[^>]+>", " ", clean_text(pick("education", "education_qualification"))))
        source_url = f"{self.config.teletalk_base}/?job_primary_id={quote(job_id)}"
        raw_text = " | ".join(x for x in (title, company, location, education) if x)
        return JobRecord(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company=company,
            source_url=source_url,
            canonical_url=canonical_url(source_url),
            discovery_url=self.config.teletalk_api,
            source_category="Government",
            source_category_url=self.config.teletalk_api,
            is_government=True,
            location=location,
            education=education,
            vacancy=vacancy,
            deadline=deadline,
            published_date=published,
            apply_url=apply_url,
            application_method="Online" if apply_url else "",
            raw_text=raw_text,
            raw_listing={"api": raw},
            extraction_status="listing_validated",
        )

    def enrich(self, job: JobRecord) -> JobRecord:
        # The API is the official authoritative source used for this lane.
        job.extraction_status = "detail_validated"
        return job
