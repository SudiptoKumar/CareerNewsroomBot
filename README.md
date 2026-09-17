# CareerNewsBot V2.2

Production-oriented Bangladesh career news bot for `@CareerNewsroom`.

## Output policy
- Facts are source-locked. AI cannot replace the job title, company, location, deadline, apply URL, or other job facts.
- The Telegram card uses `JOB SNAPSHOT` with high-impact information in a table. Missing fields are omitted instead of showing `Not specified`.
- No `Suitable For` or `Key Highlights` sections. High-impact application details such as vacancies, age limit, application fee, application method, application period, and selection process are moved into the table when present.
- Order is: hashtags → official source → single `📝 APPLY NOW ↗` button. URLs are hidden in the body.
- Real article images are used when available. Missing images never block a post, which is published as text-only.
- After source extraction, editorial formatting is deterministic, which avoids an unnecessary second AI call and protects vacancy identity.

## Audience/editorial strategy
The selection layer prioritizes useful opportunities for Bangladesh-based young professionals around the 20–30 age range, with extra relevance for BBA/MBA, business, finance/accounting, banking, marketing/sales, HR, management trainee, graduate, and internship roles. Nationality phrases are not added to the post copy.

## Discovery
RSS and job portals are primary. Google News and Exa provide gap filling. Source diversity is enforced during selection with up to 3 published posts per source per run when at least 5 sources are available. The system relaxes that cap only when needed to fill the run target.

Current direct portal coverage includes Bdjobs, BDJobs Live, Dohaj, Job.com.bd, Smart Job, Alljobs Teletalk, BPSC, BCC e-Recruitment, JobsNoticeBD, JobsInfo, JobFeeds, CircularBD and Bangladesh career/news job sections.

## Freshness and deadline
- Discovery window: latest 72 hours.
- Expired jobs are rejected.
- Deadline distance is a ranking factor, not a hard seven-day gate.
- Missing publication timestamps may use source/fetch evidence according to the existing reference framework.

## Telegram delivery
- Rich HTML message when available.
- Real photo when available.
- Text-only Rich Message when no photo exists.
- Bot API fallback with a single Apply button.

## Runtime
Same core technical family as the reference Tech News bot: Exa, Cerebras, Telegram, RSS, Google News, persistent state, URL/event deduplication, article extraction, image processing, GitHub Actions and self-test.

## Files
```text
CareerNewsBot-main-V2.2/
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
Run:

```bash
python -m py_compile main.py
python main.py --self-test
```
