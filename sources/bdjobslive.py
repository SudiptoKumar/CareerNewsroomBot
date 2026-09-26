from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from bs4 import BeautifulSoup

from core.config import Config
from core.models import JobRecord
from core.parsing import label_value_rows, section_items, soup_text, extract_labeled_value
from core.utils import clean_text, canonical_url, iso_date
from .base import SourceAdapter

JOB_RE = re.compile(r"/bdjobs-details/[^/?#]+-(\d+)(?:[/?#]|$)", re.I)

class BdjobsLiveAdapter(SourceAdapter):
    name = "BDJobs Live"

    def __init__(self, http, config: Config):
        super().__init__(http)
        self.config = config

    def discover(self) -> list[JobRecord]:
        out: list[JobRecord] = []
        seen: set[str] = set()
        sources = list(self.config.bdjobslive_category_urls)
        sources.append(("internship-opportunity", "Internship Opportunity", self.config.bdjobslive_internship_url))
        for slug, category, url in sources:
            limit = self.config.internship_discovery_cap if "internship" in slug else self.config.bdjobslive_category_cap
            count = 0
            for page_no in range(1, self.config.max_category_pages + 1):
                page_url = self._page_url(url, page_no)
                result = self.http.fetch(page_url, referer=self.config.bdjobslive_base, timeout=self.config.discovery_timeout,
                                         wait_for='a[href*="/bdjobs-details/"]')
                if not result.ok:
                    break
                cards = self._parse_listing(result.text, result.url or page_url, category, url)
                if not cards:
                    break
                added = 0
                for item in cards:
                    if item.canonical_url in seen:
                        continue
                    seen.add(item.canonical_url)
                    out.append(item)
                    count += 1; added += 1
                    if count >= limit:
                        break
                if count >= limit or added == 0:
                    break
            # keep categories independent; global cap is intentionally not used here
        return out

    def _page_url(self, base: str, page: int) -> str:
        if page == 1:
            return base
        p = urlparse(base)
        q = dict(parse_qsl(p.query, keep_blank_values=True)); q["page"] = str(page)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))

    def _parse_listing(self, html: str, page_url: str, category: str, category_url: str) -> list[JobRecord]:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.select('a[href*="/bdjobs-details/"]')
        out = []
        for anchor in anchors:
            href = urljoin(page_url, anchor.get("href", ""))
            m = JOB_RE.search(href)
            if not m:
                continue
            job_id = m.group(1)
            card = self._card(anchor)
            title_node = anchor.select_one("h3,h2,h1")
            title = clean_text(title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True))
            if not title or len(title) > 180:
                continue
            rows = label_value_rows(card)
            company = self._company(anchor, card, title)
            employment = clean_text(rows.get("job type", rows.get("employment type", "")))
            location = clean_text(rows.get("location", ""))
            experience = clean_text(rows.get("experience", ""))
            education = clean_text(rows.get("education", ""))
            deadline = iso_date(rows.get("deadline", ""))
            internship = "intern" in title.lower() or "internship" in employment.lower() or "internship" in category.lower()
            out.append(JobRecord(
                source=self.name,
                source_job_id=job_id,
                title=title,
                company=company,
                source_url=f"{self.config.bdjobslive_base}/bdjobs-details/{href.rsplit('/',1)[-1].split('?')[0]}",
                canonical_url=canonical_url(href),
                discovery_url=page_url,
                source_category=category,
                source_category_url=category_url,
                is_internship=internship,
                location=location,
                experience=experience,
                education=education,
                job_type=employment,
                deadline=deadline,
                raw_listing={"is_hot": bool(re.search(r"\bHOT\b", soup_text(card), re.I)) if card else False},
                extraction_status="listing_validated",
            ))
        # dedupe same anchor rendered twice by responsive markup
        dedup = {}
        for job in out:
            dedup[job.canonical_url] = job
        return list(dedup.values())

    @staticmethod
    def _card(anchor):
        node = anchor
        best = anchor
        for _ in range(7):
            if not node.parent:
                break
            node = node.parent
            text = soup_text(node)
            if 50 <= len(text) <= 2200:
                best = node
                if len(node.find_all("a", href=True)) <= 5:
                    break
        return best

    @staticmethod
    def _company(anchor, card, title):
        # The source spec places company as a span below the title inside the anchor/card.
        spans = [clean_text(x.get_text(" ", strip=True)) for x in anchor.find_all("span")]
        for value in spans:
            if value and value.lower() != title.lower() and len(value) < 180:
                return value
        # second pass: likely employer heading/link
        for node in card.find_all(["span","p","strong","b"]):
            value = clean_text(node.get_text(" ", strip=True))
            if value and value.lower() != title.lower() and len(value) < 180 and not re.match(r"^(dhaka|location|experience|education|deadline)$", value, re.I):
                return value
        return ""

    def enrich(self, job: JobRecord) -> JobRecord:
        result = self.http.fetch(job.source_url, referer=job.discovery_url, timeout=self.config.detail_timeout,
                                 wait_for="#section-education")
        if not result.ok:
            job.extraction_status = "detail_fetch_failed"
            return job
        soup = BeautifulSoup(result.text, "html.parser")
        if not self._valid_detail(soup, job.source_job_id):
            job.extraction_status = "detail_page_invalid"
            return job
        job.detail_backend = result.backend
        job.extraction_status = "detail_validated"
        h1 = soup.select_one("h1")
        if h1:
            job.title = clean_text(h1.get_text(" ", strip=True))
        employer = soup.select_one('a[href*="/company-detail/"]')
        if employer:
            job.company = clean_text(employer.get_text(" ", strip=True))
            job.company_profile_url = urljoin(result.url, employer.get("href", ""))
        rows = label_value_rows(soup.select_one("main") or soup.select_one("body"))
        job.deadline = iso_date(extract_labeled_value(soup, "Application Deadline") or rows.get("application deadline", "") or job.deadline)
        job.vacancy = clean_text(rows.get("vacancy", ""))
        job.age = clean_text(rows.get("age", ""))
        job.location = clean_text(rows.get("location", ""))
        job.salary = clean_text(rows.get("salary", ""))
        job.experience = clean_text(rows.get("experience", ""))
        job.job_type = clean_text(rows.get("job type", ""))
        job.job_shift = clean_text(rows.get("job shift", ""))
        job.published_date = iso_date(rows.get("published", ""))
        job.category = clean_text(rows.get("category", "")) or self._meta_description_category(soup)
        edu = soup.select_one("#section-education")
        exp = soup.select_one("#section-experience")
        skills = soup.select_one("#section-skills")
        resp = soup.select_one("#section-responsibilities")
        company = soup.select_one("#section-company")
        if edu: job.education = " | ".join(section_items(edu))
        if exp and not job.experience: job.experience = " | ".join(section_items(exp))
        if skills: job.skills = section_items(skills)
        if resp: job.responsibilities = section_items(resp)
        if company:
            ctext = soup_text(company)
            job.company = job.company or self._label_value_text(ctext, "Company Information")
            job.company_industry = self._after_label(ctext, "Industry")
            job.company_address = self._after_label(ctext, "Address")
            job.company_website = self._after_label(ctext, "Website")
            website = company.find("a", href=True)
            if website and website.get("href", "").startswith(("http://", "https://")):
                job.company_website = website["href"]
        apply_button = next((b for b in soup.find_all("button") if clean_text(b.get_text(" ", strip=True)).lower() == "apply now"), None)
        job.apply_url = ""
        job.application_method = "Online" if apply_button else job.application_method
        job.raw_text = soup_text(soup.select_one("body"))[:16000]
        og = soup.find("meta", attrs={"property": "og:image"})
        if og: job.raw_listing["job_share_image"] = clean_text(og.get("content", ""))
        logo = soup.find("img", alt=re.compile(re.escape(job.company), re.I)) if job.company else None
        if logo: job.raw_listing["company_logo_url"] = urljoin(result.url, logo.get("src", ""))
        return job

    @staticmethod
    def _valid_detail(soup, job_id):
        h1 = soup.select_one("h1")
        company = soup.select_one('a[href*="/company-detail/"]')
        core = soup.select_one("#section-education") or soup.select_one("#section-experience") or soup.select_one("#section-responsibilities")
        return bool(h1 and company and core)

    @staticmethod
    def _meta_description_category(soup):
        for attrs in ({"name": "description"}, {"property": "og:description"}):
            tag = soup.find("meta", attrs=attrs)
            if tag:
                m = re.search(r"Category\s*:?\s*([^\.]+)", clean_text(tag.get("content", "")), re.I)
                if m: return clean_text(m.group(1))
        return ""

    @staticmethod
    def _after_label(text, label):
        m = re.search(rf"{re.escape(label)}\s*:?\s*(.+?)(?=\s+(?:Address|Website|Industry)\s*:|$)", text, re.I)
        return clean_text(m.group(1)) if m else ""

    @staticmethod
    def _label_value_text(text, label):
        m = re.search(rf"{re.escape(label)}\s*:?\s*(.+)", text, re.I)
        return clean_text(m.group(1)) if m else ""
