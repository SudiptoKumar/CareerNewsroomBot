from __future__ import annotations

import json
import logging

from .config import CEREBRAS_MODEL, MAX_CEREBRAS_BATCH, CEREBRAS_TIMEOUT
from .utils import safe_text, clamp

logger = logging.getLogger("career-news-bot")

class JudgeUnavailable(RuntimeError):
    pass

SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "maxItems": MAX_CEREBRAS_BATCH,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "audience_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "early_career_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "factual_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "publish": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "risk_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                },
                "required": [
                    "id","quality_score","audience_score","early_career_fit",
                    "factual_confidence","publish","reason","risk_flags"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the strict editorial judge for CareerNewsroom.
The source is BDjobs.com only. Evaluate already-extracted vacancy records for an audience mainly aged 20-30, especially BBA/MBA students and graduates.

Judge the vacancy itself, not employer fame.
Prioritize:
1) direct BBA/MBA compatibility,
2) entry-level/fresher/0-3 years accessibility,
3) management trainee/graduate trainee/internship roles,
4) finance/accounting/banking/marketing/sales/HR/business/operations/supply-chain/commercial roles,
5) meaningful career relevance,
6) clear application information,
7) active deadline and usable information.

Do not invent, repair, or infer missing facts. Missing salary is not a defect by itself. Do not penalize a vacancy just because a field is absent.
Reject obvious non-vacancy pages, clearly unrelated technical/medical/manual roles, expired roles, or records with weak factual support.

Return one judgment per input ID. The publish field is a recommendation to the program, but the program still applies deterministic safety gates."""

def _client(api_key):
    if not api_key:
        return None
    try:
        from cerebras.cloud.sdk import Cerebras
        return Cerebras(api_key=api_key)
    except Exception as exc:
        logger.warning("Cerebras SDK unavailable: %s", exc)
        return None

def judge_batches(api_key: str, records: list[dict]) -> dict[str, dict]:
    client = _client(api_key)
    if client is None:
        return {}
    out = {}
    for start in range(0, len(records), MAX_CEREBRAS_BATCH):
        batch = records[start:start + MAX_CEREBRAS_BATCH]
        parts = []
        for i, r in enumerate(batch, 1):
            parts.append(
                f"""ID {i}
TITLE: {safe_text(r.get('job_title'))}
COMPANY: {safe_text(r.get('company'))}
LOCATION: {safe_text(r.get('location'))}
TYPE: {safe_text(r.get('job_type'))}
EDUCATION: {safe_text(r.get('education'))}
EXPERIENCE: {safe_text(r.get('experience'))}
SALARY: {safe_text(r.get('salary'))}
VACANCIES: {safe_text(r.get('vacancies'))}
APPLICATION: {safe_text(r.get('application_method'))}
DEADLINE: {safe_text(r.get('deadline'))}
POSTED DATE: {safe_text(r.get('posted_date'))}
LOCAL AUDIENCE SCORE: {r.get('local_audience_score',0)}
EVIDENCE:
{safe_text(r.get('source_text'))[:6500]}"""
            )
        try:
            response = client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n===== JOB =====\n\n".join(parts)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"career_job_judgment_{start // MAX_CEREBRAS_BATCH + 1}",
                        "strict": True,
                        "schema": SCHEMA,
                    },
                },
                reasoning_effort="low",
                temperature=0,
                max_completion_tokens=3500,
            )
            data = json.loads(safe_text(response.choices[0].message.content))
        except Exception as exc:
            logger.warning("CEREBRAS JUDGE BATCH %d FAILED: %s", start // MAX_CEREBRAS_BATCH + 1, exc)
            continue
        for row in data.get("jobs", []):
            idx = int(row.get("id", 0)) - 1
            if 0 <= idx < len(batch):
                key = safe_text(batch[idx].get("canonical") or batch[idx].get("source_url"))
                out[key] = row
    return out
