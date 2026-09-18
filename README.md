# CareerNewsBot V1

Production Bangladesh job-news bot for `@CareerNewsroom`, built on the existing TechNewsroom technical framework.

## Post structure
- Job title and company first.
- High-impact job facts are kept in `JOB SNAPSHOT` as a compact Rich HTML table.
- Missing fields are omitted completely.
- No separate `Suitable For` or `Key Highlights` blocks.
- Order: job card → hashtags → Official Source → one native Telegram Inline Keyboard button.
- The Apply button text is exactly `APPLY NOW`. It contains only the actual application destination.
- Source URL and Apply URL are separate fields end-to-end. The source/details URL is shown only behind the source name.
- No raw URL is displayed in the message body.
- If no usable job photo exists, the bot can publish a text-only Rich Message.

## Application URL rules
- `source_url` is the original job/details page.
- `apply_url` is the real application destination.
- A source URL is never used as an Apply URL fallback.
- Application links found in the Application section, anchors, forms, data attributes, or verified page text are candidates for `apply_url`.
- If the source and application URLs differ by domain, both are preserved independently.
- If no verified application URL exists, the bot does not attach an incorrect fallback button.

## Job snapshot fields
High-value fields are shown only when present: location, type, education, experience, salary, vacancies, age limit, application fee, application method, application period, selection process, deadline and posted date. Raw links inside the Application field are hidden from the body and used as the Apply URL when verified.

## Discovery and selection
- RSS, Bangladesh job portals, Google News RSS and Exa are complementary discovery layers.
- Freshness window: 72 hours.
- Expired vacancies are rejected. Deadline distance is a ranking factor, not a seven-day hard gate.
- Large portals such as Bdjobs are important discovery sources, but are not forwarded indiscriminately.
- Relevance scoring prioritizes BBA/MBA, business, finance/accounting, banking, marketing/sales, HR, business development, operations, management trainee, graduate, internship and other early-career roles.
- Clearly unrelated service/manual roles are filtered unless the job itself contains strong relevance to the target audience.
- Local deterministic scoring runs before AI ranking. Only the strongest locally ranked candidates consume the bounded AI ranking budget.
- Event, URL and title deduplication are retained.
- Source diversity is enforced during candidate processing and publication when multiple sources are available.
- Minimum publication target is 5 and maximum is 15. The bot never fabricates vacancies to meet the target.

## Technical architecture
The implementation preserves the working framework from the reference TechNewsroom bot: RSS ingestion, Google News gap-fill, Exa gap-fill, persistent queue/state, canonical URL handling, event deduplication, article extraction, source/image fallback, 1200×675 image processing, Telegram Rich Messages, native InlineKeyboardMarkup, Bot API fallback, GitHub Actions, numeric grounding, claim verification and self-test.

## Repository tree
```text
CareerNewsBot-main/
├── .github/
│   └── workflows/
│       ├── import-zip.yml
│       └── newbot.yml
├── README.md
├── main.py
├── news_state.json
├── posted_urls.txt
└── requirements.txt
```

## Local validation
```bash
python -m py_compile main.py
python main.py --self-test
```
