---
name: postgres-dependency-bump
description: Discover upstream releases, batch compatible PostgreSQL and bundled-library updates, and follow the proposed PR through CI.
---

The automation performs daily discovery directly in its managed sandbox. Use
this procedure in postgresbuild; do not modify Factory or ggbuild. GitHub Actions
has no periodic discovery or build jobs. Use managed host tools for publication
and `automation_action` for declared GitHub operations; do not obtain credentials
or configure a GitHub App or Slack bot in the sandbox.

1. Start from current `main`. Run `uv sync --frozen`, then
   `uv run python -m postgresbuild.maintenance discover`. This reads upstream
   version metadata and open PR markers directly; no GitHub Actions report,
   build, or release-note classification is needed. Public repository reads
   work without a GitHub token; do not provision credentials. Failed discovery
   is a failure, never evidence that dependencies are current. If there are
   no candidates, finish without edits, commits, a PR, or build/test work.
   Report blocked entries separately. Never create empty speculative PRs.
2. Review `candidates.json`. Exclude blocked entries and versions already in
   open PRs, including `<!-- postgresbuild-candidate:<digest> -->` markers.
   Prefer one batch of compatible library minor/patch updates and PostgreSQL
   point releases across supported majors. Keep unrelated compatibility work
   separate; defer a package already covered by an open maintenance PR.
   Apply the batch with
   `uv run python -m postgresbuild.maintenance apply candidates.json`
   or select a subset by appending its space-separated digests. The helper
   revalidates every candidate before staging any update, checks declarations and
   compatibility bounds, downloads/hashes archives with the pinned ggbuild,
   rerolls patches before writing, synchronizes exact runtime/build pins, and
   regenerates ggbuild's workflow once for the whole batch. A stale candidate
   requires rediscovery. Stop and report unresolved patch conflicts, new majors
   without a reviewed baseline, or incompatible requirements. A failed batch
   must leave files unchanged; a smaller valid batch may be proposed separately.
3. Review the complete diff. Include declarations, archive hashes, corresponding
   exact pins, necessary version fixtures, and generated workflow output in the
   same PR. Preserve range constraints and PostgreSQL's supported stable and
   upcoming alpha/beta/RC tracks. Do not change Python/tool/Actions dependencies,
   ggbuild's pin, build logic, automation policy, or publication/index APIs.
   Do not edit generated YAML by hand. Use `uv run ggbuild ci render-workflow`.
4. Run `uv run poe qa`, `uv run zizmor --offline .github/workflows`, and
   `uv run ggbuild ci check-workflow`. Explain any blocked validation.
5. Commit the verified changes with messages describing the package/version
   changes, then publish one speculative PR through `open_pull_request`, with
   a concise title describing the batch, a table of each package's old/new
   versions, upstream release links, and one exact marker
   `<!-- postgresbuild-candidate:<digest> -->` per candidate in its body. Use
   managed GitHub action `github.add_label` to add `dependencies`. Publication
   uses the service's configured identity and signed Git transport. Commit
   scoped fixes and use `push_checkpoint` on the same run-owned PR; mark it
   ready after verification. Honor host publication limits; report a rejected
   publication as blocked rather than omitting required generated files.
6. Follow CI and the entire PostgreSQL build/test workflow for the current head.
   Read current PR state with `github.read_pull_request` and CI runs, jobs, and
   logs with the declared `github.read_actions` operations. Fix scoped failures
   and rerun the relevant checks. Refresh against main if another PR merged
   first, revalidate, regenerate, and wait for fresh checks.
   Never bypass checks or signed commits. Report an update that cannot pass as
   blocked. Leave the passing PR open for elprans to review and merge. Never
   merge it or enable auto-merge. Review relies on GitHub's existing PR and
   check notifications. A human merge starts the normal main build, immutable
   publication, and index ingestion with no separate deployment approval.
