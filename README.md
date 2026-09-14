# Career Newsroom Bot

Bangladesh Job Listing Aggregation, Extraction, Verification, Ranking and Telegram Publishing Bot.

## Core services

- Exa API: broad job/source discovery and content fallback
- Cerebras API: structured extraction and semantic classification
- Telegram Bot API: publishing to `@CareerNewsroom`
- GitHub Actions: scheduled execution

No external database is used.

## Required GitHub Secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

## Optional GitHub Variables

- `CEREBRAS_MODEL` (defaults to `gpt-oss-120b`)
- `TELEGRAM_CHANNEL` (defaults to `@CareerNewsroom`)
- `TELEGRAM_ADMIN_CHAT_ID`

`gpt-oss-120b` is a current Cerebras production model. The bot defaults to it when `CEREBRAS_MODEL` is empty.

## State

- `source_registry.json`: source definitions
- `job_state.json`: job/event state, scores, verification, Telegram state
- `posted_urls.txt`: URLs successfully published to Telegram

A URL is written to `posted_urls.txt` only after Telegram confirms successful publication.

## Discovery

The bot combines:

1. Registered direct sources
2. Google News RSS
3. Exa search

Direct fetch failures do not automatically discard an Exa result. Exa-returned text is used as a content fallback.

## Telegram image fallback

1. Source page image
2. Employer logo
3. Generated Career Newsroom fallback card
4. Text-only Telegram post

Telegram inline `APPLY NOW` button uses the normalized application URL.

## Local checks

```bash
python main.py --self-test
python main.py --dry-run
```

The GitHub workflow runs the self-test before the live run.
