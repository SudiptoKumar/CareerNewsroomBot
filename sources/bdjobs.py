from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup

from core.config import Config
from core.models import JobRecord
from core.parsing import extract_labeled_value, label_value_rows, section_items, soup_text
from core.utils import clean_text, canonical_url, iso_date, is_placeholder
from .base import SourceAdapter

JOB_RE = re.compile(r"/h/details/(\d+)(?:[/?#]|$)", re.I)

class BdjobsAdapter(SourceAdapter):
    name = "Bdjobs"

    def __init__(self, http, config: Config):
        super().__init__(http)
        self.config = config

    def discover(self) -> list[JobRecord]:
        out: list[JobRecord] = []
        seen: set[str] = set()
        for cid, category, url in self.config.bdjobs_category_urls:
            category_count = 0
            for page_no in range(1, self.config.max_category_pages + 1):
                page_url = self._page_url(url, page_no)
                result = self.http.fetch(page_url, referer=url, timeout=self.config.discovery_timeout,
                                         wait_for="app-job-list")
                if not result.ok:
                    break
                cards = self._parse_listing(result.text, result.url or page_url, category, url)
                if not cards:
                    break
                fresh_added = 0
                for item in cards:
                    if item.canonical_url in seen:
                        continue
                    seen.add(item.canonical_url)
                    out.append(item)
                    fresh_added += 1
                    category_count += 1
                    if category_count >= self.config.bdjobs_category_cap:
                        break
                if category_count >= self.config.bdjobs_category_cap:
                    break
                if fresh_added == 0:
                    break
            if len(out) >= 200:
                break
        return out

    def _page_url(self, base: str, page: int) -> str:
        if page == 1:
            return base
        p = urlparse(base)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q["page"] = str(page)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))

    def _parse_listing(self, html: str, page_url: str, category: str, category_url: str) -> list[JobRecord]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("app-job-card")
        if not cards:
            cards = self._anchor_cards(soup, "/h/details/")
        out = []
        for card in cards:
            anchor = card.select_one('a[href*="/h/details/"]') if hasattr(card, "select_one") else None
            if anchor is None and getattr(card, "name", None) == "a":
                anchor = card
            if not anchor:
                continue
            href = urljoin(page_url, anchor.get("href", ""))
            m = JOB_RE.search(href)
            if not m:
                continue
            job_id = m.group(1)
            title_node = anchor.select_one('[data-testid="job-title"]') or anchor.select_one("h3,h2,h1")
            title = clean_text(title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True))
            if not title or len(title) > 180:
                continue
            scope = card
            rows = label_value_rows(scope)
            company = self._company(scope, title)
            location = rows.get("location", "")
            experience = rows.get("experience", "")
            education = rows.get("education", "")
            deadline = iso_date(rows.get("deadline", ""))
            logo = ""
            img = scope.find("img") if scope else None
            if img and clean_text(img.get("alt", "")) and company and company.lower() in clean_text(img.get("alt", "")).lower():
                logo = urljoin(page_url, img.get("src", ""))
            out.append(JobRecord(
                source=self.name,
                source_job_id=job_id,
                title=title,
                company=company,
                source_url=self._canonical_detail_url(href, job_id),
                canonical_url=canonical_url(self._canonical_detail_url(href, job_id)),
                discovery_url=page_url,
                source_category=category,
                source_category_url=category_url,
                location=location,
                experience=experience,
                education=education,
                deadline=deadline,
                raw_listing={"company_logo_url": logo, "location": location, "experience": experience, "education": education},
                extraction_status="listing_validated",
            ))
        return out

    @staticmethod
    def _anchor_cards(soup, path_part: str):
        cards = []
        for a in soup.select(f'a[href*="{path_part}"]'):
            node = a
            for _ in range(6):
                if not node.parent:
                    break
                node = node.parent
                text = soup_text(node)
                if 50 <= len(text) <= 2500 and len(node.find_all("a", href=True)) <= 8:
                    cards.append(node)
                    break
        return cards

    @staticmethod
    def _company(card, title: str) -> str:
        if card is None:
            return ""
        # Prefer common semantic markers, then a nearby text span following title.
        for sel in ('[data-testid="company-name"]', '.company-name'):
            node = card.select_one(sel)
            if node:
                val = clean_text(node.get_text(" ", strip=True))
                if val and val.lower() != title.lower():
                    return val
        anchor = card.select_one('a[href*="/h/details/"]')
        if anchor:
            texts = [clean_text(x) for x in anchor.stripped_strings]
            for t in texts:
                if t and t.lower() != title.lower() and len(t) < 180 and not re.match(r"^(deadline|location|experience|education)\b", t, re.I):
                    return t
        # Last-resort nearest bold text, but never generic site labels.
        for node in card.find_all(["strong", "b", "span", "p"]):
            val = clean_text(node.get_text(" ", strip=True))
            if val and val.lower() != title.lower() and 2 <= len(val) <= 180 and not is_placeholder(val):
                if not re.search(r"^(jobs|accounting|finance|marketing|home|bdjobs)$", val, re.I):
                    return val
        return ""

    @staticmethod
    def _canonical_detail_url(url: str, job_id: str) -> str:
        return f"https://bdjobs.com/h/details/{job_id}"

    def enrich(self, job: JobRecord) -> JobRecord:
        result = self.http.fetch(job.source_url, referer=job.discovery_url, timeout=self.config.detail_timeout,
                                 wait_for="#allSection")
        if not result.ok:
            job.extraction_status = "detail_fetch_failed"
            return job
        soup = BeautifulSoup(result.text, "html.parser")
        if not self._valid_detail(soup, job.source_job_id):
            job.extraction_status = "detail_page_invalid"
            return job
        job.detail_backend = result.backend
        job.extraction_status = "detail_validated"
        job.title = self._title(soup, job.title)
        job.company = self._company_detail(soup, job.company)
        all_section = soup.select_one("#allSection")
        rows = label_value_rows(all_section)
        job.deadline = iso_date(extract_labeled_value(soup, "Application Deadline") or rows.get("application deadline", "") or job.deadline)
        job.published_date = iso_date(rows.get("published", ""))
        job.vacancy = clean_text(rows.get("vacancy", job.vacancy))
        job.age = clean_text(rows.get("age", ""))
        job.location = clean_text(rows.get("location", "")) or job.location
        job.salary = clean_text(rows.get("salary", ""))
        job.experience = clean_text(rows.get("experience", ""))
        job.job_type = clean_text(rows.get("job type", rows.get("employment status", "")))
        job.workplace = clean_text(rows.get("job work place", rows.get("workplace", "")))
        job.category = clean_text(rows.get("category", ""))

        req = soup.select_one("#requirements")
        if req:
            edu_section = self._section_by_heading(req, "education")
            exp_section = self._section_by_heading(req, "experience")
            add_section = self._section_by_heading(req, "additional requirements")
            job.requirements = section_items(req)
            if edu_section:
                job.education = " | ".join(section_items(edu_section))
            if exp_section and not job.experience:
                job.experience = " | ".join(section_items(exp_section))
            if add_section and not job.age:
                txt = soup_text(add_section)
                m = re.search(r"Age\s*:?\s*(.+)", txt, re.I)
                if m:
                    job.age = clean_text(m.group(1))

        resp = soup.select_one("#responsibilitiesSection")
        job.responsibilities = section_items(resp)
        skills = soup.select_one("#skills")
        job.skills = section_items(skills)
        salary_section = soup.select_one("#salary")
        job.benefits = section_items(salary_section)
        job.raw_text = soup_text(soup.select_one("body"))[:16000]
        job.apply_url = ""
        apply_button = soup.select_one('[data-testid="applyNowBtn"]')
        job.application_method = "Online" if apply_button else job.application_method
        job.raw_listing["og_image"] = self._meta(soup, "og:image")
        job.raw_listing["apply_button_present"] = bool(apply_button)
        return job

    @staticmethod
    def _valid_detail(soup, job_id: str) -> bool:
        title = bool(soup.select_one("h2") or soup.title)
        section = bool(soup.select_one("#allSection"))
        url_id = ""
        og = soup.find("meta", attrs={"property": "og:url"})
        if og:
            m = JOB_RE.search(clean_text(og.get("content", "")))
            url_id = m.group(1) if m else ""
        return title and section and (not url_id or url_id == job_id)

    @staticmethod
    def _title(soup, fallback):
        h2s = [clean_text(x.get_text(" ", strip=True)) for x in soup.select("h2") if clean_text(x.get_text(" ", strip=True))]
        for value in h2s:
            if value.lower() != clean_text(fallback).lower() and len(value) <= 180:
                if "bdjobs.com" not in value.lower():
                    return value
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta:
            value = clean_text(meta.get("content", "")).split(":")[0]
            if value:
                return value
        return clean_text(fallback)

    @staticmethod
    def _company_detail(soup, fallback):
        button_h2 = soup.select_one("button h2")
        if button_h2:
            return clean_text(button_h2.get_text(" ", strip=True))
        meta = soup.find("meta", attrs={"property": "og:title"})
        if meta:
            val = clean_text(meta.get("content", ""))
            if ":" in val:
                tail = val.split(":", 1)[1]
                tail = re.sub(r"\|\|\s*Bdjobs\.com.*$", "", tail, flags=re.I)
                if tail.strip():
                    return tail.strip()
        return clean_text(fallback)

    @staticmethod
    def _section_by_heading(container, heading: str):
        for node in container.find_all(["h3", "h4", "strong", "p"]):
            if clean_text(node.get_text(" ", strip=True)).lower() == heading.lower():
                return node.parent
        return None

    @staticmethod
    def _meta(soup, prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop})
        return clean_text(tag.get("content", "")) if tag else ""
