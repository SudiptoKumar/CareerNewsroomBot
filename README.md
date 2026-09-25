# CareerNewsroom V2.1

Production Telegram job-news bot for BBA/MBA-relevant opportunities in Bangladesh.

## V2.1 goal

V2.1 keeps the existing Bdjobs + Teletalk architecture, adds BDJobs Live and dedicated internship lanes, and strengthens source-specific extraction, duplicate protection, research allocation, scheduling, and state persistence.

The core rule remains:

> Job availability is not a failure condition. Quality gates decide how many jobs are published.

## Production sources

### Regular private

- Bdjobs: category-first discovery across the existing 14 BBA/MBA-relevant categories.
- BDJobs Live: the requested 14 BBA/MBA-relevant functional categories.

BDJobs Live categories:

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

### Government

- Teletalk / AllJobs API

### Internships

- Bdjobs: `https://bdjobs.com/h/jobs?lang=en&JobType=intern`
- BDJobs Live: `https://www.bdjobslive.com/bdjobs-circular/internship-opportunity`

Internships are a separate lane. Missing normal work experience is allowed, but location, deadline, source identity, BBA/MBA relevance, and snapshot quality still have to pass.

## Per-run publication model

The selector uses source-balanced targets, not failure-causing quotas:

```text
Regular private target     10
Government target           5
Internship target           4
  ├─ Bdjobs preferred       2
  └─ BDJobs Live preferred  2
Flexible extra              up to 6
Hard maximum                25
Target total                19
```

These are quality-first targets. They are not guarantees.

Examples:

```text
10 private + 5 govt + 4 internship + 6 extra = 25
6 private + 5 govt + 3 internship             = 14
0 qualifying jobs                             = 0
```

The workflow remains successful when fewer jobs are available, provided discovery, processing, publishing, and state persistence themselves complete successfully.

No filler jobs are created to hit 10/5/4/25.

## Schedule

GitHub Actions uses two publishing sessions per day in `Asia/Dhaka`. Each session has three scheduler opportunities with a persistent once-per-session guard:

```text
Morning:   10:05 / 10:20 / 10:35
Afternoon: 17:05 / 17:20 / 17:35
```

Only one trigger can execute the actual bot for each morning/afternoon session. Recovery triggers exit without publishing when the session is already marked completed. Manual `workflow_dispatch` runs bypass the scheduler guard.


```text
10:05 Asia/Dhaka
17:05 Asia/Dhaka
```

The five-minute offset avoids relying on the exact top-of-hour boundary. Each run is state-aware and must not republish already-posted vacancies.

## V2 discovery architecture

```text
Bdjobs ------------------┐
BDJobs Live --------------┤
Bdjobs Internships --------┤
BDJobs Live Internships ---┤→ normalize → dedupe → freshness/BBA filter
Teletalk ------------------┘                         ↓
                                               adaptive detail research
                                                        ↓
                                                source-authoritative fields
                                                        ↓
                                                  snapshot integrity
                                                        ↓
                                                   deterministic rank
                                                        ↓
                                                   Cerebras audit
                                                        ↓
                                                source-balanced selection
                                                        ↓
                                                       Telegram
                                                        ↓
                                                GitHub state persistence
```

The discovery adapters are source-specific. The downstream business rules remain shared.

## Bdjobs reliability

Bdjobs keeps the established production retrieval chain:

```text
curl_cffi / fingerprint rotation
        ↓
Bdjobs detail/listing retrieval
        ↓
Patchright/Scrapling browser fallback for rendered pages
        ↓
DOM-aware validation
        ↓
Jina fallback
        ↓
listing-backed fallback when detail enrichment fails
```

Existing identity protections remain in place. A rendered title that conflicts with the listing title is rejected instead of replacing the trusted listing identity.

## BDJobs Live reliability

BDJobs Live uses the actual category routes supplied for the project:

```text
https://www.bdjobslive.com/bdjobs-circular/<category>-jobs
```

The `/bdjobs/<category>` route is only a compatibility fallback.

Category discovery:

```text
curl_cffi / Jina
        ↓
if no /bdjobs-details/ links:
        ↓
one bounded Scrapling/StealthyFetcher browser render
        ↓
extract /bdjobs-details/... links
        ↓
homepage supplement when category feeds are weak
```

The browser listing path uses a bounded timeout and one browser attempt per source page. A temporary BDJobs Live outage does not stop Bdjobs or Teletalk from running.

### BDJobs Live detail extraction contract

The detail parser follows the supplied stable semantic map:

```text
h1                                  → job title
a[href*="/company-detail/"]        → company
labelled summary values             → salary, experience, location, deadline,
                                       published, vacancy, age, job type,
                                       job shift, gender
#section-education                 → education
#section-experience                → experience
#section-skills                    → skills
#section-responsibilities          → responsibilities
#section-company                   → company information/address/website
canonical + OG metadata             → identity/media metadata
```

The parser deliberately avoids Tailwind/presentation classes.

## Source fidelity and field protection

Source extraction is authoritative. AI may rank, audit, and choose display fields, but it cannot invent source facts or remove protected facts.

Protected source fields include:

```text
Location
Experience
Salary
Deadline
Posted
Education
Employment
Workplace
Vacancy
Age
Application
```

Important collision protections include:

```text
Salary & Benefits          ≠ salary value
Compensation & Other Benefits ≠ salary value
Additional Requirements    ≠ experience value
Application Deadline       ≠ Application
```

A real salary such as `Tk. 18,000 - 22,000` must survive extraction and appear in Telegram when source-backed.

## Duplicate protection

Duplicate detection is layered:

```text
exact canonical URL
        ↓
same-source native job ID
        ↓
cross-source company + normalized title
        ↓
location + recent posting compatibility
        ↓
application URL as supporting evidence only
```

A shared Teletalk application URL alone does not merge two different government roles from the same circular.

A mirrored vacancy such as the same company and same role on Bdjobs and BDJobs Live is treated as one vacancy.

Persistent duplicate state uses both `news_state.json` and `posted_urls.txt`.

## Detail research allocation

`PRIVATE_DETAIL_TARGET` remains a research budget, not a publication quota.

V2 reserves a bounded internship research slice and gives BDJobs Live a real regular-private research share. Unused source capacity is returned to the remaining private pool.

The goal is to prevent the large Bdjobs discovery volume from starving BDJobs Live and dedicated internship sources without imposing an artificial source publication split for ordinary private jobs.

## Selection

The final selector operates in this order:

1. regular private target
2. internship target with Bdjobs/BDJobs Live source preference
3. government target
4. remaining flexible capacity up to the hard maximum

Quality, freshness, BBA/MBA relevance, source fidelity, information quality, experience, deadline, duplicate status, and diversity remain enforced.

## Government handling

Government jobs remain a separate Teletalk lane and are capped at five base selections per run. They cannot consume the private target.

## Deadline expiry

At the start of every normal run, previously published posts with expired deadlines are edited through the Telegram Bot API.

```text
APPLY NOW
   ↓
EXPIRED DEADLINE
```

The expired button uses the configured CareerNewsroom promo URL. The original application/source link is removed from the expired rich message while the plain source name remains.

Date-only deadlines remain valid through the end of the Bangladesh local date.

No Telethon session is required.

## State persistence

State remains in:

```text
news_state.json
posted_urls.txt
```

The GitHub Actions workflow:

```text
commit
  ↓
push
  ↓
if remote advanced:
    fetch
    reconcile local state snapshot with remote state
    retry
  ↓
only declare success after persistence is confirmed
```

No force push is used. Newer remote state is not discarded.

## Source failure semantics

A valid empty source result is different from a source exception.

```text
source returns 0 jobs       → healthy, continue
source temporarily errors   → log source as unhealthy, continue other sources
all discovery sources error  → genuine pipeline failure
```

Therefore `Published=0` is not an error, but a completely unavailable discovery system is.

BDJobs Live diagnostic failure alone is never allowed to prevent Bdjobs + Teletalk production execution.

## AI policy

Cerebras is an optional semantic audit layer.

It may evaluate relevance, seniority, qualification fit, contradictions, internship status, and display-field preference.

When AI is unavailable, deterministic scoring continues the pipeline.

AI is never the source of factual job fields.

## Configuration

Core production values:

```text
MAX_POST_AGE_DAYS=5
TARGET_STORIES_PER_RUN=19
MAX_STORIES_PER_RUN=25
PRIVATE_TARGET_PER_RUN=10
GOVERNMENT_TARGET_PER_RUN=5
INTERNSHIP_TARGET_PER_RUN=4
INTERNSHIP_BDJOBS_TARGET=2
INTERNSHIP_BDJOBSLIVE_TARGET=2
EXTRA_TARGET_PER_RUN=6
PRIVATE_DETAIL_TARGET=60
INTERNSHIP_DETAIL_TARGET=12
DETAIL_WORKERS=8
```

Publication minimums intentionally remain zero:

```text
MIN_PRIVATE_POSTS_PER_RUN=0
MIN_GOVERNMENT_POSTS_PER_RUN=0
MIN_INTERNSHIP_POSTS_PER_RUN=0
```

## Production validation

Before release:

```text
python -m py_compile main.py
python main.py --self-test
```

The built-in self-test covers:

```text
Bdjobs extraction
Bdjobs salary collision regression
BDJobs Live DOM extraction
BDJobs Live salary collision regression
BDJobs Live browser configuration
internship lane detection
internship snapshot rules
cross-source duplicate protection
same-circular/different-role protection
source-balanced selection
zero-job success behavior
deadline expiry
Telegram message-id flow
Git state reconciliation
```

The production ZIP contains no tests directory, logs, cache, or debug artifacts.

## Required GitHub secrets

```text
TELEGRAM_BOT_TOKEN
CEREBRAS_API_KEY
```

Optional:

```text
CEREBRAS_MODEL
```

Do not commit secrets to the repository.

## Production tree

```text
Career News V1/
├── main.py
├── README.md
├── requirements.txt
├── news_state.json
├── posted_urls.txt
└── .github/
    └── workflows/
        └── newbot.yml
```

The existing `Career News V1/` repository folder name is retained for deployment compatibility. The application pipeline itself reports `CareerNewsroom V2.1`.
