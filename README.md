# PostgreSQL Build Standalone

This repository contains the PostgreSQL and dependency recipes used to build
relocatable, symbol-enabled PostgreSQL distributions for every point release
of each upstream-supported major on six native targets.

Reusable orchestration lives in `ggbuild`. This project contains only recipes,
patches and install lists, a target list and workflow policy in
`pyproject.toml`, the CI setup action, and PostgreSQL's specialized upstream
release discovery script. ggbuild derives execution environments and runners
from target triples, and owns the Linux image pins and Dockerfile templates.

## Deterministic builds

Create a canonical v3 plan at runtime:

```console
uv run ggbuild ci plan
```

Run one configured target locally (the source reference is optional; omitting
it selects the latest registered release of every supported major):

```console
uv run ggbuild build --target aarch64-unknown-linux-gnu --source-ref 17.10
```

Or inspect the complete local schedule without executing it:

```console
uv run ggbuild ci run --target aarch64-unknown-linux-gnu --dry-run
```

The static Actions scaffold plans at runtime and runs one matrix job per
target/root release. Linux closures run directly in content-addressed,
multiarch ggbuild Actions from `ghcr.io/vercel-labs/ggbuild`; macOS closures
use the project setup action. Regenerate or validate the scaffold with:

```console
uv run ggbuild ci render-workflow
uv run ggbuild ci check-workflow
```

Bundles under `.cache/bundles` are content addressed. Every restored filename,
manifest, and payload is checked against the exact v3 node key before use.

Every PostgreSQL artifact is followed by a recipe-driven test node. The node
extracts the shipped payload and its build-time test-data side tarball, then
runs standalone prebuilt regression, isolation, and ECPG drivers against the
shipped `bin`, `lib`, `share`, and `pg_config`. The sidecar contains only the
enabled suites' inputs, expected output, schedules, executables, and runtime
manifest; it does not require Make, compilers, or PostgreSQL source/build
trees. CI uploads the artifact only after this test succeeds. Direct native
testing uses the same hook:

```console
uv run ggbuild test dist/<target>/<version>/postgresql__*.tar.zst
```

Artifact tests require an exact architecture, OS, and libc host match. Docker
nodes use the invoking UID/GID with a rootful daemon; rootless Docker keeps its
mapped identity. Registry-backed CI containers also drop root before building
or testing, and PostgreSQL keeps clusters, sockets, logs, sources, and temporary
state inside the node work directory. Builds with `build-dbgsym = true` also
publish a separate `dbgsym` tarball while keeping the primary payload
stripped.

Snapshot publication uses a protected GitHub `release` environment. Configure
that environment with required reviewers, and configure the deployed index
service with `PUBLICATION_REPOSITORY=owner/repository` and `BLOB_STORE_ID`.
`PUBLICATION_REF`, `PUBLICATION_WORKFLOW_PATH`, `PUBLICATION_ENVIRONMENT`, and
`PUBLICATION_AUDIENCE` may override their canonical-main defaults.
The service accepts both GitHub's name-based and immutable-ID OIDC subjects,
while requiring signed repository, owner, ref, workflow, and environment
identity to match the configured publication policy.
After authentication, the workflow's ephemeral, read-scoped GitHub token is
used to verify release assets when the repository is private; it is not stored.
Verified primary archives are copied to immutable public Vercel Blob paths, and
`/versions.ndjson` advertises those consumer URLs.
Successful runs publish twelve-digit UTC release tags, canonical distribution
archives, `SHA256SUMS`, and `ggbuild-snapshot-v1.json`. The validated public
inventory is available at `/index.json` and `/versions.ndjson`. If release
publication succeeds but index ingestion fails, dispatch `ingest-existing`
with the immutable release tag; that operation skips planning and all builds.

## Updating releases

Dependency recipes declare ggbuild update policies. Update them with:

```console
uv run ggbuild update
```

PostgreSQL overrides release discovery to track the latest point release of
every supported major. The same update rerolls patches when a release changes.

## Development

```console
uv sync
uv run poe setup
uv run poe qa
```

`ggbuild==0.1.0` is pinned so plans use the same protocol and cache identity.
Until that release is published, uv resolves it from the immutable Git commit
recorded in `pyproject.toml` and `uv.lock`.
