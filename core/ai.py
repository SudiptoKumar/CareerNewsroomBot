from __future__ import annotations

import json
import logging
import requests

from .config import Config
from .models import JobRecord

logger = logging.getLogger("career-news-v2.ai")

class AIJudge:
    def __init__(self, config: Config):
        self.config = config
        self.api_key = __import__("os").getenv("CEREBRAS_API_KEY", "").strip()
        self.url = "https://api.cerebras.ai/v1/chat/completions"
        self.available = bool(self.api_key)
        self.reason = "configured" if self.available else "missing_api_key"

    def judge(self, jobs: list[JobRecord]) -> list[JobRecord]:
        if not jobs:
            return []
        if not self.available:
            self.reason = "ai_unavailable"
            return [self._fallback(j) for j in jobs]
        result: list[JobRecord] = []
        for start in range(0, len(jobs), self.config.ai_batch_size):
            batch = jobs[start:start + self.config.ai_batch_size]
            try:
                judged = self._call(batch)
                by_id = {str(x.get("source_job_id")): x for x in judged if isinstance(x, dict)}
                for job in batch:
                    row = by_id.get(str(job.source_job_id), {})
                    job.ai = {
                        "available": True,
                        "publish": bool(row.get("publish", True)),
                        "score": float(row.get("score", 50)),
                        "reason": str(row.get("reason", "AI classification completed."))[:500],
                        "category": str(row.get("category", job.category or "Business")),
                    }
                    result.append(job)
            except Exception as exc:
                logger.warning("AI batch failed; deterministic fallback used | error=%s", exc)
                self.available = False
                self.reason = str(exc)[:200]
                for job in batch:
                    result.append(self._fallback(job))
        return result

    def _call(self, jobs: list[JobRecord]) -> list[dict]:
        payload_jobs = []
        for j in jobs:
            payload_jobs.append({
                "source_job_id": j.source_job_id,
                "title": j.title,
                "company": j.company,
                "published_date": j.published_date,
                "deadline": j.deadline,
                "education": j.education,
                "experience": j.experience,
                "age": j.age,
                "location": j.location,
                "salary": j.salary,
                "category": j.category,
                "responsibilities": j.responsibilities[:12],
                "skills": j.skills[:12],
            })
        system = (
            "You are the final interpretation layer of CareerNewsroom V2. "
            "All jobs in this payload have already passed deterministic source, identity, freshness, deadline, "
            "experience, age, duplicate, business-relevance and vacancy gates. "
            "Do not override those facts. Return ONLY JSON array. For each job include source_job_id, publish, score 0-100, category, reason."
        )
        user = json.dumps(payload_jobs, ensure_ascii=False)
        response = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.config.cerebras_model, "temperature": 0, "messages":[{"role":"system","content":system},{"role":"user","content":user}]},
            timeout=self.config.cerebras_timeout,
        )
        if response.status_code in {402, 429}:
            raise RuntimeError(f"Cerebras {response.status_code}: {response.text[:180]}")
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if "```" in content:
            content = content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            raise ValueError("AI response was not a JSON list")
        return parsed

    @staticmethod
    def _fallback(job: JobRecord) -> JobRecord:
        from .rules import deterministic_score
        job.ai = {
            "available": False,
            "publish": True,
            "score": deterministic_score(job),
            "reason": "AI unavailable; deterministic source-first ranking used.",
            "category": job.category or "Business",
        }
        return job
