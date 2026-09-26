from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from core.config import Config
from core.http import HttpClient
from core.models import JobRecord
from core.parsing import parse_experience_bounds, parse_age_bounds
from core.pipeline import Pipeline
from core.rules import date_gate, experience_gate, age_gate, bba_mba_gate, hard_eligibility
from core.state import StateStore
from core.utils import canonical_url, iso_date
from sources.bdjobs import BdjobsAdapter
from sources.bdjobslive import BdjobsLiveAdapter

TODAY = date(2026, 9, 27)

BDJOBS_DETAIL_FIXTURE = """
<html><head>
<title>Executive - Accounts And Finance : A Reputed Woven Garments Manufacturing Unit || Bdjobs.com</title>
<meta property="og:url" content="https://bdjobs.com/h/details/1538003">
<meta property="og:title" content="Executive - Accounts And Finance : A Reputed Woven Garments Manufacturing Unit || Bdjobs.com">
</head><body>
<div class="header"><button><h2>A Reputed Woven Garments Manufacturing Unit</h2></button><h2>Executive - Accounts And Finance</h2>
<p>Application Deadline :</p><p>26 Oct 2026</p></div>
<div id="allSection">
<div><span>Vacancy:</span><span>01</span></div>
<div><span>Age:</span><span>At least 25 years</span></div>
<div><span>Location:</span><span>Dhaka (Tejgaon)</span></div>
<div><span>Salary:</span><span>Negotiable</span></div>
<div><span>Experience:</span><span>At least 1 year</span></div>
<div><span>Published:</span><span>26 Sep 2026</span></div>
<div><span>Employment Status:</span><span>Full Time</span></div>
<div><span>Job Work Place:</span><span>Work at office</span></div>
</div>
<div id="requirements">
<h3>Education</h3><ul><li>Bachelor of Business Administration (BBA) in Accounting, Finance</li></ul>
<h3>Experience</h3><ul><li>At least 1 year</li></ul>
<h3>Additional Requirements</h3><ul><li>Age At least 25 years</li><li>Garments, Group of Companies</li></ul>
</div>
<div id="responsibilitiesSection"><h3>Responsibilities &amp; Context</h3><ul><li>Check and post handover documents/vouchers.</li><li>Record petty cash entries in EBS and Tally.</li></ul></div>
<div id="skills"><h3>Skills &amp; Expertise</h3><button>Communication and interpersonal skill</button></div>
<div id="salary"><h3>Compensation &amp; Other Benefits</h3><ul><li>Provident fund,Mobile bill</li><li>Festival Bonus: 2</li></ul></div>
<button data-testid="applyNowBtn">Apply Now</button>
</body></html>
"""

BDJOBSLIVE_DETAIL_FIXTURE = """
<html><head>
<title>Outlet Cashier | Gentle Park - BDJobs Live</title>
<meta name="description" content="Gentle Park is looking for a Outlet Cashier. Category Accounting/ Finance.">
</head><body><main>
<h1>Outlet Cashier</h1><a href="/company-detail/gentle-park-3565">Gentle Park</a>
<p>Application Deadline : 24 Oct 2026</p>
<div class="summary">
<div><span>Vacancy:</span><span>5</span></div>
<div><span>Age:</span><span>22 to 35 Years</span></div>
<div><span>Location:</span><span>Dhaka, Dhanmondi</span></div>
<div><span>Salary:</span><span>18000 - 20000 BDT</span></div>
<div><span>Experience:</span><span>1-2 Year</span></div>
<div><span>Job Type:</span><span>Full Time/Permanent</span></div>
<div><span>Job Shift:</span><span>Day Shift</span></div>
<div><span>Published:</span><span>24 Sept 2026</span></div>
</div>
<section id="section-education"><h2>Education</h2><ul><li>Higher Secondary, HSC</li></ul></section>
<section id="section-experience"><h2>Experience</h2><ul><li>1-2 Year</li></ul></section>
<section id="section-skills"><h2>Skills</h2><ul><li>Accounting</li><li>Cashier</li><li>Cash management</li></ul></section>
<section id="section-responsibilities"><h2>Responsibilities &amp; Context</h2><ul><li>Handle customer payments efficiently.</li><li>Maintain accurate records and counts.</li></ul></section>
<section id="section-company"><h2>Company Information</h2><p>Gentle Park</p><p>Industry: Fashion &amp; Jewelry</p><p>Address: Ahmed Tower (9th floor), 28,30, Kamal Ataturk, Banani, Dhaka-1213</p><p>Website: <a href="http://gentlepark.com">http://gentlepark.com</a></p></section>
<button>Apply Now</button>
</main></body></html>
"""


def job(**kwargs):
    base = dict(source="Bdjobs", source_job_id="1", title="Management Trainee", company="Example Bank",
                source_url="https://bdjobs.com/h/details/1", canonical_url=canonical_url("https://bdjobs.com/h/details/1"),
                published_date="2026-09-27", deadline="2026-10-01", education="BBA", experience="1-2 years",
                age="18 to 30 years", location="Dhaka", vacancy="2", extraction_status="detail_validated",
                raw_text="Management Trainee BBA accounting finance", discovery_url="https://bdjobs.com/h/jobs?lang=en&fcatId=1")
    base.update(kwargs)
    return JobRecord(**base)


def run_self_test():
    cfg = Config()
    assert cfg.max_post_age_days == 3
    assert cfg.private_target == 10 and cfg.government_target == 5 and cfg.internship_target == 4
    assert cfg.flexible_extras == 6 and cfg.hard_max_posts == 25

    # Calendar-day freshness, not rolling 72 hours.
    assert date_gate(job(published_date="2026-09-27"), TODAY, cfg)[0]
    assert date_gate(job(published_date="2026-09-26"), TODAY, cfg)[0]
    assert date_gate(job(published_date="2026-09-25"), TODAY, cfg)[0]
    assert date_gate(job(published_date="2026-09-24"), TODAY, cfg)[1] == "too_old"
    assert date_gate(job(published_date=""), TODAY, cfg)[1] == "published_date_unknown"
    assert date_gate(job(published_date="2026-09-28"), TODAY, cfg)[1] == "future_published_date"
    assert date_gate(job(deadline=""), TODAY, cfg)[1] == "deadline_unknown"
    assert hard_eligibility(job(deadline=""), today=TODAY, config=cfg, published_events=[])[1] == "deadline_unknown"
    assert date_gate(job(deadline="2026-09-26"), TODAY, cfg)[1] == "deadline_expired"

    # Experience boundaries.
    for value in ("Fresher", "0 Year", "1 year", "1-2 Year", "2-3 years", "3 years"):
        assert experience_gate(job(experience=value), cfg)[0], value
    for value in ("3+ years", "At least 3 years", "3 years or more", "more than 3 years", "3-5 years", "4 years"):
        assert experience_gate(job(experience=value), cfg)[1] == "experience_above_3_years", value
    assert parse_experience_bounds("At least 3 years")[2] == "open_min"

    # Age intersection with allowed 18-30.
    assert age_gate(job(age="18 to 30 years"), cfg)[0]
    assert age_gate(job(age="18 to 28 years"), cfg)[0]
    assert age_gate(job(age="20 to 30 years"), cfg)[0]
    assert age_gate(job(age="18 to 35 years"), cfg)[1] == "age_incompatible"
    assert age_gate(job(age="25 to 32 years"), cfg)[1] == "age_incompatible"
    assert parse_age_bounds("22 to 35 Years") == (22, 35)

    # Source-specific Bdjobs detail extraction.
    http = HttpClient(cfg)
    bd = BdjobsAdapter(http, cfg)
    bjob = job(source_job_id="1538003", title="Executive - Accounts And Finance", company="A Reputed Woven Garments Manufacturing Unit",
               source_url="https://bdjobs.com/h/details/1538003", canonical_url=canonical_url("https://bdjobs.com/h/details/1538003"))
    # Test parser through direct HTML fixture, without network.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(BDJOBS_DETAIL_FIXTURE, "html.parser")
    # Monkeypatch fetch to ensure enrich uses fixture.
    original = http.fetch
    http.fetch = lambda *args, **kwargs: type("R", (), {"ok": True, "url": bjob.source_url, "text": BDJOBS_DETAIL_FIXTURE, "backend": "fixture"})()
    enriched = bd.enrich(bjob)
    http.fetch = original
    assert enriched.published_date == "2026-09-26"
    assert enriched.deadline == "2026-10-26"
    assert "BBA" in enriched.education
    assert enriched.salary == "Negotiable"
    assert enriched.vacancy == "01"
    assert enriched.company == "A Reputed Woven Garments Manufacturing Unit"
    assert enriched.responsibilities

    # Source-specific BDJobs Live detail extraction.
    live = __import__("sources.bdjobslive", fromlist=["BdjobsLiveAdapter"]).BdjobsLiveAdapter(http, cfg)
    lj = job(source="BDJobs Live", source_job_id="13513", title="Outlet Cashier", company="Gentle Park",
             source_url="https://www.bdjobslive.com/bdjobs-details/outlet-cashier-13513", canonical_url=canonical_url("https://www.bdjobslive.com/bdjobs-details/outlet-cashier-13513"),
             published_date="", deadline="")
    original = http.fetch
    http.fetch = lambda *args, **kwargs: type("R", (), {"ok": True, "url": lj.source_url, "text": BDJOBSLIVE_DETAIL_FIXTURE, "backend": "fixture"})()
    enriched2 = live.enrich(lj)
    http.fetch = original
    assert enriched2.published_date == "2026-09-24"
    assert enriched2.deadline == "2026-10-24"
    assert enriched2.salary == "18000 - 20000 BDT"
    assert enriched2.experience == "1-2 Year"
    assert enriched2.job_type == "Full Time/Permanent"
    assert enriched2.company_address.startswith("Ahmed Tower")
    assert enriched2.company_website == "http://gentlepark.com"

    # Category card discovery selectors from supplied specifications.
    category_bd = """
    <html><body><app-job-list>
      <app-job-card><a href="https://bdjobs.com/h/details/1538003"><p data-testid="job-title">Executive - Accounts And Finance</p><span>Example Garments</span></a><p>Location: Dhaka</p><p>Experience: 1 to 2 years</p><p>Education: BBA</p><p>Deadline: 26 Oct 2026</p></app-job-card>
    </app-job-list></body></html>
    """
    cards = bd._parse_listing(category_bd, "https://bdjobs.com/h/jobs?lang=en&fcatId=1", "Accounting / Finance", "https://bdjobs.com/h/jobs?lang=en&fcatId=1")
    assert len(cards) == 1 and cards[0].source_job_id == "1538003"

    category_live = """
    <html><body>
      <div class="card"><a href="https://www.bdjobslive.com/bdjobs-details/outlet-cashier-13513"><h3>Outlet Cashier</h3><span>Gentle Park</span></a><div>Full Time/Permanent</div><div>Dhaka, Dhanmondi</div><div>1-2 Year</div><div>Higher Secondary, HSC</div><div>Deadline: 24 Oct 2026</div></div>
    </body></html>
    """
    live_cards = live._parse_listing(category_live, "https://www.bdjobslive.com/bdjobs-circular/accounting-finance-jobs", "Accounting / Finance", "https://www.bdjobslive.com/bdjobs-circular/accounting-finance-jobs")
    assert len(live_cards) == 1 and live_cards[0].source_job_id == "13513"

    # BBA relevance / profession rules.
    assert bba_mba_gate(job(title="Accounts Executive", education="BBA in Finance"))[0]
    assert bba_mba_gate(job(title="Software Engineer", education="BBA"))[1] == "clearly_unrelated_profession"
    assert bba_mba_gate(job(title="Random Position", education="Higher Secondary", category="Random", raw_text="Random Position Higher Secondary"))[1] == "bba_mba_relevance_missing"

    # Selection is opportunistic: no minimums, but target ceilings and hard maximum are enforced.
    with tempfile.TemporaryDirectory() as td:
        state = StateStore(cfg, Path(td))
        pipe = Pipeline(cfg, state, http)
        small = [job(source_job_id=str(i), title=f"Accounts Executive {i}", canonical_url=canonical_url(f"https://bdjobs.com/h/details/{i}")) for i in range(3)]
        selected_small = pipe.select(small)
        assert len(selected_small) == 3
        many = [job(source_job_id=str(100+i), title=f"Accounts Executive {100+i}", canonical_url=canonical_url(f"https://bdjobs.com/h/details/{100+i}")) for i in range(30)]
        selected_many = pipe.select(many)
        assert len(selected_many) <= 25

    # Absolute pre-AI contract: unknown dates can never pass.
    with tempfile.TemporaryDirectory() as td:
        state = StateStore(cfg, Path(td))
        pipe = Pipeline(cfg, state, http)
        bad = job(published_date="", deadline="2026-10-01")
        try:
            pipe.ai_rank([bad])
            raise AssertionError("unknown published date reached AI")
        except RuntimeError as exc:
            assert "PRE_AI_INVARIANT_FAILED" in str(exc)

    # State persistence: preserve arbitrary existing keys and posted history.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "news_state.json").write_text('{"format_version": 5, "events": {"x": {"status": "published"}}, "legacy": {"keep": true}}', encoding="utf-8")
        (p / "posted_urls.txt").write_text("https://bdjobs.com/h/details/9\n", encoding="utf-8")
        state = StateStore(cfg, p)
        assert state.data["legacy"]["keep"] is True
        assert canonical_url("https://bdjobs.com/h/details/9") in state.posted_urls

    # Unauthorized provenance must not be eligible even if the detail URL looks valid.
    unauthorized = job(discovery_url="https://bdjobs.com/")
    assert hard_eligibility(unauthorized, today=TODAY, config=cfg, published_events=[])[1] == "invalid_source"

    print("all V2 tests passed")
