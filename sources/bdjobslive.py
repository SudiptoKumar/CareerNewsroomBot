"""BDJobs Live source adapter configuration.

The downstream pipeline owns normalization, eligibility, ranking, AI review,
and publishing. This module owns the BDJobs Live discovery entry points and
stable source URL contracts so source-specific configuration stays isolated.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

BDJOBSLIVE_BASE = "https://www.bdjobslive.com"
BDJOBSLIVE_CATEGORY_PATH = "/bdjobs-circular"
BDJOBSLIVE_INTERNSHIP_URL = f"{BDJOBSLIVE_BASE}/bdjobs-circular/internship-opportunity"

# name, primary slug, compatibility slug
BDJOBSLIVE_CATEGORIES = (
    ("Accounting / Finance", "accounting-finance-jobs", "accounting-finance"),
    ("Bank / Financial Institution", "bank-financial-institution-jobs", "bank-financial-institution"),
    ("Commercial", "commercial-jobs", "commercial"),
    ("Company Secretary / Regulatory Affairs", "company-secretary-regulatory-affairs-jobs", "company-secretary-regulatory-affairs"),
    ("Customer Service / Call Centre", "customer-service-call-centre-jobs", "customer-service-call-centre"),
    ("E-commerce / Digital Marketing", "e-commerce-digital-marketing-jobs", "e-commerce-digital-marketing"),
    ("General Management / Admin", "general-management-admin-jobs", "general-management-admin"),
    ("HR / Organizational Development", "hr-organizational-development-jobs", "hr-organizational-development"),
    ("Marketing / Sales", "marketing-sales-jobs", "marketing-sales"),
    ("Media / Advertising / Event Management", "media-advertising-event-management-jobs", "media-advertising-event-management"),
    ("NGO / Development", "ngo-development-jobs", "ngo-development"),
    ("Production / Operation", "production-operation-jobs", "production-operation"),
    ("Research / Consultancy", "research-consultancy-jobs", "research-consultancy"),
    ("Supply Chain / Procurement", "supply-chain-procurement-jobs", "supply-chain-procurement"),
)

BDJOBSLIVE_ALLOWED_CATEGORY_TERMS = tuple(
    re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    for name, _, _ in BDJOBSLIVE_CATEGORIES
)


def category_urls(primary_slug: str, compatibility_slug: str | None = None) -> tuple[str, ...]:
    """Return approved category entry points only, primary first."""
    urls = [urljoin(BDJOBSLIVE_BASE, f"{BDJOBSLIVE_CATEGORY_PATH}/{primary_slug}")]
    if compatibility_slug and compatibility_slug != primary_slug:
        urls.append(urljoin(BDJOBSLIVE_BASE, f"{BDJOBSLIVE_CATEGORY_PATH}/{compatibility_slug}"))
    return tuple(urls)


def is_approved_category_url(url: str) -> bool:
    """Return True only for configured category entry points."""
    return url in {
        candidate
        for _, primary, compatibility in BDJOBSLIVE_CATEGORIES
        for candidate in category_urls(primary, compatibility)
    }


__all__ = [
    "BDJOBSLIVE_ALLOWED_CATEGORY_TERMS",
    "BDJOBSLIVE_CATEGORIES",
    "BDJOBSLIVE_INTERNSHIP_URL",
    "category_urls",
    "is_approved_category_url",
]
