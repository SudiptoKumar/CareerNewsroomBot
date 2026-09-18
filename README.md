# CareerNewsroom

Production Bangladesh job-news bot for `@CareerNewsroom`, using the proven TechNewsroom runtime framework with a dedicated career discovery and selection engine.

## Publishing flow
```text
RSS + direct job portals + Google News RSS + Exa
→ normalize + URL dedup
→ job-detail detection
→ 72-hour rolling inventory
→ local relevance scoring
→ source-balanced AI ranking (bounded)
→ source-page extraction
→ separate Source URL / Apply URL resolution
→ fact-locked job record
→ Rich HTML + inline keyboard
→ Telegram
```

## Key behavior
- Each run targets **5 to 15** verified vacancies when enough eligible jobs exist. The bot never fabricates jobs to satisfy the minimum.
- Discovery remains a **72-hour rolling window**. Jobs with no explicit portal timestamp may use `first_seen` as a lower-confidence discovery anchor.
- Google News and Exa are always-on complementary discovery channels, not only emergency gap-fillers.
- Bdjobs is a high-priority source and is extracted using job-detail URL detection instead of requiring the title to contain the word `job`. Google News and Exa are always-on parallel discovery channels so large direct-source inventories do not suppress secondary sources.
- Large sources are filtered by vacancy intent, audience relevance, page type, freshness, deadline usefulness, source reliability, completeness and source diversity.
- BBA/MBA, business, finance/accounting, banking, marketing/sales, HR, operations, management trainee, graduate, entry-level and internship roles receive stronger audience-fit signals.
- Official government, NGO, education and reputable corporate vacancies remain eligible when relevant.
- Generic career advice, portal utility pages, scholarships, training/event content, profiles, list pages and obvious non-vacancies are filtered before expensive AI processing.
- AI ranking is bounded to a single batch of the strongest candidates. There is no per-job AI claim-verification loop. AI job-record extraction is a small fallback only for ambiguous records.
- Runtime is designed around a low-token funnel: broad discovery → deterministic job filtering → local scoring → one AI ranking batch → local source-locked publishing. A bounded reserve pass can inspect additional candidates only when the 5-post target has not been reached.
- AI cannot replace source-backed job identity. Title, company, location, deadline, application information and URLs remain source-locked.

## URL rules
- `source_url` = original job/details page.
- `apply_url` = actual application destination.
- They are stored independently from discovery through state and Telegram.
- The source URL is **never** used as an Apply URL fallback.
- The Apply button is created only from a verified HTTP(S) `apply_url`.
- The button text is exactly `APPLY NOW`, with no emoji.
- Raw URLs are not displayed in the message body.

## Post layout
```text
📣 JOB TITLE

🏢 Company: ...

JOB SNAPSHOT
┌──────────┬────────────────┐
│ FIELD    │ DETAILS        │
├──────────┼────────────────┤
│ ...      │ ...            │
└──────────┴────────────────┘

#hashtags

🔎 Official Source: Source Name

[ APPLY NOW ]   ← native Telegram Inline Keyboard
```

Missing fields are omitted. Gender is not collected or displayed. High-impact information is kept in the snapshot; lower-value editorial sections are intentionally removed.

## Technical framework preserved
RSS ingestion, Google News, Exa, persistent queue/state, canonical URL handling, event deduplication, article extraction, image recovery, source-logo/source-name fallback, optional text-only publishing, 1200×675 image processing, Telegram Rich Messages, native InlineKeyboardMarkup, Bot API fallback, GitHub Actions, numeric grounding and deterministic fact validation.

## Repository tree
```text
CareerNewsroom/
├── .github/workflows/
│   ├── import-zip.yml
│   └── newbot.yml
├── README.md
├── main.py
├── news_state.json
├── posted_urls.txt
└── requirements.txt
```

## Validation
```bash
python -m py_compile main.py
python main.py --self-test
```
The self-test covers source/apply URL separation, real application-link extraction, Bdjobs detail-page detection, no-placeholder fields, no gender field, native inline keyboard payload, source diversity, 72-hour freshness, expired-deadline rejection, and the one-batch ranking design.
