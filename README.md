# CareerNewsBot V2

Bangladesh job discovery and publishing bot for `@CareerNewsroom`.

## Architecture

This build preserves the proven TheTechNewsroom framework: RSS ingestion, persistent queue, Google News gap fill, Exa discovery/extraction, Cerebras ranking/generation, event deduplication, numeric/claim grounding, Pillow image recovery, Telegram Rich Message publishing, Bot API fallback and GitHub Actions state persistence.

Career-specific redesign: discovery -> 72h freshness -> Bangladesh integrity -> event dedup -> hybrid AI/local ranking -> full extraction -> immutable JobRecord -> editorial generation -> fact locking -> verification -> image -> Rich HTML -> Telegram.

## Decision policy

The previous `deadline >= 7 days` hard gate is removed. Deadline is a soft ranking factor. Expired vacancies remain a hard integrity rejection. Missing deadlines are lower-confidence but are not automatically rejected.

Freshness remains a 72-hour discovery window.

Only integrity failures are hard gates: non-job, clearly non-Bangladesh, expired, duplicate, malformed/unverifiable source, low-confidence job record, or failed factual verification.

## Immutable JobRecord

Before editorial generation, the bot extracts and locks: job title, company, location, job type, education, experience, salary, deadline and apply URL. The AI cannot replace these facts with a different vacancy.

## Hybrid ranking

Cerebras returns a 0-100 ranking score, relevance and career value. A deterministic local score uses freshness, deadline usefulness, source reliability, completeness and job-signal strength. Final ranking combines both. If Cerebras ranking is incomplete, local ranking fills the missing candidates.

## Sources

RSS and direct Bangladesh job sources include ProjobsBD, BD Govt Jobs, JobPagol, Bangladesh Pratidin Jobs, JagoNews24, Bangla Tribune, BD24Live, RisingBD, Bangladesh Journal, Prothom Alo, Jugantor, Kaler Kantho, The Daily Star, The Daily Ittefaq, Bdjobs, Dohaj, Job.com.bd, Smart Job, Alljobs Teletalk, BPSC, BCC e-Recruitment, JobsNoticeBD, JobsInfo, JobFeeds, CircularBD, Bangladesher Khabor, Dhaka Post and Dhaka Tribune.

Blocked/timeout sources are skipped so discovery continues through other sources.

## Telegram format

```text
📣 JOB TITLE

🏢 Company: ...
📍 Location: ...
💼 Type: ...
🎓 Education: ...
👨‍💼 Experience: ...
💰 Salary: ...
📅 Deadline: ...

🎯 Suitable For
...

📌 Key Highlights
• ...
• ...
• ...

📝 Apply Now
Apply Here

🔎 Source
...

#CareerNewsroom ...
```

Rich HTML, 1200x675 image generation, article-image recovery, source-logo fallback and source-name fallback follow the reference bot.

## Secrets

`EXA_API_KEY`, `CEREBRAS_API_KEY`, `TELEGRAM_BOT_TOKEN`. Optional `TELEGRAM_ADMIN_CHAT_ID`.

## Run

`python -m py_compile main.py`

`python main.py --self-test`

`python main.py`
