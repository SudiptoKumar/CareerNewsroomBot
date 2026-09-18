# CareerNewsroom V0.5

A simple production bot that discovers and publishes Bangladesh job vacancies for a BBA/MBA and early-career audience.

## Sources

### Bdjobs

Bdjobs is discovered in two independent ways:

1. Bdjobs official listing pages, including the official New Jobs/search surface. Individual `/jobdetails/?id=...` vacancy links are extracted and the vacancy page is researched before publishing.
2. Exa is used as a Bdjobs-only research/discovery layer with a seven-day discovery window.

Bdjobs listing metadata such as functional category, organization type, industry, location, posted period, deadline, job nature, job level, experience and age are used as the basis for the local filter.

### Dohaj

Only these eight pages are used, and only the latest five job-detail links from each page are considered per run:

- https://dohaj.com/category/accounting-finance
- https://dohaj.com/category/marketing-sales
- https://dohaj.com/category/hr-org-development
- https://dohaj.com/category/gen-mgt-admin
- https://dohaj.com/category/commercial
- https://dohaj.com/category/supply-chain-procurement
- https://dohaj.com/category/bank-non-bank-fin-institution
- https://dohaj.com/gov-jobs

The latest two active, unpublished jobs from the Government Jobs page are reserved for publication when available.

For Dohaj, the job-detail page is the Source. When a verified original application URL is present in the detail page, the button uses that URL. Otherwise the button is `READ MORE` and opens the Dohaj detail page.

## Audience filter

Priority is given to BBA/MBA, fresher, trainee, internship, graduate/management trainee, finance, accounting, banking, marketing, sales, HR, business development, management, commercial, operations and supply-chain roles.

## Publishing

A run can publish up to 15 verified jobs. The target is at least 5 when at least 5 genuine, active and publishable jobs are available. No filler jobs are invented.

## Post format

- Optional real image only. If no usable photo exists, the post is text-only.
- Job title
- `🏢 Company Name`
- Compact `JOB SNAPSHOT` table with high-impact fields only
- Job-related hashtags only
- `Source: Bdjobs` or `Source: Dohaj`
- One native inline button: `APPLY NOW` or `READ MORE`

## Required GitHub secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

The workflow sets the production channel to `@CareerNewsroom`.
