# CareerNewsroomBot V1

Bangladesh job aggregation, ranking, deduplication, verification and Telegram publishing bot.

Built from the proven operational patterns of TheTechNewsroomBot, with career-specific discovery and Job Event logic.

## Services

- Exa API: discovery and page-content fallback
- Cerebras API: limited batch enrichment and ranking
- Telegram Bot API: publishing to `@CareerNewsroom`
- GitHub Actions: scheduled execution

No external database is used.

## Required Secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

## Optional Variables

- `CEREBRAS_MODEL` - defaults to `gpt-oss-120b`
- `TELEGRAM_ADMIN_CHAT_ID`

The channel is intentionally fixed by the workflow to `@CareerNewsroom`.

## State

- `job_state.json` - Job Event state, scores, verification, source health and Telegram state
- `posted_urls.txt` - canonical source URLs successfully published
- `source_registry.json` - audited Bangladesh source universe

## Discovery

1. Direct registered sources
2. Google News RSS
3. Exa Search with page contents/highlights

Direct fetch failures do not automatically discard Exa evidence.

## AI usage

The bot does NOT make a Cerebras call for every job.

1. Deterministic extraction first
2. One bounded batch enrichment call for ambiguous jobs
3. One bounded ranking call for the strongest event pool

This reduces rate-limit pressure while retaining AI-based classification/ranking.

## Verification

Official government and official employer sources have the highest trust. Suspicious recruitment/payment signals are rejected.

## Telegram

Preflight checks:

- bot identity
- target channel existence
- bot membership/admin status

Publication:

- 1200x675 image
- image fallback card
- `APPLY NOW` button
- photo -> text fallback
- state is marked published only after Telegram confirms success

## Workflow

Runs hourly from 07:00 to 23:00 Asia/Dhaka using the same timezone-explicit GitHub Actions pattern as the proven Tech bot.

## Local validation

```bash
python main.py --self-test
```


## Processing Pipeline

Normalized jobs → Job fingerprinting → Same-job detection → NEW / UPDATE / REPOST → Verification → Scam filtering → Quality score → Importance score → Publish threshold → Telegram publishing → Image/logo fallback → State update.

A stable Job Event ID is retained across genuine updates. Mutable changes such as deadline, salary or vacancy are tracked instead of creating duplicate Job Events.
