# CareerNewsroom V1

Production-oriented Bangladesh job newsroom for BBA/MBA students, graduates, freshers and early-career professionals.

## Source pipeline

### Bdjobs

Bdjobs is discovered **directly from the Bdjobs website**, not through Exa search. The bot reads the native Bdjobs job-search pages, follows real `jobdetails` vacancy URLs, then retrieves each actual vacancy page.

The current Bdjobs detail URL pattern is handled explicitly, including URLs such as `jobs.bdjobs.com/jobdetails/?id=...`.

### Dohaj

Dohaj is discovered directly from these approved pages:

- `https://dohaj.com/category/accounting-finance`
- `https://dohaj.com/category/marketing-sales`
- `https://dohaj.com/category/hr-org-development`
- `https://dohaj.com/category/gen-mgt-admin`
- `https://dohaj.com/category/commercial`
- `https://dohaj.com/category/supply-chain-procurement`
- `https://dohaj.com/category/bank-non-bank-fin-institution`
- `https://dohaj.com/gov-jobs`

The first five job-detail links from each approved page are considered. Government jobs are tracked separately so the latest two eligible government jobs can be reserved for each run without depending on the global ranking order.

## AI roles

- **Exa:** research/retrieval fallback only when a direct source page is blocked or too thin. It is not the primary Bdjobs discovery engine.
- **Cerebras:** editorial relevance judge for BBA/MBA and early-career suitability.
- **Python:** source discovery, page retrieval, field extraction, deadline validation, deduplication, application-link validation and selection rules.

If Cerebras returns an incomplete batch, verified source-backed candidates have a conservative deterministic fallback so an AI/API parsing failure does not automatically turn a healthy source run into zero posts.

## Publishing

- Target: 5–15 verified jobs per run when enough genuine jobs are available.
- Maximum: 15.
- No invented jobs to satisfy the minimum.
- Two latest eligible Dohaj government jobs are reserved separately.
- Exactly one native inline button:
  - `APPLY NOW` when a verified application destination exists.
  - `READ MORE` otherwise, linking to the source job page.

## Post format

- `📣` job title
- `🏢` company name without a `Company:` prefix
- compact high-impact information table
- job-specific hashtags
- `Source:` linked to the source job page
- no `🔎`
- no `Official Source`
- no `#BBA_MBA`
- if a genuine source image exists, it may be attached
- if no genuine image exists, the bot sends rich text only; it does not create a placeholder image

## State

- `news_state.json` stores queue and publication state.
- `posted_urls.txt` prevents direct URL duplication.

## GitHub Actions secrets

Required:

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

The workflow runs on the Asia/Dhaka schedule and can also be started manually with **Run workflow**.
