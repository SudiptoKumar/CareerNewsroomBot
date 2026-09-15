# CareerNewsroomBot V2 FINAL

Production-grade Bangladesh job aggregation and Telegram publishing for `@CareerNewsroom`.

## Editorial source policy

The bot now uses an **authoritative-source-only publishing policy**. A website that merely reposts jobs from another employer or job portal is not a valid source. Examples include BDJobs-style job boards, Dohaj-style republishers, and similar aggregators. They may appear as discovery hints, but they can never become the displayed source and their listing URL is not publishable unless the bot resolves the same vacancy to an authoritative employer/government source.

Authoritative sources are limited to the registry: official Bangladesh government portals, official government agencies, official universities/organizations, official employer career pages, and official ATS/career systems operated on behalf of an employer.

## Publishing architecture

```text
Official registry sources ───────┐
                                ├─> current job candidate filter
Exa / discovery services ───────┘
                                      │
                                      v
                           authoritative URL resolution
                                      │
                                      v
                         job title + deadline validation
                                      │
                                      v
                         government Grade 1–10 gate
                                      │
                                      v
                           duplicate/event detection
                                      │
                                      v
                      BBA/MBA relevance + fair ordering
                                      │
                                      v
                             image intelligence
                                      │
                                      v
                        Telegram Rich Message API
                                      │
                                      v
                             @CareerNewsroom
```

## Telegram post design

Rich Message is the primary publisher. The new mobile-first layout uses a compact hierarchy rather than repeating the title inside a generic `Post:` field:

```text
# JOB TITLE
🏢 Company
Short verified summary

JOB SNAPSHOT
┌───────────────┬────────────────┐
│ Location      │ Dhaka          │
│ Vacancy       │ 4              │
│ Education     │ ...            │
│ Experience    │ ...            │
│ Salary        │ ...            │
│ Employment    │ Full-time      │
│ Deadline      │ 24 Sep 2026    │
└───────────────┴────────────────┘

✅ REQUIREMENTS      (collapsed)
📝 RESPONSIBILITIES (collapsed)

⏰ Check the deadline before applying.

[ APPLY NOW ]
🏛️ OFFICIAL SOURCE: Employer
@CareerNewsroom
```

Bangla content is explicitly sent **left-to-right** with `is_rtl: false`.

## Image policy

1. Actual circular/PDF page or job-specific image
2. High-quality official source logo
3. Centered official source name fallback

The channel username is added only as the image branding marker, not as a fake job label.

## Runtime protection

- Every enabled authoritative registry source is still attempted.
- Direct discovery is bounded per source to reduce link explosions.
- Obviously historical PDFs are rejected before download/OCR.
- PDF size is capped at 20 MB.
- Native PDF text extraction runs before OCR.
- OCR is limited to the first two pages and a per-run document budget.
- PDF rendering is delayed until after publication gates pass.
- An 18-minute internal runtime budget prevents the GitHub job from spending the whole 25-minute timeout on low-value documents.

## Environment variables

Required:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional:

```text
TELEGRAM_CHANNEL=@CareerNewsroom
CEREBRAS_MODEL=gpt-oss-120b
TELEGRAM_ADMIN_CHAT_ID=<chat id>
```

## Local checks

```bash
python -m py_compile main.py
EXA_API_KEY=dummy CEREBRAS_API_KEY=dummy TELEGRAM_BOT_TOKEN=dummy python main.py --self-test
```

## Important

A locally mocked Telegram test is not a live Telegram test. The workflow log is the source of truth for the real Bot API run.
