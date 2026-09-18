# CareerNewsBot V1

Production-oriented Bangladesh job discovery and publishing bot for `@CareerNewsroom`.

## V1 goals
- Publish **5–15 high-value vacancies per run** when enough valid jobs exist.
- Maintain a rolling **72-hour job inventory** so good jobs not published in one run remain eligible for the next run.
- Prioritize jobs useful to young Bangladesh job seekers, especially BBA/MBA, finance/accounting, banking, business, management, HR, marketing/sales, operations, supply chain, graduate, fresher, trainee and internship roles.
- Treat deadline distance as a ranking signal. Expired jobs are rejected, but there is no mandatory 7-day deadline gate.
- Use source-backed facts only. AI ranks candidates only; factual vacancy identity and application routing are source-derived and fact-locked.

## Telegram post format
```text
📣 JOB TITLE

🏢 Company: ...

JOB SNAPSHOT

FIELD | DETAILS
... dynamic factual rows ...

#hashtags

🔎 Official Source: Source Name

[ APPLY NOW ]
```

### Output rules
- Missing information is omitted completely.
- No `Not specified` placeholders.
- The table contains only high-impact information.
- `Application` is a short method such as `Online`, `Email`, `Via Bdjobs`, or `Hard copy`, not a copied paragraph.
- `source_url` is the page used for details.
- `apply_url` is the direct application destination.
- `source_url` and `apply_url` are **never allowed to be the same**.
- The `APPLY NOW` button is a single native Telegram inline keyboard button with no emoji.
- The source name is clickable in the body and opens the details/source page.
- A missing article image never blocks publication.

## Discovery architecture
```text
RSS / job portals / Google News / Exa
                ↓
         URL + navigation filter
                ↓
       Real job/detail detection
                ↓
      Bangladesh relevance filter
                ↓
        72-hour inventory queue
                ↓
      Event / URL deduplication
                ↓
         Local relevance score
                ↓
       Batched Cerebras ranking
                ↓
        Top detailed candidates
                ↓
  Source-backed JobRecord extraction
                ↓
      Distinct apply URL required
                ↓
      Numeric + deadline grounding
                ↓
       Dynamic JOB SNAPSHOT table
                ↓
     Image → text-only fallback
                ↓
 Telegram Rich Message + InlineKeyboard
                ↓
        Telegram Bot API fallback
```

## Advanced portal strategy

### Bdjobs
Bdjobs exposes functional categories, organization/industry filters, Bangladesh location filters, posted-within filters, fresher/experience filters, and dedicated job-detail links. V1 uses the live listing as a discovery index and then follows only real job-detail URLs. This prevents navigation pages, app pages and employer tools from becoming candidates. citeturn924004search0turn236266view2

Priority lanes are derived from the target audience rather than blindly reposting every listing:
- BBA/MBA/business/management
- Accounting/finance/banking
- Marketing/sales/business development
- HR/operations/supply chain
- Management trainee/graduate/fresher
- Internship

### BDJobs Live
BDJobs Live currently exposes `New Jobs`, `Intern Jobs`, `Freshers Jobs`, `Deadline Tomorrow`, and category/industry groupings. Its job-detail pages expose fields such as vacancy, age, location, salary, experience, job type, education, application instructions and an `Apply Now` action. V1 monitors the job-detail URL pattern and its high-value current-job lanes rather than arbitrary navigation links. citeturn447637view0turn447637view1turn447637view2turn236266view0

## Source vs Apply URL

The separation is mandatory:

```text
Official Source → source_url
Apply Now    → apply_url
```

The resolver checks links, forms, data attributes, buttons, page text and external application URLs. If a distinct HTTP application destination cannot be established, the vacancy is not published instead of pointing the user back to the source page.

## Ranking model

The local ranking layer considers:
- job intent / real vacancy confidence
- audience relevance
- freshness
- deadline usefulness
- source reliability
- data completeness

Cerebras then performs a batched editorial ranking over the strongest candidates. AI ranking failure falls back to deterministic ranking instead of aborting the run.

## Source diversity

Publication uses a dynamic per-source cap so one publisher does not dominate the run when several sources are available. Diversity is secondary to vacancy quality, but the selector actively rotates across sources.

## Integrity rules
Hard rejects are limited to data integrity problems:
- clearly non-job content
- clearly foreign-only vacancy
- expired vacancy
- duplicate vacancy/event
- missing or invalid source URL
- no distinct direct application destination
- insufficient source confidence

The old fixed 7-day deadline rejection is intentionally removed.

## Technical framework
V1 keeps the proven framework of the reference TechNewsroom bot:
- RSS ingestion and persistent queue
- Google News RSS gap filling
- Exa gap filling
- URL/event deduplication
- source/article extraction
- persistent JSON state
- numeric/date grounding
- Pillow image pipeline
- article image and source-logo fallback
- 1200×675 image processing
- Telegram Rich Messages
- native InlineKeyboard markup
- Bot API fallback
- GitHub Actions
- self-test and compile gate

## Project tree
```text
CareerNewsBot-main-V1/
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

## Verification
```bash
python -m py_compile main.py
python main.py --self-test
```

The GitHub Action runs the compile gate, then the self-test, then the live bot. Runtime/API/portal failures are isolated per source wherever possible so one blocked site does not abort the entire run.
