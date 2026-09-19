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
    assert row["listing_fields"]["company"] == "ABC Finance Ltd."



def test_listing_company_inference_from_current_card_shape():
    html = """
    <html><body>
      <div class="job-card">
        <a href="/jobdetails/?id=555003&ln=1">Management Trainee</a>
        <div class="company">Example Bank PLC</div>
        <div>Job Location</div><div>Dhaka</div>
        <div>Experience required</div><div>0 to 1 year(s)</div>
        <div>Deadline</div><div>25 Sep 2026</div>
        <div>Education required</div><div>BBA / MBA</div>
      </div>
    </body></html>
    """
    rows = main._bdjobs_listing_candidates(
        html,
        "https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=1",
        1,
        "Accounting / Finance",
    )
    assert rows and rows[0]["listing_fields"]["company"] == "Example Bank PLC"


def test_spa_shell_is_not_a_valid_detail_page():
    html = """
    <html>
      <head><style>.foo{--tw-gradient-to-position:}</style></head>
      <body><app-root></app-root><script>window.__APP__={}</script></body>
    </html>
    """
    ok, reason = main._looks_like_job_document(html, detail=True)
    assert not ok
    assert reason in {"empty_visible_text", "bdjobs_application_shell", "thin_or_non_job_page:0"}


def test_direct_html_is_normalized_before_field_extraction():
    html = """
    <html><head><style>body{--tw-gradient-to-position:}</style></head>
    <body>
      <h1>Management Trainee</h1>
      <p>Company Name</p><p>Example Bank</p>
      <p>Education</p><p>BBA / MBA</p>
      <p>Experience</p><p>0 to 1 year(s)</p>
      <p>Deadline</p><p>2026-10-01</p>
    </body></html>
    """
    fetched = {
        "ok": True, "status": 200, "text": html,
        "url": "https://jobs.bdjobs.com/jobdetails.asp?id=1",
        "backend": "curl_cffi:safari18_0_ios", "cloudflare": False,
    }
    normalized = main._detail_payload_from_fetch(fetched)
    assert "--tw-" not in normalized["text"]
    assert "<style>" not in normalized["text"]
    parsed = main.extract_job_fields(
        normalized["text"], normalized["html"], normalized["url"],
        {"title": "Management Trainee", "url": normalized["url"]},
    )
    assert parsed["title"] == "Management Trainee"
    assert parsed["company"] == "Example Bank"


def test_distinct_source_ids_are_not_fuzzy_deduped():
    a = {
        "source": "Bdjobs", "source_job_id": "1001", "title": "Assistant Manager",
        "company": "Example Ltd.", "location": "Dhaka",
        "source_url": "https://jobs.bdjobs.com/jobdetails/?id=1001&ln=1",
        "posted_date": "2026-09-19",
    }
    b = dict(a, source_job_id="1002", source_url="https://jobs.bdjobs.com/jobdetails/?id=1002&ln=1")
    assert not main.likely_same_job(a, b)


def test_jina_markdown_is_cleaned_before_parsing():
    markdown = """
## Customer Support Executive E-Commerce
🏢 name-share-details.gif
[![Image 18](https://bdjobs.com/h/images/matching_lock_en.webp)](https://bdjobs.com/h/)
[![Image 19](https://bdjobs.com/h/images/matching_lock_en_res.svg)](https://bdjobs.com/h/)
Job Summary
Company Name
Chino Carts
Job Location
Dhaka (Dakshinkhan)
Experience
At Least 1 Year
Education
BBA
Salary
Negotiable
Vacancy
5
Age
23 Years
Application
Online
Deadline
25-09-2026
Posted
19-09-2026
"""
    cleaned = main.clean_reader_markdown(markdown)
    assert "name-share-details.gif" not in cleaned
    assert "![Image" not in cleaned
    assert "matching_lock_en.webp" not in cleaned
    assert "[](" not in cleaned
    item = {
        "title": "Customer Support Executive E-Commerce",
        "url": "https://jobs.bdjobs.com/jobdetails/?id=888001&ln=1",
        "source": "Bdjobs",
        "source_job_id": "888001",
        "listing_fields": {},
    }
    parsed = main.extract_job_fields(cleaned, "", item["url"], item)
    assert parsed["title"] == "Customer Support Executive E-Commerce"
    assert parsed["company"] == "Chino Carts"
    assert parsed["location"] == "Dhaka (Dakshinkhan)"
    assert parsed["experience"] == "At Least 1 Year"
    assert parsed["vacancy"] == "5"


def test_private_minimum_and_government_minimum_selection():
    private = []
    for i in range(14):
        private.append({
            "canonical": f"bdjobs-{i}",
            "source": "Bdjobs",
            "source_job_id": str(880000+i),
            "source_url": f"https://jobs.bdjobs.com/jobdetails/?id={880000+i}&ln=1",
            "title": f"Business Executive {i}",
            "company": f"Company {i}",
            "location": "Dhaka",
            "education": "BBA / MBA",
            "experience": "0 to 2 years",
            "salary": "Tk. 30,000",
            "vacancy": "2",
            "deadline": "2026-10-10",
            "posted_date": "2026-09-19",
            "raw_text": "Business Executive BBA MBA sales marketing business development operations",
            "bba_mba_target_score": 80,
            "final_score": 72 - i * 0.3,
            "deterministic_score": 72 - i * 0.3,
            "judge_publish": True,
        })
    govt = []
    for i in range(5):
        govt.append({
            "canonical": f"tel-{i}",
            "source": "Teletalk",
            "source_job_id": f"TL-{i}",
            "source_url": "https://alljobs.teletalk.com.bd/",
            "title": f"Government Officer {i}",
            "company": "Government Organization",
            "deadline": "2026-10-10",
            "posted_date": "2026-09-19",
            "vacancy": "5",
            "education": "Bachelor degree",
            "location": "Dhaka",
            "raw_text": "Government vacancy",
            "is_government": True,
        })
    old = main.candidate_already_posted
    main.candidate_already_posted = lambda _job: False
    try:
        selected = main.select_final_jobs(private, govt)
    finally:
        main.candidate_already_posted = old
    assert sum(1 for j in selected if j.get("is_government")) >= 3
    assert sum(1 for j in selected if not j.get("is_government")) >= 10
    assert len(selected) <= 20


def test_judge_batch_salvages_malformed_structured_output(monkeypatch):
    class Msg:
        content = '{"results":[{"id":1,"publish":true,"score":88,"bba_mba_fit":90,"early_career_fit":86,"role_fit":89,"reason":"Strong business-role fit."},{"id":2,"publish":true,"score":82,"bba_mba_fit":85,"early_career_fit":80,"role_fit":83,"reason":"Good fit."}'
    class Choice:
        message = Msg()
    class Resp:
        choices = [Choice()]
    class FakeCompletions:
        def create(self, **_kwargs):
            return Resp()
    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()
    monkeypatch.setattr(main, "get_cerebras", lambda: FakeClient())
    batch = [
        {"source":"Bdjobs","title":"Accounts Executive","company":"A","career_category":"Finance & Accounting","education":"BBA","experience":"0 to 2 years","raw_text":"finance account"},
        {"source":"Bdjobs","title":"Marketing Executive","company":"B","career_category":"Marketing & Sales","education":"BBA","experience":"0 to 2 years","raw_text":"marketing sales"},
    ]
    rows = main.judge_batch(batch, 1)
    assert len(rows) == 2
    assert rows[0]["score"] == 88
    assert rows[1]["role_fit"] == 83


def test_distinct_source_ids_with_shared_board_url_are_not_collapsed():
    a = {
        "source": "Teletalk", "source_job_id": "TL-1001",
        "title": "Officer", "company": "Department A", "location": "Dhaka",
        "source_url": "https://alljobs.teletalk.com.bd/",
        "posted_date": "2026-09-19",
    }
    b = dict(a, source_job_id="TL-1002", title="Officer", company="Department B")
    assert not main.likely_same_job(a, b)


def test_private_minimum_fill_expands_below_strict_floor():
    jobs = []
    for i in range(12):
        jobs.append({
            "canonical": f"fill-{i}",
            "source": "Bdjobs",
            "source_job_id": str(990000+i),
            "source_url": f"https://jobs.bdjobs.com/jobdetails/?id={990000+i}&ln=1",
            "title": f"Business Executive {i}",
            "company": f"Fill Company {i}",
            "location": "Dhaka",
            "education": "BBA / MBA",
            "experience": "0 to 2 years",
            "salary": "Negotiable",
            "vacancy": "2",
            "deadline": "2026-10-10",
            "posted_date": "2026-09-19",
            "raw_text": "business development marketing sales BBA MBA",
            "bba_mba_target_score": 70,
            "final_score": 60 - i * 0.2,
            "deterministic_score": 60 - i * 0.2,
            "judge_publish": True,
            "is_government": False,
        })
    old = main.candidate_already_posted
    main.candidate_already_posted = lambda _job: False
    try:
        selected = main.select_private_jobs_by_category(jobs, 10, minimum_required=10)
    finally:
        main.candidate_already_posted = old
    assert len(selected) == 10


def test_source_page_chrome_is_removed_from_title_and_company():
    item = {
        "title": "Executive/ Sr. Executive (Sales & Marketing)",
        "company": "Northsouth Group",
        "source": "Bdjobs",
        "url": "https://jobs.bdjobs.com/jobdetails/?id=700001&ln=1",
    }
    text = """
Executive/ Sr. Executive (Sales & Marketing) : Northsouth Group || Bdjobs.com
🏢 name-share-details.gif ## Executive/ Sr. Executive (Sales & Marketing)
Job Summary
Company Name
Northsouth Group
Deadline
2026-10-19
Posted
2026-09-19
"""
    parsed = main.extract_job_fields(text, "", item["url"], item)
    assert parsed["title"] == "Executive/ Sr. Executive (Sales & Marketing)"
    assert parsed["company"] == "Northsouth Group"
