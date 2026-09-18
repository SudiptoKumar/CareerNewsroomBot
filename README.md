# CareerNewsroom V3

Production-oriented Bangladesh job newsroom for BBA/MBA students, graduates, freshers and early-career business candidates.

## Source pipeline

### Dohaj

Dohaj is discovered directly from fixed, source-owned feeds rather than generic search results:

- `https://dohaj.com/category/accounting-finance`
- `https://dohaj.com/category/marketing-sales`
- `https://dohaj.com/category/hr-org-development`
- `https://dohaj.com/category/gen-mgt-admin`
- `https://dohaj.com/category/commercial`
- `https://dohaj.com/category/supply-chain-procurement`
- `https://dohaj.com/category/bank-non-bank-fin-institution`
- `https://dohaj.com/jobs/all`
- `https://dohaj.com/gov-jobs`

Category feeds use pagination. The newest pages are scanned every run and duplicate source URLs are removed with `posted_urls.txt`.

The government feed is handled separately and recognizes Dohaj's `/gov-job/` detail URL path. Eligible, unposted government vacancies are not reduced to a two-post global ranking reservation.

### Bdjobs

Bdjobs is discovered directly from the native Bdjobs job-search pages. The bot follows real vacancy URLs, retrieves the actual vacancy page, and then applies the BBA/MBA business-candidate filter.

## AI roles

- **Exa:** retrieval fallback only when a direct source page is blocked or too thin. Exa images are not used for the strict source-photo path.
- **Cerebras:** editorial relevance judge for private BBA/MBA/business-candidate suitability.
- **Python:** source discovery, pagination, retrieval, extraction, normalization, deadline validation, deduplication, application-link validation and publication selection.

## Publication rules

- Minimum total target: **5 posts per run** when at least 5 eligible jobs exist.
- Maximum: **no hard post-count limit**.
- Government: publish all eligible/unposted government jobs discovered in the current government feed window; target **at least 3** whenever 3 or more are eligible.
- Private: publish only jobs relevant to BBA/MBA/business candidates.
- Private category diversity: for each category represented by eligible ranked jobs, select at least **2** before filling with the remaining eligible jobs.
- Expired vacancies are excluded.
- No invented jobs are created to satisfy a quota.

## Photo rules

A photo is sent only when the actual source job page provides a usable source photo.

Rejected media includes:

- black/blank/near-solid images
- white/empty placeholders
- placeholder/no-image/default/favicons/avatars
- common site/company/logo assets
- generic page-wide images outside article/main/figure context

The selected source photo is uploaded without a CareerNewsroom watermark, crop card or generated fallback. When no usable source photo exists, the rich message contains **no photo block at all** and is sent as text-only rich content.

## Job information format

The post uses Telegram's structured Rich Message blocks:

```text
📣 Job Title
🏢 Company Name

JOB SNAPSHOT

FIELD | DETAILS
📍 Location | Dhaka
🎓 Education | BBA/MBA
💼 Employment | Full Time
🏢 Workplace | On-site
💰 Salary | Tk. 30,000-40,000/month
👥 Vacancy | 2
🎂 Age | 24-50 years
📝 Application | Online
🧪 Selection | Written + Viva
📅 Deadline | 30 Sep 2026

EXPERIENCE
🧑‍💼 2-4 years

#Marketing
Source: Dohaj
```

The table uses Telegram's native bordered table block with compact cells. Experience is intentionally outside the table.

Only high-impact facts are shown in the table. Each table value is normalized to a short one-line value where possible. Missing values are omitted instead of showing `--`, `N/A`, `Not Available`, or similar placeholders.

## Extraction safeguards

The V3 label parser reads a field block only until the next recognized job field. This prevents the previous `Experience:` parsing bug where an empty experience field could accidentally absorb the following `Published:` value.

Education, experience, salary, age, workplace, application and selection are normalized into compact candidate-facing values before rendering.

## Telegram Rich Messages

V3 uses `sendRichMessage` with native `blocks`. The job photo is a native photo block when and only when a validated source image exists. The job table is a native table block with borders enabled. The action remains a single inline `APPLY NOW`/`READ MORE` button.

## State

- `news_state.json` stores queue and publication state.
- `posted_urls.txt` prevents direct URL duplication.

## GitHub Actions

The existing schedule is retained for the Asia/Dhaka operating window. The workflow runs a Python compile check and self-test before publication.

Required secrets:

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

Optional environment overrides include:

- `DOHAJ_PRIVATE_PAGES_PER_SECTION` default `3`
- `DOHAJ_GOVERNMENT_PAGES_PER_RUN` default `12`
- `MAX_PRIVATE_JUDGE_CANDIDATES` default `160`
- `POST_DELAY_SECONDS` default `3.0`
- `DISCOVERY_LOOKBACK_DAYS` default `14`
- `GOVERNMENT_LOOKBACK_DAYS` default `60`
