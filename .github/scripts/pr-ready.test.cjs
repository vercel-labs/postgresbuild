'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { gate, identity } = require('./pr-ready.cjs');

const SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);

function fixture(eventName = 'workflow_run') {
  const state = {
    pr: {
      number: 18, state: 'open', draft: false,
      head: { sha: SHA, ref: 'update', repo: { id: 100 } },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      labels: [{ name: 'dependencies' }],
      html_url: 'https://github.com/vercel-labs/postgresbuild/pull/18',
    },
    checks: Object.fromEntries(['CI', 'All builds'].map((name, i) => [
      name, [{ id: i + 1, name, head_sha: SHA,
        app: { slug: 'github-actions' }, status: 'completed', conclusion: 'success',
        html_url: `https://github.com/vercel-labs/postgresbuild/actions/runs/${i + 1}` }],
    ])),
    run: { head_sha: SHA, status: 'completed', run_attempt: 1 },
    additions: [], removals: [], messages: [],
    reads: 0, onChecks: () => {}, error: null,
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
      actions: { getWorkflowRun: async args => {
        assert.equal(args.run_id, 100);
        return { data: structuredClone(state.run) };
      } },
      issues: {
        addLabels: async args => {
          state.additions.push(args);
          if (state.error) throw state.error;
          state.pr.labels.push(...args.labels.map(name => ({ name })));
        },
        removeLabel: async args => {
          state.removals.push(args);
          state.pr.labels = state.pr.labels.filter(label => label.name !== args.name);
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
      const checks = structuredClone(state.checks[args.check_name]);
      state.onChecks();
      return checks;
    }
    throw new Error('Unexpected read');
  };
  const context = {
    repo: { owner: 'vercel-labs', repo: 'postgresbuild' }, eventName,
    payload: {
      repository: { default_branch: 'main' },
      action: 'completed',
      workflow_run: { id: 100, event: 'pull_request', head_sha: SHA },
    },
  };
  const core = { info: value => state.messages.push(value) };
  return { state, github, context, core };
}

for (const [eventName, payload] of [
  ['workflow_run', { workflow_run: { id: 100, event: 'pull_request', head_sha: SHA } }],
  ['pull_request_target', { number: 18, pull_request: { head: { sha: SHA } } }],
  ['workflow_dispatch', { inputs: { pr_number: '18', head_sha: SHA } }],
]) {
  test(`${eventName} labels a ready PR`, async () => {
    const f = fixture(eventName);
    f.context.payload = { repository: f.context.payload.repository, ...payload };
    await gate(f);
    assert.equal(f.state.reads, 2);
    assert.deepEqual(f.state.additions, [{
      owner: 'vercel-labs', repo: 'postgresbuild', issue_number: 18,
      labels: ['ready-to-merge'],
    }]);
  });
}

test('push workflow completions do not notify', async () => {
  const f = fixture();
  f.context.payload.workflow_run.event = 'push';
  await gate(f);
  assert.equal(f.state.reads, 0);
  assert.equal(f.state.additions.length, 0);
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
    assert.equal(f.state.additions.length, 0);
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
      assert.equal(f.state.additions.length, 0);
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
      assert.equal(f.state.additions.length, 0);
    });
  }
}

test('an older passing check does not mask a newer failed run', async () => {
  const f = fixture();
  f.state.checks.CI.push({ ...f.state.checks.CI[0], id: 99, conclusion: 'failure' });
  await gate(f);
  assert.equal(f.state.additions.length, 0);
});

test('a successful rerun supersedes an older failed check', async () => {
  const f = fixture();
  f.state.checks.CI.push({ ...f.state.checks.CI[0], id: 0, conclusion: 'failure' });
  await gate(f);
  await gate(f);
  assert.equal(f.state.additions.length, 1);
});

test('a present readiness label suppresses repeated callbacks', async () => {
  const f = fixture();
  await gate(f);
  await gate(f);
  assert.equal(f.state.additions.length, 1);
  assert.deepEqual(f.state.pr.labels.map(label => label.name), ['dependencies', 'ready-to-merge']);
});

test('a recovered head can notify after its label was removed', async () => {
  const f = fixture();
  f.state.pr.labels.push({ name: 'ready-to-merge' });
  f.state.checks.CI[0].status = 'in_progress';
  await gate(f);
  assert.equal(f.state.removals.length, 1);
  f.state.checks.CI[0].status = 'completed';
  await gate(f);
  assert.equal(f.state.additions.length, 1);
});

for (const action of ['requested', 'in_progress']) {
  test(`${action} clears readiness even while old passing checks remain visible`, async () => {
    const f = fixture();
    await gate(f);
    f.context.payload.action = action;
    Object.assign(f.state.run, { status: action === 'requested' ? 'queued' : action, run_attempt: 2 });
    await gate(f);
    assert.equal(f.state.removals.length, 1);
    assert.deepEqual(f.state.pr.labels, [{ name: 'dependencies' }]);

    // The first notification may have skipped this rerun. Completing it must
    // add the label again, generating a fresh notification event.
    f.context.payload.action = 'completed';
    f.state.run.status = 'completed';
    await gate(f);
    await gate(f);
    assert.equal(f.state.additions.length, 2);
  });
}

test('a delayed start event does not clear a completed run\'s readiness', async () => {
  const f = fixture();
  await gate(f);
  f.context.payload.action = 'in_progress';
  await gate(f);
  assert.equal(f.state.removals.length, 0);
  assert.equal(f.state.additions.length, 1);
});

test('a delayed completion does not label a run that is rerunning', async () => {
  const f = fixture();
  f.state.run.status = 'in_progress';
  f.state.run.run_attempt = 2;
  await gate(f);
  assert.equal(f.state.additions.length, 0);
});

test('an old head\'s start event does not clear the current readiness label', async () => {
  const f = fixture();
  f.state.pr.labels.push({ name: 'ready-to-merge' });
  f.state.pr.head.sha = OTHER_SHA;
  f.context.payload.action = 'in_progress';
  f.state.run.status = 'in_progress';
  await gate(f);
  assert.equal(f.state.removals.length, 0);
  assert.equal(f.state.additions.length, 0);
});

test('a run read failure prevents label writes', async () => {
  const f = fixture();
  f.context.payload.action = 'in_progress';
  f.github.rest.actions.getWorkflowRun = async () => { throw new Error('rate limited'); };
  await assert.rejects(gate(f), /rate limited/);
  assert.equal(f.state.removals.length, 0);
  assert.equal(f.state.additions.length, 0);
});

test('mismatched run evidence prevents label writes', async () => {
  const f = fixture();
  f.context.payload.action = 'in_progress';
  f.state.run.head_sha = OTHER_SHA;
  await assert.rejects(gate(f), /does not match event commit/);
  assert.equal(f.state.removals.length, 0);
  assert.equal(f.state.additions.length, 0);
});

for (const change of [
  pr => { pr.state = 'closed'; },
  pr => { pr.draft = true; },
  pr => { pr.base.ref = 'other'; },
]) {
  test(`remove readiness when PR becomes ineligible: ${change}`, async () => {
    const f = fixture();
    f.state.pr.labels.push({ name: 'ready-to-merge' });
    change(f.state.pr);
    await gate(f);
    assert.deepEqual(f.state.removals, [{
      owner: 'vercel-labs', repo: 'postgresbuild', issue_number: 18, name: 'ready-to-merge',
    }]);
    assert.deepEqual(f.state.pr.labels, [{ name: 'dependencies' }]);
  });
}

test("a stale completion does not remove the newer head's readiness label", async () => {
  const f = fixture();
  f.state.pr.labels.push({ name: 'ready-to-merge' });
  f.state.pr.head.sha = OTHER_SHA;
  await gate(f);
  assert.equal(f.state.additions.length, 0);
  assert.equal(f.state.removals.length, 0);
});

test('a push during check reads prevents labelling', async () => {
  const f = fixture();
  f.state.onChecks = () => { f.state.pr.head.sha = OTHER_SHA; };
  await gate(f);
  assert.equal(f.state.additions.length, 0);
});

test('a draft conversion during check reads removes readiness', async () => {
  const f = fixture();
  f.state.pr.labels.push({ name: 'ready-to-merge' });
  f.state.onChecks = () => { f.state.pr.draft = true; };
  await gate(f);
  assert.equal(f.state.additions.length, 0);
  assert.equal(f.state.removals.length, 1);
});

test('read failures fail closed', async () => {
  const f = fixture();
  f.github.paginate = async () => { throw new Error('rate limited'); };
  await assert.rejects(gate(f), /rate limited/);
  assert.equal(f.state.additions.length, 0);
});

test('an uncertain label write is not retried blindly', async () => {
  const f = fixture();
  f.state.error = new Error('connection lost');
  await assert.rejects(gate(f), /connection lost/);
  assert.equal(f.state.additions.length, 1);
});

for (const [number, sha] of [[0, SHA], ['1; echo bad', SHA], [18, 'main'], [18, SHA.toUpperCase()]]) {
  test(`reject malformed notification identity: ${number}/${sha}`, () => {
    assert.throws(() => identity(number, sha), /positive PR number/);
  });
}
