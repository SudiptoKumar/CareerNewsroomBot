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
        if "jobdetails.asp" in url or "jobdetailsbn.asp" in url:
            return None
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
    assert any(kind == "jina" for kind, _ in calls)


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


def test_current_bdjobs_listing_labels_without_colons_are_parsed():
    text = (
        "Sales Representative (Odoo) Sysnova Information Systems "
        "Image: Job LocationDhaka Image: Experience required 0 to 1 year(s) "
        "Image: Deadline for apply the job 21 Sep 2026"
    )
    fields = main._extract_listing_baseline(text)
    assert fields["location"] == "Dhaka"
    assert fields["experience"] == "0 to 1 year"
    assert fields["deadline"] == "2026-09-21"


def test_merge_preserves_listing_fields_when_detail_only_has_deadline():
    item = make_listing_item()
    merged = main.merge_job_fields(
        {"title": "Accounts Executive", "company": "Example Finance Ltd.", "deadline": "2026-10-01"},
        {
            "company": "Example Finance Ltd.", "location": "Dhaka", "education": "BBA",
            "experience": "1 to 2 years", "salary": "Tk. 30,000", "vacancy": "3",
            "employment_type": "Full Time", "workplace": "On-site", "age": "23-30 Years",
            "application_method": "Online", "deadline": "2026-10-01", "posted_date": "2026-09-19",
        },
        item,
    )
    assert merged["location"] == "Dhaka"
    assert merged["education"] == "BBA"
    assert merged["experience"] == "1 to 2 years"
    assert merged["salary"] == "Tk. 30,000"
    assert merged["vacancy"] == "3"
    assert merged["employment_type"] == "Full Time"
    assert merged["workplace"] == "On-site"
    assert merged["age"] == "23-30 Years"
    assert merged["application_method"] == "Online"
    assert merged["source_url"].startswith("https://jobs.bdjobs.com/")


def test_sparse_snapshot_is_rejected_before_publish():
    job = {
        "title": "Investment Officer",
        "company": "Example Bank PLC",
        "source": "Bdjobs",
        "source_url": "https://bdjobs.com/h/details/123456?ln=1",
        "deadline": "2026-09-30",
        "is_government": False,
    }
    ok, reason = main.snapshot_integrity(job)
    assert not ok
    assert reason in {"private_missing_core_location", "snapshot_too_sparse:1"}


def test_rich_snapshot_accepts_source_backed_fields():
    job = {
        "title": "Investment Officer",
        "company": "Example Bank PLC",
        "source": "Bdjobs",
        "source_url": "https://bdjobs.com/h/details/123456?ln=1",
        "location": "Dhaka", "experience": "1 to 2 Years", "education": "BBA/MBA",
        "salary": "Negotiable", "vacancy": "3", "deadline": "2026-09-30", "posted_date": "2026-09-19",
        "is_government": False,
    }
    ok, reason = main.snapshot_integrity(job)
    assert ok and reason == "ok"
    assert main.snapshot_field_quality(job) >= 6


def test_bangla_job_summary_extracts_all_snapshot_fields():
    html = """
    <html><body>
    <h1>Investment Officer</h1>
    <p>প্রতিষ্ঠানের নাম</p><p>Example Bank PLC</p>
    <p>চাকরির সারসংক্ষেপ</p>
    <p>খালি পদ</p><p>3</p>
    <p>বয়স</p><p>২৪ থেকে ৩০ বছর</p>
    <p>কর্মস্হল</p><p>Dhaka</p>
    <p>বেতন</p><p>আলোচনা সাপেক্ষ</p>
    <p>অভিজ্ঞতা</p><p>১ থেকে ২ বছর</p>
    <p>প্রকাশ তারিখ</p><p>19 Sep 2026</p>
    <p>শেষ তারিখ</p><p>30 Sep 2026</p>
    <p>শিক্ষাগত যোগ্যতা</p><p>BBA/MBA</p>
    <p>কর্মক্ষেত্র</p><p>অফিসে</p>
    <p>চাকরির ধরন</p><p>ফুল টাইম</p>
    </body></html>
    """
    parsed = main.extract_job_fields(
        main._text_from_html(html), html,
        "https://bdjobs.com/h/details/123457?ln=1",
        {"title": "Investment Officer", "listing_fields": {}},
    )
    assert parsed["company"] == "Example Bank PLC"
    assert parsed["vacancy"] == "3"
    assert parsed["age"] == "24-30 Years"
    assert parsed["location"] == "Dhaka"
    assert parsed["experience"] == "1 to 2 years"
    assert parsed["posted_date"] == "2026-09-19"
    assert parsed["deadline"] == "2026-09-30"
    assert parsed["employment_type"] == "Full Time"
    assert parsed["workplace"] == "On-site"


def test_screenshot_style_detail_fallback_keeps_rich_snapshot(monkeypatch):
    item = make_listing_item()
    item["listing_fields"] = {
        "company": "Chino Carts",
        "location": "Dhaka (Dakshinkhan)",
        "experience": "At Least 1 Year",
        "education": "BBA",
        "salary": "Negotiable",
        "vacancy": "5",
        "age": "23 Years",
        "application_method": "Online",
        "deadline": "2026-09-25",
        "posted_date": "2026-09-19",
    }
    item["title"] = "Customer Support Executive E-Commerce"
    item["url"] = "https://jobs.bdjobs.com/jobdetails/?id=888001&ln=1"
    item["source_url"] = item["url"]
    item["source_job_id"] = "888001"
    monkeypatch.setattr(main, "_fetch_bdjobs_detail", lambda _item: None)
    job = main.research_job(item)
    assert job["company"] == "Chino Carts"
    rows = dict(main.job_snapshot_rows(job))
    assert rows["Location"] == "Dhaka (Dakshinkhan)"
    assert rows["Experience"] == "At Least 1 Year"
    assert rows["Salary"] == "Negotiable"
    assert rows["Vacancy"] == "5"
    assert rows["Age"] == "23 Years"
    assert rows["Application"] == "Online"
    assert rows["Deadline"] == "25-09-2026"
    assert rows["Posted"] == "19-09-2026"
    # The item excerpt still contains an old vacancy value (3); fallback parsing
    # must never override the authoritative listing_fields value (5).
    assert job["vacancy"] == "5"
    ok, reason = main.snapshot_integrity(job)
    assert ok and reason == "ok"
    assert main.snapshot_field_quality(job) >= 8


def test_snapshot_eligibility_happens_before_quota_selection():
    sparse_private = {
        "title": "Investment Officer", "company": "Example Bank PLC", "source": "Bdjobs",
        "source_url": "https://bdjobs.com/h/details/1?ln=1", "deadline": "2026-09-30",
        "final_score": 95, "is_government": False,
    }
    rich_private = dict(sparse_private, source_job_id="2", source_url="https://bdjobs.com/h/details/2?ln=1",
        location="Dhaka", experience="0 to 2 years", education="BBA", salary="Negotiable", posted_date="2026-09-19")
    sparse_gov = {
        "title": "Government Officer", "company": "Government Organization", "source": "Teletalk",
        "source_url": "https://alljobs.teletalk.com.bd/", "deadline": "2026-09-30",
        "final_score": 90, "is_government": True,
    }
    rich_gov = dict(sparse_gov, source_job_id="4", location="Dhaka", vacancy="5")
    private, government, rejected = main.split_snapshot_eligible([sparse_private, rich_private, sparse_gov, rich_gov])
    assert rejected == 2
    assert [j["source_job_id"] for j in private] == ["2"]
    assert [j["source_job_id"] for j in government] == ["4"]


def test_current_bdjobs_sibling_card_window_recovers_listing_fields():
    html = """
    <html><body>
      <section class="job-card">
        <div class="title-row">
          <h3><a href="/jobdetails/?id=600001&ln=1">Executive - Digital Marketing</a></h3>
        </div>
        <div class="company-row">ESNL EVERGREEN AGRO ECO RESORT LIMITED</div>
        <div class="meta"><span>Image: Job Location</span><strong>Anywhere in Bangladesh</strong></div>
        <div class="meta"><span>Image: Experience required</span><strong>At least 2 year(s)</strong></div>
        <div class="meta"><span>Image: Deadline for apply the job</span><strong>25 Sep 2026</strong></div>
        <div class="meta"><span>Image: Education required</span><ul><li>Minimum Bachelor's degree in any discipline</li></ul></div>
      </section>
      <section class="job-card">
        <div class="title-row">
          <h3><a href="/jobdetails/?id=600002&ln=1">Management Trainee</a></h3>
        </div>
        <div class="company-row">Example Bank PLC</div>
        <div class="meta"><span>Image: Job Location</span><strong>Dhaka</strong></div>
        <div class="meta"><span>Image: Experience required</span><strong>0 to 1 year(s)</strong></div>
        <div class="meta"><span>Image: Deadline for apply the job</span><strong>30 Sep 2026</strong></div>
        <div class="meta"><span>Image: Education required</span><ul><li>BBA / MBA preferred</li></ul></div>
      </section>
    </body></html>
    """
    rows = main._bdjobs_listing_candidates(
        html,
        "https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=1",
        1,
        "Accounting / Finance",
    )
    assert len(rows) == 2
    first = rows[0]["listing_fields"]
    assert rows[0]["company"] == "ESNL EVERGREEN AGRO ECO RESORT LIMITED"
    assert first["location"] == "Anywhere in Bangladesh"
    assert first["experience"] == "At least 2 year"
    assert first["deadline"] == "2026-09-25"
    assert "Bachelor" in first["education"]
    second = rows[1]["listing_fields"]
    assert rows[1]["company"] == "Example Bank PLC"
    assert second["location"] == "Dhaka"
    assert second["experience"] == "0 to 1 year"
    assert second["deadline"] == "2026-09-30"
    assert second["education"] == "BBA/MBA"


def test_current_bdjobs_window_produces_publishable_private_record():
    html = """
    <section class="job-card">
      <h3><a href="/jobdetails/?id=600003&ln=1">Business Development Executive</a></h3>
      <div>ABC Business Ltd.</div>
      <div>Job Location</div><div>Dhaka</div>
      <div>Experience required</div><div>0 to 2 year(s)</div>
      <div>Deadline for apply the job</div><div>30 Sep 2026</div>
      <div>Education required</div><div>Bachelor's degree in any discipline</div>
    </section>
    """
    rows = main._bdjobs_listing_candidates(
        html,
        "https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=3",
        3,
        "Commercial / Supply Chain",
    )
    assert rows
    item = rows[0]
    # Simulate the same fallback path used when a current Bdjobs detail page
    # returns the Angular shell.
    old = main._fetch_bdjobs_detail
    main._fetch_bdjobs_detail = lambda _item: None
    try:
        job = main.research_job(item)
    finally:
        main._fetch_bdjobs_detail = old
    assert job["company"] == "ABC Business Ltd."
    assert job["location"] == "Dhaka"
    assert job["experience"] == "0 to 2 year"
    assert job["education"] == "Bachelor's"
    assert job["deadline"] == "2026-09-30"
    ok, reason = main.snapshot_integrity(job)
    assert ok, reason
    assert main.deterministic_job_gate(job)[0]


def test_bdjobs_search_page_sequence_with_image_labels_is_rich():
    html = """
    <div class="job-card">
      <div><a href="/jobdetails/?id=700001&ln=1">Relationship Officer</a></div>
      <div>United Commercial Bank PLC</div>
      <div><img alt="Job Location" src="/x.gif"></div><div>Anywhere in Bangladesh</div>
      <div><img alt="Experience required" src="/x.gif"></div><div>At least 3 year(s)</div>
      <div><img alt="Deadline for apply the job" src="/x.gif"></div><div>15 Oct 2026</div>
      <div><img alt="Education required" src="/x.gif"></div><div>4-year Bachelor's degree in any discipline</div>
    </div>
    <div class="job-card">
      <div><a href="/jobdetails/?id=700002&ln=1">Sales Executive</a></div>
      <div>Example Company Ltd.</div>
      <div><img alt="Job Location" src="/x.gif"></div><div>Dhaka</div>
      <div><img alt="Experience required" src="/x.gif"></div><div>0 to 2 year(s)</div>
      <div><img alt="Deadline for apply the job" src="/x.gif"></div><div>30 Sep 2026</div>
      <div><img alt="Education required" src="/x.gif"></div><div>BBA / MBA</div>
    </div>
    """
    rows = main._bdjobs_listing_candidates(
        html,
        "https://jobs.bdjobs.com/jobsearch-cache.asp?fcatId=2",
        2,
        "Bank / Non-Bank Financial Institution",
    )
    assert len(rows) == 2
    for row in rows:
        fields = row["listing_fields"]
        assert row["company"]
        assert fields["location"]
        assert fields["experience"]
        assert fields["deadline"]
        assert fields["education"]
        assert sum(bool(fields.get(k)) for k in ("location","education","experience","deadline")) >= 3



def test_current_bdjobs_detail_full_snapshot_and_identity():
    text = """Partners in Health and Development (PhD)
Officer Procurement and Supply Chain
Application Deadline : 22 Sep 2026
Vacancy: 1
Age:
(no age requirement shown here)
Location: Anywhere in Bangladesh
Salary: Negotiable
Experience: At least 3 years
Published: 17 Sep 2026
Requirements
Education
Bachelor's
Workplace
Work at office
Employment Status
Full Time"""
    parsed = main.extract_job_fields(
        text, "", "https://bdjobs.com/h/details/1535054?ln=1",
        {"title":"Officer Procurement and Supply Chain", "listing_fields":{}},
    )
    assert parsed["company"] == "Partners in Health and Development (PhD)"
    assert parsed["location"] == "Anywhere in Bangladesh"
    assert parsed["salary"] == "Negotiable"
    assert parsed["experience"] == "At least 3 years"
    assert parsed["vacancy"] == "1"
    assert parsed["posted_date"] == "2026-09-17"
    assert parsed["deadline"] == "2026-09-22"
    assert parsed["employment_type"] == "Full Time"
    assert parsed["workplace"] == "On-site"
    assert parsed["age"] == ""


def test_age_never_reuses_experience_and_location_logo_text_is_removed():
    assert main.compact_age("At least 3 years") == ""
    assert main.compact_age("2 to 3 years") == ""
    assert main.compact_age("24 to 28 years") == "24-28 Years"
    assert main.compact_age("At most 35 years") == "At most 35 Years"
    assert main.compact_location("Dhaka Logo of Averroes International School") == "Dhaka"
    assert main.compact_location("Logo of Averroes International School") == ""


def test_bdjobs_shell_then_legacy_detail_fallback(monkeypatch):
    item = make_listing_item()
    calls=[]
    def fake_curl(url, **kwargs):
        calls.append(url)
        if "/h/details/" in url:
            return {"ok":True,"status":200,"text":"<html><app-root></app-root></html>","url":url,"backend":"curl_cffi:safari18_0_ios","cloudflare":False}
        if "/hn/details/" in url:
            return {"ok":True,"status":200,"text":"<html><app-root></app-root></html>","url":url,"backend":"curl_cffi:safari18_0_ios","cloudflare":False}
        return {"ok":True,"status":200,"text":"Job Summary\nCompany Name\nExample Finance Ltd.\nEducation\nBBA\nExperience\n1 to 2 years\nVacancy\n3\nDeadline\n2026-10-01\nPublished\n2026-09-19\nLocation\nDhaka\nSalary\nTk. 30000","url":url,"backend":"curl_cffi:safari184_ios","cloudflare":False}
    def fail_jina(*args, **kwargs):
        raise AssertionError("Jina should not run when the legacy route succeeds")
    monkeypatch.setattr(main, "_fetch_with_curl", fake_curl)
    monkeypatch.setattr(main, "_fetch_jina", fail_jina)
    main.DETAIL_CACHE.clear()
    result=main._fetch_bdjobs_detail(item)
    assert result and "jobdetails.asp" in result["detail_route"]
    assert any("/h/details/" in x for x in calls)
    assert any("/hn/details/" in x for x in calls)
    assert any("jobdetails.asp" in x for x in calls)


def test_apply_button_always_uses_apply_now():
    markup=main._button_markup({"apply_url":"","source_url":"https://bdjobs.com/h/details/1?ln=1"})
    assert markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    markup2=main._button_markup({"apply_url":"https://example.com/apply","source_url":"https://bdjobs.com/h/details/1?ln=1"})
    assert markup2["inline_keyboard"][0][0]["text"] == "APPLY NOW"


def test_source_aware_hashtags_for_government_and_internship():
    gov={"is_government":True,"title":"Assistant Officer","source":"Teletalk"}
    assert main.job_hashtags(gov) == ["#GovtJob"]
    internship={"is_government":False,"title":"Accounts Intern","employment_type":"Internship","career_category":"Finance & Accounting"}
    tags=main.job_hashtags(internship)
    assert "#Internship" in tags
    assert "#Finance" in tags


def test_current_bdjobs_screenshot_style_snapshot_all_fields_and_correct_age():
    text = """ARTEK
HR Executive
Application Deadline : 17 Oct 2026
Vacancy: 01
Age:
26 to 28 years
Location:
Dhaka
Salary: Tk. 20000 - 25000 (Monthly)
Experience:
2 to 3 years
Published: 17 Sep 2026
Requirements
Education
Bachelor of Business Administration (BBA)
Workplace
Work at office
Employment Status
Full Time
Job Location
Dhaka"""
    parsed = main.extract_job_fields(
        text, "", "https://bdjobs.com/h/details/1539999?ln=1",
        {"title": "HR Executive", "listing_fields": {"company": "ARTEK"}},
    )
    assert parsed["company"] == "ARTEK"
    assert parsed["location"] == "Dhaka"
    assert parsed["salary"] == "Tk. 20000 - 25000/month"
    assert parsed["vacancy"] == "01"
    assert parsed["experience"] == "2 to 3 years"
    assert parsed["age"] == "26-28 Years"
    assert parsed["posted_date"] == "2026-09-17"
    assert parsed["deadline"] == "2026-10-17"
    assert parsed["education"] in {"BBA", "BBA/Bachelor's"}


def test_browser_fallback_is_used_for_angular_shell(monkeypatch):
    item = make_listing_item()
    main.DETAIL_CACHE.clear()
    monkeypatch.setattr(main, "_fetch_with_curl", lambda *a, **k: {
        "ok": True, "status": 200,
        "text": "<html><body><app-root></app-root></body></html>",
        "url": item["url"], "backend": "curl_cffi:safari18_0_ios", "cloudflare": False,
    })
    class FakeBody:
        def get_all_text(self, strip=True):
            return "Example Finance Ltd.\nAccounts Executive\nApplication Deadline : 01 Oct 2026\nVacancy: 3\nAge: 24 to 30 years\nLocation: Dhaka\nSalary: Tk. 30000\nExperience: 1 to 2 years\nPublished: 19 Sep 2026\nRequirements\nEducation\nBBA\nWorkplace\nWork at office\nEmployment Status\nFull Time"
    class FakePage:
        def css(self, selector):
            return [FakeBody()] if selector == "body" else []
    class FakeFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            return FakePage()
    monkeypatch.setattr(main, "StealthyFetcher", FakeFetcher)
    monkeypatch.setattr(main, "_fetch_jina", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Jina should not run after browser success")))
    main.SCRAPLING_BROWSER_FETCH_COUNT = 0
    result = main._fetch_bdjobs_detail(item)
    assert result is not None
    assert result["backend"] == "scrapling_stealthy"
    assert result["detail_quality"] == "browser_valid"


def test_final_selector_guarantees_private_government_and_internship_minima():
    private=[]
    for i in range(14):
        private.append({
            "source":"Bdjobs", "source_job_id":str(500+i), "canonical":f"https://bdjobs.com/h/details/{500+i}",
            "title":"Accounts Internship" if i < 2 else f"Finance Executive {i}",
            "company":f"Company {i}", "location":"Dhaka", "education":"BBA",
            "experience":"Freshers" if i < 2 else "0 to 2 years", "salary":"Negotiable",
            "vacancy":"1", "age":"20-30 Years", "deadline":"2026-10-01", "posted_date":"2026-09-19",
            "employment_type":"Internship" if i < 2 else "Full Time", "career_category":"Finance & Accounting",
            "is_government":False, "final_score":82-i, "deterministic_score":82-i,
            "information_quality":7,
        })
    government=[{
        "source":"Teletalk", "source_job_id":str(800+i), "canonical":f"https://alljobs.teletalk.com.bd/?job_primary_id={800+i}",
        "title":f"Govt Job {i}", "company":"Government Office", "location":"Dhaka", "vacancy":"1",
        "deadline":"2026-10-01", "posted_date":"2026-09-19", "is_government":True, "final_score":90-i,
    } for i in range(4)]
    selected=main.select_final_jobs(private, government)
    assert len(selected) <= 20
    assert sum(not j.get("is_government") for j in selected) >= 10
    assert sum(j.get("is_government") for j in selected) >= 3
    assert sum(main.is_internship_job(j) for j in selected if not j.get("is_government")) >= 2


def test_job_snapshot_rows_keep_source_fields_and_omit_only_missing_values():
    job = {
        "location": "Dhaka", "employment_type": "Full Time", "workplace": "On-site",
        "education": "BBA", "experience": "2 to 3 years", "salary": "Tk. 20000 - 25000/month",
        "vacancy": "01", "age": "26-28 Years", "application_method": "Online",
        "deadline": "2026-10-17", "posted_date": "2026-09-17",
    }
    rows = dict(main.job_snapshot_rows(job))
    for label in ("Location","Employment","Workplace","Education","Experience","Salary","Vacancy","Age","Application","Deadline","Posted"):
        assert label in rows
    assert rows["Age"] == "26-28 Years"
    assert rows["Salary"] == "Tk. 20000 - 25000/month"
    assert rows["Vacancy"] == "01"
    assert rows["Posted"] == "17-09-2026"



def test_live_style_bdjobs_source_fields_are_separate():
    text="""
    Averroes International School
    Logistics Executive
    Application Deadline :
    17 Oct 2026
    Vacancy: 01
    Age: 26 to 28 years
    Location: Dhaka
    Salary: Negotiable
    Experience: 2 to 3 years
    Published: 17 Sep 2026
    Requirements
    Education
    Bachelor of Business Administration (BBA)
    Workplace
    Work at office
    Employment Status
    Full Time
    """
    job=main.extract_job_fields(text,"","https://bdjobs.com/h/details/999001?ln=1",{"title":"Logistics Executive"})
    assert job["company"]=="Averroes International School"
    assert job["location"]=="Dhaka"
    assert job["vacancy"]=="01"
    assert job["salary"]=="Negotiable"
    assert job["age"]=="26-28 Years"
    assert job["experience"]=="2 to 3 years"
    assert job["posted_date"]=="2026-09-17"
    assert job["deadline"]=="2026-10-17"


def test_jina_uses_current_server_rendered_route_first(monkeypatch):
    item=make_listing_item()
    calls=[]
    def fake_curl(url, **kwargs):
        calls.append(("curl",url))
        return {"ok":True,"status":200,"text":"Just a moment...","url":url,"backend":"curl_cffi:safari18_0_ios","cloudflare":True}
    def fake_jina(url, **kwargs):
        calls.append(("jina",url))
        return {"ok":True,"status":200,"text":"Company\nExample Finance Ltd.\nManagement Trainee\nApplication Deadline :\n1 Oct 2026\nVacancy: 3\nAge: 24 to 30 years\nLocation: Dhaka\nSalary: Negotiable\nExperience: 1 to 2 years\nPublished: 19 Sep 2026\nEducation\nBBA","url":url,"backend":"jina_reader","cloudflare":False}
    monkeypatch.setattr(main,"_fetch_with_curl",fake_curl)
    monkeypatch.setattr(main,"_fetch_jina",fake_jina)
    main.DETAIL_CACHE.clear()
    result=main._fetch_bdjobs_detail(item)
    assert result["backend"]=="jina_reader"
    jina_urls=[url for kind,url in calls if kind=="jina"]
    assert jina_urls and "/hn/details/777001" in jina_urls[0]


def test_internship_snapshot_can_omit_experience():
    job={
        "title":"Finance Internship","company":"Example Bank","location":"Dhaka",
        "experience":"","education":"BBA","salary":"Negotiable","vacancy":"2",
        "deadline":"2026-10-01","posted_date":"2026-09-19","source_url":"https://bdjobs.com/h/details/1",
        "is_government":False,
    }
    ok,reason=main.snapshot_integrity(job)
    assert ok,reason
    assert "#Internship" in main.job_hashtags(job)


def test_apply_button_never_says_read_more():
    markup=main._button_markup({"source_url":"https://bdjobs.com/h/details/1","apply_url":""})
    assert markup["inline_keyboard"][0][0]["text"]=="APPLY NOW"


def test_flattened_bdjobs_fields_do_not_cross_contaminate():
    text = "Company Name: Example Bank Vacancy: 01 Age: 26 to 28 years Location: Dhaka Salary: Negotiable Experience: 2 to 3 years Published: 17 Sep 2026 Deadline: 17 Oct 2026"
    fields = main.extract_job_fields(
        text, "", "https://bdjobs.com/h/details/999010?ln=1", {"title": "HR Executive"}
    )
    assert fields["vacancy"] == "01"
    assert fields["age"] == "26-28 Years"
    assert fields["location"] == "Dhaka"
    assert fields["salary"] == "Negotiable"
    assert fields["experience"] == "2 to 3 years"
    assert fields["posted_date"] == "2026-09-17"
    assert fields["deadline"] == "2026-10-17"
