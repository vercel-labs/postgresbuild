---
description: Check upstream releases and propose compatible batched dependency bumps
on: { schedule: { cron: "0 9 * * *" } }
permissions: { publish: true }
timeout_minutes: 240
actions:
  github.read_pull_request: true
  github.read_actions: { workflows: [ci.yml, postgresbuild.yml] }
  github.add_label: { labels: [dependencies] }
---

Load the postgres-dependency-bump skill before doing any work.

Perform upstream discovery and any resulting update directly in this run's
approved repository checkout. Do not request or delegate another coding task.
Discovery runs in the managed sandbox, never in a scheduled GitHub Actions job.
Check current main and upstream version metadata using the skill. Finish
successfully without a PR when there are no applicable new versions. Do not
claim that a new release exists before discovery succeeds.

Only publish a PR when upstream versions have changed. Batch compatible minor
or patch library updates and PostgreSQL point releases across supported majors
in one PR. Version-based candidate digests and open PR inspection deduplicate
the resulting changes across scans. There is no release-note classification or
preliminary GitHub build gate before proposing the update. Follow the skill
through managed PR publication and its normal CI; leave merging to maintainers.
Create at most one PR per run and reuse its run-owned branch for fixes. Preserve
candidate markers and inspect open PRs to suppress duplicates across runs.
Report the checked versions, blocked updates, commands, CI state, and PR URL in
the run's final result through `finish_run`. If checks outlast the run, report
them as pending. Use `report_blocked` when discovery, required local validation,
or publication cannot complete.
