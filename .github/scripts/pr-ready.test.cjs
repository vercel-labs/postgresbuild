'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { gate, identity } = require('./pr-ready.cjs');

const SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);
const PREFIX = 'refs/heads/automation/pr-ready/';
const SIGNAL = PREFIX + '18-100.1-200.1';

function fixture(eventName = 'workflow_run') {
  const state = {
    pr: {
      number: 18, state: 'open', draft: false,
      head: { sha: SHA, ref: 'update', repo: { id: 100 } },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      labels: [{ name: 'dependencies' }],
    },
    runs: Object.fromEntries(['ci.yml', 'postgresbuild.yml'].map((workflow, i) => [
      workflow, [{ id: (i + 1) * 100, head_sha: SHA, event: 'pull_request',
        status: 'completed', run_attempt: 1 }],
    ])),
    jobs: Object.fromEntries(['CI', 'All builds'].map((name, i) => [
      name, [{ id: i + 1, name, head_sha: SHA, run_id: (i + 1) * 100,
        run_attempt: 1, status: 'completed', conclusion: 'success' }],
    ])),
    refs: new Map(), refWrites: [], additions: [], removals: [], messages: [],
    reads: 0, runReads: 0, onJobs: () => {}, onRuns: () => {},
  };
  const github = {
    rest: {
      pulls: { get: async () => {
        state.reads++;
        return { data: structuredClone(state.pr) };
      } },
      repos: { listPullRequestsAssociatedWithCommit: () => {} },
      actions: { listWorkflowRuns: () => {}, listJobsForWorkflowRunAttempt: () => {} },
      git: {
        listMatchingRefs: () => {},
        createRef: async args => {
          assert.ok(args.ref.startsWith(PREFIX));
          assert.equal(state.refs.has(args.ref), false);
          state.refs.set(args.ref, args.sha);
          state.refWrites.push({ operation: 'create', ...args });
        },
        updateRef: async args => {
          assert.ok(args.ref.startsWith(PREFIX.slice(5)));
          assert.equal(args.force, true);
          state.refs.set('refs/' + args.ref, args.sha);
          state.refWrites.push({ operation: 'update', ...args });
        },
        deleteRef: async args => {
          assert.ok(args.ref.startsWith(PREFIX.slice(5)));
          assert.equal(state.refs.delete('refs/' + args.ref), true);
          state.refWrites.push({ operation: 'delete', ...args });
        },
      },
      issues: {
        addLabels: async args => {
          state.additions.push(args);
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
    if (method === github.rest.actions.listWorkflowRuns) {
      assert.equal(args.event, 'pull_request');
      assert.equal(args.head_sha, SHA);
      assert.equal(args.status, undefined);
      const runs = structuredClone(state.runs[args.workflow_id]);
      state.onRuns(args.workflow_id, ++state.runReads);
      return runs;
    }
    if (method === github.rest.actions.listJobsForWorkflowRunAttempt) {
      const run = Object.values(state.runs).flat().find(run => run.id === args.run_id);
      assert.equal(args.attempt_number, run.run_attempt);
      const name = args.run_id === 200 ? 'All builds' : 'CI';
      const jobs = structuredClone(state.jobs[name]);
      state.onJobs(name);
      return jobs;
    }
    if (method === github.rest.git.listMatchingRefs) {
      assert.equal(args.ref, PREFIX.slice(5) + '18');
      return [...state.refs].filter(([ref]) => ref.startsWith('refs/' + args.ref))
        .map(([ref, sha]) => ({ ref, object: { sha } }));
    }
    throw new Error('Unexpected read');
  };
  const context = {
    repo: { owner: 'vercel-labs', repo: 'postgresbuild' }, eventName,
    payload: {
      repository: { default_branch: 'main' }, action: 'completed',
      workflow_run: { id: 100, event: 'pull_request', head_sha: SHA },
      number: 18, pull_request: { head: { sha: SHA } },
      inputs: { pr_number: '18', head_sha: SHA },
    },
  };
  return { state, github, context, core: { info: value => state.messages.push(value) } };
}

for (const eventName of ['workflow_run', 'pull_request_target', 'workflow_dispatch']) {
  test(eventName + ' labels and signals a ready PR', async () => {
    const f = fixture(eventName);
    assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
    assert.equal(f.state.reads, 2);
    assert.deepEqual(f.state.additions, [{
      owner: 'vercel-labs', repo: 'postgresbuild', issue_number: 18,
      labels: ['ready-to-merge'],
    }]);
    assert.deepEqual([...f.state.refs], [[SIGNAL, SHA]]);
  });
}

test('a PR from a fork main branch receives completion updates', async () => {
  const f = fixture();
  f.state.pr.head = { sha: SHA, ref: 'main', repo: { id: 999 } };
  f.context.payload.workflow_run.head_branch = 'main';
  assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
});

for (const action of ['requested', 'in_progress']) {
  test(action + ' callbacks are ignored without clearing prior readiness', async () => {
    const f = fixture();
    await gate(f);
    f.context.payload.action = action;
    f.state.runs['ci.yml'][0].status = 'in_progress';
    assert.deepEqual(await gate(f), []);
    assert.equal(f.state.removals.length, 0);
    assert.deepEqual([...f.state.refs], [[SIGNAL, SHA]]);
  });
}

test('push workflow completions do not read PRs or notify', async () => {
  const f = fixture();
  f.context.payload.workflow_run.event = 'push';
  assert.deepEqual(await gate(f), []);
  assert.equal(f.state.reads, 0);
  assert.equal(f.state.refWrites.length, 0);
});

for (const change of [
  pr => { pr.state = 'closed'; },
  pr => { pr.draft = true; },
  pr => { pr.base.ref = 'other'; },
  pr => { pr.base.repo.full_name = 'other/repository'; },
]) {
  test('remove readiness when PR becomes ineligible: ' + change, async () => {
    const f = fixture();
    await gate(f);
    change(f.state.pr);
    await gate(f);
    assert.equal(f.state.removals.length, 1);
    assert.equal(f.state.refs.size, 0);
  });
}

for (const workflow of ['ci.yml', 'postgresbuild.yml']) {
  for (const status of ['queued', 'in_progress']) {
    test('reject old passing gates while ' + workflow + ' is ' + status, async () => {
      const f = fixture();
      f.state.runs[workflow][0].status = status;
      await gate(f);
      assert.equal(f.state.additions.length, 0);
      assert.equal(f.state.refWrites.length, 0);
    });
  }
  test('missing ' + workflow + ' does not establish readiness', async () => {
    const f = fixture();
    f.state.runs[workflow] = [];
    await gate(f);
    assert.equal(f.state.additions.length, 0);
  });
  test('a newer pending ' + workflow + ' supersedes an older success', async () => {
    const f = fixture();
    f.state.runs[workflow].push({ ...f.state.runs[workflow][0], id: 999, status: 'queued' });
    await gate(f);
    assert.equal(f.state.additions.length, 0);
  });
}

for (const name of ['CI', 'All builds']) {
  for (const [status, conclusion] of [
    ['queued', null], ['in_progress', null], ['completed', 'failure'],
    ['completed', 'cancelled'], ['completed', 'skipped'], ['completed', 'neutral'],
    ['completed', 'timed_out'], ['completed', 'action_required'],
  ]) {
    test('wait for ' + name + ': ' + status + '/' + conclusion, async () => {
      const f = fixture();
      Object.assign(f.state.jobs[name][0], { status, conclusion });
      await gate(f);
      assert.equal(f.state.additions.length, 0);
      assert.equal(f.state.refWrites.length, 0);
    });
  }
  for (const mismatch of ['missing', 'sha', 'name', 'run', 'attempt']) {
    test('reject ' + mismatch + ' ' + name + ' evidence', async () => {
      const f = fixture();
      const job = f.state.jobs[name][0];
      if (mismatch === 'missing') f.state.jobs[name] = [];
      if (mismatch === 'sha') job.head_sha = OTHER_SHA;
      if (mismatch === 'name') job.name = 'other';
      if (mismatch === 'run') job.run_id = 999;
      if (mismatch === 'attempt') job.run_attempt = 99;
      await gate(f);
      assert.equal(f.state.additions.length, 0);
    });
  }
}

test('an older passing gate does not mask a newer failure', async () => {
  const f = fixture();
  f.state.jobs.CI.push({ ...f.state.jobs.CI[0], id: 99, conclusion: 'failure' });
  await gate(f);
  assert.equal(f.state.additions.length, 0);
});

test('replays and the other workflow completion reuse the successful pair', async () => {
  const f = fixture();
  await gate(f);
  f.context.payload.workflow_run.id = 200;
  await gate(f);
  await gate(f);
  assert.equal(f.state.additions.length, 1);
  assert.equal(f.state.refWrites.length, 1);
});

for (const workflow of ['ci.yml', 'postgresbuild.yml']) {
  test('a successful ' + workflow + ' rerun signals without a start callback', async () => {
    const f = fixture();
    await gate(f);
    f.state.runs[workflow][0].run_attempt = 2;
    const name = workflow === 'ci.yml' ? 'CI' : 'All builds';
    f.state.jobs[name][0].run_attempt = 2;
    assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
    assert.deepEqual(await gate(f), []);
    assert.deepEqual([...f.state.refs.keys()], [
      PREFIX + (workflow === 'ci.yml' ? '18-100.2-200.1' : '18-100.1-200.2'),
    ]);
    assert.deepEqual(f.state.refWrites.map(write => write.operation), ['create', 'create', 'delete']);
    assert.equal(f.state.additions.length, 1);
  });
}

test('a failed rerun completion clears readiness and a later success recovers', async () => {
  const f = fixture();
  await gate(f);
  f.state.runs['postgresbuild.yml'][0].run_attempt = 2;
  Object.assign(f.state.jobs['All builds'][0], { run_attempt: 2, conclusion: 'failure' });
  await gate(f);
  assert.equal(f.state.removals.length, 1);
  assert.equal(f.state.refs.size, 0);
  f.state.runs['postgresbuild.yml'][0].run_attempt = 3;
  Object.assign(f.state.jobs['All builds'][0], { run_attempt: 3, conclusion: 'success' });
  assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
});

test('a new run of the same commit gets a fresh signal', async () => {
  const f = fixture();
  await gate(f);
  f.state.runs['ci.yml'].push({ ...f.state.runs['ci.yml'][0], id: 101 });
  f.state.jobs.CI[0].run_id = 101;
  assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
  assert.deepEqual([...f.state.refs], [[PREFIX + '18-101.1-200.1', SHA]]);
});

for (const change of [
  run => { run.run_attempt++; },
  run => { run.status = 'in_progress'; },
  run => { run.id++; },
]) {
  test('workflow changes during reads prevent readiness: ' + change, async () => {
    const f = fixture();
    f.state.onJobs = name => { if (name === 'All builds') change(f.state.runs['ci.yml'][0]); };
    await gate(f);
    assert.equal(f.state.additions.length, 0);
  });
}

for (const successful of [true, false]) {
  test('a push during ' + (successful ? 'successful' : 'failed') + ' reads preserves newer readiness', async () => {
    const f = fixture();
    f.state.pr.labels.push({ name: 'ready-to-merge' });
    f.state.refs.set(SIGNAL, OTHER_SHA);
    f.state.onJobs = () => { f.state.pr.head.sha = OTHER_SHA; };
    if (!successful) f.state.jobs.CI[0].conclusion = 'failure';
    await gate(f);
    assert.equal(f.state.removals.length, 0);
    assert.equal(f.state.refWrites.length, 0);
  });
}

test('a draft conversion during reads removes readiness', async () => {
  const f = fixture();
  await gate(f);
  f.state.onJobs = () => { f.state.pr.draft = true; };
  await gate(f);
  assert.equal(f.state.removals.length, 1);
  assert.equal(f.state.refs.size, 0);
});

test('migration and cleanup only touch this PR dedicated signal refs', async () => {
  const f = fixture();
  const unrelated = [PREFIX + '180-100.1-200.1', PREFIX + '18-other', 'refs/heads/main'];
  for (const ref of [PREFIX + '18', PREFIX + '18-99.1-200.1', ...unrelated]) {
    f.state.refs.set(ref, SHA);
  }
  await gate(f);
  assert.deepEqual([...f.state.refs.keys()].sort(), [...unrelated, SIGNAL].sort());
  f.state.pr.state = 'closed';
  await gate(f);
  assert.deepEqual([...f.state.refs.keys()], unrelated);
});

test('an already-labelled PR still gets its first signal', async () => {
  const f = fixture();
  f.state.pr.labels.push({ name: 'ready-to-merge' });
  assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
  assert.equal(f.state.additions.length, 0);
});

test('a mismatched dedicated signal target can be repaired', async () => {
  const f = fixture();
  f.state.refs.set(SIGNAL, OTHER_SHA);
  await gate(f);
  assert.equal(f.state.refs.get(SIGNAL), SHA);
  assert.equal(f.state.refWrites[0].operation, 'update');
});

for (const endpoint of ['listWorkflowRuns', 'listJobsForWorkflowRunAttempt', 'listMatchingRefs']) {
  test(endpoint + ' read failures do not cause blind signal writes', async () => {
    const f = fixture();
    const paginate = f.github.paginate;
    const method = f.github.rest.actions[endpoint] || f.github.rest.git[endpoint];
    f.github.paginate = async (target, args) => {
      if (target === method) throw new Error('rate limited');
      return paginate(target, args);
    };
    await assert.rejects(gate(f), /rate limited/);
    assert.equal(f.state.refWrites.length, 0);
  });
}

test('an uncertain label write does not publish a signal', async () => {
  const f = fixture();
  f.github.rest.issues.addLabels = async () => { throw new Error('connection lost'); };
  await assert.rejects(gate(f), /connection lost/);
  assert.equal(f.state.refWrites.length, 0);
});

test('a failed signal write recovers after the label was added', async () => {
  const f = fixture();
  const create = f.github.rest.git.createRef;
  f.github.rest.git.createRef = async () => { throw new Error('connection lost'); };
  await assert.rejects(gate(f), /connection lost/);
  f.github.rest.git.createRef = create;
  assert.deepEqual(await gate(f), [{ number: 18, sha: SHA }]);
  assert.equal(f.state.additions.length, 1);
});

test('an uncertain successful signal write is not duplicated on retry', async () => {
  const f = fixture();
  const create = f.github.rest.git.createRef;
  f.github.rest.git.createRef = async args => {
    await create(args);
    throw new Error('connection lost');
  };
  await assert.rejects(gate(f), /connection lost/);
  assert.deepEqual(await gate(f), []);
  assert.equal(f.state.refWrites.length, 1);
});

test('a cleanup failure after publishing does not duplicate the new signal', async () => {
  const f = fixture();
  f.state.refs.set(PREFIX + '18', SHA);
  const remove = f.github.rest.git.deleteRef;
  f.github.rest.git.deleteRef = async () => { throw new Error('connection lost'); };
  await assert.rejects(gate(f), /connection lost/);
  f.github.rest.git.deleteRef = remove;
  assert.deepEqual(await gate(f), []);
  assert.deepEqual([...f.state.refs], [[SIGNAL, SHA]]);
  assert.equal(f.state.refWrites.filter(write => write.operation === 'create').length, 1);
});

for (const [number, sha] of [[0, SHA], ['1; echo bad', SHA], [18, 'main'], [18, SHA.toUpperCase()]]) {
  test('reject malformed notification identity: ' + number + '/' + sha, () => {
    assert.throws(() => identity(number, sha), /positive PR number/);
  });
}
