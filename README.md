# CareerNewsBot V2.1

Bangladesh job discovery and publishing bot for `@CareerNewsroom`.

## Technical framework

Built from the proven TheTechNewsroomBot runtime pattern: RSS ingestion, persistent queue, Google News gap fill, Exa discovery/extraction, Cerebras ranking/generation, URL/event deduplication, article extraction, numeric/claim grounding, Pillow image recovery, Telegram Rich Message publishing, Bot API fallback, and GitHub Actions state persistence.

Career-specific flow:

`Discovery → normalization → job validation → 72h freshness → event deduplication → hybrid ranking → JobRecord lock → editorial generation → fact verification → image → Rich HTML → Apply button → Telegram`

## Selection policy

The 72-hour window remains the freshness boundary for newly discovered jobs.

The previous minimum 7-day deadline hard gate is not used. Deadline is a ranking factor. Expired jobs remain a hard rejection. Missing optional fields are not rejection reasons.

Hard integrity checks are limited to:

- not a real vacancy
- clearly outside Bangladesh relevance
- expired vacancy
- duplicate vacancy/event
- unusable or unverifiable source
- insufficient job identity confidence
- failed factual verification

## Fact-locking

The source controls factual vacancy identity. Before editorial generation the bot locks:

- job title
- company
- location
- job type
- education
- experience
- salary
- deadline
- apply URL

AI cannot replace these fields with a different vacancy.

## Ranking

Cerebras provides semantic relevance and career-value ranking. Local scoring contributes freshness, deadline usefulness, source reliability, completeness and job-signal strength. A local fallback remains available when AI ranking is incomplete.

The runtime can publish up to 15 jobs in one run when that many eligible unpublished vacancies exist. Availability is data-dependent and is never fabricated.

## Sources

RSS and direct Bangladesh job sources include ProjobsBD, BD Govt Jobs, JobPagol, Bangladesh Pratidin Jobs, JagoNews24, Bangla Tribune, BD24Live, RisingBD, Bangladesh Journal, Prothom Alo, Jugantor, Kaler Kantho, The Daily Star, The Daily Ittefaq, Bdjobs, Dohaj, Job.com.bd, Smart Job, Alljobs Teletalk, BPSC, BCC e-Recruitment, JobsNoticeBD, JobsInfo, JobFeeds, CircularBD, Bangladesher Khabor, Dhaka Post and Dhaka Tribune.

Blocked or unavailable sources are skipped so the remaining discovery paths can continue.

## Telegram output format

Unavailable information is omitted completely. The post does not display raw URLs in the message body. The application URL is attached to the `APPLY NOW` inline button, while the source URL remains attached to the named source link.

```text
📣 JOB TITLE

🏢 Company: ...

JOB SNAPSHOT
┌──────────────┬──────────────────────┐
│ FIELD        │ DETAILS              │
├──────────────┼──────────────────────┤
│ Location     │ ...                  │
│ Type         │ ...                  │
│ Education    │ ...                  │
│ Experience   │ ...                  │
│ Salary       │ ...                  │
│ Deadline     │ ...                  │
└──────────────┴──────────────────────┘

🎯 Suitable For
• ...
• ...

📌 Key Highlights
• ...
• ...
• ...

🔎 Official Source: Source Name

#CareerNewsroom #BangladeshJob

[📝 APPLY NOW]
```

Only fields present in the source-backed job record are rendered. For example, when salary is not provided, the `Salary` row is not shown.

## Rich Message implementation

The bot uses Telegram `sendRichMessage` with HTML rich formatting and a media attachment. The application action is provided through Telegram's `reply_markup` inline keyboard. The Bot API `sendPhoto` fallback uses the same button when Rich Message delivery fails.

The HTML output uses a real Rich Message `<table>` for the job snapshot, hidden link targets for the source/apply destinations, and compact sections for applicant suitability and factual highlights.

## Image pipeline

1. article image
2. source logo fallback
3. source-name fallback

All generated images are normalized to `1200×675` and branded with `@CareerNewsroom`.

## Files

```text
CareerNewsBot-main-V2.1/
├── .github/
│   └── workflows/
│       ├── newbot.yml
│       └── import-zip.yml
├── main.py
├── news_state.json
├── posted_urls.txt
├── requirements.txt
└── README.md
```

## Required secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

Optional:

- `TELEGRAM_ADMIN_CHAT_ID`
- `CEREBRAS_MODEL`
- `NEWS_MODE`

## Validation

```bash
python -m py_compile main.py
python main.py --self-test
```
