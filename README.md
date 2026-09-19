# CareerNewsroomBot V5

Fast, incremental Bangladesh job-news pipeline for Career Newsroom.

## Production source architecture

V5 intentionally uses **only two job-board sources**:

```text
Government jobs
    Teletalk AllJobs API
            │
            ▼
      government pool

Private jobs
    Bdjobs API
            │
            ▼
    Bdjobs official HTML
            │
            ▼
       private pool

            └──────────────┐
                           ▼
                    canonical dedup
                           ▼
                  bounded research
                           ▼
               deterministic filtering
                           ▼
                  private ranking
                           ▼
              one optional Cerebras review
                           ▼
                 government first, up to 5
                           ▼
                    fill with private
                           ▼
                     hard max 20
                           ▼
                 Telegram Rich Message
```

### Important source policy

- **Dohaj has been removed from the production acquisition pipeline.**
- Dohaj is not a government fallback.
- Dohaj is not a private fallback.
- Ever Jobs is not used.
- Bdjobs DynamicFetcher/browser scraping is not used in the normal production pipeline.
- If Teletalk returns fewer government jobs, the bot simply has fewer government jobs.
- If Bdjobs returns fewer private jobs, the bot simply has fewer private jobs.
- The bot does not silently substitute a different job board.

This makes source attribution predictable and prevents the previous run from becoming dominated by Dohaj listings.

## Publication rules

- Hard maximum: **20 posts per run**.
- Minimum target: **5 posts** when enough eligible new jobs exist.
- Government jobs are selected separately and appear first, with a maximum of **5**.
- There is **no artificial government minimum**.
- Government jobs do not receive a BBA/MBA relevance gate.
- Private jobs are filtered for BBA/MBA/business relevance.
- Private jobs with an explicit experience requirement above 3 years are excluded by default.
- Already-published jobs are removed using persistent state and canonical/event deduplication.

## Government source: Teletalk

Primary endpoint:

`https://alljobs.teletalk.com.bd/api/v1/published-jobs/search?searchKeyword=`

The bot reads the structured published-job response and keeps source-native identifiers such as the government job ID, title, organization, vacancy, deadline and application URL.

The bot does not fall back to another job board when Teletalk has fewer records.

## Private source: Bdjobs

V5 uses two official Bdjobs acquisition paths:

1. **Bdjobs JSON search endpoint**
   `https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch`
2. **Official Bdjobs listing HTML**
   `https://jobs.bdjobs.com/jobsearch-cache.asp`

The JSON response is preferred because it provides structured fields. The official listing page is used to enlarge the candidate pool when the API response is insufficient.

The current Bdjobs site has migrated toward an Angular SPA. V5 therefore does not depend on a guessed browser DOM or an undocumented browser-rendered card structure. The previous DynamicFetcher path returned HTTP 200 with zero extracted jobs in the production run, so it was removed from the normal pipeline.

Independent current scraper projects also report that Bdjobs has moved to the Angular SPA backed by `apiv1.bdjobs.com`, while maintaining separate provider-specific extraction logic. This is why V5 keeps the working official API/static paths instead of treating a successful browser HTTP status as proof that jobs were extracted.

## Deduplication

The pipeline uses:

- canonical URL
- persistent `posted_urls.txt`
- persistent `news_state.json`
- source job IDs when available
- normalized title/company/location
- event identity
- fuzzy duplicate detection

The same recruitment mirrored across different URLs can be collapsed before publication.

## Filtering

### Government

Government jobs are validated as genuine Teletalk records, checked for expiry/duplication, and ranked using freshness, deadline urgency and source-backed information quality. No BBA/MBA suitability filter is applied.

### Private

Private jobs must:

- come from Bdjobs
- have a usable title and company
- have an active deadline when a deadline is supplied
- match the BBA/MBA/business audience
- stay within the early-career experience cap

Private ranking considers education fit, early-career suitability, role relevance, freshness, deadline usefulness and information quality.

## Cerebras

Cerebras is optional. Deterministic filtering happens before AI review. At most one compact review is performed for the highest-ranked private candidates. If Cerebras fails or is unavailable, deterministic ranking continues without blocking the run.

## Performance design

The previous production run completed in about one minute, so V5 does not add heavyweight browser scraping. Detail retrieval remains bounded and parallelized.

Important defaults:

```text
MAX_STORIES_PER_RUN=20
MIN_STORIES_PER_RUN=5
MAX_GOVERNMENT_POSTS_PER_RUN=5
FAST_PRIVATE_CANDIDATE_TARGET=60
FAST_GOVERNMENT_CANDIDATE_TARGET=10
FAST_DETAIL_WORKERS=8
FAST_AI_CANDIDATE_LIMIT=20
FAST_DISCOVERY_TIMEOUT=15
FAST_DETAIL_TIMEOUT=18
TELETALK_API_TIMEOUT=15
MAX_PRIVATE_EXPERIENCE_YEARS=3
SCRAPLING_ENABLED=1
SCRAPLING_DYNAMIC_ENABLED=0
MAX_BDJOBS_DISCOVERY_PAGES=1
```

## Telegram output

The bot publishes text-only Telegram Rich Messages with the current Career Newsroom layout. The job snapshot includes available fields such as location, employment, workplace, education, experience, salary, vacancy, age, application, deadline and posted date.

Missing fields are omitted rather than filled with misleading placeholders.
