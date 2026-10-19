---
description: Check upstream releases in Factory and propose compatible batched dependency bumps
on: { every: 1d }
actions:
  factory.request_change: { maxPerDay: 1, labels: [dependencies] }
---

Load the postgres-dependency-bump skill before doing any work.

Request one upstream discovery task in this repository. Discovery runs in the
Factory coding environment, never in a scheduled GitHub Actions job. The task
checks current main and upstream version metadata using the skill. It may finish
successfully without a PR when there are no applicable new versions. Do not
claim that a new release exists before the coding task discovers it.

Use request key `postgresbuild-upstream-scan:YYYY-MM-DD` for the current UTC day
and title `Check upstream releases for batched dependency bumps`. This identifies
the daily scan, not a proposed version or PR. Repeated dispatch on the same day
must reuse the same key. Honor deferred requests and the one-request-per-day
limit. Include this workflow's instructions and the skill in the problem.

Only publish a PR when upstream versions have changed. Batch compatible minor
or patch library updates and PostgreSQL point releases across supported majors
in one PR. Version-based candidate digests and open PR inspection deduplicate
the resulting changes across scans. There is no release-note classification or
preliminary GitHub build gate before proposing the update. Follow the skill
through PR publication and its normal CI; leave merging to elprans.
