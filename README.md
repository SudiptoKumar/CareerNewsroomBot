# CareerNewsroom V4

Production-oriented Bangladesh job newsroom for BBA/MBA students, graduates, freshers and early-career business candidates.

## What V4 fixes

### Hard publication limit
- **Maximum: 20 posts per run.**
- **Minimum target: 5 total** when at least five eligible jobs are available.
- **Government: 3-5 posts first** on every run when enough eligible/unposted government vacancies are available.
- Government vacancies are **not filtered by BBA/MBA relevance**.
- Private vacancies fill the remaining slots up to the hard limit of 20.

### Government coverage
Dohaj government discovery scans the exposed `https://dohaj.com/gov-jobs` feed across its available pagination window, queues verified active/unpublished circulars, and publishes 3-5 of them first on each run. The current Dohaj government feed is paginated at 40 jobs per page and, as of the current source snapshot, exposes hundreds of archived/current entries.

### Private-job source coverage
The private pipeline uses fixed Dohaj category feeds for the business-heavy areas listed below plus the main all-jobs feed:

- `https://dohaj.com/category/accounting-finance`
- `https://dohaj.com/category/marketing-sales`
- `https://dohaj.com/category/hr-org-development`
- `https://dohaj.com/category/gen-mgt-admin`
- `https://dohaj.com/category/commercial-supply-chain`
- `https://dohaj.com/category/secretary-receptionist`
- `https://dohaj.com/category/bank-non-bank-fin-institution`
- `https://dohaj.com/category/customer-service-call-centre`
- `https://dohaj.com/category/media-advertisement-event-mgt`
- `https://dohaj.com/category/production-operation`
- `https://dohaj.com/category/ngo-development`
- `https://dohaj.com/jobs/all`

Each private candidate is retrieved from its detail page before publication filtering.

## Ranking model
Private jobs are ranked in this order of importance:

1. **Education fit**: BBA/MBA first, then closely related business degrees, then broader bachelor/master requirements.
2. **Experience level**: fresher/no experience and lower-year requirements are prioritized over higher-year requirements.
3. **Publish freshness**: newer jobs are prioritized.
4. **Deadline urgency**: active vacancies with closer deadlines receive higher priority.
5. **Job quality**: completeness, verified source/apply link, and clean extracted fields.
6. **AI editorial fit**: Cerebras suitability judgment is used as an eligibility signal and a small tie-break factor.

Government jobs use a separate recency/deadline/quality ranking and do not pass through the private BBA/MBA gate.

## Field extraction and mismatch protection
V4 separates source sections instead of taking the first matching number/text anywhere on the page. This prevents problems such as: salary or age becoming vacancy, responsibility text becoming experience, or one post's value being attached to another field.

Supported source labels include English and Dohaj's Bengali labels such as `প্রতিষ্ঠানের নাম`, `চাকুরি স্থান`, `বয়সসীমা`, `বেতন`, `প্রকাশিত`, `শেষ তারিখ`, and application-period labels. Dates are normalized to ISO `YYYY-MM-DD`.

Experience is displayed in the table only when the source explicitly states a duration/fresher status. Technical skill lists or `Area of Experience` descriptions are not shown as the Experience value.

Missing fields are omitted, not replaced with `--` or invented values.

## Post format

```text
📣 Job Title
🏢 Company

JOB SNAPSHOT

FIELD | DETAILS
📍 Location | Dhaka
💼 Employment | Full Time
🏢 Workplace | On-site
🎓 Education | BBA/MBA
🧑‍💼 Experience | Freshers
💰 Salary | Tk. 30,000-40,000/month
👥 Vacancy | 2
🎂 Age | 18-30 years
📝 Application | Online
🗓️ Application Period | 2026-09-24 to 2026-09-25
🧪 Selection | Written + Viva
📅 Deadline | 2026-09-30
🕒 Posted | 2026-09-18

#Marketing #EarlyCareer
Source: Dohaj
```

The table uses Telegram Bot API 10.3 Rich Messages with a native table, `is_bordered=true`, striped rows and non-compact cells for clearer visual separation.

## Photo feature
**Completely disabled in V4.** No image URL is downloaded, no photo block is added, and no source/logo/placeholder image is generated or used. Every post is text-only Rich Message content.

## Duplicate protection
Duplicate detection uses:
- canonical source URL
- persistent `posted_urls.txt`
- cross-source event identity from normalized title/company/location
- verified application-target identity when available
- fuzzy title/company/location comparison for mirrored or slightly edited copies
- persistent `news_state.json` event history

This is designed to prevent repeated Dohaj category copies and Dohaj/Bdjobs mirrors from being published again as separate jobs.

## AI and retrieval
- **Exa:** fallback retrieval only when direct source retrieval is blocked/thin.
- **Cerebras:** private-job audience/editorial suitability.
- **Python:** discovery, pagination, detail retrieval, multilingual extraction, normalization, deadline validation, duplicate filtering, ranking and Telegram publication.

## Required GitHub secrets
- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

Optional variables:
- `DOHAJ_PRIVATE_PAGES_PER_SECTION` default `3`
- `DOHAJ_GOVERNMENT_MAX_PAGES` default `30`
- `MAX_PRIVATE_JUDGE_CANDIDATES` default `220`
- `MAX_BDJOBS_DISCOVERY_PAGES` default `8`
- `MAX_BDJOBS_DETAIL_CANDIDATES` default `160`
- `POST_DELAY_SECONDS` default `2.5`
