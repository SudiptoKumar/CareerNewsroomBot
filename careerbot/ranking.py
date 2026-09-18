from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import CEREBRAS_MODEL, MAX_AI_RANK_CANDIDATES, MAX_SOURCE_POSTS_PER_RUN, SOURCE_DIVERSITY_TARGET, TARGET_ROLE_TERMS, MIN_FINAL_QUALITY_SCORE
from .extraction import audience_fit
from .utils import normalize_title, parse_datetime, safe_text, short_hash, title_similarity

logger=logging.getLogger("career-news-bot")


def source_reliability(item):
    t=item.get("source_type",""); d=safe_text(item.get("url","")).lower()
    if t=="official_job_portal": return 100
    if any(x in d for x in ("bdjobs.com","dohaj.com","bdjobslive.com","job.com.bd")): return 96
    if t=="job_portal": return 94
    if t=="news_jobs": return 88
    if t=="rss": return 84
    if t=="google_news": return 80
    if t=="exa": return 78
    return 70


def freshness_score(item, now):
    dt=parse_datetime(item.get("posted_date") or item.get("published_date"))
    if not dt: return 35
    age=max(0,(now-dt).total_seconds()/3600)
    if age<=6:return 100
    if age<=12:return 96
    if age<=24:return 92
    if age<=48:return 80
    if age<=72:return 65
    return 20


def deadline_score(item, now):
    dt=parse_datetime(item.get("deadline_iso") or item.get("deadline"))
    if not dt:return 50
    if dt<now:return 0
    days=(dt-now).total_seconds()/86400
    if days<1:return 20
    if days<3:return 45
    if days<7:return 65
    if days<14:return 78
    if days<30:return 90
    return 100


def completeness(item):
    fields=("job_title","company","location","job_type","education","experience","salary","vacancies","application_method","deadline")
    present=sum(bool(safe_text(item.get(x))) for x in fields)
    return round(100*present/len(fields))


def job_quality(item,now):
    source=source_reliability(item); fresh=freshness_score(item,now); deadline=deadline_score(item,now); comp=completeness(item)
    identity=min(100,int(item.get("identity_confidence",0))); audience=int(item.get("audience_score",0)); bd=int(item.get("bangladesh_relevance",0))
    apply=100 if item.get("apply_url") else 60
    score=round(0.18*source+0.10*fresh+0.10*deadline+0.10*comp+0.15*identity+0.24*audience+0.08*bd+0.05*apply)
    return score,{"source":source,"freshness":fresh,"deadline":deadline,"completeness":comp,"identity":identity,"audience":audience,"bangladesh":bd,"application":apply}


def local_rank(records,now):
    rows=[]
    for item in records:
        score,components=job_quality(item,now)
        row=dict(item); row.update({"local_score":score,"quality_components":components})
        rows.append(row)
    rows.sort(key=lambda x:(-x["local_score"],-int(x.get("audience_score",0)),-freshness_score(x,now)))
    return rows


def ai_rank(client, records, now):
    if not records or client is None:return records
    candidates=records[:MAX_AI_RANK_CANDIDATES]
    schema={"type":"object","properties":{"ranked":{"type":"array","items":{"type":"object","properties":{"id":{"type":"integer"},"score":{"type":"integer","minimum":0,"maximum":100},"relevance":{"type":"integer","minimum":0,"maximum":100},"reason":{"type":"string"}},"required":["id","score","relevance","reason"],"additionalProperties":False}}},"required":["ranked"],"additionalProperties":False}
    rows=[]
    for i,r in enumerate(candidates,1):
        rows.append(f"ID {i}\nTitle: {r.get('job_title','')}\nCompany: {r.get('company','')}\nLocation: {r.get('location','')}\nEducation: {r.get('education','')}\nExperience: {r.get('experience','')}\nType: {r.get('job_type','')}\nDeadline: {r.get('deadline','')}\nSalary: {r.get('salary','')}\nVacancies: {r.get('vacancies','')}\nApplication: {r.get('application_method','')}\nLocal score: {r.get('local_score',0)}\nAudience score: {r.get('audience_score',0)}")
    prompt="""Rank these already-verified Bangladesh vacancies for a 20–30 early-career audience. Prioritize BBA/MBA, management trainee, graduate trainee, internship, finance, accounting, banking, marketing, sales, HR, business development, operations, supply chain, commercial, relationship and analyst roles. Prefer legitimate source quality and complete application information. Do not invent or change facts. Score usefulness, not prestige or employer popularity."""
    try:
        response=client.chat.completions.create(model=CEREBRAS_MODEL,messages=[{"role":"system","content":prompt},{"role":"user","content":"\n\n".join(rows)}],response_format={"type":"json_schema","json_schema":{"name":"career_rank","strict":True,"schema":schema}},reasoning_effort="low",temperature=0,max_completion_tokens=2200)
        data=json.loads(safe_text(response.choices[0].message.content))
    except Exception as exc:
        logger.warning("AI ranking unavailable: %s",exc); return records
    for row in data.get("ranked",[]):
        idx=int(row.get("id",0));
        if 1<=idx<=len(candidates):
            candidates[idx-1]["ai_score"]=int(row.get("score",candidates[idx-1].get("local_score",0))); candidates[idx-1]["ai_relevance"]=int(row.get("relevance",0)); candidates[idx-1]["ai_reason"]=safe_text(row.get("reason"))
    for r in candidates:
        r["publish_score"]=round(0.70*int(r.get("ai_score",r.get("local_score",0)))+0.30*int(r.get("local_score",0)))
    candidates.sort(key=lambda x:(-x.get("publish_score",x.get("local_score",0)),-x.get("audience_score",0)))
    return candidates+records[len(candidates):]


def event_key(item):
    return normalize_title(f"{item.get('company','')} {item.get('job_title','')} {item.get('location','')}")


def event_dedup(records):
    clusters=[]
    for item in records:
        key=event_key(item); item["event_key"]=key
        placed=False
        for cluster in clusters:
            rep=cluster[0]
            if key and key==rep.get("event_key"):
                cluster.append(item); placed=True; break
            if title_similarity(item.get("job_title",""),rep.get("job_title",""))>=0.90 and safe_text(item.get("company")).lower()==safe_text(rep.get("company")).lower():
                cluster.append(item); placed=True; break
        if not placed: clusters.append([item])
    winners=[]
    for cluster in clusters:
        cluster.sort(key=lambda x:(-int(x.get("publish_score",x.get("local_score",0))),-source_reliability(x)))
        winner=dict(cluster[0]); winner["event_cluster_size"]=len(cluster); winner["event_sources"]=sorted({safe_text(x.get("source")) for x in cluster if safe_text(x.get("source"))}); winner["event_source_count"]=len(winner["event_sources"])
        winners.append(winner)
    return sorted(winners,key=lambda x:-int(x.get("publish_score",x.get("local_score",0))))


def select_for_publish(records, minimum=5, maximum=15):
    selected=[]; deferred=[]; counts={}
    distinct=len({safe_text(x.get("source")) for x in records})
    cap=distinct>=SOURCE_DIVERSITY_TARGET
    for item in records:
        score=int(item.get("publish_score",item.get("local_score",0)))
        if score < MIN_FINAL_QUALITY_SCORE:
            continue
        src=safe_text(item.get("source")) or "Unknown"
        if cap and counts.get(src,0)>=MAX_SOURCE_POSTS_PER_RUN:
            deferred.append(item); continue
        selected.append(item); counts[src]=counts.get(src,0)+1
        if len(selected)>=maximum: break
    if len(selected)<minimum:
        for item in deferred:
            score=int(item.get("publish_score",item.get("local_score",0)))
            if score < MIN_FINAL_QUALITY_SCORE: continue
            selected.append(item)
            if len(selected)>=minimum: break
    return selected[:maximum]
