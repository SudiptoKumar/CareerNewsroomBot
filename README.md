# CareerNewsroomBot V1

Production-oriented Bangladesh job discovery and Telegram publishing bot.

## Publication policy

Every enabled source in `source_registry.json` is attempted on every run. There is no source rotation, category/news diversity filter, artificial per-run post quota, or importance threshold that blocks a valid job.

A job is published only when:

1. It is a real Bangladesh job/circular, not a source homepage, dashboard, login page, or generic portal page.
2. A meaningful organization and job title are supported by the source or recovered from the circular.
3. A real application deadline is extracted. Missing deadlines are never invented.
4. The deadline is **at least 7 full days in the future**.
5. Verification passes and scam filtering passes.
6. Quality is at least 60.
7. The Job Event lifecycle permits `NEW`, `UPDATE`, or `REPOST` publication.

## Source coverage and runtime

All 45 registered sources are attempted each run. Source crawling uses bounded concurrency so a few inaccessible `gov.bd`/Teletalk hosts cannot hold the entire workflow for 30 minutes.

Source status is truthful:

- `SOURCE CRAWL OK` means the HTTP/source request completed and candidates were read.
- `SOURCE CRAWL FAILED` means the source request or extraction failed.

`mailto:`, `javascript:`, localhost, login, account, and other non-source URLs are ignored.

## PDF-first extraction

Government and institutional Bangladesh recruitment notices are often PDFs. V1 treats PDF circulars as first-class job documents:

```text
Registered source
→ HTML/PDF discovery
→ direct PDF / embedded PDF / JavaScript PDF URL detection
→ pypdf text extraction
→ deadline/date extraction
→ organization/title/application URL extraction
→ 7-day eligibility
→ Job Event processing
→ Telegram
```

Bengali digits and Bengali month names are normalized for date extraction.

## AI rate-limit protection

Cerebras is **not** called for every discovered job. Deterministically complete jobs bypass AI.

AI is used as a rescue layer only when an essential field is missing, especially when the document is a PDF/circular or contains deadline evidence.

The Cerebras client is configured with automatic retries disabled when supported, and calls are bounded to a small rate window. A `429` skips the affected rescue batch instead of sleeping through the whole GitHub Actions run.

## Telegram post template

```text
Photo

📌 JOB TITLE

One-sentence job summary.

KEY HIGHLIGHTS
🏢 Organization: ...
📍 Location: ...
👥 Vacancy: ...
🎓 Education: ...
🧑‍💼 Experience: ...
💰 Salary: ...
💼 Employment: ...
📅 Application Deadline: ...

REQUIREMENTS
• ...
• ...

JOB RESPONSIBILITIES
• ...
• ...

HOW TO APPLY
Use the APPLY NOW button below and follow the official application instructions.

Source: ...

[APPLY NOW]
```

Generic source-page titles such as `National Job Portal` or `AllJobs by Teletalk | ...` are rejected instead of being published as jobs.

## Event lifecycle

```text
Normalized Job
→ Job Fingerprint
→ Same-job Detection
→ NEW / UPDATE / REPOST
→ Verification
→ Scam Filter
→ Quality / Importance metadata
→ 7-day Deadline Gate
→ Telegram Publishing
→ Image / Logo / Branded Fallback
→ State Update
```

The Job Event ID excludes mutable fields such as deadline and application URL, so a deadline/salary/application update remains the same event and becomes an `UPDATE` instead of a duplicate.

## State files

- `source_registry.json`
- `job_state.json`
- `posted_urls.txt`

## Secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- optional `TELEGRAM_CHANNEL` (default `@CareerNewsroom`)
- optional `TELEGRAM_ADMIN_CHAT_ID`

## Current AI model

`gpt-oss-120b` through the Cerebras API.

## Scheduling

GitHub Actions runs hourly during the Bangladesh day/evening window and supports `workflow_dispatch`.
