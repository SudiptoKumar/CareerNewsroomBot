from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class JobRecord:
    source: str
    source_job_id: str
    title: str
    company: str
    source_url: str
    canonical_url: str
    discovery_url: str = ""
    source_category: str = ""
    source_category_url: str = ""
    is_internship: bool = False
    is_government: bool = False

    location: str = ""
    salary: str = ""
    vacancy: str = ""
    age: str = ""
    experience: str = ""
    education: str = ""
    job_type: str = ""
    job_shift: str = ""
    workplace: str = ""
    category: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    company_industry: str = ""
    company_address: str = ""
    company_website: str = ""
    company_profile_url: str = ""

    published_date: str = ""
    deadline: str = ""
    deadline_status: str = "unknown"
    age_days: int | None = None
    days_to_deadline: int | None = None

    apply_url: str = ""
    application_method: str = ""
    raw_text: str = ""
    raw_listing: dict[str, Any] = field(default_factory=dict)
    extraction_status: str = ""
    detail_backend: str = ""

    eligibility: dict[str, Any] = field(default_factory=dict)
    ai: dict[str, Any] = field(default_factory=dict)
    publication: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def job_from_dict(data: dict[str, Any]) -> JobRecord:
    allowed = set(JobRecord.__dataclass_fields__)
    return JobRecord(**{k: v for k, v in data.items() if k in allowed})
