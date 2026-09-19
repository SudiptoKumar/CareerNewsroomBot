# Career News V1

**Bangladesh career and job intelligence bot for Telegram**

Career News V1 is a production-oriented Python pipeline that discovers recent Bangladesh jobs from a controlled set of **Bdjobs private-job categories** and the **Teletalk government-job API**, filters them for business and early-career relevance, enriches promising records, validates source-backed fields, ranks candidates, and publishes a controlled number of Telegram job posts.

The project is designed around one rule:

> **Source facts first. AI and ranking decide relevance, not facts.**

The bot does not invent missing job information, does not use an uncontrolled global job search, and does not replace a failed source with random third-party job boards.

---

## 1. What This Project Does

At a high level, every production run follows this flow:

```text
Bdjobs category discovery ─┐
                           ├─> Normalize ─> Deduplicate ─> Validate
Teletalk government API ───┘
                                      │
                                      v
                              Business relevance
                                      │
                                      v
                         Deterministic job ranking
                                      │
                                      v
                           Detail-page enrichment
                                      │
                                      v
                              Cerebras AI audit
                                      │
                                      v
                         Source-backed snapshot check
                                      │
                                      v
                       Diversity + publication selection
                                      │
                                      v
                              Telegram publishing
```

The system is intentionally **category-first**. It does not first collect a large uncontrolled list of jobs and then try to decide whether the jobs belong to the user's target career areas.

---

## 2. Primary Goals

Career News V1 is built to provide:

- Recent Bangladesh private-sector opportunities relevant to BBA/MBA and business careers.
- Government opportunities from Teletalk's published-jobs API.
- Strong source-backed job information instead of AI-generated facts.
- Protection against incomplete or contaminated Bdjobs detail pages.
- A controlled publication mix instead of publishing every discovered vacancy.
- Internship coverage when qualifying internships exist.
- Company and career-family diversity.
- Persistent state so previously handled jobs can be tracked.
- GitHub Actions automation every three hours.
- Deterministic behavior when the optional Cerebras AI layer is unavailable.

---

## 3. Production Sources

### 3.1 Private jobs

```text
Source: Bdjobs
Method: Category-first discovery
```

The private lane uses a controlled set of Bdjobs categories rather than uncontrolled search-engine discovery.

### 3.2 Government jobs

```text
Source: Teletalk AllJobs API
Method: Published-jobs API
```

Government jobs use a separate source-specific ranking path and do not pass through the private BBA/MBA relevance gate.

### 3.3 Sources deliberately not used

The production pipeline does **not** substitute or expand into:

- Dohaj
- Ever Jobs
- Random Google/search-engine results
- Uncontrolled job aggregators
- Random alternate job boards

This keeps source identity and quality control predictable.

---

## 4. Target Bdjobs Categories

The private discovery universe is currently:

| ID | Bdjobs category |
|---:|---|
| 1 | Accounting / Finance |
| 2 | Bank / Non-Bank Financial Institution |
| 3 | Commercial / Supply Chain |
| 9 | Marketing / Sales |
| 17 | HR / Organization Development |
| 7 | General Management / Admin |
| 16 | Customer Service / Call Centre |
| 10 | Media / Advertisement / Event Management |
| 13 | Research / Consultancy |
| 12 | NGO / Development |
| 20 | Hospitality / Travel / Tourism |
| 6 | Garments / Textile |
| 8 | IT / Telecom |
| 4 | Education / Training |

Category priority changes discovery effort. It does not replace the final relevance and quality checks.

---

## 5. Publication Policy

The production workflow is configured with these publication guardrails:

| Rule | Production value |
|---|---:|
| Target total posts | 15 |
| Maximum total posts | 20 |
| Minimum private jobs | 10 |
| Minimum government jobs | 3 |
| Minimum internships | 2 |
| Quality floor | 65 |
| Private snapshot minimum | 4 usable source-backed fields |
| Government snapshot minimum | 3 usable source-backed fields |

The internship minimum is a subset of the private-job minimum.

The bot **does not fabricate filler jobs** to satisfy a number. If enough qualifying records do not exist, the corresponding quota can fall short and the log records the shortfall.

The final count is therefore dynamic within the configured limits.

---

## 6. Five-Day Freshness Rule

Private jobs are restricted to a five-day freshness window:

```text
MAX_POST_AGE_DAYS = 5
```

Discovery expands progressively:

```text
Today
  ↓
Last 2 days
  ↓
Last 3 days
  ↓
Last 4 days
  ↓
Last 5 days
```

Featured position or card ordering is not treated as the publication date.

---

## 7. Discovery Architecture

### 7.1 Private discovery

The private lane discovers jobs separately from each configured Bdjobs category.

Production workflow values:

```text
PRIVATE_DISCOVERY_TARGET = 160
PRIVATE_DISCOVERY_MAX    = 200
```

These values describe the **candidate comparison pool**, not the number of Telegram posts.

The source code also has a lower default target for environments where the workflow variables are not supplied:

```text
PRIVATE_DISCOVERY_TARGET default = 120
```

GitHub Actions overrides this with `160`.

### 7.2 Discovery principle

The intended architecture is:

```text
Category
   ↓
Recent jobs from that category
   ↓
Normalize
   ↓
Merge category pools
   ↓
Deduplicate
   ↓
Relevance + validity gates
```

It is **not**:

```text
Find 100 arbitrary jobs
   ↓
Classify them later
```

---

## 8. Bdjobs Fetching and Cloudflare Protection

Bdjobs can return Cloudflare challenge pages or an Angular/application shell even when the HTTP status is `200`.

Therefore:

> **HTTP 200 alone is never treated as proof that a job detail page was successfully retrieved.**

The production acquisition chain is designed to handle this.

### 8.1 Primary HTTP acquisition

`curl_cffi` is used with browser TLS impersonation.

Configured production fingerprints include:

```text
safari18_0_ios
safari184_ios
safari260_ios
safari_ios
```

The implementation can rotate fingerprints when a request appears challenged or unusable.

### 8.2 Rendered browser fallback

When a normal HTTP response is an application shell or otherwise fails job-content validation, the bot uses:

```text
Scrapling StealthyFetcher
        ↓
Patchright Chromium
        ↓
Rendered Bdjobs page
```

This is important because Scrapling's stealth browser fetcher is Patchright-powered.

GitHub Actions therefore installs the browser with:

```bash
scrapling install --force
patchright install chromium --with-deps
```

### 8.3 Jina Reader fallback

If direct acquisition and browser rendering do not produce usable job content, the bot can use Jina Reader:

```text
https://r.jina.ai/<original-url>
```

Jina output is converted into parser-safe text before field extraction.

### 8.4 Final listing preservation

If a detail page cannot be enriched, the original Bdjobs listing record is not silently discarded when it already contains enough trustworthy information.

The enrichment chain is therefore conceptually:

```text
curl_cffi
   ↓
Patchright browser render
   ↓
Jina Reader
   ↓
listing-backed preservation
```

A detail failure is treated as an **enrichment failure**, not automatically as a candidate deletion.

---

## 9. Detail-Page Validation

A Bdjobs detail page may return a large HTML document containing mostly JavaScript and CSS while the actual job text is missing.

Career News V1 therefore validates the **visible content** after removing script/style material.

The system checks that the response contains enough actual job-like content before treating it as a valid detail page.

This prevents a page such as:

```text
HTTP 200
Large HTML document
Angular application shell
No real job information
```

from being mistaken for a successful job extraction.

Production logs distinguish:

```text
PRIVATE DETAIL SUCCESS
PRIVATE DETAIL FALLBACK
PRIVATE DETAIL FAILED
```

This makes a private-job shortfall diagnosable.

---

## 10. Source-First Extraction

The extraction architecture is deliberately separated from AI.

```text
Raw source page
      ↓
Clean source document
      ↓
Structured field extraction
      ↓
Normalized JobRecord
      ↓
Ranking / AI review
      ↓
Telegram rendering
```

Cerebras does **not** create missing facts.

The source parser extracts values such as:

- Location
- Employment
- Workplace
- Education
- Experience
- Salary
- Vacancy
- Age
- Application method
- Deadline
- Posted date
- Company
- Job title

Each field is bound to its own source label or structured source evidence.

---

## 11. Field-Contamination Protection

A major design requirement is that one field must never be copied into another field simply because the parser found text nearby.

Examples:

```text
Age        → Age
Experience → Experience
Salary     → Salary
Vacancy    → Vacancy
Deadline   → Deadline
Posted     → Posted
```

For example, an experience value such as `2 years` must never become an age value such as `22 years`.

The parser also protects against label-prefix collisions. Longer labels are matched before shorter labels, so:

```text
Application Deadline: 19 Oct 2026
Application: Online
```

must remain:

```text
Deadline           = 19 Oct 2026
Application method = Online
```

rather than accidentally assigning the deadline to the application field.

---

## 12. JobRecord and Field Precedence

The bot normalizes each candidate into one internal job record.

When detail research provides additional information, the precedence is:

```text
Verified detail value
        ↓
Listing value
        ↓
Existing normalized value
        ↓
Omit field
```

The detail lane is therefore an enrichment layer. It must not erase trustworthy information already obtained from the listing.

---

## 13. Job Snapshot Contract

Telegram posts use the following preferred field order:

```text
Location
Employment
Workplace
Education
Experience
Salary
Vacancy
Age
Application
Deadline
Posted
```

Only fields with trustworthy available values are displayed.

The system does not insert fake placeholders merely to make a table look complete.

### Private minimum quality

A private record must satisfy core source-backed requirements and contain at least the configured minimum number of useful snapshot fields.

Normal private jobs require:

```text
Location
Experience
Deadline
```

as core information, plus additional usable information.

Internships have a different core requirement because experience may legitimately be absent:

```text
Location
Deadline
```

plus additional usable information.

### Government minimum quality

Government records require at least the configured government snapshot field minimum before publication.

---

## 14. Relevance and Business-Career Filtering

Private candidates are evaluated for business-career relevance before final publication.

The pipeline considers factors such as:

- BBA/MBA eligibility
- Business-function relevance
- Career-stage fit
- Freshness
- Deadline usefulness
- Salary information
- Vacancy information
- Information completeness
- Specialist-role requirements
- Seniority and experience requirements

The goal is not to publish every job discovered in the configured categories.

---

## 15. Deterministic Ranking

Private candidates use a deterministic 100-point ranking model:

| Factor | Weight |
|---|---:|
| BBA/MBA eligibility | 25 |
| Business career / role fit | 20 |
| Career-stage fit | 15 |
| Freshness | 15 |
| Deadline usefulness | 10 |
| Salary attractiveness | 5 |
| Vacancy opportunity | 5 |
| Information quality | 5 |
| **Total** | **100** |

This deterministic score remains available even when AI is disabled or unavailable.

Government jobs use a separate government-specific ranking path.

---

## 16. Detail Research Funnel

The production workflow narrows the private candidate pool before expensive detail research.

```text
Broad category pool
        ↓
Fast deterministic ranking
        ↓
Top private detail candidates
        ↓
Bdjobs detail enrichment
        ↓
AI audit candidates
        ↓
Final ranking
```

Current production targets:

```text
PRIVATE_FAST_RANK_TARGET = 60
PRIVATE_DETAIL_TARGET    = 60
AI_REVIEW_TARGET        = 50
AI_BATCH_SIZE           = 8
```

Internships receive dedicated detail and AI reservation settings so that the internship minimum is not accidentally consumed by general private-job selection.

---

## 17. Cerebras AI Layer

Cerebras is an **optional semantic audit layer**.

It is not the source of truth.

The AI layer can evaluate:

- Education match
- Business-role fit
- Career-stage fit
- Specialist-degree requirements
- Seniority
- Qualification contradictions
- Internship relevance
- Semantic relevance

### Final score

When AI is available:

```text
final_score = deterministic_score × 0.85
              + AI_score × 0.15
```

When AI is unavailable, invalid, rate-limited, or times out:

```text
final_score = deterministic_score
```

Therefore a Cerebras failure does not automatically stop the publication pipeline.

### Source-of-truth rule

AI may select or evaluate available information, but it must not invent factual job values.

---

## 18. Diversity Selection

After quality and ranking checks, final selection applies controlled diversity.

The private selector attempts to avoid over-concentration around:

- One company
- One career family
- One type of vacancy

The diversity logic is intentionally soft rather than an absolute quota system. Strong candidates can still be selected when the available pool is narrow.

Internships are reserved first when qualifying internships are available so that general private-job diversity does not consume every private slot.

---

## 19. Final Selection Logic

The final selector works approximately as follows:

```text
Eligible government pool
        ↓
Reserve government minimum
        ↓
Eligible internship pool
        ↓
Reserve internship minimum
        ↓
Fill private minimum
        ↓
Use remaining capacity for strongest candidates
        ↓
Apply final diversity
        ↓
Hard maximum of 20
```

The intended minimums are:

```text
Private       ≥ 10 when enough qualifying private jobs exist
Government    ≥ 3 when enough qualifying government jobs exist
Internships   ≥ 2 when enough qualifying internships exist
Total         ≤ 20
```

A quota shortfall is logged instead of being hidden.

---

## 20. Telegram Output

Career News V1 publishes text-only Telegram Rich Messages.

Photos are intentionally disabled in the current design to avoid mismatched images, source placeholders, and black or broken media cards.

The structure is:

```text
JOB TITLE
Company

JOB SNAPSHOT
Location
Employment
Workplace
Education
Experience
Salary
Vacancy
Age
Application
Deadline
Posted

Source / career information

[APPLY NOW]
```

All 11 snapshot rows are rendered in a fixed order. When a source does not provide a value, the row displays `Not specified`. Missing values are not counted as source information for snapshot eligibility.

### Apply button

The button text is:

```text
APPLY NOW
```

The destination uses the identified application URL when available. Otherwise the verified source URL is used.

### Hashtags

Posts can include source/category tags such as:

```text
#GovtJob
#Internship
#Finance
#Banking
#Marketing
#BusinessDevelopment
#HR
#SupplyChain
#Management
#CustomerService
#Operations
#Media
#Research
#NGO
#Hospitality
#EarlyCareer
```

Only applicable tags are generated for each job.

---

## 21. Final Snapshot Integrity Check

A candidate is checked before selection and again immediately before Telegram publication.

The integrity layer rejects records with problems such as:

```text
Missing title
Missing company
Missing source URL
Sparse snapshot
Missing required private core fields
Source-page artifacts inside fields
Age/experience contamination
Invalid source-derived content
```

This two-stage check is deliberate:

```text
Research-time validation
        ↓
Selection
        ↓
Final publication validation
```

A failed final check cannot silently consume a publication quota slot.

---

## 22. State and Deduplication

The repository stores persistent state in:

```text
news_state.json
posted_urls.txt
```

The state layer tracks information such as:

- Source identity
- Source job ID
- Canonical URL
- Application URL
- Title
- Company
- Location
- Posted date
- Deadline
- Score
- Pipeline status
- Selection time
- Publication time
- Telegram message ID when available

The bot also uses canonical job identity and URL normalization to reduce duplicate publication.

Candidates receive an explicit lifecycle disposition instead of silently disappearing between pipeline stages.

---

## 23. Repository Structure

The repository intentionally keeps a simple GitHub-friendly structure:

```text
Career News V1/
│
├── .github/
│   └── workflows/
│       └── newbot.yml          # GitHub Actions automation
│
├── tests/
│   └── test_v1.py              # Unit and regression tests
│
├── main.py                     # Complete production pipeline
├── requirements.txt             # Python dependencies
├── README.md                    # Project documentation
├── LICENSE                      # Project license
├── news_state.json              # Persistent pipeline state
├── posted_urls.txt              # Published URL/state history
└── .gitignore                   # Git exclusions
```

### Main files

#### `main.py`

Contains the complete runtime pipeline, including:

- Configuration
- Source acquisition
- Bdjobs discovery
- Teletalk discovery
- Deduplication
- Field extraction
- Detail enrichment
- Ranking
- Cerebras audit
- Internship handling
- Final selection
- Telegram rendering
- State management
- Self-tests and source diagnostics

#### `tests/test_v1.py`

Contains regression tests for important production behavior, including source extraction, field separation, fallback behavior, AI schema compatibility, and publication safeguards.

#### `.github/workflows/newbot.yml`

Runs the production pipeline on GitHub Actions every three hours and supports manual execution.

---

## 24. GitHub Actions Workflow

The production workflow runs on:

```text
OS: Ubuntu 24.04
Python: 3.12
Timeout: 18 minutes
Schedule: every 3 hours
Manual dispatch: enabled
```

Schedule:

```cron
0 */3 * * *
```

### Workflow stages

```text
Checkout
   ↓
Python setup
   ↓
Install Python dependencies
   ↓
Install Scrapling browser support
   ↓
Install Patchright Chromium
   ↓
Compile check
   ↓
Unit tests
   ↓
Self-test
   ↓
Optional live source diagnostics on manual runs
   ↓
Production bot run
   ↓
Save state to Git
```

### Browser installation

The workflow explicitly installs the browser required by the Scrapling stealth fetcher:

```bash
scrapling install --force
patchright install chromium --with-deps
```

This is required for the browser fallback.

---

## 25. Required GitHub Secrets

Add these under:

```text
GitHub repository
→ Settings
→ Secrets and variables
→ Actions
```

### Required

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHANNEL
```

The current production workflow sets:

```text
TELEGRAM_CHANNEL = @CareerNewsroom
```

### Optional

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

The current workflow defaults the model to:

```text
gpt-oss-120b
```

Never commit API keys or bot tokens to the repository.

---

## 26. Production Environment Configuration

The current GitHub Actions configuration uses:

```text
MAX_POST_AGE_DAYS=5

PRIVATE_DISCOVERY_TARGET=160
PRIVATE_DISCOVERY_MAX=200
PRIVATE_FAST_RANK_TARGET=60
PRIVATE_DETAIL_TARGET=60

INTERNSHIP_DETAIL_TARGET=10
INTERNSHIP_AI_TARGET=8
MIN_INTERNSHIP_POSTS_PER_RUN=2

PRIVATE_SNAPSHOT_MIN_FIELDS=4
GOVERNMENT_SNAPSHOT_MIN_FIELDS=3

AI_REVIEW_TARGET=50
AI_BATCH_SIZE=8
AI_RETRY_COUNT=1

MIN_PRIVATE_POSTS_PER_RUN=10
MIN_GOVERNMENT_POSTS_PER_RUN=3

PRIVATE_MIN_FILL_SCORE=58
PRIVATE_HARD_FILL_SCORE=55
PRIVATE_MIN_INFORMATION_QUALITY=3
PRIVATE_HARD_MIN_INFORMATION_QUALITY=3

DETAIL_WORKERS=8
DETAIL_TIMEOUT=14
LEGACY_DETAIL_TIMEOUT=8

SCRAPLING_BROWSER_ENABLED=1
SCRAPLING_BROWSER_TIMEOUT=15000
SCRAPLING_BROWSER_WAIT_MS=1000
SCRAPLING_BROWSER_DETAIL_LIMIT=60
DETAIL_MIN_TEXT_CHARS=180

POST_DELAY_SECONDS=1.0

CURL_IMPERSONATES=safari18_0_ios,safari184_ios,safari260_ios,safari_ios
CURL_MAX_FINGERPRINT_ATTEMPTS=4

JINA_ENABLED=1
JINA_TIMEOUT=12
JINA_RPM_LIMIT=24
```

These are workflow-level production values. Most settings can also be overridden through environment variables when running the program elsewhere.

---

## 27. Local Installation

### Requirements

- Python 3.11+ recommended
- Internet access for live source testing
- Telegram bot token for publishing
- Optional Cerebras API key
- Chromium for the browser fallback when using the full acquisition chain

### Install

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
scrapling install --force
patchright install chromium --with-deps
```

On systems where `--with-deps` is not appropriate, install the required Chromium/system dependencies according to the operating system, then install Patchright Chromium normally.

---

## 28. Local Commands

### Self-test

```bash
python main.py --self-test
```

The self-test is intended to verify deterministic/offline behavior before a production release.

Expected release condition:

```text
Career News V1 self-test passed.
```

### Compile check

```bash
python -m py_compile main.py
```

### Source diagnostics

```bash
python main.py --source-test
```

This performs live diagnostics against the configured sources, including Teletalk and Bdjobs acquisition paths.

### Dry run

```bash
python main.py --dry-run
```

Runs the pipeline without publishing to Telegram.

### Ranking inspection

```bash
python main.py --print-ranking
```

Prints ranking information for inspection.

---

## 29. Recommended Release Test Sequence

Before publishing a new repository build:

```bash
python -m py_compile main.py
pytest -q
python main.py --self-test
```

Then, for a live-source release verification:

```bash
python main.py --source-test
```

Finally run a controlled production/dry run and inspect the funnel logs.

A release should not be considered production-ready merely because Python compilation succeeds. The source acquisition, extraction, fallback, AI audit, snapshot, and publication stages must also pass their checks.

---

## 30. Important Production Log Signals

When investigating a GitHub Actions run, the following messages are especially useful.

### Discovery

```text
TELETALK API DISCOVERY
BDJOBS CATEGORY
BDJOBS CATEGORY-FIRST
DISCOVERED
```

These show whether the source discovery stage produced candidates.

### Detail research

```text
BDJOBS DETAIL SUCCESS
BDJOBS BROWSER SUCCESS
PRIVATE DETAIL SUCCESS
PRIVATE DETAIL FALLBACK
PRIVATE DETAIL FAILED
```

These show whether Bdjobs detail enrichment worked or whether the pipeline had to preserve listing data.

### AI

```text
PRIVATE AI AUDITED
```

A zero AI audit count does not automatically mean the run failed because deterministic ranking remains available.

### Snapshot and selection

```text
SNAPSHOT
PRIVATE SCORED
INTERNSHIP QUOTA
FINAL SELECTED
PUBLICATION QUOTA
```

These show whether candidates survived the final quality and quota gates.

---

## 31. Troubleshooting Guide

### Problem: `pytest` fails before production starts

Check the failing test first.

The workflow is intentionally ordered so that:

```text
Tests fail
   ↓
Production run does not start
```

Do not diagnose Telegram output from a run that never reached the production stage.

---

### Problem: Browser executable does not exist

Look for an error similar to:

```text
Executable doesn't exist at ... chromium ...
```

Verify the workflow contains:

```bash
scrapling install --force
patchright install chromium --with-deps
```

Normal Playwright installation alone is not the correct installation path for the Scrapling Patchright fallback.

---

### Problem: Bdjobs returns HTTP 200 but no job data

This usually indicates an application/Angular shell or another thin page.

Check for:

```text
BDJOBS DETAIL REJECT
thin_or_non_job_page
```

The correct behavior is to reject the invalid detail document and continue through browser/Jina/listing preservation rather than treating HTTP 200 as success.

---

### Problem: Telegram post contains too few fields

Check these stages in order:

```text
Source extraction
      ↓
merge_job_fields()
      ↓
snapshot_integrity()
      ↓
AI scoring/filtering (never field selection)
      ↓
job_snapshot_rows()
      ↓
Telegram rendering
```

The source may have been successfully retrieved but a field can still disappear if it failed extraction, normalization, integrity checks, or display selection.

The intended snapshot order is:

```text
Location
Employment
Workplace
Education
Experience
Salary
Vacancy
Age
Application
Deadline
Posted
```

---

### Problem: AI fails

Check the exact Cerebras response.

The expected behavior is deterministic fallback:

```text
AI unavailable
      ↓
deterministic score remains active
```

An AI schema/API problem should not cause the source facts to be invented or silently discarded.

---

### Problem: Too few private jobs

Inspect:

```text
BDJOBS CATEGORY-FIRST
DISCOVERED
PRIVATE DETAIL SUCCESS
PRIVATE DETAIL FALLBACK
PRIVATE DETAIL FAILED
PRIVATE SCORED
SNAPSHOT
FINAL SELECTED
```

A low final private count can originate from:

- Too few fresh Bdjobs jobs.
- Category discovery failure.
- Detail/enrichment failure.
- Relevance filtering.
- Specialist-role exclusion.
- Sparse snapshot rejection.
- Duplicate rejection.
- Quality-floor filtering.
- Internship reservation.
- Company/category diversity.

The bot should log these stages rather than silently dropping records.

---

## 32. Security

Never place credentials directly in source files.

Do not commit:

```text
TELEGRAM_BOT_TOKEN
CEREBRAS_API_KEY
```

Use GitHub Actions Secrets or environment variables.

Persistent state files are not substitutes for credentials and should be reviewed before committing if they contain sensitive operational information.

---

## 33. Design Principles

### Principle 1: Source before AI

```text
Source → normalize → AI audit → render source facts
```

### Principle 2: Category before global search

```text
Controlled category universe → candidate pool
```

### Principle 3: HTTP status is not content validation

```text
200 OK ≠ valid job page
```

### Principle 4: Detail enrichment must not destroy listing data

```text
Detail failure → preserve trustworthy listing fields
```

### Principle 5: Missing data stays missing

```text
Missing source field → omit field
```

It is never replaced with an invented value.

### Principle 6: AI failure must degrade safely

```text
AI failure → deterministic ranking
```

### Principle 7: Quotas never justify fabrication

```text
Not enough qualifying jobs → publish fewer
```

### Principle 8: Every final post gets a final integrity check

```text
Research validation → selection → publication validation
```

---

## 34. Current Architecture Summary

```text
                         CAREER NEWS V1

 ┌────────────────────── SOURCE LAYER ──────────────────────┐
 │                                                         │
 │  Bdjobs categories                         Teletalk API  │
 │        │                                         │       │
 │        ▼                                         ▼       │
 │  Category discovery                         Gov discovery│
 │        │                                         │       │
 └────────┼─────────────────────────────────────────┼───────┘
          ▼                                         ▼
 ┌──────────────────── NORMALIZATION ───────────────────────┐
 │  URL canonicalization                                   │
 │  Source identity                                        │
 │  Field extraction                                       │
 │  Freshness                                              │
 │  Deduplication                                          │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
 ┌──────────────────── RELEVANCE LAYER ─────────────────────┐
 │  BBA/MBA relevance                                      │
 │  Business-career fit                                    │
 │  Career stage                                            │
 │  Specialist-role filtering                              │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
 ┌──────────────────── RESEARCH LAYER ──────────────────────┐
 │  curl_cffi                                               │
 │       ↓                                                 │
 │  Patchright / Scrapling                                 │
 │       ↓                                                 │
 │  Jina Reader                                             │
 │       ↓                                                 │
 │  Listing preservation                                    │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
 ┌──────────────────── INTELLIGENCE LAYER ───────────────────┐
 │  Deterministic 100-point ranking                         │
 │       +                                                 │
 │  Cerebras semantic audit                                 │
 │       ↓                                                 │
 │  85% deterministic + 15% AI                             │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
 ┌──────────────────── QUALITY LAYER ───────────────────────┐
 │  Snapshot integrity                                     │
 │  Field contamination protection                          │
 │  Duplicate checks                                       │
 │  Quality floor                                          │
 │  Internship reservation                                 │
 │  Company/category diversity                             │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
 ┌──────────────────── PUBLICATION LAYER ────────────────────┐
 │  Minimum private: 10                                    │
 │  Minimum government: 3                                  │
 │  Minimum internship: 2                                  │
 │  Target total: 15                                       │
 │  Maximum total: 20                                      │
 │  Telegram Rich Message                                  │
 └──────────────────────────┬──────────────────────────────┘
                            ▼
                     Telegram channel
                            │
                            ▼
                    Persistent state
```

---

## 35. Release Acceptance Checklist

A production release should satisfy all of the following:

```text
[ ] Category-first Bdjobs discovery
[ ] No global-first arbitrary job discovery path
[ ] Teletalk government discovery
[ ] Five-day private freshness gate
[ ] curl_cffi browser impersonation
[ ] Fingerprint rotation
[ ] Patchright Chromium installation
[ ] Scrapling browser fallback
[ ] Jina Reader fallback
[ ] Visible-content detail validation
[ ] Listing-data preservation after detail failure
[ ] Source-first field extraction
[ ] Independent Age / Experience / Salary / Vacancy parsing
[ ] Application / Application Deadline label protection
[ ] BBA/MBA/business relevance filtering
[ ] Specialist-role filtering
[ ] Deterministic ranking
[ ] Cerebras semantic audit
[ ] Safe AI fallback
[ ] 85/15 deterministic + AI scoring
[ ] Internship reservation
[ ] Company/category diversity
[ ] Snapshot integrity validation
[ ] Duplicate protection
[ ] Dynamic publication count
[ ] Telegram Rich Message rendering
[ ] APPLY NOW button
[ ] Persistent state update
[ ] `py_compile` passes
[ ] All unit tests pass
[ ] `--self-test` passes
[ ] Live source diagnostics pass when required
```

---

## 36. Version Scope

This README documents the **Career News V1** production repository and its current GitHub Actions configuration.

The project name remains:

```text
Career News V1
```

No version number beyond V1 is required for the repository naming convention.
