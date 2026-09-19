# Career News V1

BBA/MBA-focused Bangladesh job intelligence bot for Telegram.

## Purpose

Career News V1 searches a controlled set of Bangladesh job-board categories, builds a broad comparison pool, removes duplicates and invalid records, ranks the strongest private jobs, verifies only the strongest detail pages, optionally audits them with Cerebras, applies company/category diversity, and publishes a dynamic set of high-quality opportunities.

The system is designed to avoid the previous failure mode where a large global job pool is collected first and category relevance is determined afterward.

## Production sources

```text
Government → Teletalk AllJobs API
Private    → Bdjobs category discovery
```

No Dohaj, Ever Jobs, random search-engine results, uncontrolled aggregators, or alternate-board substitution is used.

## Private discovery: category first

The Bdjobs private lane uses the configured BBA/MBA-relevant category universe:

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
PRIVATE_DISCOVERY_TARGET = 120
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
Jina Reader fallback
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

Scrapling is not required for the V1 runtime path. `curl_cffi` is the browser-impersonation layer and Jina is the plain-text fallback.

A detail-page failure is not allowed to silently delete a candidate. When the Bdjobs detail page is blocked, thin, or unavailable, the pipeline records the failure and preserves a job when the listing already contains enough source-backed fields.

The production log exposes separate private detail counts:

```text
PRIVATE DETAIL SUCCESS
PRIVATE DETAIL FALLBACK
PRIVATE DETAIL FAILED
```

This makes a zero-private run diagnosable instead of appearing as a normal successful publication run.

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
Top 40 detail candidates
      ↓
Detail enrichment
      ↓
Top 30–35 semantic audit
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
TARGET_STORIES_PER_RUN = 15
MAX_STORIES_PER_RUN    = 20
QUALITY_FLOOR          = 65
```

The count is dynamic. The bot never pads the feed with weak vacancies merely to reach a target.

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

## State

The repository keeps the same simple GitHub-friendly state files:

```text
news_state.json
posted_urls.txt
```

State tracks source identity, job identity, dates, ranking values, lifecycle status, publication time, and pipeline version.

Candidates receive an explicit lifecycle disposition rather than silently disappearing between stages.

## Repository tree

```text
Career News V1/
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

Optional AI configuration:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

Important discovery/ranking settings:

```text
MAX_POST_AGE_DAYS=5
PRIVATE_DISCOVERY_TARGET=120
PRIVATE_DISCOVERY_MAX=200
PRIVATE_FAST_RANK_TARGET=40
PRIVATE_DETAIL_TARGET=40
AI_REVIEW_TARGET=35
DETAIL_WORKERS=8
QUALITY_FLOOR=65
MAX_STORIES_PER_RUN=20
TARGET_STORIES_PER_RUN=15
```

## Local commands

```bash
python main.py --self-test
python main.py --source-test
python main.py --dry-run
python main.py --print-ranking
```

`--self-test` is offline and must pass before a release.

`--source-test` performs live source diagnostics and checks the Teletalk API plus one Bdjobs category through the V1 acquisition chain.

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
