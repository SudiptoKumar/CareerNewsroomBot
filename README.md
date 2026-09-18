# CareerNewsroomBot V0.5

Bdjobs-only job intelligence bot for @CareerNewsroom.

## Pipeline

Bdjobs discovery -> Exa web research -> deterministic job extraction -> BBA/MBA audience pre-score -> Cerebras editorial judging -> final selection -> Telegram.

## Source policy

The only source domain is Bdjobs (`bdjobs.com` and its subdomains). External URLs are allowed only when they are extracted as the application's destination from a Bdjobs vacancy page.

## Audience

Prioritizes BBA/MBA students, fresh graduates, internships, graduate/management trainee roles, finance, accounting, banking, marketing, HR, business, commercial, operations, supply chain and related early-career roles.

## Freshness

The last 7 days are the primary Exa discovery window. A 72-hour boundary is not a publication gate. An active, verified vacancy can remain in the persistent inventory after 72 hours and can still be published while its application deadline remains active.

## Telegram

The bot uses one native inline keyboard button. A verified application destination uses `APPLY NOW`; otherwise the source/details page uses `READ MORE`.
