# CareerNewsroomBot V1

CareerNewsroom V1 is a category-first Bangladesh job-intelligence pipeline for BBA/MBA students, graduates, freshers and early-career business candidates.

## Sources

### Government
Teletalk AllJobs API is the government source. Government vacancies are evaluated for active validity/freshness and are not passed through the BBA/MBA private-job relevance filter.

### Private
Bdjobs is the only private-job source. V1 checks every configured BBA/MBA-oriented category on every run:

1. Accounting / Finance
2. Bank / Non-Bank Financial Institution
3. Commercial / Supply Chain
4. Marketing / Sales
5. HR / Organization Development
6. General Management / Admin
7. Customer Service / Call Centre
8. Media / Advertisement / Event Management
9. Research / Consultancy
10. NGO / Development
11. Hospitality / Travel / Tourism
12. Garments / Textile
13. IT / Telecom - Business Roles
14. Education / Training - Business Roles

The legacy `jobs.bdjobs.com/jobsearch.asp?fcatId=...` URLs are used because they are still useful server-rendered category pages for discovery. Some categories now redirect to Bdjobs' newer `/h/jobs/?fcatId=...` application. V1 treats these as the same source and does not use a second job board as fallback.

Bdjobs' public search page currently exposes the same functional category system together with Posted within windows through 5 days, deadline windows, job level, fresher/experience, age range, job nature and up to 100 jobs per page. V1 uses those source concepts but applies the final freshness rule from authoritative job data instead of depending on undocumented filter parameter names. citeturn851991view0

## Discovery strategy

Every configured category is queried on every run. A bounded number of candidates is collected from each category so large categories such as Marketing/Sales cannot consume the entire discovery budget.

The structured Bdjobs JSON search API is added as a supplementary lane when the category windows do not already fill the private candidate target.

The private funnel is:

```text
14 categories x bounded candidate window
        + Bdjobs structured API when needed
                       |
                       v
                cross-category dedup
                       |
                 broad recall pool
                       |
                 cheap first score
                       |
                   top ~40
                       |
              detail-page enrichment
                       |
             100-point private score
                       |
             Cerebras semantic audit
                       |
             diversity-aware rerank
                       |
                  best 12-20
```

## Private ranking: 100 points

| Factor | Points |
|---|---:|
| BBA/MBA education fit | 20 |
| Business career/function fit | 20 |
| Fresher/early-career fit | 10 |
| Preferred age fit around 18-30 | 5 |
| Posted freshness | 15 |
| Deadline actionability | 10 |
| Salary | 5 |
| Vacancy | 5 |
| Information quality | 5 |
| Category confidence | 5 |
| **Total** | **100** |

Experience is ranked smoothly from fresher to experienced instead of using the old hard 3-year rejection. Private jobs with a verified posted date older than 5 days are excluded from publication, and expired jobs are excluded.

The 18-30 age target is a ranking preference, not a universal hard rejection. Wider age ranges can still survive when the overall job is otherwise relevant.

## Semantic audit

Cerebras receives only the strongest private candidates. It checks for hidden mismatches such as specialist degree requirements, title/description contradictions and seniority that is not obvious from a title.

AI does not own the publication decision. The deterministic score remains the core ranking signal, with semantic audit used as a bounded adjustment and red-flag signal.

## Final selection

The selector is quality-first and diversity-aware. It gives a small bonus to an otherwise underrepresented source category and penalizes company or career-family monopolies. There are no fixed category quotas, so weak jobs are not published just to fill a category.

Government jobs are selected separately and placed before private jobs.

## Runtime configuration

```text
MAX_STORIES_PER_RUN=20
MAX_GOVERNMENT_POSTS_PER_RUN=5
BDJOBS_CATEGORY_CANDIDATES_PER_CATEGORY=10
BDJOBS_CATEGORY_WORKERS=8
FAST_PRIVATE_CANDIDATE_TARGET=160
PRIVATE_RESEARCH_TARGET=40
FAST_DETAIL_WORKERS=10
FAST_AI_CANDIDATE_LIMIT=32
MAX_PRIVATE_POST_AGE_DAYS=5
PRIVATE_QUALITY_FLOOR=64
```

## Required secrets

```text
TELEGRAM_BOT_TOKEN
```

Optional:

```text
CEREBRAS_API_KEY
CEREBRAS_MODEL
```

## Validation

```bash
python -m py_compile main.py
python main.py --self-test
```

Use `python main.py --source-test` from a manual GitHub Actions run to inspect every configured Bdjobs category and the Teletalk/Bdjobs source health.

## Source notes

Bdjobs currently exposes New Jobs and Deadline Tomorrow pages in addition to category search. The category-first collector is intentional because it gives the bot explicit coverage of the business career areas instead of relying on a single mixed listing. citeturn899899view0turn899899view1

The Teletalk AllJobs search endpoint used by the government lane is documented by an open-source integration that records job ID, title, organization, vacancy, deadline and application URL. citeturn917758search1
