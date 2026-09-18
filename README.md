# CareerNewsBot 2.0

Production-oriented Bangladesh job-news bot for `@CareerNewsroom`, optimized for early-career users around 20–30 and especially BBA/MBA, internship, graduate-trainee, finance, accounting, banking, marketing, HR, business, operations and related roles.

## Core design

The bot uses an Agent-Reach-inspired retrieval router without installing the whole Agent-Reach stack:

`Discovery → URL dedup → vacancy gate → retrieval router → deterministic extraction → local audience/quality scoring → bounded Cerebras batch extraction/ranking → event dedup → source diversity → Telegram`

The web retrieval order is:

`Direct HTTP → Jina Reader → Exa Contents`

See `ARCHITECTURE.md` for the full data flow and `CHANGELOG.md` for the release changes.

Jina Reader is the zero-key web fallback. Exa remains a separate discovery/content capability. Agent-Reach's current project uses Jina Reader as its web backend and emphasizes ordered backends plus health checking rather than hard-coding one fragile access path.

## Discovery

- RSS from Bangladesh job/news publishers
- Direct job portals and government recruitment portals
- Google News RSS targeted to Bangladesh vacancies
- Exa targeted discovery only when the current pool is insufficient
- Rolling 72-hour discovery window

Discovery sources are not treated as final authority. The actual vacancy page is retrieved before final publication.

## Job quality model

The bot publishes only when the vacancy has sufficiently strong:

- employer/title identity
- Bangladesh relevance
- early-career/BBA/MBA relevance
- source reliability
- source content quality
- non-expired deadline status
- application evidence when available

Missing salary, education, experience or image does not automatically reject a genuine vacancy.

## Application URL integrity

`source_url` and `apply_url` are always separate.

- `source_url`: original details page
- `apply_url`: actual submission/application destination
- `APPLY NOW`: only when a verified application destination exists
- `READ MORE`: fallback when only the source/details page is available
- direct application URL is never printed as raw text in the body
- the source page is never silently copied into `apply_url`

Application links are collected from HTML/Markdown and classified using deterministic signals plus bounded AI. The final button can only point to a URL already found on the source-backed page.

## Telegram post

`JOB TITLE → Company → JOB SNAPSHOT → hashtags → Official Source → one native inline keyboard button`

Gender, `Suitable For`, and `Key Highlights` are removed. Only source-backed, decision-useful fields are rendered.

## Cerebras efficiency

The bot avoids a one-request-per-candidate extraction design. It first retrieves and deterministically extracts pages, locally pre-ranks them, then sends only the highest-value ambiguous records to Cerebras in batches. Structured Outputs are used for predictable JSON.

## Persistent state

`news_state.json` stores queue records, content hashes, retrieval backends, structured job records, deadlines, verification state and publication state. Unchanged pages can reuse cached job records instead of invoking Cerebras again.

`posted_urls.txt` prevents URL-level republishing.

## GitHub Actions

The scheduled workflow runs every 30 minutes from 08:00 through 02:00 Bangladesh time, with a manual trigger available. GitHub scheduled runs can be delayed during high-load periods, especially around the start of an hour.

The workflow runs during Bangladesh daytime/evening hours and persists state back to the repository. The workflow performs:

1. dependency installation
2. source compilation
3. self-test
4. bot run
5. state commit

Live credentials are required for Exa, Cerebras and Telegram. The local self-test does not require them.
