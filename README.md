# CareerNewsroomBot Polish

Fast, incremental Bangladesh job-news pipeline for Career Newsroom with a polished, standardized post format.

## Core goal

Each scheduled run should finish in **about 5 minutes or less under normal source/network conditions**.
The bot does **not** crawl the historical job archive on every run.

```text
Dedicated source URLs
        ↓
Latest listing pages only
        ↓
Early duplicate + cheap relevance filter
        ↓
Small detail-page shortlist
        ↓
Parallel detail retrieval
        ↓
Source-backed extraction
        ↓
Deterministic ranking
        ↓
Optional ONE Cerebras review of top private candidates
        ↓
3–5 Government first
        ↓
Fill remaining slots with private BBA/MBA jobs
        ↓
Hard maximum 20
        ↓
Consistency validation
        ↓
Polish formatter: fixed table + English government text + DD-MM-YYYY dates
        ↓
Telegram Rich Message
```

## Publication rules

- Maximum **20 posts per run**.
- Minimum target **5 posts** when at least 5 eligible new jobs exist.
- **3–5 government jobs first** whenever at least 3 valid new government jobs are available.
- Government jobs have **no BBA/MBA filter**.
- Private jobs must be relevant to BBA/MBA/business candidates.
- Private ranking priority:
  1. BBA/MBA and related business education fit
  2. Fresher/no-experience/early-career suitability
  3. Role relevance
  4. Publish freshness
  5. Deadline usefulness
  6. Job-information quality
  7. Optional AI editorial fit
- More years of experience are not automatically ranked higher. Early-career roles are favored for the channel audience.
- Photo feature is completely disabled.

## Source discovery

### Government

`https://dohaj.com/gov-jobs`

V1 reads the latest government listing page first and goes to another page only when the current candidate pool is insufficient. It does not scan the full government archive every run.

### Dohaj private

The bot uses the existing dedicated business-oriented sections:

- `https://dohaj.com/category/accounting-finance`
- `https://dohaj.com/category/marketing-sales`
- `https://dohaj.com/category/hr-org-development`
- `https://dohaj.com/category/gen-mgt-admin`
- `https://dohaj.com/category/commercial-supply-chain`
- `https://dohaj.com/category/secretary-receptionist`
- `https://dohaj.com/category/bank-non-bank-fin-institution`
- `https://dohaj.com/category/customer-service-call-centre`
- `https://dohaj.com/category/media-advertisement-event-mgt`
- `https://dohaj.com/category/production-operation`
- `https://dohaj.com/category/ngo-development`
- `https://dohaj.com/jobs/all`

Only the latest listing windows are read. Discovery stops when the private candidate pool reaches its target.

### Bdjobs

The official Bdjobs search page is used as the source. Page 1 is read first; page 2 is used only when the private pool is still too small.

## Detail retrieval

Only shortlisted jobs receive detail-page requests.

Detail pages are fetched concurrently with a bounded worker pool. Default: **8 workers**.

V1 does not use Exa search or Exa content retrieval. Direct source retrieval is authoritative and faster. If a detail page fails, that candidate is skipped rather than starting a slow external search workflow.

## AI usage

Cerebras is optional. Deterministic filtering/ranking happens first.

Only **one compact Cerebras batch**, containing at most 20 private candidates, is sent for editorial suitability review. If Cerebras returns a quota/rate-limit/error response, V1 immediately continues with deterministic ranking. There are no multi-batch retry loops.

## Ranking

Private score emphasizes:

- BBA/MBA fit
- Fresher/early-career fit
- Relevant business function
- Freshness
- Deadline
- Information completeness
- Optional AI tie-break

Government jobs use a separate freshness/deadline/quality score and are selected before private jobs.

## Duplicate protection

V1 uses:

1. Canonical source URL
2. Persistent `posted_urls.txt`
3. Persistent `news_state.json` events
4. Normalized title + company + location identity
5. Application-target identity when available
6. Fuzzy title/company/location matching
7. Cross-source mirror detection between Dohaj and Bdjobs

A job appearing in several Dohaj categories is treated as one vacancy.

## Field consistency

Each job is converted into one source-backed record. The Telegram post is rendered only from that record.

This prevents mismatches such as one job's vacancy appearing in another job, salary becoming vacancy, responsibilities becoming Experience, incorrect date formats, or the wrong source being shown.

### Table fields

The polished format uses the same 13 data rows on every post, so the table structure stays consistent across private and government jobs:

- Location
- Employment
- Workplace
- Education
- Experience
- Salary
- Vacancy
- Age
- Application
- Application Start
- Application End
- Deadline
- Posted

Unavailable fields use a compact `—` placeholder instead of changing the table structure. Application Start and Application End are separate rows, with one date per row.

Experience appears only for real duration/fresher status. Technical skills, responsibilities and `Area of Experience` text are not treated as Experience.

## Polish formatting

- Company names are normalized to readable title case instead of appearing in all-lowercase source casing.
- Detail values are normalized to readable casing.
- Age is rendered as `18-30 Years` or `25 Years`; phrases such as `at least 25 years` are removed.
- All displayed dates use `DD-MM-YYYY`.
- Government posts use `Source: Dohaj`, not `Source: Dohaj Government Jobs`.
- Government circular titles and Bengali fields are converted to English before publication. One compact Cerebras translation call is used for Bengali government fields when available; a local English/Latin fallback keeps the post publishable if the AI service is unavailable.

## Telegram design

The polished version uses Telegram Bot API `sendRichMessage` with native Rich Message blocks. The table is:

- bordered
- striped
- compact for mobile

No photo block, logo fallback, placeholder image or generated image is used.

`APPLY NOW` is shown when a verified application URL exists. Otherwise `READ MORE` opens the source page.

## Runtime controls

```text
MAX_STORIES_PER_RUN=20
MIN_STORIES_PER_RUN=5
MIN_GOVERNMENT_POSTS_PER_RUN=3
MAX_GOVERNMENT_POSTS_PER_RUN=5
FAST_PRIVATE_CANDIDATE_TARGET=60
FAST_GOVERNMENT_CANDIDATE_TARGET=10
FAST_DETAIL_WORKERS=8
FAST_AI_CANDIDATE_LIMIT=20
FAST_DISCOVERY_TIMEOUT=15
FAST_DETAIL_TIMEOUT=18
POST_DELAY_SECONDS=1.0
```

## GitHub secrets

Required:

```text
TELEGRAM_BOT_TOKEN
```

Optional:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

`EXA_API_KEY` is not required in V1.

## Validation

```bash
python -m py_compile main.py
python main.py --self-test
```
