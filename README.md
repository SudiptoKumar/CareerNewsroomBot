# Career Newsroom Bot

Bangladesh Job Listing Aggregation, Extraction, Verification, Ranking and Telegram Publishing Bot.

## Current implementation stage

Step 10 foundation:
- source registry
- repository state
- direct HTML discovery
- RSS / Google News discovery
- Exa discovery
- HTML/PDF text extraction
- deterministic hints
- Cerebras structured extraction
- failure isolation
- GitHub Actions scheduling

Publishing and the full intelligence/ranking gates are deliberately kept as the next implementation layer.

## Required GitHub Secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`

## Optional GitHub Variables

- `CEREBRAS_MODEL`
- `TELEGRAM_CHANNEL`
- `TELEGRAM_ADMIN_CHAT_ID`

## Persistence

No external database is used.

- `job_state.json` stores job/event state.
- `posted_urls.txt` stores normalized processed URLs.
- `source_registry.json` stores source definitions and health metadata.

## Repository

- `main.py` - runtime
- `source_registry.json` - source universe seed
- `job_state.json` - repository state
- `posted_urls.txt` - URL history
- `requirements.txt` - Python dependencies
- `.github/workflows/careerbot.yml` - scheduled automation
