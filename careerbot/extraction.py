from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .config import APPLICATION_TERMS, BAD_CONTENT_TERMS, BD_TZ, CEREBRAS_MODEL, DEADLINE_TERMS, NON_TARGET_TECH_TERMS, SENIORITY_TERMS, TARGET_ROLE_TERMS, EARLY_CAREER_TERMS
from .retrieval import RetrievalResult, RetrievalRouter
from .utils import canonical_url, clean_text, normalize_domain, normalize_title, parse_datetime, safe_text, short_hash

logger = logging.getLogger("career-news-bot")

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

JOBPOSTING_TYPE = {"jobposting", "job posting"}


def _first(*values):
    for value in values:
        if safe_text(value): return safe_text(value)
    return ""


def _flatten_jsonld(value):
    if isinstance(value, list):
        for x in value: yield from _flatten_jsonld(x)
    elif isinstance(value, dict):
        yield value
        graph=value.get("@graph")
        if graph: yield from _flatten_jsonld(graph)


def extract_jsonld_jobposting(html: str) -> dict:
    soup=BeautifulSoup(html or "","html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        raw=script.string or script.get_text(" ",strip=True)
        try: data=json.loads(raw)
        except Exception: continue
        for obj in _flatten_jsonld(data):
            types=obj.get("@type",[])
            if isinstance(types,str): types=[types]
            if not any(safe_text(t).lower() in JOBPOSTING_TYPE for t in types): continue
            org=obj.get("hiringOrganization") or {}
            loc=obj.get("jobLocation") or obj.get("applicantLocationRequirements") or {}
            if isinstance(loc,list): loc=loc[0] if loc else {}
            address=(loc.get("address") if isinstance(loc,dict) else {}) or {}
            salary=obj.get("baseSalary") or {}
            if isinstance(salary,list): salary=salary[0] if salary else {}
            value=salary.get("value") if isinstance(salary,dict) else ""
            if isinstance(value,dict):
                value=_first(value.get("value"), value.get("minValue") and value.get("maxValue") and f"{value.get('minValue')}-{value.get('maxValue')}", value.get("minValue"), value.get("maxValue"))
            if value:
                salary_text=f"{_first(salary.get('currency'), '')} {value}".strip() if isinstance(salary,dict) else safe_text(value)
            else:
                salary_text=""
            return {
                "job_title": _first(obj.get("title"), obj.get("name")),
                "company": _first(org.get("name") if isinstance(org,dict) else org),
                "location": _first(address.get("streetAddress"), address.get("addressLocality"), address.get("addressRegion"), loc.get("name") if isinstance(loc,dict) else loc),
                "employment_type": _first(obj.get("employmentType")),
                "posted_at": _first(obj.get("datePosted")),
                "deadline": _first(obj.get("validThrough")),
                "salary": salary_text,
                "description": _first(obj.get("description")),
            }
    return {}


def _date_from_text(raw: str, now: datetime):
    raw=safe_text(raw).strip()
    for pattern in (
        r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b",
        r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})\b",
        r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b",
    ):
        m=re.search(pattern,raw,re.I)
        if not m: continue
        a,b,c=m.groups()
        try:
            if a.isdigit() and b.isdigit():
                return datetime(int(c),int(b),int(a),tzinfo=BD_TZ)
            if a.isdigit():
                return datetime(int(c),MONTHS[b.lower()],int(a),tzinfo=BD_TZ)
            return datetime(int(c),MONTHS[a.lower()],int(b),tzinfo=BD_TZ)
        except Exception: continue
    return parse_datetime(raw)


def extract_deadline(text: str, now: datetime):
    text=clean_text(text)
    date_patterns=[
        r"\b(?:0?[1-9]|[12]\d|3[01])[\s./-](?:0?[1-9]|1[0-2])[\s./-]20\d{2}\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}\b",
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+20\d{2}\b",
    ]
    marker=re.compile("|".join(re.escape(x) for x in DEADLINE_TERMS),re.I)
    candidates=[]
    for pat in date_patterns:
        for m in re.finditer(pat,text,re.I):
            dt=_date_from_text(m.group(0),now)
            if not dt: continue
            context=text[max(0,m.start()-180):min(len(text),m.end()+180)]
            score=2 if marker.search(context) else 0
            candidates.append((score,dt,m.group(0),context))
    candidates.sort(key=lambda x:(-x[0],x[1]))
    return candidates[0] if candidates else None


def extract_label_value(text: str, labels: list[str], limit=220) -> str:
    label=re.compile(r"(?:"+"|".join(re.escape(x) for x in labels)+r")\s*[:：-]?\s*([^|\n]{1,"+str(limit)+r"})",re.I)
    m=label.search(text)
    return clean_text(m.group(1)) if m else ""


def collect_apply_links(base_url: str, html: str, content: str = "") -> list[dict]:
    rows=[]; seen=set()
    soup=BeautifulSoup(html or "","html.parser")
    for a in soup.find_all("a",href=True):
        href=urljoin(base_url,safe_text(a.get("href")))
        if not href.startswith(("http://","https://")): continue
        if canonical_url(href)==canonical_url(base_url): continue
        label=_first(a.get_text(" ",strip=True),a.get("aria-label"),a.get("title"))
        context=safe_text(a.parent.get_text(" ",strip=True))[:260] if getattr(a,"parent",None) else ""
        key=canonical_url(href)
        if not key or key in seen: continue
        seen.add(key)
        blob=(label+" "+context+" "+href).lower()
        score=0
        for i,t in enumerate(APPLICATION_TERMS):
            if t in blob: score=max(score,100-i)
        if any(x in blob for x in ("download", "circular", ".pdf")): score-=15
        rows.append({"url":href,"label":label[:180],"kind":"anchor","context":context,"deterministic_score":score})
    md=re.findall(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)",content or "")
    for label,href in md:
        key=canonical_url(href)
        if not key or key in seen or key==canonical_url(base_url): continue
        seen.add(key)
        blob=(label+" "+href).lower()
        score=max([100-i for i,t in enumerate(APPLICATION_TERMS) if t in blob] or [0])
        rows.append({"url":href,"label":label[:180],"kind":"markdown","context":f"Markdown link: {label}","deterministic_score":score})
    rows.sort(key=lambda x:(-x["deterministic_score"],len(x["url"])))
    return rows[:40]


def classify_apply_url(candidates: list[dict], source_url: str) -> tuple[str,float]:
    candidates=[x for x in (candidates or []) if safe_text(x.get("url")) and canonical_url(x.get("url"))!=canonical_url(source_url)]
    if not candidates: return "",0.0
    best=max(candidates,key=lambda x:x.get("deterministic_score",0))
    score=float(best.get("deterministic_score",0))
    if score>=85: return best["url"], min(1.0,score/100.0)
    if score>=60: return best["url"], min(0.89,score/100.0)
    return "",0.0


def collect_image_candidates(html: str, base_url: str) -> list[str]:
    """Collect likely source/article image URLs without making image retrieval a hard gate."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    seen = set()

    def add(value):
        value = safe_text(value)
        if not value or value.startswith(("data:", "javascript:", "blob:")):
            return
        url = urljoin(base_url, value)
        if not url.startswith(("http://", "https://")):
            return
        can = canonical_url(url)
        if not can or can in seen:
            return
        seen.add(can)
        rows.append(url)

    for selector in (
        'meta[property="og:image"]',
        'meta[property="og:image:url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ):
        tag = soup.select_one(selector)
        if tag:
            add(tag.get("content"))

    try:
        data = extract_jsonld_jobposting(html)
        # extract_jsonld_jobposting intentionally returns a compact record, so JSON-LD
        # image extraction is handled separately below from all JSON-LD objects.
    except Exception:
        pass

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _flatten_jsonld(data):
            image = obj.get("image") if isinstance(obj, dict) else None
            if isinstance(image, dict):
                add(image.get("url") or image.get("contentUrl"))
            elif isinstance(image, list):
                for item in image[:5]:
                    add(item.get("url") or item.get("contentUrl") if isinstance(item, dict) else item)
            else:
                add(image)

    for img in soup.find_all("img", src=True)[:30]:
        add(img.get("src"))
        add(img.get("data-src"))
        add(img.get("data-lazy-src"))

    return rows[:12]


def deterministic_job_fields(source_title: str, source_url: str, text: str, html: str, now: datetime) -> dict:
    schema=extract_jsonld_jobposting(html)
    clean=clean_text(text)
    deadline_info=extract_deadline(clean,now)
    location=_first(schema.get("location"), extract_label_value(clean,["Location","Job Location","কর্মস্থল"]))
    education=extract_label_value(clean,["Education","Educational Qualification","Qualification","শিক্ষাগত যোগ্যতা"],260)
    experience=extract_label_value(clean,["Experience","Work Experience","অভিজ্ঞতা"],220)
    salary=_first(schema.get("salary"), extract_label_value(clean,["Salary","Compensation","Remuneration","বেতন"],160))
    vacancies=extract_label_value(clean,["No. of Vacancies","Vacancies","Number of Vacancy","Vacancy","শূন্যপদ"],80)
    fee=extract_label_value(clean,["Application Fee","Fee","আবেদন ফি"],100)
    method=extract_label_value(clean,["How to Apply","Application Method","Apply","আবেদনের নিয়ম","আবেদন পদ্ধতি"],240)
    period=extract_label_value(clean,["Application Period","Application Time","আবেদনের সময়"],180)
    age=extract_label_value(clean,["Age Limit","Age","বয়সসীমা"],100)
    selection=extract_label_value(clean,["Selection Process","Selection Procedure","নিয়োগ প্রক্রিয়া","বাছাই প্রক্রিয়া"],220)
    posted_label=extract_label_value(clean,["Published","Published Date","Posted","Posted Date","Date Posted","প্রকাশের তারিখ","প্রকাশিত"],80)
    deadline=_first(schema.get("deadline"))
    deadline_dt=_date_from_text(deadline,now) if deadline else None
    if not deadline_dt and deadline_info: deadline_dt=deadline_info[1]; deadline=deadline_info[2]
    job_type=_first(schema.get("employment_type"), extract_label_value(clean,["Employment Type","Job Type","Type","চাকরির ধরন"],100))
    company=_first(schema.get("company"), extract_label_value(clean,["Company","Organization","Employer","প্রতিষ্ঠান","নিয়োগকারী"],160))
    title=_first(schema.get("job_title"), source_title)
    posted_dt=_date_from_text(schema.get("posted_at"),now) if schema.get("posted_at") else (_date_from_text(posted_label,now) if posted_label else None)
    return {
        "job_title":title,"company":company,"location":location,"job_type":job_type,
        "employment_type":schema.get("employment_type","") or job_type,"education":education,"experience":experience,
        "salary":salary,"vacancies":vacancies,"age_limit":age,"application_fee":fee,"application_method":method,
        "application_period":period,"selection_process":selection,"deadline":deadline or (deadline_dt.strftime("%d %B %Y") if deadline_dt else ""),
        "deadline_dt":deadline_dt,"posted_dt":posted_dt,"description":schema.get("description","")[:3000],
        "image_candidates": collect_image_candidates(html, source_url),
    }


def audience_fit(record: dict, text: str) -> tuple[int,list[str]]:
    blob=clean_text(" ".join(safe_text(record.get(k)) for k in ("job_title","education","experience","job_type","description"))+" "+text[:5000]).lower()
    title=clean_text(record.get("job_title")).lower()
    score=35; reasons=[]
    for term,weight in TARGET_ROLE_TERMS.items():
        if term in blob:
            score+=weight
            if weight>=9: reasons.append(term)
    early=sum(1 for x in EARLY_CAREER_TERMS if x in blob)
    senior=sum(1 for x in SENIORITY_TERMS if x in blob or x in title)
    score+=min(20,early*6)
    score-=min(35,senior*8)
    if any(x in title for x in ("management trainee","graduate trainee","intern","internship","business analyst","financial analyst","credit analyst")):
        score+=12
    non_target=sum(1 for x in NON_TARGET_TECH_TERMS if x in title)
    if non_target and not any(t in blob for t in ("finance","bank","marketing","hr","business","management","accounting","sales")):
        score-=30
    return max(0,min(100,score)),reasons[:6]


def is_real_vacancy(record: dict, text: str) -> tuple[bool,list[str]]:
    reasons=[]; blob=(record.get("job_title","")+" "+record.get("company","")+" "+text[:7000]).lower()
    positives=sum(1 for x in ("vacancy","hiring","recruitment","apply","application","position","responsibilities","requirements","deadline") if x in blob)
    negatives=sum(1 for x in BAD_CONTENT_TERMS if x in blob)
    if not record.get("job_title"): reasons.append("missing title")
    if not record.get("company"): reasons.append("missing employer")
    if positives < 3: reasons.append("insufficient vacancy evidence")
    if negatives >= 2 and positives < 6: reasons.append("career-content rather than vacancy")
    return not reasons,reasons



JOB_FIELDS=(
    "job_title","company","location","job_type","education","experience","salary","vacancies",
    "age_limit","application_fee","application_method","application_period","selection_process","deadline",
)


def _ai_job_schema():
    props={k:{"type":"string"} for k in JOB_FIELDS}
    props.update({"apply_url":{"type":"string"},"confidence":{"type":"integer","minimum":0,"maximum":100},"bangladesh_relevance":{"type":"integer","minimum":0,"maximum":100}})
    return {"type":"object","properties":props,"required":list(props),"additionalProperties":False}


def batch_extract_with_ai(client, entries: list[dict], batch_size: int = 5) -> dict[int, dict]:
    """Extract multiple vacancies in a bounded number of Cerebras calls."""
    if client is None or not entries:
        return {}
    output={}
    job_schema=_ai_job_schema()
    item_props={"id":{"type":"integer"},**job_schema["properties"]}
    item_required=["id"]+list(job_schema["required"])
    schema={"type":"object","properties":{"jobs":{"type":"array","items":{"type":"object","properties":item_props,"required":item_required,"additionalProperties":False},"maxItems":batch_size}},"required":["jobs"],"additionalProperties":False}
    system="""You are a strict vacancy data extractor for a Bangladesh job-news channel. Extract exactly one vacancy per input ID. Source page content is authoritative. Never invent facts, merge vacancies, infer eligibility, or change identity. Missing fields must be empty strings. apply_url MUST be exactly one of the application-link candidates supplied for that ID, or empty. Never use source_url as apply_url. Prefer actual submission/form URLs, including external application domains. Return only the required JSON schema."""
    for start in range(0,len(entries),batch_size):
        batch=entries[start:start+batch_size]
        chunks=[]
        for local_id,e in enumerate(batch,1):
            det=e["deterministic"]
            links="\n".join(f"{i+1}. {x['url']} | label={x.get('label','')} | score={x.get('deterministic_score',0)}" for i,x in enumerate(e.get("apply_candidates",[])[:25])) or "(none)"
            chunks.append(f"ID: {local_id}\nSOURCE: {e['candidate'].get('source','')}\nSOURCE TITLE: {e['candidate'].get('title','')}\nSOURCE URL: {e['candidate'].get('url','')}\nDETERMINISTIC: {json.dumps(det,ensure_ascii=False,default=str)}\nAPPLICATION LINKS:\n{links}\nPAGE TEXT:\n{e['retrieval'].content[:9000]}")
        try:
            response=client.chat.completions.create(model=CEREBRAS_MODEL,messages=[{"role":"system","content":system},{"role":"user","content":"\n\n===== VACANCY =====\n\n".join(chunks)}],response_format={"type":"json_schema","json_schema":{"name":f"career_job_batch_{start//batch_size+1}","strict":True,"schema":schema}},reasoning_effort="low",temperature=0,max_completion_tokens=5000)
            data=json.loads(safe_text(response.choices[0].message.content))
            for row in data.get("jobs",[]):
                rid=int(row.get("id",0))
                if 1<=rid<=len(batch):output[start+rid-1]=row
        except Exception as exc:
            logger.warning("AI extraction batch %d failed: %s",start//batch_size+1,exc)
    return output

def extract_with_ai(client, source_title: str, source_url: str, page_text: str, deterministic: dict, candidates: list[dict]) -> dict | None:
    if client is None: return None
    schema={"type":"object","properties":{
        "job_title":{"type":"string"},"company":{"type":"string"},"location":{"type":"string"},"job_type":{"type":"string"},
        "education":{"type":"string"},"experience":{"type":"string"},"salary":{"type":"string"},"vacancies":{"type":"string"},"age_limit":{"type":"string"},
        "application_fee":{"type":"string"},"application_method":{"type":"string"},"application_period":{"type":"string"},"selection_process":{"type":"string"},"deadline":{"type":"string"},"apply_url":{"type":"string"},
        "confidence":{"type":"integer","minimum":0,"maximum":100},"bangladesh_relevance":{"type":"integer","minimum":0,"maximum":100},
    },"required":["job_title","company","location","job_type","education","experience","salary","vacancies","age_limit","application_fee","application_method","application_period","selection_process","deadline","apply_url","confidence","bangladesh_relevance"],"additionalProperties":False}
    links="\n".join(f"{i+1}. {x['url']} | label={x.get('label','')} | score={x.get('deterministic_score',0)}" for i,x in enumerate(candidates[:25])) or "(none)"
    sys="""Extract exactly one real job vacancy from the supplied source page. Source identity is authoritative. Never invent or infer facts. Missing facts must be empty strings. Never fabricate an application URL. apply_url must be exactly one of the supplied application link candidates, or empty. Do not convert source_url into apply_url. Prefer direct application/submission endpoints over detail pages. Return only JSON matching the schema."""
    user=(f"SOURCE TITLE: {source_title}\nSOURCE URL: {source_url}\n\nDETERMINISTIC FIELDS:\n{json.dumps(deterministic,ensure_ascii=False,default=str)}\n\nAPPLICATION LINK CANDIDATES:\n{links}\n\nPAGE TEXT:\n{page_text[:18000]}")
    try:
        response=client.chat.completions.create(model=CEREBRAS_MODEL,messages=[{"role":"system","content":sys},{"role":"user","content":user}],response_format={"type":"json_schema","json_schema":{"name":"career_job_record","strict":True,"schema":schema}},reasoning_effort="low",temperature=0,max_completion_tokens=1900)
        return json.loads(safe_text(response.choices[0].message.content))
    except Exception as exc:
        logger.warning("AI extraction failed: %s",exc); return None


def lock_record(candidate: dict, retrieval: RetrievalResult, ai_client=None, now=None) -> dict | None:
    now=now or datetime.now(BD_TZ)
    text=safe_text(retrieval.content)
    if not text: return None
    deterministic=deterministic_job_fields(candidate.get("title","") or safe_text(retrieval.metadata.get("title")),candidate["url"],text,retrieval.html,now)
    apply_candidates=collect_apply_links(retrieval.final_url or candidate["url"],retrieval.html,text)
    deterministic_apply,det_apply_conf=classify_apply_url(apply_candidates,candidate["url"])
    ai=extract_with_ai(ai_client,candidate.get("title","") ,candidate["url"],text,deterministic,apply_candidates)
    merged=dict(deterministic)
    if ai:
        # AI may normalize, but deterministic/source-derived identity and timestamps win unless absent.
        for key in ("job_title","company","location","job_type","education","experience","salary","vacancies","age_limit","application_fee","application_method","application_period","selection_process","deadline"):
            value=safe_text(ai.get(key))
            if value and not deterministic.get(key): merged[key]=value
            elif value and key in {"job_title","job_type"} and deterministic.get(key)=="": merged[key]=value
        ai_apply=safe_text(ai.get("apply_url"))
        allowed={canonical_url(x["url"]):x["url"] for x in apply_candidates}
        if canonical_url(ai_apply) in allowed: deterministic_apply=allowed[canonical_url(ai_apply)]; det_apply_conf=0.96
        merged["confidence"]=int(ai.get("confidence",0)); merged["bangladesh_relevance"]=int(ai.get("bangladesh_relevance",0))
    else:
        merged["confidence"]=85 if deterministic.get("company") and deterministic.get("job_title") else 65
        merged["bangladesh_relevance"]=80 if any(x in (text+candidate.get("title","")).lower() for x in ("bangladesh","dhaka","chattogram","chittagong","barishal","rajshahi","khulna","sylhet","rangpur","mymensingh")) else 55
    okay,reasons=is_real_vacancy(merged,text)
    if not okay: return None
    aud,aud_reasons=audience_fit(merged,text)
    deadline_dt=merged.get("deadline_dt") or (_date_from_text(merged.get("deadline",""),now) if merged.get("deadline") else None)
    if deadline_dt and deadline_dt < now.replace(hour=23,minute=59,second=59,microsecond=0): return None
    published_dt=merged.get("posted_dt") or parse_datetime(candidate.get("published_date")) or now
    return {
        **candidate,
        **{k:v for k,v in merged.items() if k not in {"deadline_dt","posted_dt","description"}},
        "source_url":candidate["url"],"apply_url":deterministic_apply,"apply_confidence":det_apply_conf,
        "apply_link_candidates":apply_candidates,"retrieval_backend":retrieval.backend,"retrieval_quality":retrieval.quality,
        "content_hash":short_hash(text),"source_text_evidence":text[:22000],"source_description":merged.get("description","")[:2500],
        "deadline_iso":deadline_dt.isoformat() if deadline_dt else "","posted_date":published_dt.isoformat() if published_dt else candidate.get("published_date","") ,
        "audience_score":aud,"audience_reasons":aud_reasons,"identity_confidence":min(100,int(merged.get("confidence",0))),
        "bangladesh_relevance":min(100,int(merged.get("bangladesh_relevance",0))),
    }
