from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

from careerbot.config import (
    BD_TZ, DISCOVERY_DAYS, EXA_QUERIES, MAX_CONTENTS_URLS, MAX_DISCOVERY,
    MAX_POSTS, MIN_FINAL_SCORE, MIN_AUDIENCE_SCORE, MIN_FACT_CONFIDENCE,
    MAX_INDEX_PAGES, current_window,
)
from careerbot.bdjobs import (
    quick_filter, is_index, discover_index_pages, fetch_html,
    extract_job_record, validate_active, audience_score, hard_noise,
)
from careerbot.exa_research import ExaResearcher
from careerbot.judge import judge_batches
from careerbot.ranking import local_quality, final_score, event_dedup, select_for_publish
from careerbot.publisher import publish
from careerbot.state import (
    load_state, save_state, load_posted, mark_posted, prune,
    upsert_inventory, already_published, remember_published,
)
from careerbot.utils import canonical_url, event_key, normalize_title, parse_datetime, safe_text, short_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("career-news-bot")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent":"Mozilla/5.0 (compatible; CareerNewsBot-BDjobs/3.0)"})

def _make_exa(now):
    key = os.getenv("EXA_API_KEY", "")
    return ExaResearcher(key, now) if key else None

def _retrieval_cache_get(state, canonical, now):
    key = f"{canonical}|{now.date().isoformat()}"
    row = state.get("retrieval_cache", {}).get(key)
    if not isinstance(row, dict):
        return None
    ts = parse_datetime(row.get("retrieved_at"))
    if not ts or (now - ts).total_seconds() > 24 * 3600:
        return None
    return row

def _retrieval_cache_put(state, canonical, now, text, metadata):
    key = f"{canonical}|{now.date().isoformat()}"
    state.setdefault("retrieval_cache", {})[key] = {
        "retrieved_at": now.isoformat(),
        "text": text[:26000],
        "metadata": metadata,
    }

def _prepare_candidates(items):
    out = {}
    stats = {"noise":0, "index":0, "kept":0}
    for item in items:
        title, url = safe_text(item.get("title")), safe_text(item.get("url"))
        if hard_noise(title, url, item.get("snippet","")):
            stats["noise"] += 1
            continue
        if is_index(url, title):
            stats["index"] += 1
            # Keep index pages separately. They are not scored as jobs.
            can = canonical_url(url)
            if can:
                item = dict(item)
                item["canonical"] = can
                out[("index", can)] = item
            continue
        ok, score, reasons = quick_filter(item)
        if not ok:
            continue
        can = canonical_url(url)
        if not can:
            continue
        item = dict(item)
        item["canonical"] = can
        item["quick_score"] = score
        item["quick_reasons"] = reasons
        old = out.get(("vacancy", can))
        if old is None or score > old.get("quick_score", 0):
            out[("vacancy", can)] = item
    stats["kept"] = sum(1 for k in out if k[0] == "vacancy")
    return out, stats

def _expand_indexes(indexes, state, now):
    rows = []
    for item in indexes[:MAX_INDEX_PAGES]:
        can = item["canonical"]
        cached = _retrieval_cache_get(state, can, now)
        if cached:
            html = cached.get("metadata", {}).get("html", "")
        else:
            html, final_url, status = fetch_html(HTTP, item["url"])
            if not html:
                logger.info("INDEX READ FAIL title=%s status=%s", item.get("title"), status)
                continue
            _retrieval_cache_put(state, can, now, "", {"html": html[:60000], "final_url": final_url})
        from careerbot.bdjobs import extract_bdjobs_links
        links = extract_bdjobs_links(html, item["url"])
        for link in links[:250]:
            child = {
                "url": link["url"],
                "canonical": canonical_url(link["url"]),
                "title": link["label"],
                "snippet": link["context"],
                "published_date": item.get("published_date",""),
                "source": "Bdjobs.com",
                "source_type": "bdjobs",
                "discovery": "bdjobs_index_expansion",
            }
            if hard_noise(child["title"], child["url"], child["snippet"]):
                continue
            ok, score, reasons = quick_filter(child)
            if ok and not is_index(child["url"], child["title"]):
                child["quick_score"] = score
                child["quick_reasons"] = reasons
                rows.append(child)
    dedup={}
    for row in rows:
        dedup[row["canonical"]] = row
    logger.info("INDEX EXPANSION candidates=%d", len(dedup))
    return list(dedup.values())

def _build_exa_content_map(exa, candidates):
    if exa is None or not candidates:
        return {}
    ids = [safe_text(x.get("exa_id") or x.get("url")) for x in candidates[:MAX_CONTENTS_URLS]]
    return exa.get_contents(ids)

def _direct_link_evidence(url):
    # Deterministic verifier only: used for application links and OG images.
    html, final_url, status = fetch_html(HTTP, url)
    return html, final_url, status

def _resolve_apply(job):
    candidates = job.get("apply_link_candidates") or []
    if not candidates:
        return "", 0.0
    for row in sorted(candidates, key=lambda x: -int(x.get("deterministic_score",0))):
        url = safe_text(row.get("url"))
        score = int(row.get("deterministic_score",0))
        if score >= 80 and url != safe_text(job.get("source_url")):
            return url, min(0.99, 0.70 + score/300)
    return "", 0.0

def build_hashtags(job):
    text = normalize_title(" ".join([
        safe_text(job.get("job_title")), safe_text(job.get("education")),
        safe_text(job.get("job_type")), safe_text(job.get("source_text"))[:3000],
    ]))
    tags = ["#CareerNewsroom"]
    pairs = [
        ("#BBA","bba"),("#MBA","mba"),("#ManagementTrainee","management trainee"),
        ("#GraduateTrainee","graduate trainee"),("#Internship","intern"),
        ("#Finance","finance"),("#Accounting","accounting"),("#Banking","bank"),
        ("#Marketing","marketing"),("#Sales","sales"),("#HR","human resources"),
        ("#Business","business development"),
    ]
    for tag, term in pairs:
        if normalize_title(term) in text and tag not in tags:
            tags.append(tag)
    return tags[:5]

def recover_inventory(state, now, posted):
    rows=[]
    for key, job in state.get("inventory", {}).items():
        if not isinstance(job, dict):
            continue
        if already_published(state, job):
            continue
        if canonical_url(job.get("source_url")) in posted:
            continue
        deadline = parse_datetime(job.get("deadline_iso"))
        if deadline and deadline < now:
            continue
        last_seen = parse_datetime(job.get("last_seen"))
        if not deadline and last_seen and (now-last_seen).days > 30:
            continue
        rows.append(dict(job, inventory=True))
    return rows

def run(dry_run=False) -> int:
    w = current_window()
    state = load_state()
    posted = load_posted()
    prune(state, w.now)

    logger.info("CAREERNEWSBOT BDJOBS-ONLY | discovery=%s -> %s", w.discovery_start.isoformat(), w.now.isoformat())
    logger.info("SOURCES=BDJOBS_ONLY")
    if not os.getenv("EXA_API_KEY"):
        logger.error("EXA_API_KEY missing. This build requires Exa for research.")
        save_state(state)
        return 2

    exa = _make_exa(w.now)
    if exa is None:
        logger.error("Exa client unavailable.")
        return 2

    # 1) Exa research: all discovery is restricted to BDjobs.
    discovered = exa.discover(EXA_QUERIES, w.discovery_start, MAX_DISCOVERY)

    # 2) Direct BDjobs index pages are cheap expansion only.
    index_candidates = []
    for url in ("https://jobs.bdjobs.com/jobsearch-cache.asp", "https://jobs.bdjobs.com/bn/otherjobsbn.asp?JobType=new"):
        index_candidates.append({
            "url": url, "canonical": canonical_url(url), "title": "BDjobs New Jobs Index",
            "snippet": "BDjobs.com new jobs", "source":"Bdjobs.com", "source_type":"bdjobs", "discovery":"direct_index"
        })
    prepared, stats = _prepare_candidates(discovered + index_candidates)
    indexes = [v for (kind,_),v in prepared.items() if kind=="index"]
    vacancies = [v for (kind,_),v in prepared.items() if kind=="vacancy"]
    expanded = _expand_indexes(indexes, state, w.now)
    vacancies = vacancies + expanded
    # Dedup and rerank cheap candidates.
    merged = {}
    for item in vacancies:
        can = canonical_url(item.get("url"))
        if not can or can in posted:
            continue
        old = merged.get(can)
        if old is None or float(item.get("quick_score",0)) > float(old.get("quick_score",0)):
            merged[can] = item
    vacancies = list(merged.values())
    vacancies.sort(key=lambda x: -float(x.get("quick_score",0)))
    logger.info(
        "DISCOVERY exa=%d index=%d index_expanded=%d vacancy_candidates=%d noise_blocked=%d",
        len(discovered), len(indexes), len(expanded), len(vacancies), stats["noise"]
    )

    # 3) Full research content for top candidates.
    content_map = _build_exa_content_map(exa, vacancies[:MAX_CONTENTS_URLS])
    records = []
    retrieval_fail = 0
    for candidate in vacancies[:MAX_CONTENTS_URLS]:
        can = candidate["canonical"]
        cached = _retrieval_cache_get(state, can, w.now)
        text = ""
        meta = {}
        if cached:
            text = safe_text(cached.get("text"))
            meta = cached.get("metadata") or {}
            logger.debug("CACHE HIT %s", can)
        else:
            row = content_map.get(candidate.get("exa_id")) or content_map.get(candidate.get("url")) or {}
            text = safe_text(row.get("text"))
            meta = row
            if not text:
                retrieval_fail += 1
                logger.info("EXA CONTENT FAIL title=%s", candidate.get("title"))
                continue
            _retrieval_cache_put(state, can, w.now, text, meta)

        html, final_url, status = _direct_link_evidence(candidate["url"])
        # Exa is the researcher; direct HTTP is only a deterministic link/image verifier.
        job = extract_job_record(candidate, text, html, w.now)
        job["source"] = "Bdjobs.com"
        job["source_url"] = candidate["url"]
        job["retrieval_backend"] = "exa_contents"
        job["retrieval_content_length"] = len(text)
        job["content_hash"] = short_hash(text)
        job["last_seen"] = w.now.isoformat()

        # Exa page date is a fallback only. Explicit job page date wins.
        if not job.get("posted_date") and candidate.get("published_date"):
            job["posted_date"] = candidate["published_date"]
            job["date_source"] = "exa_published_date"
            job["date_confidence"] = 55

        ok, reasons = validate_active(job, w.now)
        if not ok:
            logger.info("VACANCY REJECT title=%s reasons=%s", job.get("job_title"), ",".join(reasons))
            continue
        aud, aud_reasons = audience_score(job)
        job["local_audience_score"] = aud
        job["audience_reasons"] = aud_reasons
        job["identity_confidence"] = 92 if job.get("job_title") and job.get("company") else 68

        # Application link and image candidates require deterministic source-page HTML.
        apply_url, apply_conf = _resolve_apply(job)
        job["apply_url"] = apply_url
        job["apply_confidence"] = apply_conf

        local, components = local_quality(job, w.now)
        job["local_score"] = local
        job["quality_components"] = components

        # Don't hard reject thin retrieved content. It can still be judged if identity is strong.
        if len(text) < 500:
            job["retrieval_confidence"] = "LOW"
        elif len(text) < 1500:
            job["retrieval_confidence"] = "MEDIUM"
        else:
            job["retrieval_confidence"] = "HIGH"

        if job["identity_confidence"] < 68:
            logger.info("FACT REJECT title=%s identity=%s", job.get("job_title"), job["identity_confidence"])
            continue

        records.append(job)

    logger.info("RESEARCH verified=%d retrieval_fail=%d", len(records), retrieval_fail)

    # 4) Batch Cerebras editorial judgment.
    judgments = judge_batches(os.getenv("CEREBRAS_API_KEY",""), records)
    if not judgments:
        logger.error("CEREBRAS JUDGE returned no usable judgments.")
        save_state(state)
        return 3

    judged=[]
    for job in records:
        key = safe_text(job.get("canonical") or job.get("source_url"))
        row = judgments.get(key)
        if not row:
            continue
        job = dict(job)
        job["ai_quality_score"] = int(row.get("quality_score",0))
        job["ai_audience_score"] = int(row.get("audience_score",0))
        job["early_career_fit"] = int(row.get("early_career_fit",0))
        job["factual_confidence"] = int(row.get("factual_confidence",0))
        job["ai_publish_recommendation"] = bool(row.get("publish"))
        job["ai_reason"] = safe_text(row.get("reason"))
        job["risk_flags"] = row.get("risk_flags") or []
        # Deterministic safety gate.
        deadline = parse_datetime(job.get("deadline_iso"))
        if deadline and deadline < w.now:
            continue
        if job["identity_confidence"] < MIN_FACT_CONFIDENCE:
            continue
        if job["factual_confidence"] < MIN_FACT_CONFIDENCE:
            continue
        if max(job["local_audience_score"], job["ai_audience_score"]) < MIN_AUDIENCE_SCORE:
            continue
        # A current BDjobs listing without a deadline is allowed only when fresh enough.
        if not deadline:
            posted = parse_datetime(job.get("posted_date"))
            if not posted or (w.now - posted).total_seconds() > DISCOVERY_DAYS * 86400:
                continue
        job["final_score"] = final_score(job, w.now)
        if job["final_score"] < MIN_FINAL_SCORE:
            continue
        job["hashtags"] = build_hashtags(job)
        job["status"] = "verified"
        upsert_inventory(state, job)
        judged.append(job)

    # 5) Merge persistent active inventory, then dedup and rank.
    for job in recover_inventory(state, w.now, posted):
        if all(event_key(job) != event_key(x) for x in judged):
            judged.append(job)

    judged = event_dedup(judged)
    for job in judged:
        if not job.get("final_score"):
            job["final_score"], _ = local_quality(job, w.now)
    judged.sort(key=lambda x: -int(x.get("final_score",0)))
    selected = select_for_publish(judged, MAX_POSTS)
    logger.info(
        "RANKING candidates=%d final=%d selected=%d top=%s",
        len(judged), len(judged), len(selected),
        selected[0].get("final_score") if selected else 0,
    )

    if dry_run:
        for i, job in enumerate(selected, 1):
            logger.info(
                "DRY RUN #%d score=%s title=%s company=%s deadline=%s apply=%s",
                i, job.get("final_score"), job.get("job_title"),
                job.get("company"), job.get("deadline"), bool(job.get("apply_url"))
            )
        save_state(state)
        return 0

    published = 0
    for i, job in enumerate(selected, 1):
        result = publish(job, i)
        if result.get("ok"):
            published += 1
            src = canonical_url(job.get("source_url"))
            if src:
                mark_posted(src)
                posted.add(src)
            remember_published(
                state, job,
                result.get("result", {}).get("message_id") if isinstance(result.get("result"), dict) else None,
            )
            inv_key = src
            if inv_key in state.get("inventory", {}):
                state["inventory"][inv_key]["status"] = "published"
                state["inventory"][inv_key]["published_at"] = w.now.isoformat()
            logger.info("PUBLISHED %d/%d title=%s score=%s", published, len(selected), job.get("job_title"), job.get("final_score"))
        else:
            logger.error("PUBLISH FAILED title=%s error=%s", job.get("job_title"), result.get("description"))
        save_state(state)
        if i < len(selected):
            import time
            time.sleep(1.5)
    state.setdefault("search_runs", []).append({
        "run_at": w.now.isoformat(),
        "discovered": len(discovered),
        "vacancy_candidates": len(vacancies),
        "verified": len(records),
        "judged": len(judged),
        "selected": len(selected),
        "published": published,
    })
    save_state(state)
    logger.info("DONE published=%d selected=%d", published, len(selected))
    return 0

def self_test():
    from datetime import datetime
    from careerbot.bdjobs import quick_filter, is_index, extract_job_record, validate_active, audience_score
    from careerbot.publisher import action
    from careerbot.ranking import final_score

    fake_now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=BD_TZ)
    sample = {
        "url":"https://jobs.bdjobs.com/jobdetails.asp?id=123456",
        "canonical":"jobs.bdjobs.com/jobdetails.asp?id=123456",
        "title":"Management Trainee Officer - Finance",
        "snippet":"BBA/MBA. Freshers can apply. Dhaka. Deadline: 30 Sep 2026.",
        "source":"Bdjobs.com",
    }
    ok, score, _ = quick_filter(sample)
    assert ok and score >= 60
    assert is_index("https://jobs.bdjobs.com/jobsearch-cache.asp", "BDjobs New Jobs Index")
    assert not is_index(sample["url"], sample["title"])

    html = """<html><head><meta property="og:image" content="https://jobs.bdjobs.com/image.jpg"></head>
    <body><h1>Management Trainee Officer - Finance</h1>
    Company: ABC Bank PLC
    Job Location: Dhaka
    Education: Bachelor of Business Administration (BBA) or MBA
    Experience: Freshers
    Salary: Tk. 40,000
    Vacancies: 03
    Job Nature: Full-time
    Published: 15 September 2026
    Deadline: 30 September 2026
    How to Apply: Apply online
    <a href="https://jobs.bdjobs.com/jobdetails.asp?id=123456">Job Details</a>
    <a href="https://jobs.bdjobs.com/apply.asp?id=123456">Apply Online</a>
    </body></html>"""
    job = extract_job_record(sample, "Management Trainee Officer - Finance Company: ABC Bank PLC Job Location: Dhaka Education: BBA or MBA Experience: Freshers Deadline: 30 September 2026", html, fake_now)
    job["identity_confidence"] = 92
    valid, reasons = validate_active(job, fake_now)
    assert valid, reasons
    aud, _ = audience_score(job)
    assert aud >= 70
    apply_url, label = action({**job, "source_url": sample["url"], "apply_url":"https://jobs.bdjobs.com/apply.asp?id=123456"})
    assert label == "APPLY NOW" and apply_url.endswith("apply.asp?id=123456")
    fallback_url, fallback_label = action({**job, "source_url": sample["url"], "apply_url":""})
    assert fallback_label == "READ MORE" and fallback_url == sample["url"]
    job.update({"local_audience_score":aud,"local_score":85,"ai_quality_score":90,"ai_audience_score":92,"early_career_fit":94})
    assert final_score(job,fake_now) >= 80

    # Continuous inventory rule: 10-day-old active job remains eligible.
    old = dict(job)
    old["posted_date"] = "2026-09-08T10:00:00+06:00"
    old["deadline_iso"] = "2026-10-01T23:59:59+06:00"
    old["local_audience_score"] = 85
    old["ai_quality_score"] = 85
    assert parse_datetime(old["deadline_iso"]) > fake_now
    assert final_score(old, fake_now) > 60

    # Noise must be blocked before any retrieval spend.
    noisy = {"url":"https://jobs.bdjobs.com/jobdetails.asp?id=9","title":"Salary Calculator","snippet":"calculate govt salary"}
    ok2,_,_ = quick_filter(noisy)
    assert not ok2

    # External source never qualifies as source.
    assert not __import__("careerbot.utils", fromlist=["is_bdjobs_url"]).is_bdjobs_url("https://example.com/job")

    logger.info("CareerNewsBot BDjobs-only self-test passed.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Run research/judging without Telegram publishing.")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    return run(dry_run=args.dry_run)

if __name__ == "__main__":
    raise SystemExit(main())
