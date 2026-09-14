# CareerNewsroomBot V1

Production-oriented Bangladesh job discovery and Telegram publishing bot.

## Core rule

Every enabled source is crawled on every run. There is no source rotation, sector-diversity filter, importance threshold, or NEW/UPDATE/REPOST quota.

A job is eligible for publication only when all of these are true:

1. It is a real Bangladesh job/circular, not a source homepage or dashboard.
2. The organization/title are meaningful and source-supported.
3. The application deadline is present and is **at least 7 full days in the future**.
4. Verification passes.
5. Scam filtering passes.
6. Quality score is at least 60.
7. Job Event state says NEW, UPDATE, or REPOST and the corresponding event is publishable.

## PDF-first discovery

Bangladesh recruitment is frequently published as PDF circulars. V1 therefore:

- crawls source landing pages every run;
- finds linked `.pdf`, embedded PDF, circular, recruitment, appointment and notice links;
- downloads PDFs;
- extracts PDF text with `pypdf`;
- extracts deadlines from English and Bengali date labels;
- sends incomplete records to Cerebras in batches so PDF jobs are not limited to a tiny AI sample.

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

The GitHub Actions workflow runs hourly during the Bangladesh day/evening window and also supports manual `workflow_dispatch` runs.
