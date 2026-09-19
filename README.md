# Career News Bot

Career News Bot is a Bangladesh job-intelligence pipeline for BBA/MBA students, graduates, freshers and early-career business candidates. It publishes Telegram Rich Messages from two production sources: Teletalk for government jobs and Bdjobs for private jobs.

## Sources

### Government
Teletalk AllJobs API is the government source. Government vacancies are evaluated separately and are not passed through the private BBA/MBA relevance gate.

### Private
Bdjobs is the only private-job source. The bot uses 14 configured career categories as its **local intelligence/selection lanes**:

1. Accounting / Finance
2. Bank / Non-Bank Financial Institution
3. Commercial / Supply Chain
4. Marketing / Sales
5. HR / Organization Development
6. General Management / Admin
7. Customer Service / Call Centre
8. Media / Advertisement / Event Management
9. Research / Consultancy
10. NGO / Development
11. Hospitality / Travel / Tourism
12. Garments / Textile
13. IT / Telecom - Business Roles
14. Education / Training - Business Roles

The production collector does **not** request all 14 Bdjobs category URLs every run. The current category routes redirect into the Angular jobs application, and repeated category probing from GitHub-hosted traffic has been producing 403 responses. Instead, the bot acquires the newest broad Bdjobs pool with a very small request budget, then classifies every candidate locally into the same career lanes. This preserves category-aware ranking and diversity without repeatedly hammering Bdjobs.

## Why the acquisition was changed

Previous production logs showed an important pattern: the same Bdjobs API and cache listing routes had returned HTTP 200 shortly before a later run returned HTTP 403. The failing version also performed a 14-category fan-out and ran live source diagnostics immediately before the real bot run. That created unnecessary repeated requests.

The repaired design therefore uses:

```text
Teletalk API                     Bdjobs API (1 probe)
      |                                  |
      |                           Bdjobs broad cache listing
      |                           (1-3 pages, only when needed)
      |                                  |
      +--------------------+-------------+
                           |
                    Candidate Pool
                           |
                URL/Event Deduplication
                           |
              Local category classification
                           |
               Cheap discovery priority
                           |
                 Detail enrichment
                           |
               Deterministic BBA/MBA gate
                           |
               100-point private score
                           |
                Cerebras semantic audit
                           |
             Diversity-aware final ranking
                           |
                    Telegram posts
```

There is also a short **recent Bdjobs state cache**. When the live Bdjobs source is temporarily unavailable, fresh unpublished candidates from the previous successful runs can be reused. The cache is still rechecked for deadline and five-day freshness before publication.

A Bdjobs outage with no fresh source data and no recent cache is treated as a real source failure. The run aborts before publication instead of reporting a misleading government-only success.

## Private ranking: 100 points

| Factor | Points |
|---|---:|
| BBA/MBA education fit | 20 |
| Business career/function fit | 20 |
| Fresher/early-career fit | 10 |
| Preferred age fit around 18-30 | 5 |
| Posted freshness | 15 |
| Deadline actionability | 10 |
| Salary | 5 |
| Vacancy | 5 |
| Information quality | 5 |
| Category confidence | 5 |
| **Total** | **100** |

Experience is ranked smoothly from fresher to experienced instead of using a universal three-year rejection. Private jobs with a verified posted date older than 5 days are excluded. Expired jobs are excluded.

## Cerebras

Cerebras is a semantic auditor for the strongest private candidates. It checks hidden education mismatch, specialist-degree requirements, seniority mismatch and title/description contradictions. The deterministic score remains the core ranking signal.

## Final selection

The selector is quality-first and diversity-aware. Underrepresented business categories get a small coverage bonus while company and career-family monopolies are penalized. There are no fixed category quotas, so weak jobs are not published merely to fill a category.

Government jobs are selected separately and appear before private jobs.

## Production request strategy

The normal scheduled run is every 3 hours. Private discovery is intentionally conservative:

```text
1 Bdjobs API probe
1 Bdjobs broad listing probe
Optional page 2/3 only when the first page exposes pagination
0 category HTTP fan-out
0 live source-test before every production run
```

This is specifically designed to avoid the 403 pattern seen in repeated GitHub Actions runs.

## Runtime configuration

```text
MAX_STORIES_PER_RUN=20
MAX_GOVERNMENT_POSTS_PER_RUN=5
FAST_PRIVATE_CANDIDATE_TARGET=160
PRIVATE_RESEARCH_TARGET=32
FAST_DETAIL_WORKERS=10
FAST_AI_CANDIDATE_LIMIT=32
MAX_PRIVATE_POST_AGE_DAYS=5
PRIVATE_QUALITY_FLOOR=64
MAX_BDJOBS_DISCOVERY_PAGES=3
BDJOBS_CACHE_MAX_DAYS=5
```

## Required secrets

```text
TELEGRAM_BOT_TOKEN
```

Optional:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

## Validation

```bash
python -m py_compile main.py
python main.py --self-test
```

The `--source-test` command performs only low-impact probes of Teletalk, the Bdjobs API and the broad Bdjobs listing. It does not fan out across the 14 categories.

## GitHub Actions

The workflow runs every 3 hours in Asia/Dhaka. Live source diagnostics are **opt-in** for a manually dispatched workflow and no longer run automatically before the production bot.

## Important operational behavior

A source returning HTTP 403 is not treated as an empty source. It is recorded as blocked. If there is no recent private cache, the run exits as failed rather than silently publishing only government jobs.
