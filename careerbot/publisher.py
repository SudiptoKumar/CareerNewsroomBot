from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from io import BytesIO

import requests

from .config import BD_TZ, BOT_API_CAPTION_LIMIT, MAX_RICH_CHARACTERS, TELEGRAM_CHANNEL
from .extraction import classify_apply_url
from .utils import visible_text, safe_text, parse_datetime

logger=logging.getLogger("career-news-bot")

HEADERS={"User-Agent":"Mozilla/5.0 (compatible; CareerNewsBot/2.0)"}


def _display(value):
    value=safe_text(value)
    if not value or value.lower() in {"not specified","not available","unknown","n/a","na","not stated","not mentioned","-","--"}:
        return ""
    return value


def _sanitize_method(value):
    value=_display(value)
    if not value: return ""
    value=re.sub(r"https?://\S+|www\.\S+","",value,flags=re.I)
    return re.sub(r"\s+"," ",value).strip(" -–—:")


def render_table(story):
    fields=[
        ("Location",story.get("location")),("Type",story.get("job_type")),("Education",story.get("education")),
        ("Experience",story.get("experience")),("Salary",story.get("salary")),("Vacancies",story.get("vacancies")),
        ("Age Limit",story.get("age_limit")),("Application Fee",story.get("application_fee")),
        ("Application",_sanitize_method(story.get("application_method"))),
        ("Application Period",story.get("application_period")),("Selection Process",story.get("selection_process")),
        ("Deadline",story.get("deadline")),
    ]
    rows=[]
    for label,value in fields:
        value=_display(value)
        if value: rows.append((label,value))
    parts=['<table bordered striped compact><tr><th><b>FIELD</b></th><th><b>DETAILS</b></th></tr>']
    for label,value in rows:
        parts.append(f"<tr><td>{html.escape(label)}</td><td>{html.escape(value,quote=False)}</td></tr>")
    parts.append('</table>')
    return ''.join(parts)


def action(story):
    source=safe_text(story.get("source_url") or story.get("url"))
    apply=safe_text(story.get("apply_url"))
    candidates=story.get("apply_link_candidates") or []
    valid,conf=classify_apply_url(candidates,source)
    if apply and any(safe_text(x.get("url"))==apply or safe_text(x.get("url")) and safe_text(x.get("url")).rstrip('/')==apply.rstrip('/') for x in candidates if isinstance(x,dict)):
        valid=apply
    if valid and conf>=0.60:
        return valid,"APPLY NOW"
    if source.startswith(("http://","https://")):
        return source,"READ MORE"
    return "",""


def render_rich(story):
    title=html.escape(safe_text(story.get("job_title") or story.get("headline") or "Job Vacancy"),quote=False)
    company=_display(story.get("company"))
    parts=[f"<h1>{title}</h1>"]
    if company: parts.append(f"<p><b>Company:</b> {html.escape(company,quote=False)}</p>")
    table=render_table(story)
    if table: parts += ["<h2>JOB SNAPSHOT</h2>",table]
    tags=story.get("hashtags") or []
    if tags: parts.append("<p>"+html.escape(" ".join(tags),quote=False)+"</p>")
    source=safe_text(story.get("source")) or "Official Source"
    source_url=safe_text(story.get("source_url") or story.get("url"))
    if source_url.startswith(("http://","https://")):
        parts.append(f'<p>Official Source: <a href="{html.escape(source_url,quote=True)}">{html.escape(source,quote=False)}</a></p>')
    else: parts.append(f"<p>Official Source: {html.escape(source,quote=False)}</p>")
    return "\n".join(parts)


def truncate_visible(text, limit):
    if len(text)<=limit:return text
    trimmed=text[:max(1,limit-3)].rsplit(" ",1)[0].rstrip()
    return trimmed+"..."


def _plain_fallback(rich_html):
    text=visible_text(rich_html)
    return truncate_visible(text,BOT_API_CAPTION_LIMIT)


def _telegram_request(method,data=None,files=None,token=None):
    token=token or os.getenv("TELEGRAM_BOT_TOKEN","")
    if not token:return {"ok":False,"description":"TELEGRAM_BOT_TOKEN missing"}
    url=f"https://api.telegram.org/bot{token}/{method}"
    last={"ok":False,"description":"unknown"}
    for attempt in range(1,5):
        try:
            r=requests.post(url,data=data or {},files=files,timeout=90)
            try: payload=r.json()
            except Exception: payload={"ok":False,"description":r.text[:400]}
            if payload.get("ok"):return payload
            last=payload
            if r.status_code==429:
                retry=int((payload.get("parameters") or {}).get("retry_after",5)); time.sleep(max(1,retry)); continue
            if r.status_code>=500:
                time.sleep(2*attempt); continue
            break
        except Exception as exc:
            last={"ok":False,"description":str(exc)}; time.sleep(min(2*attempt,10))
    return last


def _download_image(url,referer=""):
    if not url:return None
    try:
        r=requests.get(url,headers={**HEADERS,"Referer":referer},timeout=20)
        if r.status_code>=400:return None
        if r.headers.get("content-type","").lower() and not r.headers.get("content-type","").lower().startswith("image/"):return None
        from PIL import Image,ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES=True
        image=Image.open(BytesIO(r.content)).convert("RGB")
        return image
    except Exception:return None


def prepare_image(story,index):
    try:
        from PIL import Image,ImageDraw,ImageFont
    except Exception:
        return None
    candidates=[]
    for x in story.get("image_candidates",[]) or []:
        if x and x not in candidates:candidates.append(x)
    if story.get("image_url") and story["image_url"] not in candidates:candidates.append(story["image_url"])
    image=None
    for url in candidates:
        image=_download_image(url,story.get("source_url") or story.get("url") or "")
        if image:break
    if image:
        target=(1200,675)
        ratio=max(target[0]/image.width,target[1]/image.height)
        image=image.resize((int(image.width*ratio),int(image.height*ratio)),Image.Resampling.LANCZOS)
        left=(image.width-target[0])//2;top=(image.height-target[1])//2
        image=image.crop((left,top,left+target[0],top+target[1]))
    else:
        image=Image.new("RGB",(1200,675),(32,38,46))
        draw=ImageDraw.Draw(image)
        font=None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
            if os.path.exists(path): font=ImageFont.truetype(path,92); break
        text=safe_text(story.get("source")) or "CareerNewsroom"
        if font:
            bbox=draw.textbbox((0,0),text,font=font); x=(1200-(bbox[2]-bbox[0]))//2; y=(675-(bbox[3]-bbox[1]))//2-bbox[1]
            draw.text((x,y),text,font=font,fill=(245,245,245))
    draw=ImageDraw.Draw(image)
    font=None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
        if os.path.exists(path): font=ImageFont.truetype(path,22); break
    text="@CareerNewsroom"
    if font:
        bbox=draw.textbbox((0,0),text,font=font); pad=10; x=1200-(bbox[2]-bbox[0])-28-pad*2; y=675-(bbox[3]-bbox[1])-22-pad*2
        draw.rounded_rectangle((x,y,1200-28,675-22),radius=12,fill=(0,0,0))
        draw.text((x+pad,y+pad-2),text,font=font,fill=(255,255,255))
    path=f"/tmp/careernews_{index}.jpg"; image.save(path,"JPEG",quality=88,optimize=True); return path


def publish(story,index):
    rich=render_rich(story)
    if len(visible_text(rich))>MAX_RICH_CHARACTERS:
        rich=rich[:MAX_RICH_CHARACTERS]
    markup_url,label=action(story)
    markup={"inline_keyboard":[[{"text":label,"url":markup_url}]]} if markup_url else None
    image_path=prepare_image(story,index)
    data={"chat_id":TELEGRAM_CHANNEL,"rich_message":json.dumps({"html":rich,"skip_entity_detection":False,**({"media":[{"id":"jobphoto","media":{"type":"photo","media":"attach://photo"}}]} if image_path else {})},ensure_ascii=False)}
    if markup:data["reply_markup"]=json.dumps(markup,ensure_ascii=False)
    files={"photo":open(image_path,"rb")} if image_path else None
    try:
        result=_telegram_request("sendRichMessage",data=data,files=files)
    finally:
        if files: files["photo"].close()
    if result.get("ok"):return result
    # Bot API fallback. Photo captions are limited to 1024 characters after entities parsing.
    plain=_plain_fallback(rich)
    if image_path:
        f=open(image_path,"rb")
        try:
            result=_telegram_request("sendPhoto",data={"chat_id":TELEGRAM_CHANNEL,"caption":plain,**({"reply_markup":json.dumps(markup,ensure_ascii=False)} if markup else {})},files={"photo":f})
        finally:f.close()
    else:
        result=_telegram_request("sendMessage",data={"chat_id":TELEGRAM_CHANNEL,"text":truncate_visible(visible_text(rich),4096),**({"reply_markup":json.dumps(markup,ensure_ascii=False)} if markup else {})})
    return result
