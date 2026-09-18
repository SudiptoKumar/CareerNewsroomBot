from __future__ import annotations

from datetime import timedelta

from .config import MAX_POSTS, MAX_COMPANY_POSTS, MIN_FINAL_SCORE
from .utils import parse_datetime, safe_text, event_key, clamp, title_similarity

def freshness_score(job: dict, now) -> int:
    dt = parse_datetime(job.get("posted_date"))
    conf = int(job.get("date_confidence", 0) or 0)
    if not dt:
        return 35
    age_h = max(0, (now - dt).total_seconds() / 3600)
    if age_h <= 24: return 100
    if age_h <= 48: return 92
    if age_h <= 72: return 84
    if age_h <= 96: return 78
    if age_h <= 168: return 70
    if age_h <= 336: return 55 if conf >= 70 else 45
    if age_h <= 720: return 42 if conf >= 70 else 30
    return 20

def deadline_score(job: dict, now) -> int:
    dt = parse_datetime(job.get("deadline_iso"))
    if not dt:
        return 45
    if dt < now:
        return 0
    days = (dt - now).total_seconds() / 86400
    if days < 1: return 25
    if days < 3: return 45
    if days < 7: return 65
    if days < 14: return 80
    if days < 30: return 92
    return 100

def completeness(job: dict) -> int:
    fields = (
        "job_title","company","location","job_type","education","experience",
        "salary","vacancies","application_method","deadline"
    )
    return clamp(100 * sum(bool(safe_text(job.get(k))) for k in fields) / len(fields))

def local_quality(job: dict, now) -> tuple[int, dict]:
    audience = int(job.get("local_audience_score", 0) or 0)
    fresh = freshness_score(job, now)
    deadline = deadline_score(job, now)
    comp = completeness(job)
    fact = int(job.get("identity_confidence", 0) or 0)
    apply = 100 if job.get("apply_url") else 60
    score = (
        0.30 * audience +
        0.18 * fact +
        0.16 * fresh +
        0.16 * deadline +
        0.10 * comp +
        0.10 * apply
    )
    return clamp(score), {
        "audience": audience, "freshness": fresh, "deadline": deadline,
        "completeness": comp, "fact": fact, "application": apply,
    }

def final_score(job: dict, now) -> int:
    local = int(job.get("local_score", 0) or 0)
    ai = int(job.get("ai_quality_score", local) or local)
    ai_audience = int(job.get("ai_audience_score", job.get("local_audience_score", 0)) or 0)
    early = int(job.get("early_career_fit", 0) or 0)
    return clamp(0.45 * ai + 0.20 * ai_audience + 0.15 * early + 0.20 * local)

def event_dedup(records: list[dict]) -> list[dict]:
    clusters = []
    for item in records:
        placed = False
        key = event_key(item)
        item["event_key"] = key
        for cluster in clusters:
            rep = cluster[0]
            if key and key == rep.get("event_key"):
                cluster.append(item)
                placed = True
                break
            if (
                safe_text(item.get("company")).casefold() == safe_text(rep.get("company")).casefold()
                and title_similarity(item.get("job_title"), rep.get("job_title")) >= 0.94
            ):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    winners = []
    for cluster in clusters:
        cluster.sort(key=lambda x: -int(x.get("final_score", x.get("local_score", 0))))
        winner = dict(cluster[0])
        winner["duplicate_count"] = len(cluster)
        winners.append(winner)
    return winners

def select_for_publish(records: list[dict], max_posts=MAX_POSTS) -> list[dict]:
    selected = []
    company_counts = {}
    for r in sorted(records, key=lambda x: -int(x.get("final_score", 0))):
        score = int(r.get("final_score", 0))
        if score < MIN_FINAL_SCORE:
            continue
        company = safe_text(r.get("company")) or "Unknown"
        if company_counts.get(company, 0) >= MAX_COMPANY_POSTS:
            continue
        selected.append(r)
        company_counts[company] = company_counts.get(company, 0) + 1
        if len(selected) >= max_posts:
            break
    return selected
