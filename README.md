# CareerNewsroom V2

CareerNewsroom V2 is a source-first Telegram job-news pipeline for Bangladesh career opportunities.

## Architecture

```text
Approved source category/API
        ↓
Discovery
        ↓
Candidate + provenance
        ↓
Deduplication
        ↓
Authoritative detail fetch
        ↓
Normalization
        ↓
Hard deterministic eligibility
        ↓
AI interpretation/ranking
        ↓
Selection
        ↓
Telegram
        ↓
Persistent state
```

The category page is a discovery layer. The individual job-detail page is the authoritative source for the complete job record.

## Sources

### Bdjobs

14 configured BBA/MBA-oriented category URLs are used. Candidate discovery is limited to `/h/details/<numeric-id>` job URLs originating from those category pages.

### BDJobs Live

14 configured BBA/MBA-oriented category URLs plus the dedicated internship page:

`https://www.bdjobslive.com/internship-opportunity`

Only `/bdjobs-details/...` links discovered from these approved pages are accepted.

### Teletalk

Official published-jobs API only:

`https://alljobs.teletalk.com.bd/api/v1/published-jobs/search`

No homepage/global-search fallback is used.

## Hard rules

`MAX_POST_AGE_DAYS=3` uses calendar-day semantics:

- today: accepted
- yesterday: accepted
- day before yesterday: accepted
- 3+ calendar days old: rejected
- unknown published date: rejected
- future published date: rejected

Deadline:

- unknown: rejected
- expired: rejected
- today or future: active

Experience for regular private jobs:

- fresher/0: accepted
- up to 3 years: accepted
- `3+`, `at least 3`, `3 years or more`, `3-5`, `4 years`: rejected

Age compatibility is checked against 18–30 when an explicit age range is present.

BBA/MBA relevance, business-profession relevance, duplicate status, and source validity are deterministic gates.

AI cannot receive a candidate that has not passed the hard gate. V2 enforces this with a pre-AI invariant.

## Selection

Targets are ceilings, not minimum success conditions:

- regular private: 10
- government: 5
- internships: 4
- flexible extras: up to 6
- hard maximum: 25

A run with fewer qualifying jobs is still successful.

## State

`news_state.json` and `posted_urls.txt` are persistent. V2 loads existing state and preserves unknown/legacy keys rather than overwriting the file wholesale.

GitHub Actions commits only state changes and uses rebase/retry before pushing.

## Scheduler

The workflow provides six scheduler opportunities:

- 10:05, 10:20, 10:35 Asia/Dhaka
- 17:05, 17:20, 17:35 Asia/Dhaka

A persistent `schedule_guard` permits only one morning and one afternoon run per day. Manual dispatch bypasses the scheduler guard.

## Local test

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py --self-test
python main.py --manual --dry-run
```

Live crawling requires network access and valid GitHub Secrets for Telegram/Cerebras when those features are enabled.
