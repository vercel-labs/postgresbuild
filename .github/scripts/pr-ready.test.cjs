'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { gate, identity, notice, notificationName } = require('./pr-ready.cjs');

const SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);

function fixture(eventName = 'workflow_run') {
  const state = {
    pr: {
      number: 18, state: 'open', draft: false,
      head: { sha: SHA, ref: 'update', repo: { id: 100 } },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      created_at: '2026-11-01T12:00:00Z',
      html_url: 'https://github.com/vercel-labs/postgresbuild/pull/18',
    },
    checks: Object.fromEntries(['CI', 'All builds'].map((name, i) => [
      name, [{ id: i + 1, name, head_sha: SHA,
        app: { slug: 'github-actions' }, status: 'completed', conclusion: 'success',
        html_url: `https://github.com/vercel-labs/postgresbuild/actions/runs/${i + 1}` }],
    ])),
    notices: [], dispatches: [], messages: [], summary: [],
    reads: 0, onHistory: () => {}, invisible: false, error: null,
  };
  const endpoint = () => {};
  const github = {
    rest: {
      pulls: { get: async () => {
        state.reads++;
        return { data: structuredClone(state.pr) };
      } },
      repos: {
        listPullRequestsAssociatedWithCommit: endpoint,
      },
      checks: { listForRef: () => {} },
      actions: {
        listWorkflowRuns: () => {},
        createWorkflowDispatch: async args => {
          state.dispatches.push(args);
          if (state.error) throw state.error;
          if (!state.invisible) state.notices.push({
            display_title: notificationName(Number(args.inputs.pr_number), args.inputs.head_sha),
            status: 'queued', html_url: 'https://github.com/vercel-labs/postgresbuild/actions/runs/100',
          });
        },
      },
    },
  };
  github.paginate = async (method, args) => {
    assert.equal(args.per_page, 100);
    if (method === github.rest.repos.listPullRequestsAssociatedWithCommit) {
      return [{ number: state.pr.number }];
    }
    if (method === github.rest.checks.listForRef) {
      assert.equal(args.filter, 'latest');
      assert.equal(args.ref, SHA);
      return state.checks[args.check_name];
    }
    throw new Error('Unexpected read');
  };
  github.paginate.iterator = async function* (method, args) {
    assert.equal(method, github.rest.actions.listWorkflowRuns);
    assert.equal(args.workflow_id, 'pr-ready.yml');
    assert.equal(args.event, 'workflow_dispatch');
    assert.equal(args.branch, 'main');
    assert.equal(args.created, `>=${state.pr.created_at}`);
    state.onHistory();
    // The matching notification can be beyond the first page.
    yield { data: [{ display_title: 'Unrelated PR' }] };
    yield { data: state.notices };
  };
  const summary = {
    addHeading: value => { state.summary.push(value); return summary; },
    addLink: (text, url) => { state.summary.push({ text, url }); return summary; },
    addRaw: value => { state.summary.push(value); return summary; },
    addTable: value => { state.summary.push(value); return summary; },
    write: async () => {},
  };
  const context = {
    repo: { owner: 'vercel-labs', repo: 'postgresbuild' }, eventName,
    payload: {
      repository: { default_branch: 'main' },
      workflow_run: { event: 'pull_request', head_sha: SHA },
    },
  };
  const core = { info: value => state.messages.push(value), summary };
  return { state, github, context, core, pause: async () => {} };
}

for (const [eventName, payload] of [
  ['workflow_run', { workflow_run: { event: 'pull_request', head_sha: SHA } }],
  ['pull_request_target', { number: 18, pull_request: { head: { sha: SHA } } }],
  ['workflow_dispatch', { inputs: { pr_number: '18', head_sha: SHA } }],
]) {
  test(`${eventName} dispatches only the default-branch notification`, async () => {
    const f = fixture(eventName);
    f.context.payload = { repository: f.context.payload.repository, ...payload };
    await gate(f);
    assert.equal(f.state.reads, 2);
    assert.deepEqual(f.state.dispatches, [{
      owner: 'vercel-labs', repo: 'postgresbuild', workflow_id: 'pr-ready.yml',
      ref: 'main', inputs: { pr_number: '18', head_sha: SHA },
    }]);
  });
}

test('push workflow completions do not notify', async () => {
  const f = fixture();
  f.context.payload.workflow_run.event = 'push';
  await gate(f);
  assert.equal(f.state.reads, 0);
  assert.equal(f.state.dispatches.length, 0);
});

for (const change of [
  pr => { pr.state = 'closed'; },
  pr => { pr.draft = true; },
  pr => { pr.head.sha = OTHER_SHA; },
  pr => { pr.base.ref = 'other'; },
  pr => { pr.base.repo.full_name = 'other/repository'; },
]) {
  test(`ignore ineligible PR: ${change}`, async () => {
    const f = fixture();
    change(f.state.pr);
    await gate(f);
    assert.equal(f.state.dispatches.length, 0);
  });
}

for (const check of ['CI', 'All builds']) {
  for (const [status, conclusion] of [
    ['queued', null], ['in_progress', null], ['completed', 'failure'],
    ['completed', 'cancelled'], ['completed', 'skipped'], ['completed', 'neutral'],
    ['completed', 'timed_out'], ['completed', 'action_required'],
  ]) {
    test(`wait for ${check}: ${status}/${conclusion}`, async () => {
      const f = fixture();
      Object.assign(f.state.checks[check][0], { status, conclusion });
      await gate(f);
      assert.equal(f.state.dispatches.length, 0);
    });
  }
  for (const mismatch of ['missing', 'sha', 'name', 'app']) {
    test(`reject ${mismatch} ${check} evidence`, async () => {
      const f = fixture();
      const run = f.state.checks[check][0];
      if (mismatch === 'missing') f.state.checks[check] = [];
      if (mismatch === 'sha') run.head_sha = OTHER_SHA;
      if (mismatch === 'name') run.name = 'other';
      if (mismatch === 'app') run.app = { slug: 'other' };
      await gate(f);
      assert.equal(f.state.dispatches.length, 0);
    });
  }
}

test('an older passing check does not mask a newer failed run', async () => {
  const f = fixture();
  f.state.checks.CI.push({ ...f.state.checks.CI[0], id: 99, conclusion: 'failure' });
  await gate(f);
  assert.equal(f.state.dispatches.length, 0);
});

test('a successful rerun supersedes an older failed check', async () => {
  const f = fixture();
  f.state.checks.CI.push({ ...f.state.checks.CI[0], id: 0, conclusion: 'failure' });
  await gate(f);
  await gate(f);
  assert.equal(f.state.dispatches.length, 1);
});

test('a previous head notification does not suppress the new head', async () => {
  const f = fixture();
  f.state.notices.push({ display_title: notificationName(18, OTHER_SHA) });
  await gate(f);
  assert.equal(f.state.dispatches.length, 1);
});

test('a push during the history read prevents dispatch', async () => {
  const f = fixture();
  f.state.onHistory = () => { f.state.pr.head.sha = OTHER_SHA; };
  await gate(f);
  assert.equal(f.state.dispatches.length, 0);
});

test('a rerun starting during the history read prevents dispatch', async () => {
  const f = fixture();
  f.state.onHistory = () => { f.state.checks.CI[0].status = 'in_progress'; };
  await gate(f);
  assert.equal(f.state.dispatches.length, 0);
});

test('read failures fail closed', async () => {
  const f = fixture();
  f.github.paginate = async () => { throw new Error('rate limited'); };
  await assert.rejects(gate(f), /rate limited/);
  assert.equal(f.state.dispatches.length, 0);
});

test('an uncertain dispatch failure is not retried blindly', async () => {
  const f = fixture();
  f.state.error = new Error('connection lost');
  await assert.rejects(gate(f), /connection lost/);
  assert.equal(f.state.dispatches.length, 1);
});

test('hold the gate until the accepted dispatch becomes visible', async () => {
  const f = fixture();
  f.state.invisible = true;
  let polls = 0;
  f.pause = async () => {
    if (++polls === 2) f.state.notices.push({ display_title: notificationName(18, SHA) });
  };
  await gate(f);
  assert.equal(polls, 2);
  assert.equal(f.state.dispatches.length, 1);
});

test('report an accepted but invisible dispatch without dispatching again', async () => {
  const f = fixture();
  f.state.invisible = true;
  await assert.rejects(gate(f), /Dispatch accepted but not visible/);
  assert.equal(f.state.dispatches.length, 1);
});

for (const [number, sha] of [[0, SHA], ['1; echo bad', SHA], [18, 'main'], [18, SHA.toUpperCase()]]) {
  test(`reject malformed notification identity: ${number}/${sha}`, () => {
    assert.throws(() => identity(number, sha), /positive PR number/);
  });
}

test('notification links the validated PR and checks for maintainers', async () => {
  const f = fixture('workflow_dispatch');
  f.context.payload.inputs = { pr_number: '18', head_sha: SHA };
  await notice(f);
  assert.ok(f.state.summary.some(item => item.url === f.state.pr.html_url));
  assert.ok(f.state.summary.some(item => typeof item === 'string' && item.includes('Maintainers')));
  assert.equal(f.state.dispatches.length, 0);
});

test('notification summary records readiness changes after dispatch', async () => {
  const f = fixture('workflow_dispatch');
  f.context.payload.inputs = { pr_number: '18', head_sha: SHA };
  f.state.pr.head.sha = OTHER_SHA;
  await notice(f);
  assert.match(f.state.summary[0], /readiness changed/);
  assert.equal(f.state.dispatches.length, 0);
});
