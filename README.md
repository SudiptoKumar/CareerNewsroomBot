# CareerNewsroomBot V6

CareerNewsroom V6 is a fast Bangladesh job-intelligence pipeline for BBA/MBA students, graduates, freshers and early-career business candidates. It is designed to reduce a broad latest-job pool into a small set of high-value, actionable Telegram posts.

## Production sources

```text
Government
  Teletalk AllJobs API
        │
        ├─ active/current validation
        ├─ source-native dedup
        └─ freshness + deadline ranking
        │
        ▼
  Government stream (up to 5)

Private
  Bdjobs API + official Bdjobs HTML
        │
        ├─ broad latest-job collection (~100)
        ├─ canonical dedup
        ├─ cheap 100 → 40 funnel
        ├─ detail enrichment only for finalists
        ├─ deterministic 100-point career score
        ├─ one optional Cerebras semantic audit
        └─ diversity-aware reranking
        │
        ▼
  Private stream

Government + Private
        │
        ▼
  hard maximum 20
        │
        ▼
  Telegram Rich Messages
```

### Source policy

- Dohaj is completely outside the production acquisition path.
- Ever Jobs is not required.
- Bdjobs DynamicFetcher/browser rendering is disabled in the production path.
- Teletalk is the government source.
- Bdjobs is the private source.
- The bot does not silently substitute another job board.

## Why the private pipeline changed

The Bdjobs job-search page exposes functional categories, organization/industry filters, location, posted-within filters, deadline filters, job nature, job level, fresher/experience bands and jobs-per-page options including 100. The page can also show Featured jobs ahead of normal listings. V6 therefore does not trust visual order as the definition of “best” or “latest”; it collects the available recent candidates and ranks them using their actual posted/deadline data.

Current source reference: `https://jobs.bdjobs.com/jobsearch-cache.asp`

The Teletalk search API returns structured government fields including source job ID, title, organization, vacancy, deadline and application URL.

Current source reference: `https://alljobs.teletalk.com.bd/api/v1/published-jobs/search?searchKeyword=`

## 100 → 40 → 20 selection model

### Stage 1: Broad discovery

The private collector targets about 100 unique Bdjobs records. The parser intentionally does **not** require a business keyword at this point. This is important because a generic title such as `Executive`, `Officer`, `Assistant` or `Associate` can still turn out to be an excellent BBA/MBA vacancy once the authoritative fields are available.

### Stage 2: Cheap funnel

The broad pool is quickly sorted using only fields available from the listing response:

- business-role signal
- fresher/trainee/intern signal
- posted-date freshness
- deadline urgency
- specialist/non-business title signal

Only the best ~40 private candidates receive the more expensive research/enrichment step.

### Stage 3: Deep source-backed enrichment

Bdjobs API records already carry structured fields and therefore do not need a detail request merely to become rankable. HTML-discovered records receive authoritative detail-page retrieval. Detail retrieval is concurrent and bounded.

A detail request can enrich descriptions or recover an application URL, but a failed detail page must not destroy an otherwise complete API candidate.

## Private 100-point career score

Each researched private job receives a transparent base score:

| Factor | Points | What it measures |
|---|---:|---|
| BBA/MBA education fit | 25 | Explicit BBA/MBA or business-degree eligibility |
| Business role/function fit | 20 | Finance, banking, marketing, sales, HR, supply chain, management, operations, etc. |
| Career-stage fit | 15 | Fresher/intern/0–1 year gets the strongest score, then gradually declines with experience |
| Freshness | 15 | Posted today/recently receives the strongest score |
| Deadline | 10 | Active and actionable deadlines receive more weight |
| Salary | 5 | Numeric salary disclosure and relative salary strength |
| Vacancy | 5 | More openings receive a small, diminishing bonus |
| Information quality | 5 | Completeness of source-backed job fields |
| **Total** | **100** | |

Experience is a **ranking signal**, not an automatic three-year rejection. A 5-year BBA/MBA business role can still be relevant, but an equivalent fresher/early-career role receives a higher career-stage score. Clearly specialist technical, medical and senior leadership roles are filtered/downranked based on the actual role evidence.

## Career-family classification

The ranking engine maps private jobs into business career families such as:

- Finance & Accounting
- Banking & Financial Services
- Marketing & Brand
- Sales & Business Development
- HR & Recruitment
- Supply Chain & Procurement
- Management & Administration
- Operations
- Corporate & Compliance
- Research & Analytics
- Customer & Client Service
- NGO & Development
- Retail & Branch Operations
- Merchandising

This lets the final selector avoid filling the entire feed with near-identical roles when other strong business careers are available.

## Cerebras role

Cerebras is an optional **semantic auditor**, not the final publisher.

One compact batch reviews up to 32 high-ranked private candidates for hidden mismatches such as:

- the title looking business-oriented while the actual mandatory degree is unrelated;
- specialist licensing/degree requirements;
- material contradictions between the title and description;
- an apparently junior title carrying clearly senior requirements.

The AI returns a semantic-fit signal and a red-flag signal. The deterministic score remains the main ranking authority. If Cerebras fails, the pipeline continues using the deterministic score.

## Final selection

Government jobs are selected independently and placed first, up to 5. They receive no BBA/MBA suitability gate. Private jobs then fill the remaining slots.

Private selection uses a small diversity adjustment so one company or career family does not monopolize the feed. The adjustment is intentionally weaker than job quality, so variety cannot routinely push a genuinely excellent vacancy below mediocre jobs.

The final feed is capped at 20 posts. It can publish fewer than 20 when the available candidates do not meet the quality floor.

## Duplicate protection

V6 keeps:

- canonical URL identity
- source-native job IDs where available
- persistent `posted_urls.txt`
- persistent `news_state.json`
- normalized title/company/location identity
- event identity
- fuzzy duplicate detection

This also prevents repeat publication when a vacancy is rediscovered through both Bdjobs acquisition paths.

## Runtime defaults

```text
MAX_STORIES_PER_RUN=20
FAST_PRIVATE_CANDIDATE_TARGET=100
FAST_GOVERNMENT_CANDIDATE_TARGET=10
PRIVATE_RESEARCH_TARGET=40
FAST_DETAIL_WORKERS=10
FAST_AI_CANDIDATE_LIMIT=32
PRIVATE_QUALITY_FLOOR=60
FAST_DISCOVERY_TIMEOUT=15
FAST_DETAIL_TIMEOUT=18
MAX_BDJOBS_DISCOVERY_PAGES=2
POST_DELAY_SECONDS=1.0
SCRAPLING_ENABLED=1
SCRAPLING_DYNAMIC_ENABLED=0
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

## Validation

```bash
python -m py_compile main.py
python main.py --self-test
python main.py --source-test
```

`--source-test` is a live diagnostic and depends on GitHub Actions having outbound network access.

## Telegram output

The existing Career Newsroom Rich Message design is preserved. Missing source fields are omitted instead of populated with misleading placeholder values. Photo blocks remain disabled.
