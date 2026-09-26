from __future__ import annotations

import re
from datetime import date
from typing import Any

from .config import Config, NON_BUSINESS_ROLE_TERMS, TARGET_FUNCTION_TERMS, BDJOBS_CATEGORIES
from .models import JobRecord
from .parsing import parse_age_bounds, parse_experience_bounds
from .utils import date_age_days, clean_text, normalize_company, normalize_title, title_similarity

REJECT_ORDER = (
    "invalid_source", "invalid_identity", "detail_failed", "duplicate",
    "published_date_unknown", "future_published_date", "too_old", "deadline_unknown",
    "deadline_expired", "experience_above_3_years", "age_incompatible",
    "clearly_unrelated_profession", "bba_mba_relevance_missing", "invalid_vacancy",
)


def classify_internship(job: JobRecord) -> bool:
    blob = " ".join((job.title, job.job_type, job.category, job.raw_text, job.experience, job.education)).lower()
    return bool(re.search(r"\bintern(ship)?\b", blob)) or job.is_internship


def allowed_source(job: JobRecord, config: Config) -> bool:
    source = job.source
    if source not in {"Bdjobs", "BDJobs Live", "Teletalk"}:
        return False
    discovery = clean_text(job.discovery_url)
    if source == "Bdjobs":
        approved_discovery = any(
            "/h/jobs" in discovery and f"fcatId={cid}" in discovery
            for cid in BDJOBS_CATEGORIES
        )
        return "/h/details/" in job.source_url and "bdjobs.com" in job.source_url and approved_discovery
    if source == "BDJobs Live":
        approved = ("/bdjobs-circular/" in discovery) or (discovery.rstrip("/") == config.bdjobslive_internship_url.rstrip("/"))
        return "/bdjobs-details/" in job.source_url and "bdjobslive.com" in job.source_url and approved
    return source == "Teletalk" and discovery.startswith(config.teletalk_api) and "alljobs.teletalk.com.bd" in job.source_url


def duplicate_against(job: JobRecord, published: list[dict[str, Any]]) -> bool:
    for old in published:
        if old.get("source") == job.source and old.get("source_job_id") == job.source_job_id:
            return True
        if old.get("canonical_url") == job.canonical_url:
            return True
        company_same = normalize_company(old.get("company", "")) == normalize_company(job.company) and bool(job.company)
        title_sim = title_similarity(old.get("title", ""), job.title)
        if company_same and title_sim >= 0.90:
            old_loc = clean_text(old.get("location", "")).lower()
            loc = clean_text(job.location).lower()
            if not old_loc or not loc or old_loc == loc:
                return True
    return False


def date_gate(job: JobRecord, today: date, config: Config) -> tuple[bool, str, dict[str, Any]]:
    age = date_age_days(job.published_date, today)
    details = {"age_days": age}
    if age is None:
        return False, "published_date_unknown", details
    if age < 0:
        return False, "future_published_date", details
    if age >= config.max_post_age_days:
        return False, "too_old", details
    deadline = None
    if job.deadline:
        from .utils import parse_date
        deadline = parse_date(job.deadline)
    if deadline is None:
        return False, "deadline_unknown", details
    if deadline < today:
        return False, "deadline_expired", {**details, "days_to_deadline": (deadline - today).days}
    return True, "ok", {**details, "days_to_deadline": (deadline - today).days}


def experience_gate(job: JobRecord, config: Config) -> tuple[bool, str, dict[str, Any]]:
    if classify_internship(job) and not clean_text(job.experience):
        return True, "ok", {"internship": True}
    minimum, maximum, kind = parse_experience_bounds(job.experience)
    if minimum is None and maximum is None:
        # Missing experience is acceptable only for internships. For regular jobs, the source did not give enough evidence.
        return False, "experience_unknown", {"experience_kind": kind}
    if kind == "open_min" and minimum is not None and minimum >= config.max_experience_years:
        return False, "experience_above_3_years", {"min": minimum, "max": maximum, "kind": kind}
    if maximum is not None and maximum > config.max_experience_years:
        return False, "experience_above_3_years", {"min": minimum, "max": maximum, "kind": kind}
    return True, "ok", {"min": minimum, "max": maximum, "kind": kind}


def age_gate(job: JobRecord, config: Config) -> tuple[bool, str, dict[str, Any]]:
    raw = clean_text(job.age)
    if not raw:
        return True, "ok", {"age_unknown": True}
    amin, amax = parse_age_bounds(raw)
    details = {"min": amin, "max": amax, "raw": raw}
    # User compatibility rule is containment, not interval overlap:
    # every explicit bound must fit inside 18-30. Thus 18-35 and 25-32 reject.
    if amin is not None and amin < config.min_age:
        return False, "age_incompatible", details
    if amin is not None and amin > config.max_age:
        return False, "age_incompatible", details
    if amax is not None and amax > config.max_age:
        return False, "age_incompatible", details
    if amax is not None and amax < config.min_age:
        return False, "age_incompatible", details
    return True, "ok", details


def bba_mba_gate(job: JobRecord) -> tuple[bool, str, dict[str, Any]]:
    blob = " ".join((job.title, job.education, job.category, job.requirements.__str__(), job.raw_text[:6000])).lower()
    title = clean_text(job.title).lower()
    if any(term in title for term in NON_BUSINESS_ROLE_TERMS):
        return False, "clearly_unrelated_profession", {}
    bba_mba = bool(re.search(r"\bbba\b|bachelor\s+of\s+business\s+administration|\bmba\b|master\s+of\s+business\s+administration", blob))
    business_role = any(term in blob for term in TARGET_FUNCTION_TERMS)
    if not (bba_mba or business_role):
        return False, "bba_mba_relevance_missing", {}
    return True, "ok", {"bba_mba_explicit": bba_mba, "business_role": business_role}


def vacancy_gate(job: JobRecord) -> tuple[bool, str, dict[str, Any]]:
    if not clean_text(job.vacancy):
        return True, "ok", {"vacancy_unknown": True}
    if not re.search(r"\d+", job.vacancy):
        return False, "invalid_vacancy", {}
    return True, "ok", {}


def hard_eligibility(job: JobRecord, *, today: date, config: Config, published_events: list[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    if not allowed_source(job, config):
        return False, "invalid_source", {}
    if not job.source_job_id or not job.title or not job.company or not job.canonical_url:
        return False, "invalid_identity", {}
    if job.extraction_status not in {"detail_validated", "listing_validated"}:
        return False, "detail_failed", {"extraction_status": job.extraction_status}
    if duplicate_against(job, published_events):
        return False, "duplicate", {}
    ok, reason, date_details = date_gate(job, today, config)
    if not ok:
        return False, reason, date_details
    if job.source != "Teletalk":
        ok, reason, details = experience_gate(job, config)
        if not ok:
            return False, reason, details
        ok, reason, details = age_gate(job, config)
        if not ok:
            return False, reason, details
        ok, reason, details = bba_mba_gate(job)
        if not ok:
            return False, reason, details
        ok, reason, details = vacancy_gate(job)
        if not ok:
            return False, reason, details
    else:
        # Government jobs still need the same freshness/deadline rules and a business relevance check.
        ok, reason, details = bba_mba_gate(job)
        if not ok:
            return False, reason, details
    job.age_days = date_details.get("age_days")
    job.days_to_deadline = date_details.get("days_to_deadline")
    job.deadline_status = "active"
    job.is_internship = classify_internship(job)
    return True, "ok", {**date_details}


def deterministic_score(job: JobRecord) -> float:
    score = 0.0
    blob = " ".join((job.title, job.education, job.experience, job.category, job.raw_text[:9000])).lower()
    score += 25 if re.search(r"\bbba\b|bachelor\s+of\s+business\s+administration", blob) else 0
    score += 25 if re.search(r"\bmba\b|master\s+of\s+business\s+administration", blob) else 0
    score += 20 if any(term in job.title.lower() for term in TARGET_FUNCTION_TERMS) else 0
    if job.age_days is not None:
        score += 20 - min(job.age_days * 5, 15)
    if job.days_to_deadline is not None:
        score += max(0, 10 - min(job.days_to_deadline, 10))
    if job.salary:
        score += 5
    if job.vacancy:
        score += 3
    if job.responsibilities:
        score += 3
    return round(score, 3)
