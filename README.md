# CareerNewsBot V3.1 Final

Telegram career-news bot for **@CareerNewsroom**. Built around the same proven runtime pattern as the reference TechNewsroom bot, with a career-specific discovery, scoring, fact-locking, application-link resolution, source diversity, and Telegram presentation layer.

## Mission

Find recent Bangladesh job vacancies that are genuinely useful to young job seekers, with extra relevance for:

- BBA / MBA / business roles
- Finance, accounting, banking
- Management trainee / graduate / fresher / entry-level
- Marketing, sales, business development
- HR, administration, operations
- Supply chain / procurement / commercial
- NGO / development
- Internships and trainee roles

The audience is already the Bangladesh career audience. The bot does **not** add generic “Bangladeshi applicants”, gender, or similar filler to posts unless such a source fact is directly useful and belongs in an allowed field.

## Discovery window

The active discovery inventory is a rolling **72-hour window**. New and previously discovered but unpublished jobs remain in the persistent queue while still inside that window.

Expired vacancies are rejected. Deadline distance is a **ranking signal**, not a hard seven-day gate.

## Publishing target

```text
Minimum target: 5
Normal target:  10
Maximum:        15
```

The bot never fabricates or weakens factual checks to force five posts. When fewer than five genuinely valid jobs exist, it publishes the valid jobs available.

## Source diversity

The selector uses round-robin source diversification before the final ranking pass.

Dynamic per-source limits:

```text
5+ sources  -> 3 posts/source
4 sources   -> 4 posts/source
3 sources   -> 5 posts/source
2 sources   -> 8 posts/source
1 source    -> no diversity cap
```

This prevents a single portal from dominating a healthy multi-source run while still allowing a low-source run to produce useful volume.

## Primary job sources

### Bdjobs

```text
New Jobs
https://jobs.bdjobs.com/bn/otherjobsbn.asp?JobType=new

General job search
https://jobs.bdjobs.com/jobsearch-cache.asp

Internship search
https://jobs.bdjobs.com/jobsearch-cache.asp?requestType=internship
```

Bdjobs discovery is scored heavily across the target audience lanes rather than blindly publishing all listings.

### BDJobs Live

```text
New Jobs
https://www.bdjobslive.com/bdjobs-circular/new-job-circular-in-bangladesh

Internships
https://www.bdjobslive.com/bdjobs-circular/internship-opportunity

Freshers
https://www.bdjobslive.com/bdjobs-circular/fresher-jobs

Accounting / Finance
https://www.bdjobslive.com/bdjobs-circular/accounting-finance-jobs

Bank / Financial Institution
https://www.bdjobslive.com/bdjobs-circular/bank-financial-institution-jobs

General Management / Admin
https://www.bdjobslive.com/bdjobs-circular/general-management-admin-jobs

HR / Organizational Development
https://www.bdjobslive.com/bdjobs-circular/hr-organizational-development-jobs

Marketing / Sales
https://www.bdjobslive.com/bdjobs-circular/marketing-sales-jobs

Supply Chain / Procurement
https://www.bdjobslive.com/bdjobs-circular/supply-chain-procurement-jobs

Government Jobs
https://www.bdjobslive.com/bdjobs-circular/government-jobs-in-bangladesh
```

### Additional portals / official sources

```text
Dohaj                 https://dohaj.com/jobs
Job.com.bd            https://job.com.bd/jobs/new_jobs/
Smart Job             https://smartjob.portal.gov.bd/
Alljobs Teletalk      https://alljobs.teletalk.com.bd/
BPSC                  https://bpsc.gov.bd/
BCC e-Recruitment     https://erecruitment.bcc.gov.bd/
JobsNoticeBD          https://jobsnoticebd.com/
JobsInfo              https://jobsinfo.bd/
JobFeeds              https://jobfeeds.online/
CircularBD            https://www.circularbd.com/alljobs
Bangladesher Khabor   https://www.bangladesherkhabor.net/jobs
Dhaka Post            https://www.dhakapost.com/jobs-career/
Dhaka Tribune         https://bangla.dhakatribune.com/jobs
Bangla Tribune        https://www.banglatribune.com/jobs
JagoNews24            https://www.jagonews24.com/topic/%E0%A6%9A%E0%A6%BE%E0%A6%95%E0%A6%B0%E0%A6%BF
Prothom Alo            https://www.prothomalo.com/collection/chakri-all
```

## RSS discovery

RSS sources include Bangladesh job publishers and major Bangladesh news/job feeds. Google News RSS and Exa are gap-fill sources, not the primary truth layer.

The system can discover a large candidate pool before expensive processing. Candidate volume is deliberately separated from publication quality.

## Advanced relevance model

Every candidate receives local scoring for:

```text
Freshness
Deadline usefulness
Source reliability
Data completeness
Job signal
Audience fit
```

Audience fit gives extra weight to:

```text
BBA / MBA
Finance / Accounting
Banking
Management Trainee
Graduate / Fresher / Entry Level
Internship
Marketing / Sales
HR
Business Development
Operations
Supply Chain / Procurement
NGO / Development
```

Cerebras is used for batched ranking of the strongest candidates, with deterministic local fallback when the API is unavailable or rate-limited.

## Source-first JobRecord

Each accepted vacancy is converted into an immutable source-backed record before editorial rendering.

Authoritative fields:

```text
Title
Company
Location
Job type
Education
Experience
Salary
Vacancies
Age limit
Application fee
Application method
Application period
Selection process
Deadline
Source URL
Apply URL
Posted date
```

AI does not replace those facts.

AI is used for ranking and editorial emphasis, not for inventing or replacing the source vacancy identity.

## Source URL vs Apply URL

These are deliberately separate.

**Source URL** = where the job details are read.

**Apply URL** = the actual application destination.

The Telegram body shows:

```text
🔎 Official Source: Source Name
```

with the source link attached to the source name.

The body does not expose raw URLs.

The final button uses only the actionable application URL:

```text
APPLY NOW
```

It is a real Telegram **InlineKeyboardMarkup** button, not a `<tg-button>` embedded in Rich HTML.

When a distinct actionable application URL cannot be resolved, the candidate is not published.

## Telegram post format

```text
📣 Job Title

🏢 Company: Company Name

JOB SNAPSHOT

FIELD | DETAILS
Location | Dhaka
Type | Full-time
Vacancies | 02
Education | BBA / MBA
Experience | 0–2 years
Salary | BDT 40,000
Deadline | 28 September 2026
Posted | 18 September 2026
Application | Online

#BBA #MBA #CareerNewsroom

🔎 Official Source: Bdjobs

[ APPLY NOW ]
```

The table is dynamic. Missing fields are omitted completely. There is no `Not specified` placeholder.

Removed from the post:

```text
Suitable For
Key Highlights
Gender
Bangladeshi applicants
raw URLs
```

High-impact fields are kept; low-value repetition and long copied responsibilities are skipped.

## Image behavior

A vacancy never fails because it has no photo.

```text
Usable article/source image -> publish with image
No usable image            -> publish text-only
```

The existing 1200×675 image processing path is retained.

## Verification

The runtime retains the reference bot's protection layers where they are useful for career content:

- canonical URL normalization
- URL deduplication
- event/vacancy deduplication
- source validation
- source-backed JobRecord
- deadline grounding
- numeric grounding
- optional claim verification
- immutable factual fields
- Apply URL validation
- Telegram Rich HTML size checking
- publish-state persistence

Missing information is not treated as false information. A source may omit salary, education, or experience; the field is simply omitted from the card unless the source provides it.

## Persistent inventory

`news_state.json` stores the rolling career inventory, events, publication state, category coverage, and run history.

`posted_urls.txt` provides a durable URL-level duplicate barrier.

`source_health.json` stores source success/failure information for later diagnostics.

## GitHub Actions

The scheduled workflow runs in `Asia/Dhaka` and supports manual dispatch.

Required repository secrets:

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

Workflow steps:

```text
Checkout
→ Python 3.12
→ install requirements
→ py_compile
→ self-test
→ CareerNewsBot run
→ persist state
```

## Performance design

The bot does not call the LLM once for every discovered vacancy.

```text
Many discoveries
→ cheap deterministic filtering
→ local scoring
→ batched Cerebras ranking
→ top candidate extraction
→ fact-locking
→ Telegram rendering
```

The ranking stage is batched to reduce Cerebras rate-limit pressure.

## Expected healthy run

The target is not an artificial fixed number of websites or API calls. A healthy run should be capable of producing 5–15 genuinely useful posts when the 72-hour inventory contains enough eligible vacancies.

A typical funnel is:

```text
many discovered links
→ URL dedup
→ real job candidates
→ Bangladesh-relevant vacancies
→ event dedup
→ local ranking
→ batched AI ranking
→ source-backed verification
→ 5–15 publishable jobs
```

## Commands

```bash
python main.py --self-test
python main.py
```

## Repository tree

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
├── requirements.txt
└── source_health.json
```
## V3.1 reliability fixes

- Direct portal HTTP errors (403/404/5xx) are treated as source failures and skipped without aborting the run.
- The direct-portal consumer validates that a usable response object exists before reading `.text`.
- GitHub Actions uses the current Node 24-compatible `actions/checkout@v7` and `actions/setup-python@v7` releases.

