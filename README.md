# CareerNewsroom V2

CareerNewsroom V2 is a source-first Bangladesh career-news bot for Telegram. It is intentionally kept in the simple six-file production layout used by the existing repository so the project stays easy to update and maintain.

## Production source scope

Only these source families are allowed to create candidates:

```text
Private       → configured Bdjobs category URLs
Private       → configured BDJobs Live category URLs
Internships   → configured BDJobs Live internship URL
Government    → official AllJobs / Teletalk published-jobs API
```

No Google/Bing discovery, homepage scraping, global search, unrelated job boards, or substitute aggregators are used.

## Core architecture

```text
Configured category/API source
        ↓
Job discovery
        ↓
Candidate + provenance
        ↓
Deduplication
        ↓
Authoritative detail-page fetch
        ↓
Complete source extraction
        ↓
Normalization
        ↓
Hard deterministic eligibility gate
        ↓
Qualified AI input only
        ↓
AI interpretation / ranking support
        ↓
Final selection
        ↓
Telegram publication
        ↓
Persistent GitHub state
```

### Category pages are discovery pages

Category pages are used to find valid job URLs and basic card metadata. The bot does not treat the category card as the final job record.

### Detail pages are authoritative

For Bdjobs and BDJobs Live, the individual job-detail page supplies the authoritative published date, deadline, salary, vacancy, age, experience, education and other job information. The source-specific selectors are based on the supplied DOM specifications.

Bdjobs detail anchors include:

```text
/h/details/<job_id>
#allSection
#requirements
#responsibilitiesSection
#skills
Application Deadline
```

BDJobs Live detail anchors include:

```text
/bdjobs-details/<slug>-<job_id>
#section-education
#section-experience
#section-skills
#section-responsibilities
#section-company
Application Deadline :
```

No Tailwind utility class or generated framework attribute is used as the primary source of truth when a semantic selector or label/value relationship exists.

## Hard eligibility rules

### Freshness

```text
MAX_POST_AGE_DAYS = 3
```

Calendar-day meaning in Asia/Dhaka:

```text
Today           → PASS
Yesterday       → PASS
2 days ago      → PASS
3+ days ago     → REJECT
Unknown         → REJECT
Future          → REJECT
```

For Bdjobs and BDJobs Live, the published date must be verified from the authoritative detail page. A missing detail-page published date cannot be silently replaced with a listing-card date.

### Deadline

```text
Unknown deadline  → REJECT
Past deadline     → REJECT
Today             → ACTIVE
Future            → ACTIVE
```

Date-only deadlines use Bangladesh calendar-day semantics.

### Experience

Passes include:

```text
Fresher
No experience
0 years
1 year
1-2 years
2-3 years
Exactly 3 years
```

Rejected examples include:

```text
3+ years
At least 3 years
3 years or more
More than 3 years
3-5 years
4 years
5-10 years
```

Open-ended requirements are not converted into an artificial maximum of 3 years.

### Age

Compatible explicit range:

```text
18–30
18–28
20–30
```

Explicitly incompatible examples:

```text
18–35
25–32
```

Missing age remains unknown. The bot does not invent an age restriction.

### BBA/MBA relevance

The job must be relevant to BBA/MBA business careers. Clearly unrelated specialist professions are rejected before AI.

### Duplicate prevention

Primary identity:

```text
source + source_job_id
```

Then canonical URL and cross-source event similarity are used as fallbacks. An application URL by itself is never treated as a duplicate because multiple genuine jobs may share an application destination.

## Publication targets

These are targets/ceilings, never minimum success requirements:

```text
Regular private → target 10
Government      → target 5
Internship      → target 4
Flexible extras → up to 6
Hard maximum    → 25
```

A run with fewer eligible jobs is still successful. The bot never invents or pads vacancies to reach a target.

## AI contract

AI receives only records that have already passed:

```text
source validation
identity validation
detail-page validation
3-day freshness
deadline activity
BBA/MBA relevance
experience
age
business-role rules
deduplication
```

The AI layer interprets and ranks qualified jobs. It does not invent source facts and cannot override the deterministic eligibility gate.

If Cerebras is unavailable, deterministic ranking remains available so a temporary AI outage does not invalidate the source pipeline.

## Persistence

The production repository keeps:

```text
news_state.json
posted_urls.txt
```

These files contain publication history, queue/event state and duplicate history. Code updates must preserve them.

The GitHub workflow also creates them defensively if a clean checkout is missing one, so a no-op or skipped run never fails merely because a state file is absent.

## Scheduler

Asia/Dhaka sessions:

```text
Morning   10:05 / 10:20 / 10:35
Afternoon 17:05 / 17:20 / 17:35
```

A persistent `schedule_guard` prevents multiple successful runs in the same session. Manual `workflow_dispatch` runs bypass the scheduler guard.

GitHub Actions concurrency is non-cancelling so an already-running job is not killed by a later schedule tick.

## State safety on failures

A scheduled failure records the session as `failed`, not `completed`, so a later scheduler opportunity is still available. A successful run is marked `completed` only after the pipeline finishes.

## Telegram expiration

Published events retain the Telegram `message_id` and deadline. When a deadline passes, the bot edits the historical message to an expired state and removes the application/source action as configured.

## Production files

```text
CareerNewsroom/
├── .github/
│   └── workflows/
│       └── newbot.yml
├── main.py
├── README.md
├── requirements.txt
├── news_state.json
└── posted_urls.txt
```

`main.py` remains the single production engine intentionally. Source-specific discovery/detail logic is separated by functions inside the file rather than scattering a small bot across many directories.

## Validation

Before release, the package is checked with:

```bash
python -m py_compile main.py
python main.py --self-test
python main.py --source-test
```

The live source diagnostic is informational because external source availability can change independently of the code. The self-test covers the supplied Bdjobs and BDJobs Live DOM knowledge, date/deadline rules, experience boundaries, age boundaries, duplicate logic, authoritative-detail timing, unrelated-profession rejection, expiry handling and the current Bdjobs `/h/details/<id>` route.
