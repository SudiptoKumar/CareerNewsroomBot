from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

BD_TZ = ZoneInfo("Asia/Dhaka")

BDJOBS_CATEGORIES = {
    1: {"name": "Accounting / Finance", "priority": 1},
    2: {"name": "Bank / Non-Bank Financial Institution", "priority": 1},
    3: {"name": "Commercial / Supply Chain", "priority": 1},
    9: {"name": "Marketing / Sales", "priority": 1},
    17: {"name": "HR / Organization Development", "priority": 1},
    7: {"name": "General Management / Admin", "priority": 1},
    16: {"name": "Customer Service / Call Centre", "priority": 2},
    10: {"name": "Media / Advertisement / Event Management", "priority": 2},
    13: {"name": "Research / Consultancy", "priority": 1},
    12: {"name": "NGO / Development", "priority": 2},
    20: {"name": "Hospitality / Travel / Tourism", "priority": 2},
    6: {"name": "Garments / Textile", "priority": 2},
    8: {"name": "IT / Telecom", "priority": 2},
    4: {"name": "Education / Training", "priority": 2},
}

BDJOBSLIVE_CATEGORIES = {
    "accounting-finance-jobs": "Accounting / Finance",
    "bank-financial-institution-jobs": "Bank / Financial Institution",
    "commercial-jobs": "Commercial",
    "company-secretary-regulatory-affairs-jobs": "Company Secretary / Regulatory Affairs",
    "customer-service-call-centre-jobs": "Customer Service / Call Centre",
    "e-commerce-digital-marketing-jobs": "E-commerce / Digital Marketing",
    "general-management-admin-jobs": "General Management / Admin",
    "hr-organizational-development-jobs": "HR / Organizational Development",
    "marketing-sales-jobs": "Marketing / Sales",
    "media-advertising-event-management-jobs": "Media / Advertising / Event Management",
    "ngo-development-jobs": "NGO / Development",
    "production-operation-jobs": "Production / Operation",
    "research-consultancy-jobs": "Research / Consultancy",
    "supply-chain-procurement-jobs": "Supply Chain / Procurement",
}

DEFAULT_BDJOBS_LIVE_INTERNSHIP_URL = "https://www.bdjobslive.com/internship-opportunity"

TARGET_FUNCTION_TERMS = (
    "account", "finance", "bank", "credit", "audit", "tax", "treasury",
    "marketing", "sales", "brand", "digital marketing", "business development",
    "partnership", "growth", "client acquisition", "human resource", "hr",
    "recruitment", "talent acquisition", "hr operations", "procurement", "purchase",
    "sourcing", "inventory", "logistics", "supply chain", "commercial", "import",
    "export", "trade operation", "management trainee", "management executive",
    "admin", "executive assistant", "customer service", "client service",
    "customer experience", "relationship management", "e-commerce", "marketplace",
    "online business", "digital operations", "operations", "process", "business analyst",
    "account manager", "account management", "customer success", "merchandising",
    "research", "consultancy", "ngo", "development", "hospitality", "travel", "tourism",
    "event", "media", "advertisement", "corporate affairs", "cashier", "front desk",
)
NON_BUSINESS_ROLE_TERMS = (
    "software engineer", "software developer", "web developer", "frontend developer",
    "backend developer", "full stack developer", "mobile developer", "app developer",
    "devops", "data engineer", "machine learning engineer", "civil engineer",
    "electrical engineer", "mechanical engineer", "doctor", "medical officer",
    "pharmacist", "nurse", "architect", "laboratory specialist", "graphic designer",
)

@dataclass(frozen=True)
class Config:
    pipeline_version: str = "CareerNewsroom V2"
    state_format_version: int = 20
    max_post_age_days: int = int(os.getenv("MAX_POST_AGE_DAYS", "3"))
    max_experience_years: int = int(os.getenv("MAX_PRIVATE_EXPERIENCE_YEARS", "3"))
    min_age: int = int(os.getenv("MIN_ALLOWED_AGE", "18"))
    max_age: int = int(os.getenv("MAX_ALLOWED_AGE", "30"))

    private_target: int = int(os.getenv("PRIVATE_TARGET", "10"))
    government_target: int = int(os.getenv("GOVERNMENT_TARGET", "5"))
    internship_target: int = int(os.getenv("INTERNSHIP_TARGET", "4"))
    flexible_extras: int = int(os.getenv("FLEXIBLE_EXTRAS", "6"))
    hard_max_posts: int = int(os.getenv("MAX_STORIES_PER_RUN", "25"))

    bdjobs_category_cap: int = int(os.getenv("BDJOBS_CATEGORY_CAP", "20"))
    bdjobslive_category_cap: int = int(os.getenv("BDJOBSLIVE_CATEGORY_CAP", "25"))
    internship_discovery_cap: int = int(os.getenv("INTERNSHIP_DISCOVERY_CAP", "30"))
    teletalk_discovery_cap: int = int(os.getenv("TELETALK_DISCOVERY_CAP", "30"))
    max_category_pages: int = int(os.getenv("MAX_CATEGORY_PAGES", "10"))
    detail_workers: int = int(os.getenv("DETAIL_WORKERS", "8"))
    pre_detail_cap: int = int(os.getenv("PRE_DETAIL_CAP", "700"))

    discovery_timeout: int = int(os.getenv("DISCOVERY_TIMEOUT", "25"))
    detail_timeout: int = int(os.getenv("DETAIL_TIMEOUT", "25"))
    browser_timeout_ms: int = int(os.getenv("BROWSER_TIMEOUT_MS", "45000"))
    browser_wait_ms: int = int(os.getenv("BROWSER_WAIT_MS", "1200"))
    browser_enabled: bool = os.getenv("BROWSER_ENABLED", "1").lower() not in {"0", "false", "no", "off"}

    cerebras_model: str = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
    cerebras_timeout: int = int(os.getenv("CEREBRAS_TIMEOUT", "45"))
    ai_batch_size: int = int(os.getenv("AI_BATCH_SIZE", "8"))

    telegram_channel: str = os.getenv("TELEGRAM_CHANNEL", "@CareerNewsroom")
    telegram_promo_url: str = os.getenv("TELEGRAM_PROMO_URL", "https://t.me/CareerNewsroom")
    post_delay_seconds: float = float(os.getenv("POST_DELAY_SECONDS", "1"))

    bdjobs_base: str = "https://bdjobs.com"
    bdjobslive_base: str = "https://www.bdjobslive.com"
    teletalk_api: str = "https://alljobs.teletalk.com.bd/api/v1/published-jobs/search"
    teletalk_base: str = "https://alljobs.teletalk.com.bd"
    bdjobslive_internship_url: str = os.getenv(
        "BDJOBSLIVE_INTERNSHIP_URL", DEFAULT_BDJOBS_LIVE_INTERNSHIP_URL
    )

    @property
    def bdjobs_category_urls(self) -> list[tuple[int, str, str]]:
        return [
            (cid, cfg["name"], f"{self.bdjobs_base}/h/jobs?lang=en&fcatId={cid}")
            for cid, cfg in BDJOBS_CATEGORIES.items()
        ]

    @property
    def bdjobslive_category_urls(self) -> list[tuple[str, str, str]]:
        return [
            (slug, name, f"{self.bdjobslive_base}/bdjobs-circular/{slug}")
            for slug, name in BDJOBSLIVE_CATEGORIES.items()
        ]

    def scheduler_window(self, hour: int) -> str | None:
        if 10 <= hour < 11:
            return "morning"
        if 17 <= hour < 18:
            return "afternoon"
        return None
