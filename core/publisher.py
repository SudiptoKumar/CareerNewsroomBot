from __future__ import annotations

import html as html_lib
import logging
import time
import requests

from .config import Config
from .models import JobRecord

logger = logging.getLogger("career-news-v2.publisher")

class TelegramPublisher:
    def __init__(self, config: Config):
        self.config = config
        self.token = __import__("os").getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.channel = config.telegram_channel
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""

    def publish(self, job: JobRecord) -> tuple[bool, int | None, str]:
        if not self.token:
            return False, None, "TELEGRAM_BOT_TOKEN missing"
        text = self._html(job)
        reply = {"inline_keyboard": [[{"text": "APPLY NOW", "url": job.apply_url or job.source_url}]]}
        data = {"chat_id": self.channel, "text": text, "parse_mode": "HTML", "reply_markup": __import__("json").dumps(reply)}
        for attempt in range(1, 4):
            try:
                r = requests.post(f"{self.base}/sendMessage", data=data, timeout=45)
                payload = r.json()
                if payload.get("ok"):
                    return True, payload.get("result", {}).get("message_id"), ""
                if r.status_code == 429:
                    retry_after = int(payload.get("parameters", {}).get("retry_after", 2))
                    time.sleep(min(8, max(1, retry_after)))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 * attempt)
                    continue
                return False, None, str(payload.get("description", "Telegram API error"))
            except Exception as exc:
                if attempt == 3:
                    return False, None, str(exc)
                time.sleep(2 * attempt)
        return False, None, "Telegram publish failed"

    def _html(self, job: JobRecord) -> str:
        e = html_lib.escape
        rows = [
            ("Location", job.location),
            ("Employment", job.job_type),
            ("Education", job.education),
            ("Experience", job.experience),
            ("Salary", job.salary),
            ("Vacancy", job.vacancy),
            ("Age", job.age),
            ("Deadline", job.deadline),
            ("Published", job.published_date),
        ]
        rows = [(a, b) for a, b in rows if str(b).strip()]
        snapshot = "\n".join(f"<b>{e(a)}</b>: {e(str(b))}" for a, b in rows)
        source = e(job.source)
        title = e(job.title)
        company = e(job.company)
        hashtags = ["#CareerNewsroom"]
        if job.is_government: hashtags.append("#GovtJob")
        if job.is_internship: hashtags.append("#Internship")
        text = f"<b>{title}</b>\n\n🏢 <b>{company}</b>\n\n<b>JOB SNAPSHOT</b>\n{snapshot}\n\n"
        if job.responsibilities:
            text += "<b>KEY RESPONSIBILITIES</b>\n" + "\n".join(f"• {e(x)}" for x in job.responsibilities[:5]) + "\n\n"
        text += f"<b>Career News</b> · {' '.join(hashtags)}\nSource: <a href=\"{e(job.source_url)}\">{source}</a>"
        return text[:4000]

    def expire(self, event: dict) -> tuple[bool, str]:
        if not self.token or not event.get("message_id"):
            return False, "missing token/message_id"
        data = {
            "chat_id": self.channel,
            "message_id": int(event["message_id"]),
            "text": self._expired_text(event),
            "parse_mode": "HTML",
        }
        try:
            r = requests.post(f"{self.base}/editMessageText", data=data, timeout=45)
            p = r.json()
            if p.get("ok"):
                return True, ""
            return False, str(p.get("description", "Telegram edit failed"))
        except Exception as exc:
            return False, str(exc)

    def _expired_text(self, event: dict) -> str:
        from html import escape
        return f"<b>{escape(str(event.get('title','Job')))}</b>\n\n🏢 <b>{escape(str(event.get('company','')))}</b>\n\n<b>EXPIRED DEADLINE</b>\nApplication deadline: {escape(str(event.get('deadline','')))}"
