from __future__ import annotations

import argparse
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests

from careerbot.config import (
    BD_TZ, DISCOVERY_LOOKBACK_HOURS, MAX_RETRIEVE_CANDIDATES, MIN_RETRIEVE_CANDIDATES,
    MIN_STORIES_PER_RUN, MAX_STORIES_PER_RUN, POST_DELAY_SECONDS, TELEGRAM_CHANNEL, current_window,
)
from careerbot.discovery import collect_direct, collect_exa_search, collect_google_news, collect_rss, merge_candidates
from careerbot.extraction import deterministic_job_fields, collect_apply_links, is_real_vacancy, audience_fit, batch_extract_with_ai
from careerbot.ranking import ai_rank, event_dedup, local_rank, select_for_publish, source_reliability
from careerbot.retrieval import RetrievalRouter
from careerbot.state import append_posted_url, event_already_published, load_posted_urls, load_state, prune_state, remember_event, save_state
from careerbot.publisher import publish
from careerbot.utils import canonical_url, clean_text, normalize_domain, normalize_title, parse_datetime, safe_text, title_similarity

logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s")
logger=logging.getLogger("career-news-bot")

# One shared HTTP session for discovery. RetrievalRouter owns a second session so each layer is isolated.
SESSION=requests.Session()
SESSION.headers.update({"User-Agent":"Mozilla/5.0 (compatible; CareerNewsBot/2.0)"})


class Runtime:
    def __init__(self):
        self.state=load_state()
        self.posted=load_posted_urls()
        self.window=current_window()
        self.exa=None
        self.cerebras=None
        self.router=None

    def clients(self):
        if self.exa is None:
            try:
                from exa_py import Exa
                key=os.getenv("EXA_API_KEY","")
                self.exa=Exa(api_key=key) if key else None
            except Exception as exc:
                logger.warning("Exa SDK unavailable: %s",exc); self.exa=None
        if self.cerebras is None:
            try:
                from cerebras.cloud.sdk import Cerebras
                key=os.getenv("CEREBRAS_API_KEY","")
                self.cerebras=Cerebras(api_key=key) if key else None
            except Exception as exc:
                logger.warning("Cerebras SDK unavailable: %s",exc); self.cerebras=None
        if self.router is None:self.router=RetrievalRouter(self.exa)
        return self.exa,self.cerebras,self.router


def quick_vacancy_gate(item: dict) -> tuple[bool,float,list[str]]:
    from careerbot.config import BAD_CONTENT_TERMS, TARGET_ROLE_TERMS, VACANCY_TERMS
    blob=clean_text(f"{item.get('title','')} {item.get('excerpt','')}").lower()
    positive=sum(1 for x in VACANCY_TERMS if x in blob)
    target=sum(min(4,1) for x in TARGET_ROLE_TERMS if x in blob)
    bad=sum(1 for x in BAD_CONTENT_TERMS if x in blob)
    if not safe_text(item.get("url")) or not safe_text(item.get("title")):return False,0,["missing identity"]
    score=min(100,30+positive*10+target*3-bad*18)
    reasons=[]
    if positive>=2:reasons.append("vacancy evidence")
    if target>=1:reasons.append("target-role signal")
    if bad:reasons.append("career-content noise")
    return score>=35,score,reasons


def candidate_dedupe(items: list[dict]) -> list[dict]:
    out=[]
    for item in sorted(items,key=lambda x:(-float(x.get("quick_score",0)),safe_text(x.get("source")))):
        if event_already_published(STATE,item):continue
        if any(title_similarity(item.get("title",""),x.get("title",""))>=0.96 and normalize_domain(item.get("url"))==normalize_domain(x.get("url")) for x in out):continue
        out.append(item)
    return out


def retrieve_and_verify(runtime: Runtime, candidates: list[dict]) -> list[dict]:
    """Retrieve broadly in bounded parallelism, extract deterministically, then batch AI only for top records."""
    exa, cerebras, router = runtime.clients()
    limited = candidates[:MAX_RETRIEVE_CANDIDATES]

    def fetch(item):
        return item, router.read(item["url"])

    retrieved = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="retrieve") as pool:
        futures = [pool.submit(fetch, item) for item in limited]
        for future in as_completed(futures):
            try:
                item, result = future.result()
                retrieved.append((item, result))
            except Exception as exc:
                logger.warning("RETRIEVAL WORKER FAILED: %s", exc)

    preliminary = []
    ai_source_rows = []
    from careerbot.extraction import classify_apply_url, _date_from_text
    from careerbot.ranking import event_key, local_rank
    for idx, (item, result) in enumerate(retrieved, 1):
        if not result.success or result.quality < 40:
            logger.info(
                "RETRIEVAL DROP backend=%s title=%s reason=%s",
                result.backend, item.get("title"), result.error or f"quality={result.quality:.1f}",
            )
            continue

        text = result.content
        can = item.get("canonical")
        link_fingerprint = "\n".join(sorted(canonical_url(x.get("url")) for x in result.links if x.get("url")))
        content_hash = hashlib.sha1((text + "\n" + link_fingerprint).encode("utf-8", errors="ignore")).hexdigest()
        cached = runtime.state.get("queue", {}).get(can, {})

        if cached.get("content_hash") == content_hash and cached.get("job_record"):
            record = dict(cached["job_record"])
            record["source_text_evidence"] = text[:22000]
            record["retrieval_backend"] = result.backend
            record.setdefault("source_confidence", source_reliability(record))
            preliminary.append(record)
            continue

        det = deterministic_job_fields(
            item.get("title", "") or safe_text(result.metadata.get("title")),
            item["url"], text, result.html, runtime.window.now,
        )
        apply_candidates = collect_apply_links(result.final_url or item["url"], result.html, text)
        prelim = dict(item, **{k: v for k, v in det.items() if k not in {"deadline_dt", "posted_dt", "description"}})
        prelim["source_url"] = item["url"]
        prelim["apply_link_candidates"] = apply_candidates
        prelim["apply_url"], prelim["apply_confidence"] = classify_apply_url(apply_candidates, item["url"])
        prelim["retrieval_backend"] = result.backend
        prelim["retrieval_quality"] = result.quality
        prelim["content_hash"] = content_hash
        prelim["source_text_evidence"] = text[:22000]
        prelim["source_description"] = det.get("description", "")[:2500]
        prelim["deadline_iso"] = det.get("deadline_dt").isoformat() if det.get("deadline_dt") else ""

        posted_dt = det.get("posted_dt") or parse_datetime(item.get("published_date"))
        if posted_dt and posted_dt < runtime.window.start:
            logger.info("FRESHNESS DROP title=%s posted=%s", item.get("title"), posted_dt.isoformat())
            continue
        prelim["posted_date"] = (posted_dt or runtime.window.now).isoformat()
        prelim["source_confidence"] = source_reliability(item)
        if int(prelim["source_confidence"]) < 75:
            continue
        prelim["image_candidates"] = det.get("image_candidates") or ([item.get("image")] if item.get("image") else [])
        prelim["identity_confidence"] = 90 if det.get("company") and det.get("job_title") else 65
        lower = text.lower()
        prelim["bangladesh_relevance"] = 90 if any(x in lower for x in (
            "bangladesh", "dhaka", "chittagong", "chattogram", "rajshahi", "khulna", "barishal",
            "sylhet", "rangpur", "mymensingh", "গণপ্রজাতন্ত্রী বাংলাদেশ", "বাংলাদেশ",
        )) else 55

        okay, _ = is_real_vacancy(prelim, text)
        if not okay:
            continue
        aud, reasons = audience_fit(prelim, text)
        prelim["audience_score"] = aud
        prelim["audience_reasons"] = reasons
        preliminary.append(prelim)
        ai_source_rows.append((prelim, result, det, apply_candidates))

    if not preliminary:
        return []

    preliminary = local_rank(preliminary, runtime.window.now)
    selected = {r.get("canonical") for r in preliminary[:min(48, len(preliminary))]}
    ai_entries = [
        {"candidate": p, "retrieval": r, "deterministic": d, "apply_candidates": a}
        for p, r, d, a in ai_source_rows
        if p.get("canonical") in selected
    ]
    ai_rows = batch_extract_with_ai(cerebras, ai_entries, batch_size=5)
    ai_index = {e["candidate"].get("canonical"): i for i, e in enumerate(ai_entries)}

    records = []
    for prelim in preliminary:
        can = prelim.get("canonical")
        merged = dict(prelim)
        idx = ai_index.get(can)
        if idx is not None and idx in ai_rows:
            ai = ai_rows[idx]
            for key in (
                "job_title", "company", "location", "job_type", "education", "experience", "salary",
                "vacancies", "age_limit", "application_fee", "application_method", "application_period",
                "selection_process", "deadline",
            ):
                value = safe_text(ai.get(key))
                if value and not safe_text(merged.get(key)):
                    merged[key] = value
            apply = safe_text(ai.get("apply_url"))
            allowed = {canonical_url(x["url"]): x["url"] for x in merged.get("apply_link_candidates", []) if x.get("url")}
            if canonical_url(apply) in allowed:
                merged["apply_url"] = allowed[canonical_url(apply)]
                merged["apply_confidence"] = 0.96
            merged["identity_confidence"] = max(0, min(100, int(ai.get("confidence", merged.get("identity_confidence", 65)))))
            merged["bangladesh_relevance"] = max(0, min(100, int(ai.get("bangladesh_relevance", merged.get("bangladesh_relevance", 55)))))

        deadline_dt = _date_from_text(merged.get("deadline", ""), runtime.window.now) if merged.get("deadline") else None
        if deadline_dt and deadline_dt < runtime.window.now:
            continue
        aud, reasons = audience_fit(merged, merged.get("source_text_evidence", "")[:8000])
        merged["audience_score"] = aud
        merged["audience_reasons"] = reasons
        merged["deadline_iso"] = deadline_dt.isoformat() if deadline_dt else merged.get("deadline_iso", "")
        if (
            int(merged.get("identity_confidence", 0)) < 70
            or int(merged.get("bangladesh_relevance", 0)) < 70
            or aud < 55
        ):
            continue

        merged["event_key"] = event_key(merged)
        merged["status"] = "verified"
        runtime.state.setdefault("queue", {})[can] = {
            **{k: v for k, v in merged.items() if k not in {"source_text_evidence"}},
            "status": "verified",
            "last_seen": runtime.window.now.isoformat(),
            "job_record": {k: v for k, v in merged.items() if k not in {"source_text_evidence"}},
        }
        records.append(merged)
    return records


def build_hashtags(story):
    tags=["#CareerNewsroom"]
    text=(safe_text(story.get("job_title"))+" "+safe_text(story.get("education"))+" "+safe_text(story.get("job_type"))).lower()
    mapping=[
        ("#ManagementTrainee",("management trainee","graduate trainee")),
        ("#Internship",("internship","intern")),
        ("#BBA",("bba","business administration")),
        ("#MBA",("mba",)),
        ("#Finance",("finance","financial")),
        ("#Accounting",("accounting","accounts","audit")),
        ("#Banking",("bank","banking")),
        ("#Marketing",("marketing",)),
        ("#Sales",("sales",)),
        ("#HR",("human resources"," hr ")),
        ("#Business",("business development","business analyst","management")),
    ]
    for tag,terms in mapping:
        if any(t in (" "+text+" ") for t in terms) and tag not in tags:tags.append(tag)
    return tags[:5]


def pipeline() -> int:
    global STATE
    runtime=Runtime(); STATE=runtime.state
    w=runtime.window
    logger.info("CAREERNEWSBOT 2.0 | channel=%s | window=%s -> %s",TELEGRAM_CHANNEL,w.start.isoformat(),w.end.isoformat())
    prune_state(runtime.state,w.now)

    rss=collect_rss(SESSION,w,runtime.state,runtime.posted)
    direct=collect_direct(SESSION,w,runtime.posted)
    google=collect_google_news(SESSION,w,runtime.posted)
    exa=[]
    if len(rss)+len(direct)+len(google)<MIN_RETRIEVE_CANDIDATES:
        exa=collect_exa_search(runtime.clients()[0],w,runtime.posted)
    discovered=merge_candidates(rss,direct,google,exa)
    logger.info("DISCOVERED rss=%d direct=%d google=%d exa=%d merged=%d",len(rss),len(direct),len(google),len(exa),len(discovered))

    gated=[]
    for item in discovered:
        ok,score,_=quick_vacancy_gate(item)
        if ok:
            item["quick_score"]=score; gated.append(item)
    gated=candidate_dedupe(gated)
    gated.sort(key=lambda x:-float(x.get("quick_score",0)))
    logger.info("JOB GATE candidates=%d/%d",len(gated),len(discovered))

    target=max(MIN_RETRIEVE_CANDIDATES,min(MAX_RETRIEVE_CANDIDATES,len(gated)))
    records=retrieve_and_verify(runtime,gated[:target])
    logger.info("ACTIVE DEADLINES / VERIFIED CANDIDATES=%d", len(records))
    logger.info("REAL JOB RECORDS=%d",len(records))
    if not records:
        save_state(runtime.state); return 0

    records=event_dedup(records)
    logger.info("EVENT DEDUP=%d",len(records))
    records=local_rank(records,w.now)
    logger.info("LOCAL RANK=%d top_score=%s", len(records), records[0].get("local_score") if records else 0)
    # Local ranking first, then a single bounded AI ranking pass over structured JobRecords.
    records=ai_rank(runtime.clients()[1],records,w.now)
    logger.info("AI RANK=%d", len(records))
    records=event_dedup(records)
    selected=select_for_publish(records,MIN_STORIES_PER_RUN,MAX_STORIES_PER_RUN)
    logger.info("FINAL SELECTED=%d source_diversity=%d",len(selected),len({safe_text(x.get('source')) for x in selected}))

    published=0
    for index,story in enumerate(selected,1):
        story["hashtags"]=build_hashtags(story)
        result=publish(story,index)
        if result.get("ok"):
            published+=1
            canonical=canonical_url(story.get("source_url") or story.get("url"))
            if canonical:
                runtime.posted.add(canonical); append_posted_url(canonical)
                q=runtime.state.setdefault("queue",{}).get(canonical)
                if q:q["status"]="published"; q["published_at"]=runtime.window.now.isoformat()
            remember_event(runtime.state,story,True,result.get("result",{}).get("message_id") if isinstance(result.get("result"),dict) else None)
            runtime.state.setdefault("recent_titles",[]).append(normalize_title(story.get("job_title") or story.get("headline")))
            logger.info("PUBLISHED %d/%d %s",published,len(selected),story.get("job_title"))
        else:
            logger.error("PUBLISH FAILED %s",result.get("description"))
        save_state(runtime.state)
        if index<len(selected):
            import time; time.sleep(POST_DELAY_SECONDS)
    save_state(runtime.state)
    logger.info("DONE published=%d selected=%d",published,len(selected))
    return published


def self_test() -> None:
    from careerbot.config import APPLICATION_TERMS, MAX_SOURCE_POSTS_PER_RUN
    from careerbot.extraction import classify_apply_url, deterministic_job_fields, is_real_vacancy, audience_fit
    from careerbot.ranking import job_quality
    assert canonical_url("https://www.example.com/job/?utm_source=x") == "example.com/job"
    links=[{"url":"https://example.com/details","label":"Job Details","deterministic_score":0},{"url":"https://apply.example.org/123","label":"Apply Online","deterministic_score":100}]
    url,conf=classify_apply_url(links,"https://example.com/details")
    assert url=="https://apply.example.org/123" and conf>=0.9
    url2,conf2=classify_apply_url([{"url":"https://example.com/details2","label":"Read more","deterministic_score":0}],"https://example.com/details")
    assert url2=="" and conf2==0
    # Source URL can never become APPLY NOW by fallback.
    from careerbot.publisher import action
    fallback_url,fallback_label=action({"source_url":"https://example.com/job","apply_url":"","apply_link_candidates":[]})
    assert fallback_url=="https://example.com/job" and fallback_label=="READ MORE"
    # Batched extraction must map each returned ID to the correct source record.
    class _FakeCerebras:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    import json as _json
                    rows=[]
                    for rid in (1,2):
                        rows.append({"id":rid,"job_title":"Management Trainee" if rid==1 else "Finance Intern","company":"ABC PLC" if rid==1 else "XYZ Ltd","location":"Dhaka","job_type":"Full-time" if rid==1 else "Internship","education":"BBA / MBA","experience":"0-1 year","salary":"","vacancies":"01","age_limit":"","application_fee":"","application_method":"Online","application_period":"17-30 September 2026","selection_process":"","deadline":"30 September 2026","apply_url":"https://apply.example.com/"+str(rid),"confidence":95,"bangladesh_relevance":95})
                    msg=type("M",(),{"content":_json.dumps({"jobs":rows})})()
                    choice=type("C",(),{"message":msg})()
                    return type("R",(),{"choices":[choice]})()
    from careerbot.extraction import batch_extract_with_ai
    fake_entries=[]
    for rid in (1,2):
        fake_entries.append({"candidate":{"source":"Test","title":"T"+str(rid),"url":"https://example.com/"+str(rid)},"retrieval":type("R",(),{"content":"job content"})(),"deterministic":{},"apply_candidates":[{"url":"https://apply.example.com/"+str(rid),"label":"Apply Online","deterministic_score":100}]})
    rows=batch_extract_with_ai(_FakeCerebras(),fake_entries,batch_size=2)
    assert rows[0]["company"]=="ABC PLC" and rows[1]["company"]=="XYZ Ltd"
    text="ABC Bank PLC is hiring Management Trainees. BBA or MBA required. Location: Dhaka. Vacancies: 03. Deadline: 30 September 2026. Apply Online."
    record=deterministic_job_fields("Management Trainee","https://example.com/job",text,"",runtime_now())
    okay,reasons=is_real_vacancy({**record,"company":"ABC Bank PLC"},text)
    assert okay,reasons
    aud,reasons=audience_fit({**record,"company":"ABC Bank PLC","job_title":"Management Trainee","education":"BBA or MBA"},text)
    assert aud>=70 and reasons
    q,_=job_quality({**record,"company":"ABC Bank PLC","job_title":"Management Trainee","education":"BBA or MBA","identity_confidence":90,"bangladesh_relevance":95,"audience_score":aud,"source_type":"official_job_portal","source_url":"https://example.com"},runtime_now())
    assert 0<=q<=100
    logger.info("CareerNewsBot 2.0 self-test passed.")


def runtime_now():
    return datetime.now(BD_TZ)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:self_test()
    else:pipeline()
