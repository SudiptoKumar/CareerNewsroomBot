# CareerNewsroomBot V4

Fast, incremental Bangladesh job-news pipeline for Career Newsroom.

## Goal

Each scheduled run is designed to finish in **under 5 minutes under normal source/network conditions**, while avoiding the historical-archive crawl and large AI batches that slowed older versions.

```text
Government: Teletalk API → Dohaj fallback
Private:    Bdjobs API → Ever Jobs bridge (optional) → Bdjobs HTML → Dohaj
                         ↓
                 early dedup + relevance gate
                         ↓
                    small shortlist
                         ↓
                parallel detail retrieval
                         ↓
                source-backed extraction
                         ↓
                 deterministic ranking
                         ↓
             ONE optional Cerebras review
                         ↓
                 3–5 government first
                         ↓
               fill with private jobs
                         ↓
                    max 20 posts
                         ↓
                 Telegram Rich Message
```

## Publication rules

- Maximum **20 posts per run**.
- Minimum target **5 posts** when enough eligible new jobs exist.
- Government jobs occupy the first positions, with **3–5** published whenever enough valid new government vacancies are available.
- Government jobs have **no BBA/MBA relevance gate**.
- Private jobs must match the channel's BBA/MBA/business-candidate audience and must not require more than **3 years** of experience by default.
- Missing source fields are omitted instead of replaced with `—`.
- Bengali script is blocked from final publication; government fields are translated/normalized before rendering.
- Photo downloading and logo placeholders are disabled.

## Sources

### 1. Government: Teletalk AllJobs API

Primary endpoint:

`https://alljobs.teletalk.com.bd/api/v1/published-jobs/search?searchKeyword=`

V4 reads one broad published-jobs response and normalizes source-native fields such as:

- `job_primary_id`
- `job_title`
- `org_name`
- `vacancy`
- `deadline_date`
- `application_site_url`

This endpoint and field set are documented by an independent open-source Teletalk AllJobs search project. See:

`https://github.com/SazidulAlam47/teletalk-alljobs-govjob-search`

The bot does not depend on this project at runtime.

### 2. Government fallback: Dohaj

`https://dohaj.com/gov-jobs`

Dohaj government is queried only when Teletalk does not provide at least the minimum government candidate count.

### 3. Private: Bdjobs backend API

`https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch`

V4 uses the current unparameterized response as a **bounded newest-page probe**. The exact public pagination contract is not documented well enough to justify guessing parameter names, so V4 does not send speculative `page`, `pageno`, or `PageIndex` requests.

The API response is mapped directly into the bot's internal schema and is filtered for BBA/MBA/business relevance before detail-page work.

### 4. Optional private fallback: Ever Jobs

Ever Jobs explicitly lists **BDJobs** as a Bangladesh source and describes its BDJobs adapter as HTML parsing with Cheerio. Its REST API exposes `POST /api/jobs/search` and accepts `siteType: ["bdjobs"]`.

Repository:

`https://github.com/ever-jobs/ever-jobs`

V4 can call a **self-hosted Ever Jobs instance** only when `EVER_JOBS_API_URL` is configured. It is optional and is not required to run CareerNewsroomBot.

### 5. Private fallback: direct Bdjobs HTML

`https://jobs.bdjobs.com/jobsearch-cache.asp`

The raw HTML path is retained because the Bdjobs site has a client-rendered job-search interface. V4 invokes it only when the API/optional Ever Jobs path does not supply enough candidates.

### 6. Private: Dohaj business sections

The existing dedicated business-heavy Dohaj category feeds remain in place. Page 1 is queried concurrently; page 2 is used only if the private candidate pool remains short.

## Bdjobs strategy

The important change from the previous version is **bounded fallback routing**:

```text
Bdjobs API
   ↓ insufficient?
Ever Jobs bridge, if configured
   ↓ insufficient?
Bdjobs HTML
   ↓
stop when candidate target is reached
```

The bot no longer runs every Bdjobs path at full size on every cycle, and it no longer guesses undocumented API pagination parameters.

The API path is based on the endpoint already used by independent job-scraping software, but V4 treats current freshness/pagination as an operational concern rather than assuming the endpoint is an officially documented public API.

## Deduplication

V4 uses:

1. Canonical source URL
2. Persistent `posted_urls.txt`
3. Persistent `news_state.json`
4. Source-native job IDs where available
5. Normalized title + company + location
6. Application-target identity
7. Fuzzy title/company/location matching
8. Cross-source mirror detection

A Bdjobs job mirrored on Dohaj, or the same Teletalk recruitment appearing through another source, can therefore be collapsed before publication.

## Filtering and ranking

### Private

Hard gate:

- BBA/MBA/business relevance
- non-senior role
- maximum 3 years explicit experience by default
- active deadline
- not already posted

Ranking considers:

1. BBA/MBA and related business education fit
2. Fresher / early-career suitability
3. Role relevance
4. Posting freshness
5. Deadline usefulness
6. Information completeness
7. Optional AI editorial score

### Government

Government posts are ranked separately using:

- posting freshness
- deadline urgency
- source-backed information quality

## AI usage

Cerebras is optional.

Deterministic filtering and ranking happen first. At most **20 private candidates** are sent in **one compact Cerebras request** for editorial review. If Cerebras is unavailable, rate-limited, or returns an error, V4 immediately falls back to deterministic ranking. There is no multi-batch retry loop.

## Retrieval performance

Only shortlisted jobs receive detail-page requests, and those requests run concurrently with a bounded worker pool.

Default controls:

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
TELETALK_API_TIMEOUT=15
EVER_JOBS_TIMEOUT=15
POST_DELAY_SECONDS=1.0
MAX_PRIVATE_EXPERIENCE_YEARS=3
SCRAPLING_ENABLED=1
SCRAPLING_DYNAMIC_ENABLED=1
SCRAPLING_DYNAMIC_TIMEOUT_MS=15000
SCRAPLING_DYNAMIC_WAIT_MS=1500
SCRAPLING_DYNAMIC_MAX_CANDIDATES=40
MAX_BDJOBS_DISCOVERY_PAGES=1
```

## Telegram output

Telegram Bot API `sendRichMessage` is used for publishing.

The job snapshot keeps one consistent field order:

- Location
- Employment
- Workplace
- Education
- Experience
- Salary
- Vacancy
- Age
- Application
- Deadline
- Posted

Unavailable fields are omitted.

The Rich Message also includes the Career News pull-quote, hashtags, linked channel identity and source link used by the current design.

## GitHub Actions

The included workflow:

- checks out the repository
- installs Python dependencies
- runs `py_compile`
- runs the V4 self-test
- runs the production bot
- persists `news_state.json` and `posted_urls.txt`

The workflow keeps a small execution safety margin above the target runtime; the pipeline itself is designed around a much smaller discovery and AI workload than the old archive-crawl design.

## Required secrets

```text
TELEGRAM_BOT_TOKEN
```

Optional:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
EVER_JOBS_API_URL
EVER_JOBS_API_KEY
```

`EXA_API_KEY` and Agent Reach are not required.

## Source diagnostics

Run:

```bash
python main.py --source-test
```

This checks the Teletalk API, Bdjobs API, Bdjobs HTML listing and Dohaj government page from the current environment and reports response status, elapsed time and available result counts.

## Validation

Run:

```bash
python -m py_compile main.py
python main.py --self-test
```

The self-test covers field extraction, experience gating, source-native job IDs, Teletalk record mapping, Bdjobs record mapping, duplicate behavior, government-first selection and Rich Message rendering.

## Important source-status notes

Ever Jobs currently lists BDJobs as an HTML/Cheerio source. A separate TypeScript JobSpy implementation currently reports BDJobs as having moved to the Angular SPA at `bdjobs.com/h/jobs` backed by `apiv1.bdjobs.com`. That is why V4 does not treat any single scraper as permanently authoritative.

Current public source references:

- Ever Jobs: `https://github.com/ever-jobs/ever-jobs`
- Teletalk AllJobs search project: `https://github.com/SazidulAlam47/teletalk-alljobs-govjob-search`
- TypeScript JobSpy BDJobs status: `https://github.com/alpharomercoma/ts-jobspy/blob/main/README.md`
- JobSpy JS changelog: `https://github.com/borgius/jobspy-js/blob/master/CHANGELOG.md`


## V4 production repairs
- Bdjobs official JSON discovery no longer applies BBA/experience filters before detail enrichment.
- The exact `Our Valuable Partners` navigation title seen in production is blocked as non-job noise.
- Bdjobs DynamicFetcher is disabled by default because the rendered Angular shell returned zero job cards in production; static/API paths remain primary.
- Government selection keeps source validity and expiry checks only, without adding a new government relevance gate.
- Existing hard publication cap and state-based duplicate protection are preserved.
