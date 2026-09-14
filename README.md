# CareerNewsroomBot V1

Production-oriented Bangladesh job discovery and Telegram publishing bot for `@CareerNewsroom`.

## Core publication policy

Every enabled source in `source_registry.json` is attempted on every run. Valid jobs are not discarded to satisfy a category-diversity quota.

A job is published only when:

1. It is a real Bangladesh job/circular, not a source homepage, login page, dashboard, or generic article page.
2. The organization and job title are supported by the source, circular, structured data, or verified AI rescue.
3. A real application deadline is extracted.
4. The application deadline is **at least 7 full days in the future**.
5. Verification and scam filtering pass.
6. Quality is at least 60.
7. The Job Event lifecycle permits `NEW`, `UPDATE`, or `REPOST`.

## Discovery and source attribution

Exa is a discovery/retrieval service only. It is **never** displayed as the publication source.

The displayed source is resolved from the real job/application URL or the matching source in `source_registry.json`. The Source link points to the actual application/job page whenever available.

All 45 enabled registry sources are attempted every run. Source status is reported truthfully as `OK` or `FAILED`.

There is no artificial per-run post quota and no category filter that drops an otherwise valid job.

## Job extraction

The extraction order is:

```text
Registered source
→ HTML/job page
→ direct PDF / embedded PDF / JavaScript PDF
→ PDF text extraction
→ deterministic field extraction
→ AI rescue only for missing/uncertain essentials
→ validation
→ 7-day deadline gate
```

Generic portal/page titles are rejected. Fields are normalized so values such as `Vacancy: Vacancy: 100` do not reach Telegram. Broken/truncated values are removed instead of being shown with `...`.

## Circular and image priority

Recruitment circulars are first-class media.

```text
1. Actual job image / job circular PDF page
2. High-quality source logo
3. Centered bold source name
```

For PDFs, several pages are rendered and ranked by visible-content density so an empty cover page is less likely to be selected when a later page contains the actual circular table/details.

Generic `og:image`, social-share banners, favicons, avatars, placeholders, and unrelated page images are never treated as a verified job photo.

For level 1 and level 2 images, only `@CareerNewsroom` is added at the bottom-right. No other overlay text is added.

For the final source-name fallback, only the bold centered source name is shown. No username is added.

## Telegram post structure

Telegram rich messages are used when supported. The presentation is intentionally clean and contains no pin emoji.

```text
JOB TITLE

One-sentence summary.

KEY HIGHLIGHTS
Organization
Location
Vacancy
Education
Experience
Salary
Employment
Application Deadline

REQUIREMENTS

JOB RESPONSIBILITIES

[APPLY NOW]

Source: Actual source name
```

The post language is selected as English or Bangla from the job content. The generic sentence `The organization is recruiting for this position in Bangladesh.` is not used.

The `Source` label is the real publisher/application source, never `Exa Discovery`.

## BBA/MBA priority

Valid jobs remain eligible across all sectors. Publication ordering gives additional priority to roles relevant to BBA/MBA students and early-career candidates, including:

- BBA/MBA
- business administration/management
- management or graduate trainee
- internships
- freshers/entry level
- marketing
- finance/accounting/audit
- banking
- HR
- administration
- business development/sales
- procurement

This affects ordering only. It does not exclude other valid jobs.

## Source distribution

The final publish queue is round-robin interleaved across available sources after relevance ranking. This prevents one high-volume source from monopolizing the channel while preserving all qualifying jobs.

## AI rate-limit policy

Cerebras is not called for every discovered page. Complete deterministic jobs bypass AI. AI is an exception/rescue layer for incomplete or uncertain records.

The client disables automatic SDK retry storms when supported. A rate-limited rescue batch is skipped instead of blocking the whole GitHub Actions run for repeated one-minute waits.

Configured model: `gpt-oss-120b`.

## Job Event lifecycle

```text
Normalized Job
→ Fingerprint
→ Same-job Detection
→ NEW / UPDATE / REPOST
→ Verification
→ Scam Filter
→ Quality / Importance
→ 7-day Deadline Gate
→ Telegram Publishing
→ State Update
```

The Event ID is based on stable job identity, not mutable deadline/application fields. A revised deadline, salary, vacancy, or application URL can therefore remain the same job event and become an `UPDATE`.

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

## Scheduling

GitHub Actions runs hourly during the Bangladesh day/evening window and supports `workflow_dispatch`.
