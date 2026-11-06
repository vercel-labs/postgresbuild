'use strict';

const INDEX_URL = 'https://postgresbuild.labs.vercel.dev/index.json';
const SHA = /^[0-9a-f]{40}$/;
const TAG = /^[0-9]{12}$/;

// The index only exposes snapshots after validation and successful ingestion.
// This helper also runs in the notification's trusted, pinned checkout.
async function indexedSnapshot(tag, sha, fetchIndex = fetch) {
  if (!TAG.test(tag) || (sha !== undefined && !SHA.test(sha))) {
    throw new Error('Expected a twelve-digit snapshot tag and full lowercase SHA');
  }
  // Give each immutable snapshot its own CDN cache key so a freshly ingested
  // release is not hidden by a cached response from an earlier publication.
  const response = await fetchIndex(`${INDEX_URL}?snapshot=${tag}`, {
    cache: 'no-store', signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Index read failed: HTTP ${response.status}`);
  const index = await response.json();
  if (index.format !== 'postgresbuild-index-v1' || !Array.isArray(index.snapshots)) {
    throw new Error('Published index has an unexpected format');
  }
  const matching = index.snapshots.filter(item => item.tag === tag);
  if (matching.length !== 1 || !SHA.test(matching[0].source_commit) ||
      (sha !== undefined && matching[0].source_commit !== sha)) {
    throw new Error('Snapshot is absent, ambiguous, or does not match the signal commit');
  }
  const snapshot = matching[0];
  const versions = [...new Set(snapshot.successful.map(({ coordinate }) => {
    if (coordinate.package !== 'postgresql' ||
        !/^[0-9]+(?:\.[0-9]+){0,2}(?:(?:alpha|beta|rc)[0-9]+)?$/.test(coordinate.source_version)) {
      throw new Error('Published snapshot has an invalid PostgreSQL coordinate');
    }
    return coordinate.source_version;
  }))].sort((a, b) => a.localeCompare(b, 'en', { numeric: true }));
  if (!versions.length) throw new Error('Published snapshot has no PostgreSQL versions');
  return { tag, sha: snapshot.source_commit, versions };
}

// Called directly by the publication job, after successful index ingestion.
async function signal({ github, context, core, tag, fetchIndex = fetch }) {
  const repository = `${context.repo.owner}/${context.repo.repo}`;
  if (repository !== 'vercel-labs/postgresbuild' || context.ref !== 'refs/heads/main' ||
      !['push', 'workflow_dispatch'].includes(context.eventName) || !SHA.test(context.sha)) {
    throw new Error('Publication signals require a trusted main-branch run');
  }
  const recovery = context.eventName === 'workflow_dispatch' &&
    context.payload.inputs?.operation === 'ingest-existing';
  const snapshot = await indexedSnapshot(tag, recovery ? undefined : context.sha, fetchIndex);
  const ref = `heads/automation/release-published/${snapshot.tag}`;
  let existing;
  try {
    ({ data: existing } = await github.rest.git.getRef({ ...context.repo, ref }));
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  if (existing) {
    if (existing.object.sha !== snapshot.sha) throw new Error('Snapshot signal ref conflicts with index');
    core.info(`Snapshot ${snapshot.tag} was already signalled`);
    return null;
  }
  await github.rest.git.createRef({ ...context.repo, ref: `refs/${ref}`, sha: snapshot.sha });
  core.info(`Signalled published snapshot ${snapshot.tag} (${snapshot.sha})`);
  return snapshot;
}

module.exports = { signal, indexedSnapshot };

if (require.main === module) {
  indexedSnapshot(...process.argv.slice(2)).then(snapshot => {
    process.stdout.write(`${JSON.stringify(snapshot)}\n`);
  }).catch(error => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
