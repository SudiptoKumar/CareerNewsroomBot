# CareerNewsBot V2.3

Production-oriented Bangladesh career and job-news bot for `@CareerNewsroom`.

## Telegram post structure
- High-impact factual fields are presented only inside `JOB SNAPSHOT`.
- Missing fields are omitted. No `Not specified` placeholders.
- `Suitable For` and `Key Highlights` sections are removed.
- Order: job title/company → `JOB SNAPSHOT` → hashtags → official source → one native inline keyboard button.
- The application URL is hidden from the body and attached to a single `📝 APPLY NOW ↗` InlineKeyboard button. Telegram Bot API supports `reply_markup` on `sendRichMessage`, so the button renders outside the Rich Message bubble like a normal inline keyboard.
- Article photo is used when available. No image never blocks publication. Text-only Rich Messages remain valid.

## Editorial strategy
The audience is Bangladesh-based young job seekers around 20–30. Ranking gives extra weight to BBA/MBA, banking, finance/accounting, business, marketing/sales, HR, management trainee, graduate, internship and early-career roles. Nationality boilerplate is not inserted into posts.

## Publishing volume
- Minimum target: **5 best eligible jobs per run**.
- Maximum: **15 jobs per run**.
- The bot processes a broad ranked candidate pool and uses a bounded rescue pass when strict AI verification leaves fewer than five candidates.
- The bot never fabricates jobs. If fewer than five genuinely publishable vacancies exist in the active inventory, it publishes the valid vacancies that remain.
- Source diversity is preferred when five or more sources are available, with a normal cap of 3 published posts per source per run.

## Discovery and freshness
- RSS, Bangladesh job portals, Google News RSS and Exa are used as complementary discovery layers.
- Discovery window: latest 72 hours.
- Expired vacancies are rejected.
- Deadline distance is a ranking factor, not a seven-day hard gate.
- Source-backed identity and factual fields remain immutable after extraction.

## Technical architecture
The bot preserves the proven reference framework: RSS ingestion, Google News gap-fill, Exa gap-fill, persistent queue/state, URL and event deduplication, source/article extraction, fact-locked JobRecord, local + AI ranking, deterministic numeric/deadline checks, article-image recovery, source branding, 1200×675 processing, Telegram Rich Messages, native InlineKeyboardMarkup and Bot API fallback, GitHub Actions and self-test.

## Project tree
```text
CareerNewsBot-main-V2.3/
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

## Verification
```bash
python -m py_compile main.py
python main.py --self-test
```
