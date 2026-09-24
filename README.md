# CareerNewsroom

## Latest snapshot display fix

Source-backed salary is now mandatory in the Telegram JOB SNAPSHOT whenever the Bdjobs/Teletalk extractor has a salary value. AI presentation preferences can no longer silently omit salary. Deadline, posted date, and location remain protected display fields as well.


Production BBA/MBA-focused Bangladesh job intelligence bot for Telegram.

Pipeline release: Career News V1.

## Purpose

CareerNewsroom searches a controlled set of Bangladesh job-board categories, builds a broad comparison pool, removes duplicates and invalid records, ranks the strongest private jobs, verifies only the strongest detail pages, optionally audits them with Cerebras, applies company/category diversity, and publishes a dynamic set of high-quality opportunities.

The system is designed to avoid the previous failure mode where a large global job pool is collected first and category relevance is determined afterward.

## Production sources

```text
Government → Teletalk AllJobs API
Private    → Bdjobs category discovery + BDJobs Live category discovery
```

BDJobs Live currently exposes the requested business-focused functional categories. CareerNewsroom adds these 14 categories as a separate discovery lane, then sends the resulting jobs through the same private-job gates used by Bdjobs.

1. Accounting / Finance
2. Bank / Financial Institution
3. Commercial
4. Company Secretary / Regulatory Affairs
5. Customer Service / Call Centre
6. E-commerce / Digital Marketing
7. General Management / Admin
8. HR / Organizational Development
9. Marketing / Sales
10. Media / Advertising / Event Management
11. NGO / Development
12. Production / Operation
13. Research / Consultancy
14. Supply Chain / Procurement

The category lane is bounded and does not create a second publication quota. Freshness, BBA/MBA relevance, source fidelity, information quality, experience, deadline, AI review, diversity, and duplicate gates remain shared with the existing private pipeline.

No Dohaj, Ever Jobs, random search-engine results, uncontrolled aggregators, or alternate-board substitution is used.


## Publication model

Publication is opportunistic, not quota-driven. The bot publishes every selected job that passes the normal quality, freshness, source-fidelity, snapshot, and duplicate gates, up to the total cap. If 4 eligible jobs exist, 4 are published. If 1 exists, 1 is published. If none qualify, 0 are published. None of these sparse outcomes is a failure.

```text
Minimum private jobs:    0
Minimum government jobs: 0
Minimum internships:     0
Maximum total posts:    20
```

## Bdjobs Listing Parser Reliability

Current Bdjobs search cards expose metadata through image ALT labels such as:

```text
Job Location
Experience required
Deadline for apply the job
Education required
```

The parser preserves those ALT labels and uses a DOM text window from each job link to the next job link. This prevents a title-only ancestor from causing the listing record to lose company, location, experience, deadline, and education.

A detail-page failure is still treated as an enrichment failure. Listing-backed fields remain available for ranking and publication. In production, the detail lane first uses `curl_cffi`, then renders the real Bdjobs page with Scrapling/StealthyFetcher when the site returns the Angular shell, and only then falls back to Jina/listing data.

## Resilient Bdjobs acquisition

Bdjobs detail pages are currently client-rendered. CareerNewsroom uses a bounded acquisition chain:

```text
curl_cffi browser impersonation
        ↓
current Bdjobs /h/details route
        ↓
Patchright-powered Scrapling render
        ↓
DOM-aware validation and extraction
        ↓
Jina Reader fallback
        ↓
listing preservation when enrichment is unavailable
```

The browser lane waits for the Bdjobs summary container and can also wait for network quiescence. No part of the parser relies on Tailwind utility classes or the first `<h1>`.

Current dependency pin: `scrapling[fetchers]==0.4.15`.

### Source-first extraction → AI selection

CareerNewsroom extracts the complete cleaned Bdjobs source document before AI review. It keeps structured source fields and the full cleaned source content. Cerebras is used only to classify relevance, identify internships, audit contradictions, and choose which available fields should appear in the compact snapshot. It never creates factual values.

```text
source facts → normalization → AI selection → render the original source facts
```

Age, Experience, Salary, Vacancy, Location, Published date and Deadline are independently bound to their own labels. A value from one field cannot populate another field.

### Job Snapshot data contract

The Telegram Job Snapshot is generated from one normalized `JobRecord`. Detail research is enrichment only and must never erase trustworthy listing data. Field precedence is:

```text
verified detail value
        ↓
listing value
        ↓
existing normalized value
        ↓
omit field when still missing
```

The display order is:

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

No value is fabricated. A sparse or contaminated private record with fewer than 3 usable snapshot fields is rejected before final selection; government records require at least 2. This prevents Telegram posts that contain only a deadline when the source data contains additional fields.

### Runtime quality and publication guardrails

Each run is allowed up to 15 minutes so the pipeline can spend more time on research and semantic auditing instead of stopping early.

```text
PRIVATE_DETAIL_TARGET = 60
AI_REVIEW_TARGET = 50
AI_BATCH_SIZE = 8

MIN_PRIVATE_POSTS_PER_RUN = 10
MIN_GOVERNMENT_POSTS_PER_RUN = 3
MAX_STORIES_PER_RUN = 20
```

Selection order is:

```text
strict quality pool
        ↓
controlled private minimum-fill pool
        ↓
hard private minimum-fill pool
        ↓
final diversity selection
```

The minimum-fill paths still require a fresh, non-expired business role, no clearly unrelated specialist career, a company name, and source-backed information. The bot never invents filler jobs merely to reach 10 private or 3 government posts.

### Jina Reader safety

Jina Reader returns LLM-friendly Markdown. CareerNewsroom converts that Markdown to plain parser-safe text before field extraction, removing image/link syntax, headings, asset filenames and page-chrome artifacts. This prevents strings such as `name-share-details.gif` or `[![Image ...](...)](...)` from appearing in Telegram posts.


## Bdjobs rendered-detail parser

Bdjobs detail pages currently use an Angular application shell. The production browser lane waits for `app-summary #allSection`, captures the rendered HTML, and parses stable Angular tags and IDs.

The parser uses these anchors:

```text
app-details-main              → job-detail container
button > h2 + next h2          → company + title
app-summary #allSection        → Vacancy / Age / Location / Salary / Experience / Published
#requirements                  → Education / Experience / Additional Requirements
#skills                        → skill chips
label p + value p              → Workplace / Employment Status / Job Location
Application Deadline           → deadline
app-company-info-card          → company address / size
```

The listing title is the trusted identity. Rendered-page title candidates are accepted only when they match that listing identity. A missing rendered title falls back to the listing title; an explicit mismatch never overwrites listing data.

The detail validity gate is structural. A page is not considered a usable Bdjobs detail document merely because it contains thousands of characters from footer or navigation content.

## Private discovery: category first

The original Bdjobs private lane remains unchanged and uses the configured BBA/MBA-relevant category universe. BDJobs Live contributes an additional 14-category functional lane with bounded per-category discovery and the same downstream funnel.

Both private sources feed one shared pool before detail research and selection, so the new source expands discovery without introducing a separate scoring or quota system.


```text
1   Accounting / Finance
2   Bank / Non-Bank Financial Institution
3   Commercial / Supply Chain
9   Marketing / Sales
17  HR / Organization Development
7   General Management / Admin
16  Customer Service / Call Centre
10  Media / Advertisement / Event Management
13  Research / Consultancy
12  NGO / Development
20  Hospitality / Travel / Tourism
6   Garments / Textile
8   IT / Telecom
4   Education / Training
```

Discovery is performed per category. Category priority changes discovery effort, not final ranking.

The comparison-pool targets are:

```text
PRIVATE_DISCOVERY_TARGET = 160
PRIVATE_DISCOVERY_MAX    = 200
```

These numbers are not publication quotas and are not implemented as a global "find 100 jobs first" request.

## Recency

Private jobs use the actual published date and a hard five-day freshness window:

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

Featured ordering or card position is never treated as the posted date.

## Cloudflare-resistant Bdjobs fetching

Bdjobs listing and detail pages use this fetch chain:

```text
curl_cffi browser impersonation
        ↓
rotate iOS Safari fingerprints on challenge
        ↓
Cloudflare challenge detection
        ↓
Jina Reader fallback (current /hn/details route first)
        ↓
plain-text / HTML extraction
```

Default browser fingerprints:

```text
safari18_0_ios
safari180_ios
safari184_ios
safari_ios
```

A Cloudflare response is detected from status, headers, and challenge-page markers. A normal HTTP 200 is not considered sufficient unless job content can actually be extracted.

Jina fallback format:

```text
https://r.jina.ai/<original-url>
```

Scrapling is required for the protected client-rendered Bdjobs detail fallback. `curl_cffi` is the first HTTP acquisition layer and Jina is the plain-text fallback when the browser path is unavailable.

A detail-page failure is not allowed to silently delete a candidate. When the Bdjobs detail page is blocked, thin, or unavailable, the pipeline records the failure and preserves a job when the listing already contains enough source-backed fields.

The production log exposes separate private detail counts:

```text
PRIVATE DETAIL SUCCESS
PRIVATE DETAIL FALLBACK
PRIVATE DETAIL FAILED
```

This makes a zero-private run diagnosable instead of appearing as a normal successful publication run.

## Permanent detail-page protection

The Bdjobs detail route can return an application shell with HTTP `200` while the actual job content is not present. CareerNewsroom now validates the rendered Bdjobs DOM structurally before merging detail fields, so CSS/JavaScript size can never make a non-job shell look like a valid detail page.

```text
curl_cffi + iOS Safari fingerprint
        ↓
visible-content validation
        ↓
fingerprint rotation
        ↓
Jina Reader
        ↓
same-card listing preservation
```

Listing preservation also extracts the company from the same job card when the card has no explicit `Company` label. A detail failure therefore does not erase a usable private candidate.

## Private intelligence pipeline

```text
Category discovery
      ↓
Normalize
      ↓
Deduplicate
      ↓
Business-career classification
      ↓
Hard validity gate
      ↓
100-point deterministic score
      ↓
Top 60 detail candidates
      ↓
Detail enrichment
      ↓
Top 50 semantic audit
      ↓
85% deterministic + 15% AI
      ↓
Company/category diversity
      ↓
Quality floor
      ↓
Dynamic 12–20 private/government assembly
      ↓
Telegram
```

## Private ranking model

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

Government jobs use a separate source-specific score and never enter the private BBA/MBA gate.

## AI policy

Cerebras is a semantic audit layer, not the source of truth.

It can evaluate:

```text
education match
role fit
career-stage fit
qualification contradictions
specialist-degree requirements
seniority
semantic relevance
```

Missing AI configuration, invalid AI JSON, rate limits, or timeouts fall back to deterministic ranking.

When AI is unavailable:

```text
final_score = deterministic_score
```

When AI is available:

```text
final_score = deterministic_score × 0.85 + ai_score × 0.15
```

## Publication rules

```text
MAX_STORIES_PER_RUN    = 20
MIN_PRIVATE_POSTS_PER_RUN = 0
MIN_GOVERNMENT_POSTS_PER_RUN = 0
MIN_INTERNSHIP_POSTS_PER_RUN = 0
QUALITY_FLOOR          = 65
```

Selection is dynamic. There is no requirement to fill a minimum count, private/government mix, or internship count. Quality and source-backed eligibility determine how many vacancies are published on each run.

Snapshot integrity is checked before final selection, so rejected sparse records do not consume quota slots. A defensive second check runs immediately before Telegram publication.

Private selection uses soft diversity penalties:

```text
~2 jobs per company
~3–4 jobs per career family
```

These are not hard quotas.

## Telegram output

Each selected vacancy becomes one text-only Telegram Rich Message with:

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
Deadline
Posted

Career News signature
Source
```

The source URL is retained and used for `READ MORE` when a verified application URL is not available. `APPLY NOW` is used only when an application URL is identified.

## Deadline expiry updater

At the start of every normal run, CareerNewsroom checks previously published events in `news_state.json` that have a stored Telegram `message_id` and deadline. A date-only deadline remains active through the end of that date in `Asia/Dhaka`.

When a deadline has passed, the bot edits the existing post in place:

```text
APPLY NOW
     ↓
EXPIRED DEADLINE  (danger/red button)
     ↓
https://t.me/CareerNewsroom
```

The original application URL is removed from the button. The clickable source URL is also removed from the edited rich message while the plain `Source: Bdjobs` / `Source: Teletalk` label remains. The event is then marked `expired` so the same message is not repeatedly edited.

The updater uses the Telegram Bot API `editMessageText` with the rich-message payload and `editMessageReplyMarkup` as a bounded fallback. Telethon is not required for this feature. Telegram controls the exact text contrast used with the `danger` button style.

The updater processes up to `DEADLINE_SWEEP_MAX_UPDATES_PER_RUN` expired posts per run (default `100`), oldest deadlines first. Any deferred backlog is processed by later scheduled runs.

## State

The repository keeps the same simple GitHub-friendly state files:

```text
news_state.json
posted_urls.txt
```

State tracks source identity, job identity, dates, ranking values, lifecycle status, publication time, and pipeline version.

Candidates receive an explicit lifecycle disposition rather than silently disappearing between stages.

## Production validation

The production repository intentionally contains no `tests/` directory and the GitHub Actions workflow does not run `pytest`. Deterministic regression checks live in `main.py --self-test` so the production tree stays compact. The validation used for this release also checks the real Angular Bdjobs extraction pattern: footer `<h1>` rejection, header `<h2>` title identity, title-suffix cleanup, wrong-job protection, summary/requirements extraction, and browser wait configuration.

## Repository tree

```text
CareerNewsroom/
│
├── README.md
├── requirements.txt
├── main.py
├── news_state.json
├── posted_urls.txt
│
└── .github/
    └── workflows/
        └── newbot.yml
```

The tree intentionally preserves the previous repository's GitHub-oriented flat structure.

## Environment variables

Required for publishing:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHANNEL
```

Optional non-secret expiry settings:

```text
TELEGRAM_PROMO_URL=https://t.me/CareerNewsroom
DEADLINE_SWEEP_MAX_UPDATES_PER_RUN=100
```

The deadline expiry updater does **not** require `API_ID`, `API_HASH`, or `TELETHON_SESSION`.

Optional AI configuration:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

Important discovery/ranking settings:

```text
MAX_POST_AGE_DAYS=5
PRIVATE_DISCOVERY_TARGET=160
PRIVATE_DISCOVERY_MAX=200
PRIVATE_FAST_RANK_TARGET=60
PRIVATE_DETAIL_TARGET=60
AI_REVIEW_TARGET=50
AI_BATCH_SIZE=8
AI_RETRY_COUNT=1
MIN_PRIVATE_POSTS_PER_RUN=0
MIN_GOVERNMENT_POSTS_PER_RUN=0
MIN_INTERNSHIP_POSTS_PER_RUN=0
PRIVATE_MIN_FILL_SCORE=58
PRIVATE_HARD_FILL_SCORE=55
PRIVATE_MIN_INFORMATION_QUALITY=3
PRIVATE_HARD_MIN_INFORMATION_QUALITY=3
DETAIL_WORKERS=8
DETAIL_TIMEOUT=14
JINA_TIMEOUT=12
QUALITY_FLOOR=65
MAX_STORIES_PER_RUN=20
JINA_RPM_LIMIT=24
```

`TARGET_STORIES_PER_RUN` is retained only as a legacy logging setting. It does not create a publication requirement.

## Local commands

```bash
python main.py --self-test
python main.py --source-test
python main.py --dry-run
python main.py --print-ranking
python main.py --reconcile-state <snapshot-dir>
```

`--self-test` is offline and must pass before a release.

`--source-test` performs live source diagnostics and checks the Teletalk API, one Bdjobs category, and one sampled Bdjobs detail page through the V1 acquisition chain.

`--dry-run` performs the pipeline without contacting Telegram.

## GitHub Actions

The workflow uses Python 3.12 on `ubuntu-24.04` and runs every three hours through GitHub Actions. Manual workflow dispatch also runs the live source diagnostics.

The workflow stores state changes back into the repository after each run.

## Security

Never commit:

```text
TELEGRAM_BOT_TOKEN
CEREBRAS_API_KEY
```

Use GitHub Actions Secrets.

## Release acceptance

A production release must satisfy all of the following:

```text
✓ category-first Bdjobs discovery
✓ no global-first 100-job discovery path
✓ Cloudflare detection
✓ curl_cffi browser impersonation
✓ iOS Safari fingerprint rotation
✓ Jina fallback
✓ Teletalk government lane
✓ five-day private freshness gate
✓ BBA/MBA/business classification
✓ specialist-role exclusion
✓ experience parsing and early-career ranking
✓ salary / vacancy / deadline scoring
✓ duplicate detection
✓ selective detail enrichment
✓ Cerebras structured audit
✓ safe deterministic fallback
✓ 85/15 final scoring
✓ company/category diversity
✓ quality floor
✓ dynamic publication count
✓ source identity consistency
✓ Telegram rendering validation
✓ self-test and compile check
```


## Publication constraints

```text
Private jobs: no minimum
Government jobs: no minimum
Internships: no minimum
Maximum total posts: 20 per run
Private snapshot minimum: 4 source-backed fields
```


## Cross-source duplicate protection

CareerNewsroom now treats Bdjobs and BDJobs Live as potentially overlapping vacancy sources. Duplicate decisions are layered: exact canonical source URL, matching source/application target, same-source native job IDs, then a conservative cross-source identity match using normalized job title + company, recent posting proximity, and location compatibility.

A company repost with different native IDs on the same source is not fuzzy-collapsed when both source IDs are explicitly different. Cross-source mirrors such as the same company and same role appearing on Bdjobs and BDJobs Live are collapsed before detail research and again protected by persistent published-state checks.

## Dynamic publication and state persistence

CareerNewsroom does not require a minimum number of posts per run. If only 4 qualifying jobs are found, it publishes 4. If none qualify, it publishes 0. A sparse run is a successful run.

After publishing, the Telegram `message_id`, deadline, canonical URL, and related state are persisted in `news_state.json`; `posted_urls.txt` preserves deduplication history. The workflow uses fast-forward-only Git pushes, retries transient failures, and, when `origin/main` advances independently, reconciles the local state snapshot with remote state before retrying. It never force-pushes or silently discards state. A genuinely unrecoverable persistence failure keeps the workflow failed.
