import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def make_listing_item():
    return {
        "title": "Accounts Executive",
        "url": "https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1",
        "canonical": main.canonical_url("https://jobs.bdjobs.com/jobdetails/?id=777001&ln=1"),
        "source": "Bdjobs",
        "source_job_id": "777001",
        "category_id": 1,
        "category_name": "Accounting / Finance",
        "listing_posted": "2026-09-19",
        "listing_deadline": "2026-10-01",
        "listing_fields": {
            "company": "Example Finance Ltd.",
            "experience": "1 to 2 years",
            "education": "BBA",
            "deadline": "2026-10-01",
            "location": "Dhaka",
            "salary": "Tk. 30,000",
            "vacancy": "3",
        },
        "excerpt": (
            "Accounts Executive Example Finance Ltd. Dhaka "
            "Experience required: 1 to 2 year(s) Deadline: Oct 1, 2026 "
            "Education required: BBA Vacancy: 3"
        ),
    }


def test_bdjobs_current_detail_url_shape():
    assert main.is_bdjobs_job_url("https://jobs.bdjobs.com/jobdetails/?id=1511688&ln=1")


def test_listing_baseline_preserved_when_detail_unavailable(monkeypatch):
    item = make_listing_item()
    monkeypatch.setattr(main, "_fetch_bdjobs_detail", lambda _item: None)
    content = main.retrieve_job_content(item)
    assert content is not None
    assert content["detail_quality"] == "listing_fallback"

    job = main.research_job(item)
    assert job["company"] == "Example Finance Ltd."
    assert job["education"] == "BBA"
    assert job["experience"] == "1 to 2 years"
    assert job["vacancy"] == "3"
    assert job["salary"] == "Tk. 30,000"
    assert job["deadline"] == "2026-10-01"
    assert main.deterministic_job_gate(job)[0]


def test_detail_fetch_prefers_direct_then_jina(monkeypatch):
    item = make_listing_item()
    calls = []

    def fake_curl(url, **_kwargs):
        calls.append(("curl", url))
        return {
            "ok": True,
            "status": 200,
            "text": "Just a moment...",
            "url": url,
            "backend": "curl_cffi:safari18_0_ios",
            "cloudflare": True,
        }

    def fake_jina(url, **_kwargs):
        calls.append(("jina", url))
        return {
            "ok": True,
            "status": 200,
            "text": (
                "Job Summary\n"
                "Company Name\nExample Finance Ltd.\n"
                "Education\nBBA\n"
                "Experience\n1 to 2 years\n"
                "Deadline\n2026-10-01\n"
                "Responsibilities include finance operations, monthly reporting, reconciliation, budgeting support, vendor coordination and documentation. The role works with the finance team and business stakeholders.\n"
            ),
            "url": url,
            "backend": "jina_reader",
            "cloudflare": False,
        }

    monkeypatch.setattr(main, "_fetch_with_curl", fake_curl)
    monkeypatch.setattr(main, "_fetch_jina", fake_jina)
    main.DETAIL_CACHE.clear()

    result = main._fetch_bdjobs_detail(item)
    assert result is not None
    assert result["backend"] == "jina_reader"
    assert result["detail_quality"] == "jina_valid"
    assert calls[0][0] == "curl"
    assert calls[1][0] == "jina"


def test_listing_parser_keeps_job_url_and_fields():
    html = """
    <html><body>
      <div>
        <a href="/jobdetails/?id=555001&ln=1">Finance Officer</a>
        <div>ABC Finance Ltd. Dhaka Experience required: 0 to 1 year(s)
        Deadline: Sep 25, 2026 Education required: BBA Vacancy: 2</div>
      </div>
      <div><a href="/jobdetails/?id=555002&ln=1">Our Valuable Partners</a></div>
    </body></html>
    """
    rows = main._bdjobs_listing_candidates(
        html,
        "https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=1",
        1,
        "Accounting / Finance",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["source_job_id"] == "555001"
    assert row["listing_fields"]["education"] == "BBA"
    assert row["listing_fields"]["vacancy"] == "2"
    assert row["listing_fields"]["deadline"] == "2026-09-25"


def test_distinct_source_ids_are_not_fuzzy_deduped():
    a = {
        "source": "Bdjobs", "source_job_id": "1001", "title": "Assistant Manager",
        "company": "Example Ltd.", "location": "Dhaka",
        "source_url": "https://jobs.bdjobs.com/jobdetails/?id=1001&ln=1",
        "posted_date": "2026-09-19",
    }
    b = dict(a, source_job_id="1002", source_url="https://jobs.bdjobs.com/jobdetails/?id=1002&ln=1")
    assert not main.likely_same_job(a, b)
