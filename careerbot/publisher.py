from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from io import BytesIO

import requests

from .config import TELEGRAM_CHANNEL
from .utils import safe_text, parse_datetime

logger = logging.getLogger("career-news-bot")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CareerNewsBot-BDjobs/3.0)"}

def display(value) -> str:
    x = safe_text(value)
    if not x or x.casefold() in {"not specified","not available","unknown","n/a","na","not stated","not mentioned","-","--"}:
        return ""
    return x

def sanitize_method(value):
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+|www\.\S+", "", display(value))).strip(" -:")

def render_table(story):
    fields = [
        ("Location", story.get("location")),
        ("Type", story.get("job_type")),
        ("Education", story.get("education")),
        ("Experience", story.get("experience")),
        ("Salary", story.get("salary")),
        ("Vacancies", story.get("vacancies")),
        ("Age Limit", story.get("age_limit")),
        ("Application Fee", story.get("application_fee")),
        ("Application", sanitize_method(story.get("application_method"))),
        ("Application Period", story.get("application_period")),
        ("Selection Process", story.get("selection_process")),
        ("Deadline", story.get("deadline")),
    ]
    rows = [(a, display(b)) for a, b in fields if display(b)]
    if not rows:
        return ""
    parts = ['<table bordered striped compact><tr><th><b>FIELD</b></th><th><b>DETAILS</b></th></tr>']
    for label, value in rows:
        parts.append(f"<tr><td>{html.escape(label)}</td><td>{html.escape(value, quote=False)}</td></tr>")
    parts.append("</table>")
    return "".join(parts)

def action(story):
    source = safe_text(story.get("source_url"))
    apply_url = safe_text(story.get("apply_url"))
    if apply_url.startswith(("http://","https://")) and apply_url != source:
        return apply_url, "APPLY NOW"
    if source.startswith(("http://","https://")):
        return source, "READ MORE"
    return "", ""

def render_rich(story):
    title = html.escape(safe_text(story.get("job_title") or "Job Vacancy"), quote=False)
    company = display(story.get("company"))
    parts = [f"<h1>{title}</h1>"]
    if company:
        parts.append(f"<p><b>Company:</b> {html.escape(company, quote=False)}</p>")
    table = render_table(story)
    if table:
        parts.extend(["<h2>JOB SNAPSHOT</h2>", table])
    tags = " ".join(story.get("hashtags") or [])
    if tags:
        parts.append(f"<p>{html.escape(tags, quote=False)}</p>")
    source = html.escape(safe_text(story.get("source") or "Bdjobs.com"), quote=False)
    source_url = safe_text(story.get("source_url"))
    if source_url.startswith(("http://","https://")):
        parts.append(f'<p>🔎 Official Source: <a href="{html.escape(source_url, quote=True)}">{source}</a></p>')
    return "\n".join(parts)

def _telegram_request(method, data, files=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    last = {"ok": False, "description": "unknown"}
    for attempt in range(1, 5):
        try:
            r = requests.post(url, data=data, files=files, timeout=90)
            try:
                payload = r.json()
            except Exception:
                payload = {"ok": False, "description": r.text[:300]}
            if payload.get("ok"):
                return payload
            last = payload
            if r.status_code == 429:
                retry_after = int((payload.get("parameters") or {}).get("retry_after", 3))
                time.sleep(max(1, retry_after))
                continue
            if r.status_code >= 500:
                time.sleep(min(2 * attempt, 8))
                continue
            break
        except requests.RequestException as exc:
            last = {"ok": False, "description": str(exc)}
            time.sleep(min(2 * attempt, 8))
    return last

def _download_image(url, referer):
    try:
        r = requests.get(url, headers={**HEADERS, "Referer": referer}, timeout=20)
        ct = safe_text(r.headers.get("content-type")).lower()
        if r.status_code >= 400 or (ct and not ct.startswith("image/")):
            return None
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        return None

def prepare_image(story, index):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    image = None
    for url in story.get("image_candidates") or []:
        image = _download_image(url, safe_text(story.get("source_url")))
        if image:
            break
    if image:
        target = (1200, 675)
        ratio = max(target[0] / image.width, target[1] / image.height)
        image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.Resampling.LANCZOS)
        left = (image.width - target[0]) // 2
        top = (image.height - target[1]) // 2
        image = image.crop((left, top, left + target[0], top + target[1]))
    else:
        image = Image.new("RGB", (1200, 675), (35, 35, 35))
        draw = ImageDraw.Draw(image)
        font = None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
            if os.path.exists(path):
                font = ImageFont.truetype(path, 90)
                break
        label = safe_text(story.get("company") or "Bdjobs.com")
        if font:
            box = draw.textbbox((0,0), label, font=font)
            draw.text(((1200-(box[2]-box[0]))//2, (675-(box[3]-box[1]))//2), label, font=font, fill=(245,245,245))
    # Small channel mark.
    draw = ImageDraw.Draw(image)
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if os.path.exists(path):
        font = ImageFont.truetype(path, 22)
        label = "@CareerNewsroom"
        box = draw.textbbox((0,0), label, font=font)
        x = 1200 - (box[2]-box[0]) - 34
        y = 675 - (box[3]-box[1]) - 26
        draw.rounded_rectangle((x-8,y-6,1200-22,675-18), radius=8, fill=(0,0,0))
        draw.text((x,y), label, font=font, fill=(255,255,255))
    path_out = f"/tmp/careernews_{index}.jpg"
    image.save(path_out, "JPEG", quality=88, optimize=True)
    return path_out

def publish(story, index):
    rich = render_rich(story)
    action_url, action_label = action(story)
    markup = {"inline_keyboard": [[{"text": action_label, "url": action_url}]]} if action_url else None
    image_path = prepare_image(story, index)
    media = [{"id":"jobphoto","media":{"type":"photo","media":"attach://photo"}}] if image_path else []
    rich_payload = {"html": rich, "skip_entity_detection": False}
    if media:
        rich_payload["media"] = media
    data = {"chat_id": TELEGRAM_CHANNEL, "rich_message": json.dumps(rich_payload, ensure_ascii=False)}
    if markup:
        data["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    files = {"photo": open(image_path, "rb")} if image_path else None
    try:
        result = _telegram_request("sendRichMessage", data, files)
    finally:
        if files:
            files["photo"].close()
    if result.get("ok"):
        return result
    # Bot API fallback.
    from bs4 import BeautifulSoup
    plain = BeautifulSoup(rich, "html.parser").get_text(" ", strip=True)
    plain = plain[:1021] + "..." if len(plain) > 1024 else plain
    if image_path:
        with open(image_path, "rb") as f:
            return _telegram_request(
                "sendPhoto",
                {"chat_id": TELEGRAM_CHANNEL, "caption": plain, **({"reply_markup": json.dumps(markup)} if markup else {})},
                {"photo": f},
            )
    return _telegram_request(
        "sendMessage",
        {"chat_id": TELEGRAM_CHANNEL, "text": plain[:4096], **({"reply_markup": json.dumps(markup)} if markup else {})},
    )
