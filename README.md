# CareerNewsBot V1

> Automated Bangladesh job-news intelligence for Telegram, built by adapting the proven `TheTechNewsroomBot` architecture while replacing the tech-news editorial algorithm with a Bangladesh-job-specific discovery, eligibility, deadline, and ranking system.

**Telegram Channel:** `@CareerNewsroom`

## 1. Project Goal

CareerNewsBot collects newly published Bangladesh job vacancies and job circulars, removes irrelevant or expired opportunities, verifies the usable job details from the source article/page, and publishes compact Telegram job cards.

The bot is designed specifically for:

- Jobs located in Bangladesh
- Jobs explicitly open to Bangladeshi applicants/citizens when citizenship is stated
- Government jobs
- Private-sector jobs
- Bank and financial-institution jobs
- NGO/development-sector jobs
- Internship and trainee opportunities when they are real vacancies
- Fresher and experienced roles
- Bangladesh-based recruitment notices published by trusted job/news sources

The bot must **not** publish overseas-only jobs, generic career advice, recruitment commentary, exam-only news, or expired/near-expiry vacancies.

---

# 2. Technical Foundation

CareerNewsBot intentionally keeps the same technical architecture used by the reference `TheTechNewsroomBot` project.

### Runtime

- Python `3.12`
- GitHub Actions
- Linux runner: `ubuntu-latest`
- Persistent repository state through Git commits

### APIs / Services

- **Exa** for web/news discovery and gap filling
- **Cerebras** for structured editorial ranking, extraction/normalization, generation, numeric validation, and claim validation
- **Telegram Bot API** as the final delivery fallback
- **Telegram Rich Message API** through `sendRichMessage` for the primary photo + rich HTML post

### Python dependencies

```text
exa-py
cerebras_cloud_sdk
requests
urllib3
beautifulsoup4
Pillow
feedparser
trafilatura
```

No database is required for V1.

---

# 3. Same Repository Structure

The new bot follows the same compact repository layout as the reference project.

```text
CareerNewsBot-main/
├── .github/
│   └── workflows/
│       ├── import-zip.yml
│       └── newbot.yml
├── README.md
├── main.py
├── news_state.json
├── posted_urls.txt
└── requirements.txt
```

### File responsibilities

| File | Purpose |
|---|---|
| `.github/workflows/newbot.yml` | Scheduled/manual GitHub Actions execution, dependency installation, self-test, bot run, state commit |
| `.github/workflows/import-zip.yml` | Repository import/update helper, same pattern as reference project |
| `main.py` | Entire discovery, filtering, ranking, extraction, generation, image, Telegram, verification, and state pipeline |
| `news_state.json` | Persistent queue, source health, event clusters, published-event memory, and runtime state |
| `posted_urls.txt` | Durable URL-level duplicate protection |
| `requirements.txt` | Python dependencies |
| `README.md` | Project specification and operating guide |

V1 deliberately keeps `main.py` as the single application module so the new project remains structurally compatible with the reference bot.

---

# 4. Core Design Principle

The reference bot is a **tech-news ranking system**. CareerNewsBot becomes a **job-opportunity eligibility and usefulness system**.

The most important change is the editorial algorithm:

```text
REFERENCE TECH BOT
RSS → Google News → Exa → 24h filter → rank importance → event dedup → article generation → verification → image → Telegram

CAREER NEWS BOT
RSS / job pages → Google News → Exa → 72h filter → Bangladesh-job eligibility → deadline ≥ 7 days → vacancy extraction → event dedup → usefulness ranking → article verification → image → Telegram
```

The 7-day deadline requirement is a **hard eligibility gate**, not merely a ranking preference.

---

# 5. Mandatory Time Rules

## 5.1 Discovery Window: 72 Hours

Only the latest three days of newly published job news are eligible.

```text
DISCOVERY_START = NOW_BD - 72 hours
DISCOVERY_END   = NOW_BD + small future tolerance
```

The bot must reject a vacancy when its source publication date is older than the 72-hour window.

If a feed does not expose a reliable publication date, the candidate is rejected for V1 rather than treated as a new story. This preserves the strict 72-hour latest-news requirement.

## 5.2 Minimum Application Deadline: 7 Days

A vacancy is publishable only when the application deadline is at least seven full days from the current Bangladesh time/date used by the bot.

Conceptually:

```text
deadline >= NOW_BD + 7 days
```

Examples:

```text
Published: 18 Sep
Deadline: 25 Sep  → ACCEPT, exactly 7 days
Deadline: 24 Sep  → REJECT, less than 7 days
Deadline: 20 Sep  → REJECT
Deadline: 17 Sep  → REJECT / expired
```

If the source page has no identifiable deadline, the vacancy should be rejected in V1 rather than guessed.

---

# 6. Bangladesh-Only Eligibility

The bot must determine whether a vacancy is genuinely relevant to Bangladesh-based applicants.

### ACCEPT

- Location is Dhaka, Chattogram, Rajshahi, Khulna, Sylhet, Barishal, Rangpur, Mymensingh, or another Bangladesh location
- Location is explicitly `Anywhere in Bangladesh`
- Official Bangladesh government recruitment
- Employer is recruiting in Bangladesh
- Source explicitly states Bangladeshi citizenship/nationality eligibility
- A Bangladesh-based remote role clearly intended for applicants in Bangladesh

### REJECT

- India-only vacancies
- Pakistan-only vacancies
- UAE/Saudi/Qatar/other overseas vacancies
- Europe/USA/Canada/Australia-only vacancies
- International vacancy with no Bangladesh eligibility/location
- Foreign recruitment news that does not contain a Bangladesh-specific vacancy
- Generic career articles with no concrete opening

The bot should prefer an explicit source statement over inference.

---

# 7. Source Strategy

CareerNewsBot uses a layered source system. RSS is preferred, but the bot must not depend on RSS existing for every important Bangladesh job portal.

## Tier 1: Job-Focused RSS

Planned high-value RSS sources include:

| Source | Planned access | URL |
|---|---|---|
| ProjobsBD | RSS | `https://projobsbd.com/feed/` |
| ProjobsBD Running Job Circular | RSS | `https://projobsbd.com/category/running-job-circular/feed/` |
| BD Govt Jobs | RSS | `https://bdgovtjobs.com/feed/` |
| JobPagol | RSS | `https://jobpagol.com/feed/` |
| Bangladesh Pratidin Jobs | RSS/category feed | `https://bdpratidin.net/rss/category/job` |

Only feeds that are verified as working at implementation time should remain active in `RSS_FEEDS`.

## Tier 2: Bangladesh News RSS + Job Filter

These sources can provide job stories through their broader news RSS feeds and therefore need an additional job-category/content filter:

| Source | Access strategy |
|---|---|
| JagoNews24 | RSS + চাকরি category filter |
| Bangla Tribune | RSS + jobs section filter |
| BD24Live | RSS + job keyword/category filter |
| RisingBD | RSS + job section filter |
| Bangladesh Journal | RSS + job filter |
| Prothom Alo | RSS + চাকরিবাকরি/job filter |
| Jugantor | RSS + job filter |
| Kaler Kantho | RSS + job filter |
| The Daily Star | RSS + recruitment/job filter |
| The Daily Ittefaq | RSS + job filter |

These are **not automatically eligible sources** just because they contain employment news. The vacancy itself must pass the Bangladesh and deadline rules.

## Tier 3: Direct Job / Recruitment Pages

Important non-RSS sources should be scraped through their job listing or recruitment pages.

| Source | Primary extraction page |
|---|---|
| Bdjobs | `https://jobs.bdjobs.com/jobsearch.asp` |
| Dohaj | `https://dohaj.com/jobs/all` |
| Job.com.bd | `https://job.com.bd/jobs/new_jobs/` |
| Smart Job | `https://smartjob.portal.gov.bd/` |
| Alljobs / Teletalk | `https://alljobs.teletalk.com.bd/` |
| BPSC | `https://bpsc.gov.bd/` |
| Bangladesh Computer Council e-Recruitment | `https://erecruitment.bcc.gov.bd/` |
| JobsNoticeBD | `https://jobsnoticebd.com/` |
| JobsInfo | `https://jobsinfo.bd/` |
| JobFeeds | `https://jobfeeds.online/` |
| CircularBD | `https://www.circularbd.com/alljobs` |
| Bangladesher Khabor Jobs | `https://www.bangladesherkhabor.net/jobs` |
| Dhaka Post Jobs/Career | `https://www.dhakapost.com/jobs-career/` |
| Dhaka Tribune Bangla Jobs | `https://bangla.dhakatribune.com/jobs` |

Official government sources should receive higher source-trust weighting than third-party aggregators when the same circular is found in both places.

---

# 8. Discovery Pipeline

```text
Verified job RSS feeds
        ↓
Direct job listing pages
        ↓
Google News RSS gap fill
        ↓
Exa gap fill
        ↓
Source-domain validation
        ↓
72-hour publication filter
        ↓
Job-content eligibility filter
        ↓
Bangladesh-only eligibility filter
        ↓
Deadline extraction
        ↓
Minimum 7-day deadline gate
        ↓
URL deduplication
        ↓
Vacancy/event deduplication
        ↓
Candidate completeness check
        ↓
Usefulness ranking
        ↓
Article/page extraction
        ↓
Telegram story generation
        ↓
Claim + numeric/date verification
        ↓
Article image recovery
        ↓
Branded 1200×675 image
        ↓
Telegram Rich Message
        ↓
Persistent state
```

---

# 9. Vacancy Eligibility Algorithm

Every candidate must pass the following hard gates before it can reach the final publisher pool.

```text
1. Valid URL
2. Valid title
3. Valid publication date
4. Published within previous 72 hours
5. Real vacancy/recruitment/circular
6. Bangladesh location or explicit Bangladesh eligibility
7. Application deadline is present
8. Deadline >= NOW_BD + 7 days
9. Not already published by URL
10. Not already published as the same vacancy/event
11. Source is in the allowed source universe
```

Any hard-gate failure means:

```text
REJECT
```

No score should override a failed hard eligibility condition.

---

# 10. Job Content Classification

The LLM should classify the candidate into a job type and extract the vacancy itself rather than treating every employment-related article as a vacancy.

### Eligible content

- Government job circular
- Private company vacancy
- Bank / NBFI vacancy
- NGO / development-sector vacancy
- Factory / manufacturing vacancy
- Corporate vacancy
- Education-sector vacancy
- Healthcare vacancy when it is a job opening
- IT/software vacancy
- Sales/marketing vacancy
- Internship
- Management trainee
- Graduate trainee
- Apprenticeship / entry-level recruitment

### Normally rejected

- Career advice
- CV writing tips
- Interview tips
- Salary advice
- Employment statistics without a vacancy
- Job exam/result notices without a new application opportunity
- Job fair announcements without identifiable vacancies/apply details
- Recruitment opinions/commentary
- Overseas-only recruitment
- Expired circulars
- Vacancies with a deadline inside the 7-day minimum window

---

# 11. Candidate Ranking

After hard eligibility filtering, the remaining vacancies compete in one ranked pool.

There is no mandatory category quota.

Recommended usefulness factors:

```text
Freshness                         High weight
Deadline safety                  High weight
Official/primary source          High weight
Complete job information         High weight
Clear application route          High weight
Employer credibility             Medium-high weight
Bangladesh-wide availability     Medium weight
Fresher eligibility              Medium weight
Salary disclosed                 Medium weight
Strong qualification match       Medium weight
High practical applicant value   Medium-high weight
```

The bot should prefer a fully specified valid vacancy over a poorly documented duplicate, while preserving the original/official source when possible.

### Ranking principle

```text
Hard eligibility first
        ↓
Usefulness score
        ↓
Event deduplication
        ↓
Best source for the vacancy
        ↓
Freshest valid version
```

The system can keep the same reference implementation's bounded LLM ranking batches and maximum per-run publishing cap. The cap is a safety limit, not a forced publishing quota.

---

# 12. Vacancy Event Deduplication

Different websites frequently publish the same job circular. The bot must treat that as one vacancy/event.

Deduplication should use:

- Job title
- Employer/company
- Location
- Deadline
- Education requirements
- Job type
- Application URL
- Circular/reference number when available
- Similarity of title + extracted entities

Example:

```text
Company A — Management Trainee — Dhaka — Deadline 25 Oct
Company A — Management Trainee Officer — Dhaka — Deadline 25 Oct
Company A announces Management Trainee recruitment — Deadline 25 Oct
```

These should normally collapse into one event when the underlying vacancy is the same.

The official employer/government source should be preferred when available.

---

# 13. Required Extracted Job Schema

Every accepted story should contain a normalized structure approximately like:

```text
job_title
company
location
job_type
education
experience
salary
deadline
suitable_for
key_highlights
apply_url
source
published_at
canonical_url
event_key
event_cluster_id
```

Recommended additional fields for internal use:

```text
vacancy_count
application_method
citizenship_requirement
gender_requirement
age_limit
reference_number
source_type
source_trust
```

Only fields supported by the source should be shown publicly. The bot must never invent missing salary, experience, education, vacancy count, or eligibility information.

---

# 14. Telegram Output Structure

The public post must follow the user's established CareerNewsroom structure.

```text
📣 JOB TITLE

🏢 Company: Example Company Ltd.
📍 Location: Dhaka
💼 Type: Management Trainee
🎓 Education: BBA / MBA
👨‍💼 Experience: Fresh graduates may apply
💰 Salary: Not specified
📅 Deadline: 25 September 2026

🎯 Suitable For
BBA • MBA • Fresh Graduate

📌 Key Highlights
• Corporate management opportunity
• Fresh graduates eligible
• Career development opportunity

📝 Apply Now
Apply Here

🔎 Source
Official Career Page

#CareerNewsroom #BBA #MBA #FreshGraduate
```

### Output rules

**Headline**

- 3–12 meaningful words when possible
- Job title must remain recognizable
- No clickbait
- No invented urgency

**Job details**

- Use the exact normalized value when clearly stated by the source
- Use `Not specified` only when the field is genuinely absent
- Do not guess salary or requirements

**Suitable For**

Generated from explicit education/experience requirements.

Examples:

```text
BBA • MBA • Fresh Graduate

CSE • IT Graduate • 0–2 Years Experience

Honours Graduate • 1–3 Years Experience
```

**Key Highlights**

- 2–5 concise factual points
- No repeated information
- Chosen dynamically from the vacancy

**Apply Now**

The application link must lead to the actual application/vacancy page whenever possible.

**Source**

Display the publication/source name, not an invented source label.

---

# 15. Telegram Rich HTML

The same rich-message publishing mechanism from the reference project should be retained.

### Primary path

```text
sendRichMessage
    ↓
photo attachment
    ↓
rich HTML
```

### Fallback path

```text
sendRichMessage fails
    ↓
Telegram Bot API sendPhoto
    ↓
safe plain-text caption
```

The implementation should preserve the reference bot's retry logic for Telegram rate limits, HTTP 5xx errors, and temporary network failures.

The channel branding must change from:

```text
@TheTechNewsroom
```

to:

```text
@CareerNewsroom
```

---

# 16. Image Pipeline

The reference image system should be reused with CareerNewsroom branding.

### Priority

```text
1. RSS image
2. OpenGraph image
3. Twitter image
4. Preloaded image
5. JSON-LD image
6. Lazy-loaded/page image
7. Source image candidates
8. Source logo fallback
9. Source-name fallback
```

### Final image

```text
1200 × 675 px
JPEG
quality ≈ 88
```

### Branding

For a real article image:

```text
Article image
        +
@CareerNewsroom chip at bottom-right
```

For fallback:

```text
Source logo centered
        +
@CareerNewsroom chip at bottom-right
```

If no logo is available:

```text
Source name centered in bold
        +
@CareerNewsroom chip at bottom-right
```

No fabricated job artwork should be generated in V1.

---

# 17. Verification

The reference bot's verification design should be retained but adapted to job data.

## Numeric/date grounding

Validate generated values against the source page/article, especially:

- Salary
- Vacancy count
- Age limit
- Experience years
- Application fee
- Deadline
- Date of publication

A generated value not supported by the source must trigger regeneration or rejection.

## Claim verification

Verify:

- Job title
- Employer
- Location
- Education
- Experience
- Salary
- Deadline
- Highlights
- Eligibility/suitable-for statements

Do not publish a vacancy when critical fields are unsupported after retry.

---

# 18. Persistent State

The same state approach as the reference bot is retained.

### `posted_urls.txt`

Stores normalized/canonical URLs so the same page cannot be posted repeatedly.

### `news_state.json`

Stores information such as:

```text
feeds
queue
events
event_clusters
posted_event_ids
recent_titles
source health
```

The new job bot should additionally preserve normalized deadline and vacancy identity information inside events where useful.

State retention should continue to prune old entries so the JSON file does not grow indefinitely.

---

# 19. Scheduling

The GitHub Actions workflow keeps the same execution model as the reference project:

```yaml
on:
  schedule:
    - cron: "0 7-23 * * *"
      timezone: "Asia/Dhaka"
  workflow_dispatch:
```

This provides scheduled runs plus manual execution.

The workflow should retain:

- `actions/checkout@v4`
- `actions/setup-python@v5`
- Python `3.12`
- pip cache
- `py_compile`
- `--self-test`
- normal bot execution
- automatic state commit/push
- job timeout
- concurrency protection

---

# 20. Required GitHub Secrets

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional:

```text
TELEGRAM_ADMIN_CHAT_ID
CEREBRAS_MODEL
```

Default model:

```text
CEREBRAS_MODEL=gpt-oss-120b
```

Workflow environment:

```text
TELEGRAM_CHANNEL=@CareerNewsroom
NEWS_MODE=update
PYTHONUNBUFFERED=1
```

---

# 21. Reliability Features to Preserve

The reference project's production-oriented reliability features should remain intact:

- HTTP retry with exponential backoff
- `Retry-After` handling
- Feed ETag support
- `If-Modified-Since` support
- Feed health tracking
- Feed failure alerting
- Telegram retry logic
- Telegram 429 handling
- Telegram 5xx retries
- Article extraction fallback
- Image candidate fallback chain
- URL deduplication
- Event deduplication
- Persistent queue/state
- Safe Rich HTML length checking
- Bot API fallback
- Compile test
- Deterministic self-test
- GitHub state commit after each run

Only the editorial/job logic should materially change.

---

# 22. Self-Test Requirements

The new self-test should verify at minimum:

```text
✓ Bangladesh eligibility accepts valid Bangladesh job
✓ Bangladesh eligibility rejects foreign-only job
✓ 72-hour filter accepts recent vacancy
✓ 72-hour filter rejects old vacancy
✓ Deadline +7 day rule accepts exactly 7 days
✓ Deadline +7 day rule rejects 6 days
✓ Missing deadline is rejected
✓ Expired vacancy is rejected
✓ Job event duplicate clustering works
✓ Required fields are rendered
✓ Rich HTML contains correct section order
✓ @CareerNewsroom branding is present
✓ Image output is exactly 1200×675
✓ Broken image falls back correctly
✓ Source logo fallback works
✓ Source-name fallback works
✓ Telegram payload can be constructed
✓ State serialization works
```

Local checks:

```bash
python -m py_compile main.py
```

```bash
EXA_API_KEY=dummy \
CEREBRAS_API_KEY=dummy \
TELEGRAM_BOT_TOKEN=dummy \
python main.py --self-test
```

Normal run:

```bash
python main.py
```

---

# 23. Implementation Plan

## Phase 1 — Structural cloning

Copy the reference project's proven technical skeleton:

```text
main.py
requirements.txt
news_state.json
posted_urls.txt
.github/workflows/newbot.yml
.github/workflows/import-zip.yml
```

Change only project/channel identity and CareerNewsroom branding.

## Phase 2 — Replace source universe

Remove all technology publishers and replace them with the verified Bangladesh job/news source registry.

Implement separate source groups for:

```text
RSS job sources
News RSS sources
Direct job portals
Official government recruitment sources
```

## Phase 3 — Replace discovery algorithm

Implement:

```text
72-hour discovery
Bangladesh-only eligibility
Job-content classifier
Deadline extraction
7-day minimum deadline gate
```

## Phase 4 — Replace ranking algorithm

Keep bounded Cerebras ranking batches from the reference architecture, but score:

```text
Freshness
Deadline safety
Source quality
Job completeness
Applicant usefulness
Clear application route
Eligibility breadth
```

Do not use the old technology importance categories.

## Phase 5 — Replace story generator

Remove the technology template:

```text
THE CONTEXT
BOTTOM LINE
```

and generate the CareerNewsroom job card structure from Section 14.

## Phase 6 — Preserve media/publishing layer

Keep:

```text
article-image extraction
1200×675 branding
source-logo fallback
source-name fallback
sendRichMessage
Bot API fallback
Telegram retry handling
```

Change branding to `@CareerNewsroom`.

## Phase 7 — Verification and self-test

Build deterministic tests for:

```text
72h filter
7-day deadline filter
Bangladesh eligibility
duplicate vacancy detection
Rich HTML
image fallback
state handling
```

## Phase 8 — Production test

Run:

```text
compile
→ self-test
→ GitHub Actions manual run
→ source discovery check
→ candidate filter logs
→ Telegram Rich Message test
→ state commit test
→ repeated-run duplicate test
```

Only after all checks pass should the production ZIP be created.

---

# 24. Final V1 Editorial Rules

The bot should follow these rules without exception:

```text
LATEST 3 DAYS ONLY
        ↓
BANGLADESH JOBS ONLY
        ↓
REAL VACANCY ONLY
        ↓
DEADLINE MUST EXIST
        ↓
DEADLINE MUST BE ≥ 7 DAYS AWAY
        ↓
NO DUPLICATE VACANCIES
        ↓
NO INVENTED JOB DETAILS
        ↓
OFFICIAL / ORIGINAL SOURCE PREFERRED
        ↓
RICH PHOTO POST
        ↓
@CareerNewsroom BRANDING
```

The objective is not to publish the maximum number of vacancies. The objective is to publish **fresh, currently actionable Bangladesh job opportunities** with enough remaining application time for a user to realistically apply.

---

# 25. Reference Project Compatibility

CareerNewsBot V1 is intentionally derived from the proven reference architecture rather than introducing a new technical stack.

### Kept

- GitHub Actions deployment model
- One-file Python application structure
- Exa discovery
- Cerebras structured LLM processing
- `requests` session/retry layer
- `feedparser`
- `trafilatura`
- BeautifulSoup extraction
- Pillow image pipeline
- Rich Telegram publishing
- Bot API fallback
- `news_state.json`
- `posted_urls.txt`
- persistent state commits
- deterministic self-tests

### Changed

```text
Tech source universe
        ↓
Bangladesh job source universe

24-hour window
        ↓
72-hour window

Tech importance score
        ↓
Job usefulness + eligibility score

Technology story schema
        ↓
Career vacancy schema

THE CONTEXT / BOTTOM LINE
        ↓
Suitable For / Key Highlights / Apply Now

@TheTechNewsroom
        ↓
@CareerNewsroom
```

This keeps technical risk low while replacing the parts that actually need to change for the CareerNewsroom use case.
