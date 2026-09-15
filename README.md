# CareerNewsroomBot V2 FINAL

Production-grade Bangladesh job aggregation and Telegram publishing for `@CareerNewsroom`.

## Editorial source policy

The bot uses a **registered-source publishing policy**. Official government/employer/ATS sources are publishable, and the channel explicitly approves **BDJobs** and **BDJobs Live** as trusted job-board publishers. **Dohaj and other unapproved republishers remain discovery-only and can never become the displayed source.**

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

## Company Identity Image Engine

Every published job uses a company/organization identity image rather than a job-board/source logo or a source-name fallback.

Priority: official circular/PDF logo -> employer official career/homepage logo -> official ATS-hosted logo with employer provenance -> verified public company social profile (Facebook/X/Instagram/LinkedIn) -> public Google/Bing image discovery as a last-resort search layer.

Logo selection is scored by employer-name match, provenance, original pixel dimensions, content area, asset type, and anti-placeholder rules. SVG logos are rasterized at high resolution. External white margins are trimmed without destroying internal white logo details. The selected logo is displayed large on a 1200 x 675 full-bleed company card with the verified company name and only `@CareerNewsroom` in the bottom-right.

BDJobs and BDJobs Live may remain job sources, but their logos are never used as company identity images.
If no sufficiently confident employer logo can be found, the vacancy is held rather than publishing a misleading image.

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

### Deadline policy

An active job is eligible while its application deadline is still in the future. The previous hard 7-day minimum has been removed. Deadline proximity affects urgency/ranking instead of eligibility.
