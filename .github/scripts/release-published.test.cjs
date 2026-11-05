'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { signal, indexedSnapshot } = require('./release-published.cjs');

const SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);
const TAG = '202611051830';

function fixture() {
  const state = {
    index: { format: 'postgresbuild-index-v1', snapshots: [
      { tag: TAG, source_commit: SHA, successful: ['19rc1', '18.6', '18.6'].map(version => ({
        coordinate: { package: 'postgresql', source_version: version },
      })) },
    ] },
    ref: null, writes: [], indexReads: 0, refError: null,
  };
  const context = { repo: { owner: 'vercel-labs', repo: 'postgresbuild' },
    eventName: 'push', ref: 'refs/heads/main', sha: SHA, payload: {} };
  const github = { rest: { git: {
    getRef: async args => {
      assert.equal(args.ref, `heads/automation/release-published/${TAG}`);
      if (state.refError) throw state.refError;
      if (!state.ref) throw Object.assign(new Error('Not Found'), { status: 404 });
      return { data: { object: { sha: state.ref } } };
    },
    createRef: async args => {
      assert.equal(state.ref, null);
      state.writes.push(args);
      state.ref = args.sha;
    },
  } } };
  const fetchIndex = async (url, options) => {
    assert.equal(url, `https://postgresbuild.labs.vercel.dev/index.json?snapshot=${TAG}`);
    assert.equal(options.cache, 'no-store');
    assert.equal(options.headers, undefined);
    state.indexReads++;
    return new Response(JSON.stringify(state.index));
  };
  return { state, context, github, tag: TAG, fetchIndex, core: { info: () => {} } };
}

test('signal a published snapshot once per tag, without Actions API reads', async () => {
  const f = fixture();
  assert.deepEqual(await signal(f), { tag: TAG, sha: SHA, versions: ['18.6', '19rc1'] });
  assert.deepEqual(f.state.writes, [{ ...f.context.repo,
    ref: `refs/heads/automation/release-published/${TAG}`, sha: SHA }]);
  assert.equal(await signal(f), null);
  assert.equal(f.state.writes.length, 1);
});

test('a manually dispatched publication signals its own source commit', async () => {
  const f = fixture();
  f.context.eventName = 'workflow_dispatch';
  f.context.payload = { inputs: { operation: 'build-and-publish' } };
  await signal(f);
  assert.equal(f.state.ref, SHA);
});

for (const change of [
  context => { context.eventName = 'pull_request'; },
  context => { context.ref = 'refs/heads/other'; },
  context => { context.repo.owner = 'other'; },
  context => { context.sha = 'main'; },
]) {
  test(`reject an ineligible signal context: ${change}`, async () => {
    const f = fixture();
    change(f.context);
    await assert.rejects(signal(f), /trusted main/);
    assert.equal(f.state.indexReads, 0);
    assert.equal(f.state.writes.length, 0);
  });
}

test('ingest-existing signals the recovered snapshot at its original source commit', async () => {
  const f = fixture();
  f.context.eventName = 'workflow_dispatch';
  f.context.payload = { inputs: { operation: 'ingest-existing' } };
  f.context.sha = OTHER_SHA;
  await signal(f);
  assert.equal(f.state.ref, SHA);
});

for (const eventName of ['push', 'workflow_dispatch']) {
  test(`new publication must match its source commit (${eventName})`, async () => {
    const f = fixture();
    f.context.eventName = eventName;
    f.context.sha = OTHER_SHA;
    await assert.rejects(signal(f), /does not match/);
    assert.equal(f.state.writes.length, 0);
  });
}

for (const change of [
  index => { index.format = 'other'; },
  index => { index.snapshots = []; },
  index => { index.snapshots.push(index.snapshots[0]); },
  index => { index.snapshots[0].source_commit = OTHER_SHA; },
  index => { index.snapshots[0].successful = []; },
  index => { index.snapshots[0].successful[0].coordinate.package = 'other'; },
  index => { index.snapshots[0].successful[0].coordinate.source_version = '<@user>'; },
]) {
  test(`reject invalid published index evidence: ${change}`, async () => {
    const f = fixture();
    change(f.state.index);
    await assert.rejects(indexedSnapshot(TAG, SHA, f.fetchIndex));
    assert.equal(f.state.writes.length, 0);
  });
}

test('index read errors prevent signalling', async () => {
  const f = fixture();
  f.fetchIndex = async () => new Response('Unavailable', { status: 503 });
  await assert.rejects(signal(f), /HTTP 503/);
  assert.equal(f.state.writes.length, 0);
});

test('ref read errors do not cause blind signal creation', async () => {
  const f = fixture();
  f.state.refError = Object.assign(new Error('Forbidden'), { status: 403 });
  await assert.rejects(signal(f), /Forbidden/);
  assert.equal(f.state.writes.length, 0);
});

test('an existing signal is never rewritten to a different commit', async () => {
  const f = fixture();
  f.state.ref = OTHER_SHA;
  await assert.rejects(signal(f), /conflicts with index/);
  assert.equal(f.state.ref, OTHER_SHA);
  assert.equal(f.state.writes.length, 0);
});

test('retry after an uncertain successful ref write does not create another push', async () => {
  const f = fixture();
  const create = f.github.rest.git.createRef;
  f.github.rest.git.createRef = async args => {
    await create(args);
    throw new Error('connection lost');
  };
  await assert.rejects(signal(f), /connection lost/);
  assert.equal(await signal(f), null);
  assert.equal(f.state.writes.length, 1);
});

test('invalid signal identities fail before any index request', async () => {
  const f = fixture();
  for (const [tag, sha] of [['latest', SHA], [TAG, 'main'], [TAG, SHA.toUpperCase()]]) {
    await assert.rejects(indexedSnapshot(tag, sha, f.fetchIndex), /twelve-digit/);
  }
  assert.equal(f.state.indexReads, 0);
});
