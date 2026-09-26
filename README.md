# CareerNewsroom V2

CareerNewsroom V2 is a source-first Telegram job-news pipeline for Bangladesh career opportunities. It keeps the repository understandable while separating source adapters, deterministic rules, AI, Telegram publishing, state, and tests.

## Pipeline

```text
Approved category/API source
        ↓
Discovery
        ↓
Candidate + provenance
        ↓
Detail-page enrichment
        ↓
Normalization
        ↓
Hard deterministic gate
        ↓
AI only for qualified jobs
        ↓
Ranking / selection
        ↓
Telegram
        ↓
Persistent state
```

The category page is a discovery layer. The individual job-detail page is the authoritative source for the complete job record. This is why `published_date` is taken from the detail/API layer rather than inferred from category-page order.

## Sources

### Bdjobs

14 configured category URLs. Only `/h/details/<numeric-id>` links discovered from those exact category URLs are accepted.

### BDJobs Live

14 configured category URLs plus `https://www.bdjobslive.com/internship-opportunity`. Only `/bdjobs-details/...` links discovered from those exact approved URLs are accepted.

### Teletalk

Official published-jobs API only:

`https://alljobs.teletalk.com.bd/api/v1/published-jobs/search`

No homepage, global search, Google/Bing search, or unrelated-source fallback is used.

## Hard rules

### Freshness

`MAX_POST_AGE_DAYS=3` means calendar days, not rolling 72 hours:

```text
Today           PASS
Yesterday       PASS
2 days ago      PASS
3+ days ago     REJECT
Unknown         REJECT
Future          REJECT
```

### Deadline

```text
Unknown         REJECT
Past            REJECT
Today           PASS
Future          PASS
```

### Experience

Regular jobs accept fresher through exactly 3 years. These are rejected:

`3+ years`, `At least 3 years`, `3 years or more`, `more than 3 years`, `3-5 years`, `4 years`.

Internships use a separate lane and may have no experience field.

### Age

When the source explicitly specifies age, it must be compatible with the 18–30 window. Missing age is not invented.

### BBA/MBA / role relevance

Clearly unrelated professions are rejected. Business-relevant/BBA/MBA evidence is checked deterministically before AI.

### Duplicate prevention

Primary identity is source + job ID, then canonical URL. Cross-source duplicate detection uses company + similar title + compatible location/date/deadline. Application URL alone is never treated as a duplicate key.

## AI gate

AI receives only records that have already passed:

- approved source/provenance
- valid identity
- authoritative detail/API extraction
- known publication date
- 3-day freshness
- known active deadline
- duplicate checks
- BBA/MBA/business relevance
- experience/age/vacancy rules where applicable

`Pipeline.ai_rank()` has a hard pre-AI invariant and aborts if any record violates it.

## Selection

Targets are ceilings, never minimum success requirements:

- regular private: 10
- government: 5
- internships: 4
- flexible extras: up to 6
- hard maximum: 25

A run with 0, 2, 4, or any other number of qualifying jobs is still a successful run.

## Telegram

V2 keeps the structured job snapshot format, `APPLY NOW`, source attribution, hashtags, and deadline-expiry editing. If the Rich Message API is unavailable, the publisher falls back to a standard Telegram message.

## State

`news_state.json` and `posted_urls.txt` are persistent runtime state. The application preserves existing state keys and history. The import workflow explicitly backs up and restores these two files so code updates do not erase publication history.

## Scheduler

Six GitHub Actions opportunities are configured:

- 10:05, 10:20, 10:35 Asia/Dhaka
- 17:05, 17:20, 17:35 Asia/Dhaka

Only one morning and one afternoon run is allowed per day. The guard is transactional: a failed run releases its slot so a later scheduled opportunity can retry. Manual dispatch can bypass the guard.

## Repository structure

```text
CareerNewsroom-V2/
├── bot logic at root/main.py
├── core/
├── sources/
├── tests/
├── news_state.json
├── posted_urls.txt
└── .github/workflows/
```

The code is intentionally modular but compact. Source-specific changes stay inside `sources/`, while filtering and pipeline decisions stay inside `core/`.

## Local tests

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py --self-test
python main.py --manual --dry-run
```
