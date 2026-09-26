from __future__ import annotations

import re
from bs4 import BeautifulSoup

from .utils import clean_text, iso_date, is_placeholder

LABELS = [
    "Vacancy", "Age", "Location", "Salary", "Experience", "Education", "Published",
    "Deadline", "Application Deadline", "Job Type", "Job Shift", "Employment Status",
    "Job Work Place", "Job Location", "Category", "Gender", "Industry", "Workplace",
]


def soup_text(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def first_nonempty(*values: str) -> str:
    for value in values:
        if clean_text(value) and not is_placeholder(value):
            return clean_text(value)
    return ""


def label_value_rows(container) -> dict[str, str]:
    if not container:
        return {}
    result: dict[str, str] = {}
    # Strong path: find elements whose visible text is an exact label, then use the nearby
    # sibling/value within the smallest row-like ancestor.
    for tag in container.find_all(["span", "p", "div", "dt", "strong", "b"]):
        label = clean_text(tag.get_text(" ", strip=True)).rstrip(":")
        if label.lower() not in {x.lower() for x in LABELS}:
            continue
        parent = tag.parent
        if not parent:
            continue
        siblings = list(parent.children)
        try:
            idx = siblings.index(tag)
        except ValueError:
            idx = -1
        candidates = []
        if idx >= 0:
            for sib in siblings[idx + 1: idx + 4]:
                if getattr(sib, "get_text", None):
                    t = clean_text(sib.get_text(" ", strip=True))
                    if t:
                        candidates.append(t)
        if not candidates:
            text = soup_text(parent)
            m = re.search(rf"^{re.escape(label)}\s*:?\s*(.+)$", text, re.I)
            if m:
                candidates.append(clean_text(m.group(1)))
        if candidates:
            result[label.lower()] = candidates[0]

    # Flattened text fallback, bounded by the next recognized label.
    text = soup_text(container)
    if text:
        pattern_labels = sorted(LABELS, key=len, reverse=True)
        for wanted in LABELS:
            pattern = rf"{re.escape(wanted)}\s*:\s*(.+?)(?=\s+(?:{'|'.join(re.escape(x) for x in pattern_labels)})\s*:|$)"
            m = re.search(pattern, text, re.I)
            if m:
                result.setdefault(wanted.lower(), clean_text(m.group(1)))
    return {k: v for k, v in result.items() if v}


def section_items(section) -> list[str]:
    if not section:
        return []
    items = [clean_text(x.get_text(" ", strip=True)) for x in section.find_all("li")]
    items = [x for x in items if x and len(x) > 1]
    if items:
        return items
    # fallback for div-based bullet structures
    out = []
    for child in section.find_all(["p", "div"]):
        t = clean_text(child.get_text(" ", strip=True))
        if t and len(t) > 3 and t not in out:
            out.append(t)
    return out


def extract_labeled_value(container, label: str) -> str:
    if not container:
        return ""
    rows = label_value_rows(container)
    if label.lower() in rows:
        return rows[label.lower()]
    text = soup_text(container)
    m = re.search(rf"{re.escape(label)}\s*:\s*(.+?)(?=\s+(?:{'|'.join(re.escape(x) for x in LABELS)})\s*:|$)", text, re.I)
    return clean_text(m.group(1)) if m else ""


def extract_deadline(text_or_container) -> str:
    text = soup_text(text_or_container) if hasattr(text_or_container, "get_text") else clean_text(text_or_container)
    m = re.search(r"Application\s+Deadline\s*:?\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4}|[0-9]{4}[-/]\d{1,2}[-/]\d{1,2})", text, re.I)
    return iso_date(m.group(1)) if m else ""


def parse_experience_bounds(value: str) -> tuple[int | None, int | None, str]:
    raw = clean_text(value)
    low = raw.lower()
    if not low:
        return None, None, "unknown"
    if any(x in low for x in ("fresher", "fresh graduate", "fresh graduates", "no experience", "entry-level", "entry level", "0 year", "0-1 year", "0 to 1 year", "internship", "intern")):
        return 0, 0, "bounded"
    if re.search(r"(?:3\s*\+|at\s+least\s*3|3\s+years?\s+or\s+more|more\s+than\s*3)", low):
        return 3, None, "open_min"
    m = re.search(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*years?", low)
    if m:
        return int(m.group(1)), int(m.group(2)), "bounded"
    m = re.search(r"(?:at\s+least|minimum(?:\s+of)?|not\s+less\s+than)\s*(\d+)\s*years?", low)
    if m:
        return int(m.group(1)), None, "open_min"
    m = re.search(r"(\d+)\s*\+\s*years?", low)
    if m:
        return int(m.group(1)), None, "open_min"
    m = re.search(r"(\d+)\s*years?", low)
    if m:
        n = int(m.group(1)); return n, n, "bounded"
    return None, None, "unknown"


def parse_age_bounds(value: str) -> tuple[int | None, int | None]:
    raw = clean_text(value).lower()
    if not raw:
        return None, None
    m = re.search(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*years?", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"at\s+least\s+(\d+)\s*years?", raw)
    if m:
        return int(m.group(1)), None
    m = re.search(r"at\s+most\s+(\d+)\s*years?", raw)
    if m:
        return None, int(m.group(1))
    m = re.search(r"(\d+)\s*years?", raw)
    if m:
        n = int(m.group(1)); return n, n
    return None, None
