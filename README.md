# CareerNewsBot V1

Production-oriented Bangladesh career and job-news bot for `@CareerNewsroom`, based on the proven TechNews technical framework and adapted for high-value job discovery.

## Telegram post structure

Job title → Company → `JOB SNAPSHOT` table → hashtags → `Official Source` → one native Telegram Inline Keyboard button.

### Button rules

- Verified application URL found: `APPLY NOW` with no emoji.
- No verified application URL found: `READ MORE`, opening the source/details page.
- Never copy `source_url` into `apply_url` automatically.
- The application destination may be on a different domain from the source page.
- The application URL is never printed as raw URL text in the message body.

## Job information rules

- Gender is completely removed from extraction, verification, state and rendering.
- `Application` stays in `JOB SNAPSHOT` and contains only the human-readable application method.
- The direct application URL is attached only to the button.
- Missing information is omitted rather than displaying `Not specified`.
- High-impact information is prioritized: location, vacancies, deadline, application fee, application method, application period and selection process.

## Source URL vs Apply URL

`source_url` is the original job/details page. `apply_url` is a separately verified submission destination. Source-page anchors, forms, buttons and URLs found in extracted content are collected first, then Cerebras selects the actual application destination from those exact candidates. The returned URL must match a source-backed candidate. If no verified application destination exists, `apply_url` remains empty and the button becomes `READ MORE`.

## Editorial strategy

The audience is Bangladesh-based young job seekers around 20–30. Ranking gives extra weight to BBA/MBA, business, banking, finance/accounting, marketing/sales, HR, operations, management trainee, graduate and internship roles. Nationality boilerplate is not inserted into posts.

## Publishing volume

- Minimum target: 5 best eligible jobs per run.
- Maximum: 15 jobs per run.
- Source diversity is enforced when enough distinct sources are available.
- The bot never fabricates vacancies.

## Discovery

- RSS, Bangladesh job portals, Google News RSS and Exa are complementary discovery layers.
- Discovery window: latest 72 hours.
- Expired jobs are rejected.
- Deadline distance is a ranking factor, not a seven-day hard gate.

## Technical architecture

RSS → Google News → Exa → persistent queue/state → URL/event deduplication → source/article extraction → fact-locked `JobRecord` → local + AI ranking → deterministic verification → article-image recovery → source branding → 1200×675 image processing → Telegram Rich Message → native InlineKeyboardMarkup → Bot API fallback → GitHub Actions state persistence.

## Project tree

```text
CareerNewsBot-main-V1/
├── .github/
│   └── workflows/
│       ├── import-zip.yml
│       └── newbot.yml
├── README.md
├── main.py
├── news_state.json
├── posted_urls.txt
└── requirements.txt
```
