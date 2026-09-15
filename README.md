# CareerNewsroomBot V1

Production Bangladesh job newsroom bot for `@CareerNewsroom`.

## Core publishing model

1. Crawl every enabled registered source on each scheduled run.
2. Extract and validate Bangladesh job vacancies.
3. Resolve the real publisher.
4. Keep approved publishers: official government/employer/ATS sources plus the explicitly approved job boards **BDJobs** and **BDJobs Live**.
5. Third-party republishers such as **Dohaj** are never allowed to become the displayed source.
6. Active deadline means `deadline > now`; there is no arbitrary 7-day rejection.
7. Deduplicate the underlying job event across mirrors.
8. Rank with BBA/MBA/business relevance as a priority signal, but do not discard other valid jobs merely because they are less relevant.
9. Fairly interleave sources and cap any one employer at two posts per run.
10. Finalize the jobs first. Only then run the company-logo image step independently for each selected job. A logo failure for one job never stops the other jobs.

## Company-logo image system

The image is **not** a source-logo card, job-board card, building photo, generated corporate background, or small logo inside a white box.

For each finalized job the bot searches independently using:

- official employer/career/ATS pages
- official social profiles when discoverable
- Google/Bing image search
- Exa as complementary web/logo discovery

The highest-quality verified logo candidate is selected using employer-name matching, provenance, dimensions, content ratio, and asset-quality scoring. SVG is rendered at high resolution. Outer blank/white margins are trimmed while internal logo details remain intact.

The final photo is a **1200×675 transparent PNG** containing only:

- the large original company/organization logo
- verified company name
- `@CareerNewsroom` at bottom-right

No synthetic background is added.

## Telegram post structure

The Rich Message remains mobile-first:

```text
# Job Title

🏢 Company

Short verified summary
────────────────────

JOB SNAPSHOT

FIELD | DETAILS
...

✅ REQUIREMENTS [expand]
📝 RESPONSIBILITIES [expand]

⏰ Check the deadline before applying.

[ APPLY NOW ]

🏛️ OFFICIAL SOURCE: Source

@CareerNewsroom
```

Bangla messages are explicitly sent left-to-right (`is_rtl=false`).

## Environment

Required secrets:

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

Optional variables:

- `TELEGRAM_CHANNEL`
- `TELEGRAM_ADMIN_CHAT_ID`
- `CEREBRAS_MODEL`

## Workflow

`.github/workflows/careerbot.yml` runs hourly from 07:00 through 23:00 Asia/Dhaka and supports `workflow_dispatch`.

## Local validation

```bash
PYTHONPATH=/path/to/stubs EXA_API_KEY=dummy CEREBRAS_API_KEY=dummy TELEGRAM_BOT_TOKEN=dummy python main.py --self-test
```

The self-test covers syntax-level imports, source policy, deadline logic, title/company cleanup, event handling, transparent image generation, Telegram Rich Message payloads, source/employer fairness, and isolated logo selection.
