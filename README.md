# Career News Bot V0

Production-oriented Bangladesh job intelligence bot for Telegram.

## What V0 changes

The previous production logs showed that GitHub Actions HTTP requests to Bdjobs were returning `403`, so V0 changes the private-source acquisition layer instead of changing the ranking model again.

### Source architecture

```text
Government
  Teletalk API
      ↓
  government candidates

Private
  Real Chrome + Scrapling
      ↓
  Bdjobs functional-category pages
      ↓
  14 category lanes
      ↓
  broad current Bdjobs listing
      ↓
  candidate pool
      ↓
  deterministic BBA/MBA business ranking
      ↓
  detail enrichment
      ↓
  optional Cerebras semantic audit
      ↓
  diversity-aware selection
      ↓
  Telegram Rich Messages
```

### Bdjobs browser acquisition

V0 uses Scrapling's browser fetcher with the Chrome executable available on the GitHub runner.

Important settings:

```text
SCRAPLING_DYNAMIC_ENABLED=1
SCRAPLING_BROWSER_EXECUTABLE=/usr/bin/google-chrome
BDJOBS_BROWSER_CATEGORY_LIMIT=14
BDJOBS_BROWSER_PER_CATEGORY=12
BDJOBS_BROWSER_TIMEOUT_MS=20000
BDJOBS_BROWSER_WAIT_MS=2500
```

The browser path is the **primary** Bdjobs source. The old API/static HTTP paths are only recovery paths after browser acquisition returns zero candidates.

The browser collector:

1. Opens each relevant Bdjobs functional category sequentially.
2. Waits for the JavaScript-rendered page.
3. Extracts real Bdjobs job links from the rendered DOM.
4. Records the category lane used for discovery.
5. Runs a broad current-jobs browser pass for additional coverage.
6. Deduplicates before ranking.
7. Uses the same browser-first approach for Bdjobs detail retrieval.

Scrapling supports browser rendering, real Chrome, wait conditions, network-idle waiting and XHR capture. The implementation uses those capabilities rather than relying on a plain HTTP GET. urlScrapling dynamic fetching documentationhttps://scrapling.readthedocs.io/en/latest/fetching/dynamic.html

## Failure behavior

V0 does **not** treat zero Bdjobs candidates as a successful private-source run.

If:

```text
browser = 0
API recovery = 0
HTML recovery = 0
```

the job fails with:

```text
BDJOBS_PRIVATE_SOURCE_FAILED
```

This is intentional. A run that has only government jobs because the private source was inaccessible is not considered a healthy Career News Bot run.

## Candidate intelligence

The existing intelligence layer is retained:

- BBA/MBA education relevance
- business-role/function relevance
- fresher and early-career fit
- posting freshness
- deadline urgency
- salary
- vacancy
- information quality
- business career family
- duplicate/event detection
- company and career-family diversity
- optional Cerebras semantic audit

Experience is a ranking signal, not a universal hard cutoff.

## Sources

Production sources:

- Government: Teletalk AllJobs
- Private: Bdjobs

No Dohaj acquisition is used.

## Schedule

GitHub Actions runs every 3 hours in Asia/Dhaka:

```text
0 */3 * * *
```

Manual `workflow_dispatch` is also available.

## Runtime target

```text
Private discovery target: 120
Per-category browser window: 12
Private deep research: 40
Cerebras audit: up to 32
Final maximum: 20
GitHub job timeout: 8 minutes
```

The browser collector is intentionally sequential across categories to reduce simultaneous requests to Bdjobs.

## Required GitHub secrets

```text
TELEGRAM_BOT_TOKEN
```

Optional:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

## Validation

Run locally:

```bash
python -m py_compile main.py
python main.py --self-test
python main.py --source-test
```

`--source-test` is a live network diagnostic and must be run in an environment that can reach Bdjobs.

## Important limitation

A local test environment cannot prove that a GitHub-hosted runner will receive a successful Bdjobs browser response. V0 therefore includes explicit browser-source logging so the first GitHub run can distinguish:

```text
Chrome launch
→ Bdjobs navigation
→ rendered page
→ category candidates
→ detail retrieval
```

from the old opaque `403 → 0 candidates` failure.
