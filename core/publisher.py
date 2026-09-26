from __future__ import annotations

import html as html_lib
import json
import logging
import time
from typing import Any

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

    def _call(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
        last = {"ok": False, "description": "Unknown error"}
        for attempt in range(1, 4):
            try:
                r = requests.post(f"{self.base}/{method}", data=data, timeout=45)
                payload = r.json()
                if payload.get("ok"):
                    return payload
                last = payload
                if r.status_code == 429:
                    retry = int(payload.get("parameters", {}).get("retry_after", 2))
                    time.sleep(min(8, max(1, retry)))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 * attempt)
                    continue
                break
            except Exception as exc:
                last = {"ok": False, "description": str(exc)}
                if attempt < 3:
                    time.sleep(2 * attempt)
        return last

    @staticmethod
    def _display(value: object) -> str:
        return "" if value is None else str(value).strip()

    @staticmethod
    def _html(value: object) -> str:
        return html_lib.escape("" if value is None else str(value).strip())

    def _reply_markup(self, job: JobRecord, expired: bool = False) -> str:
        if expired:
            target = self.config.telegram_promo_url
            label = "EXPIRED DEADLINE"
        else:
            target = job.apply_url if str(job.apply_url).startswith(("http://", "https://")) else job.source_url
            label = "APPLY NOW"
        return json.dumps({"inline_keyboard": [[{"text": label, "url": target}]]}, ensure_ascii=False)

    def _snapshot_rows(self, job: JobRecord) -> list[tuple[str, str]]:
        mapping = [
            ("Location", job.location),
            ("Employment", job.job_type),
            ("Workplace", job.workplace),
            ("Education", job.education),
            ("Experience", job.experience),
            ("Salary", job.salary),
            ("Vacancy", job.vacancy),
            ("Age", job.age),
            ("Application", job.application_method),
            ("Deadline", job.deadline),
            ("Posted", job.published_date),
        ]
        return [(k, str(v).strip()) for k, v in mapping if str(v).strip()]

    def _rich_blocks(self, job: JobRecord, expired: bool = False) -> list[dict[str, Any]]:
        title = self._display(job.title)
        company = self._display(job.company or ("Government Organization" if job.is_government else ""))
        blocks: list[dict[str, Any]] = [
            {"type": "heading", "size": 1, "text": f"📣 {title}"},
        ]
        if company:
            blocks.append({"type": "paragraph", "text": {"type": "bold", "text": f"🏢 {company}"}})
        blocks.append({"type": "heading", "size": 2, "text": "JOB SNAPSHOT"})
        cells = [[
            {"text": "FIELD", "is_header": True, "align": "center", "valign": "middle"},
            {"text": "DETAILS", "is_header": True, "align": "center", "valign": "middle"},
        ]]
        icons = {"Location":"📍","Employment":"💼","Workplace":"🏢","Education":"🎓","Experience":"🧑‍💼","Salary":"💰","Vacancy":"👥","Age":"🎂","Application":"📝","Deadline":"📅","Posted":"🕒"}
        for label, value in self._snapshot_rows(job):
            cells.append([
                {"text": {"type": "bold", "text": f"{icons.get(label, '•')} {label}"}},
                {"text": self._display(value)},
            ])
        blocks.append({"type": "table", "cells": cells, "is_bordered": True, "is_striped": True, "is_compact": False})
        if expired:
            blocks.append({"type": "paragraph", "text": "EXPIRED DEADLINE"})
        else:
            blocks.append({"type": "paragraph", "text": "─────────────────────"})
            blocks.append({"type": "pullquote", "text": "Your next opportunity starts here. 💼"})
            blocks.append({"type": "paragraph", "text": "─────────────────────"})
        tags = ["#CareerNewsroom"]
        if job.is_government:
            tags.append("#GovtJob")
        if job.is_internship:
            tags.append("#Internship")
        text_blob = f"{job.title} {job.category} {job.raw_text}".lower()
        extras = [("#Finance", ("finance", "account", "audit", "tax")), ("#Marketing", ("marketing", "sales", "brand")), ("#Banking", ("bank", "credit", "branch")), ("#HR", ("human resource", "recruitment", "talent acquisition")), ("#SupplyChain", ("supply chain", "procurement", "logistics", "sourcing"))]
        for tag, terms in extras:
            if any(term in text_blob for term in terms):
                tags.append(tag)
        blocks.append({"type": "paragraph", "text": " ".join(dict.fromkeys(tags))})
        blocks.append({"type": "paragraph", "text": {"type": "bold", "text": {"type": "url", "text": "Career News", "url": "https://t.me/CareerNewsroom"}}})
        source_text = self._display(job.source)
        if expired:
            blocks.append({"type": "footer", "text": ["Source: ", source_text]})
        else:
            blocks.append({"type": "footer", "text": ["Source: ", {"type": "url", "text": source_text, "url": job.source_url}]})
        return blocks

    def _plain_text(self, job: JobRecord, expired: bool = False) -> str:
        lines = [f"📣 {self._html(job.title)}", f"🏢 {self._html(job.company)}", "", "JOB SNAPSHOT"]
        for label, value in self._snapshot_rows(job):
            lines.append(f"• {self._html(label)}: {self._html(value)}")
        if expired:
            lines.extend(["", "EXPIRED DEADLINE"])
        else:
            lines.extend(["", "#CareerNewsroom #Career", f"Source: {self._html(job.source)}"])
        return "\n".join(lines)[:3900]

    def publish(self, job: JobRecord) -> tuple[bool, int | None, str]:
        if not self.token:
            return False, None, "TELEGRAM_BOT_TOKEN missing"
        rich = self._call("sendRichMessage", {
            "chat_id": self.channel,
            "rich_message": json.dumps({"blocks": self._rich_blocks(job)}, ensure_ascii=False, separators=(",", ":")),
            "reply_markup": self._reply_markup(job),
        })
        if rich.get("ok"):
            return True, rich.get("result", {}).get("message_id"), ""

        text = self._call("sendMessage", {
            "chat_id": self.channel,
            "text": self._plain_text(job),
            "reply_markup": self._reply_markup(job),
        })
        if text.get("ok"):
            return True, text.get("result", {}).get("message_id"), f"rich_message_fallback:{rich.get('description', '')}"
        return False, None, str(text.get("description", rich.get("description", "Telegram publish failed")))

    def expire(self, event: dict[str, Any]) -> tuple[bool, str]:
        if not self.token or not event.get("message_id"):
            return False, "missing token/message_id"
        payload = {
            "source": event.get("source", ""),
            "source_job_id": event.get("source_job_id", ""),
            "title": event.get("title", "Job"),
            "company": event.get("company", ""),
            "source_url": "",
            "canonical_url": event.get("canonical_url", ""),
        }
        for key in JobRecord.__dataclass_fields__.keys():
            if key in payload:
                continue
            if key in event:
                payload[key] = event[key]
        job = JobRecord(**payload)
        job.source_url = ""
        job.apply_url = ""
        payload = {
            "chat_id": self.channel,
            "message_id": int(event["message_id"]),
            "rich_message": json.dumps({"blocks": self._rich_blocks(job, expired=True)}, ensure_ascii=False, separators=(",", ":")),
            "reply_markup": self._reply_markup(job, expired=True),
        }
        result = self._call("editMessageText", payload)
        if result.get("ok"):
            return True, ""
        fallback = self._call("editMessageReplyMarkup", {
            "chat_id": self.channel,
            "message_id": int(event["message_id"]),
            "reply_markup": self._reply_markup(job, expired=True),
        })
        if fallback.get("ok"):
            return True, "partial_button_update"
        return False, str(result.get("description", fallback.get("description", "Telegram expiry update failed")))
