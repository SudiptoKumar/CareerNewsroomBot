# CareerNewsroom

CareerNewsroom is a GitHub Actions Telegram bot that discovers Bangladesh job opportunities, validates them, ranks eligible BBA/MBA-relevant roles, and publishes source-backed posts to `@CareerNewsroom`.

## Production source lanes

```text
Government  -> AllJobs / Teletalk official API
Private     -> Bdjobs configured categories
Private     -> BDJobs Live configured BBA/MBA categories
Internships -> dedicated lane across supported private sources
```

No homepage/global-search fallback is used for BDJobs Live. Configured category pages are discovery sources only. The individual job-detail page is the authoritative record for published date, deadline, salary, experience, age, education, application information, and other final fields. Current BDJobs Live category pages are JavaScript-driven, while detail pages expose those fields directly.

## Pipeline

```text
Source discovery
  -> candidate normalization + provenance
  -> deterministic freshness/deadline checks
  -> detail-page research
  -> authoritative extraction
  -> deterministic BBA/MBA / experience / age eligibility
  -> duplicate detection
  -> optional Cerebras semantic audit
  -> source-balanced selection
  -> Telegram publish
  -> news_state.json + posted_urls.txt
  -> GitHub state reconciliation
```

AI is downstream of hard gates. It does not invent source fields.

## BDJobs Live categories

The adapter uses these 14 configured career lanes:

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

The primary current route family is `/bdjobs-circular/{slug}-jobs`, with explicit compatibility aliases only where needed.

## BDJobs Live retrieval

The adapter rejects a plain HTTP 200 shell when the page does not contain expected job content. It uses:

```text
curl_cffi
   -> requests
   -> Scrapling / StealthyFetcher browser render
```

Category discovery requires real `/bdjobs-details/` anchors. Detail retrieval requires real job-page content including authoritative deadline and published-date markers. Browser work is bounded and concurrency-limited so 14 categories do not start an uncontrolled browser fleet.

## Eligibility

Private jobs use calendar-day freshness in Asia/Dhaka:

```text
0 days   accepted
1 day    accepted
2 days   accepted
3 days   accepted
4+ days  rejected
```

Unknown published date is rejected. Unknown or expired deadline is rejected. Date-only deadlines remain active through the end of that local calendar day.

Experience must be bounded within fresher through 3 years. Unbounded requirements such as `at least 1 year`, `2+ years`, or `more than 2 years` are not treated as safe upper bounds. Exact or bounded values through 3 years are allowed.

For private jobs, explicitly incompatible age requirements are rejected when they exceed the 18-30 target, including ranges such as `18-35`, `25-32`, or `32-45`. Missing or unspecified age is not invented.

## Publication model

Targets are not minimum quotas:

```text
Regular private target   10
Government target          5
Internship target          4
Hard maximum              25
```

If four eligible jobs survive, four can be published. If none survive, zero is a valid result.

The private research shortlist reserves a dedicated BDJobs Live detail lane so a large Bdjobs discovery population cannot consume the entire private research capacity before BDJobs Live is researched.

## Scheduler

GitHub Actions uses Asia/Dhaka opportunities equivalent to:

```text
10:05 / 10:20 / 10:35
17:05 / 17:20 / 17:35
```

A persistent scheduler guard allows one actual morning run and one actual afternoon run per local date. Manual `workflow_dispatch` runs bypass that guard. GitHub Actions concurrency uses `cancel-in-progress: false`.

## State

The repository persists:

```text
news_state.json
posted_urls.txt
```

Published history is preserved during state-format migration. Telegram `message_id` values are stored for published events so deadline-expiration edits can be applied later. The workflow reconciles remote state without force-pushing.

## Tests

Before production execution the workflow runs:

```bash
python -m py_compile main.py sources/bdjobslive.py
python main.py --self-test
```

Manual workflow dispatch also supports the live source diagnostics command:

```bash
python main.py --source-test
```

The local self-test covers BDJobs Live listing/detail extraction, source identity, freshness, deadline semantics, experience boundaries, age compatibility, duplicate behavior, selection, deadline expiration, state reconciliation, and scheduler logic.

## Project layout

```text
CareerNews/
├── main.py
├── sources/
│   └── bdjobslive.py
├── news_state.json
├── posted_urls.txt
├── requirements.txt
├── README.md
└── .github/
    └── workflows/
        └── newbot.yml
```
