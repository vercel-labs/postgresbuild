'use strict';

// Exercise the production gate through the action's real REST and pagination
// client, intercepting only HTTP. This catches SDK contracts hidden by mocks.
const assert = require('node:assert/strict');
const { gate } = require('./pr-ready.cjs');

module.exports = async function testOctokit(Octokit) {
  for (const eventName of ['workflow_run', 'workflow_dispatch']) {
    const sha = 'a'.repeat(40);
    const root = '/repos/vercel-labs/postgresbuild';
    const prefix = 'refs/heads/automation/pr-ready/';
    const pr = { number: 18, state: 'open', draft: false,
      head: { sha, ref: 'main', repo: { id: 999 } },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      labels: [{ name: 'dependencies' }] };
    const runs = {
      'ci.yml': { id: 100, run_attempt: 1, head_sha: sha, event: 'pull_request', status: 'completed' },
      'postgresbuild.yml': { id: 200, run_attempt: 1, head_sha: sha, event: 'pull_request', status: 'completed' },
    };
    let additions = 0;
    const refs = new Map([[prefix + '180-100.1-200.1', sha]]);
    const refWrites = [];
    const github = new Octokit({ auth: 'test-token', request: {
      fetch: async (input, options) => {
        const url = new URL(input);
        assert.equal(url.origin, 'https://api.github.com');
        const path = decodeURIComponent(url.pathname);
        const json = (data, headers = {}, status = 200) => {
          const response = new Response(JSON.stringify(data), {
            status, headers: { 'content-type': 'application/json', ...headers },
          });
          Object.defineProperty(response, 'url', { value: String(input) });
          return response;
        };
        const nextPage = () => {
          const next = new URL(input);
          next.searchParams.set('page', '2');
          return { link: '<' + next + '>; rel="next"' };
        };
        if (path === root + '/pulls/18') return json(pr);
        if (path === root + '/commits/' + sha + '/pulls') return json([pr]);
        const workflows = path.match(/\/actions\/workflows\/(ci\.yml|postgresbuild\.yml)\/runs$/);
        if (workflows) {
          assert.equal(url.searchParams.get('head_sha'), sha);
          assert.equal(url.searchParams.get('event'), 'pull_request');
          assert.equal(url.searchParams.has('status'), false);
          // Real pagination must unwrap workflow_runs and follow the link.
          return url.searchParams.get('page') === '2'
            ? json({ total_count: 2, workflow_runs: [runs[workflows[1]]] })
            : json({ total_count: 2, workflow_runs: [
              { ...runs[workflows[1]], head_sha: 'b'.repeat(40), id: 999 },
            ] }, nextPage());
        }
        const jobs = path.match(/\/actions\/runs\/(100|200)\/attempts\/([1-9][0-9]*)\/jobs$/);
        if (jobs) {
          const runId = Number(jobs[1]);
          const run = runs[runId === 100 ? 'ci.yml' : 'postgresbuild.yml'];
          assert.equal(Number(jobs[2]), run.run_attempt);
          return url.searchParams.get('page') === '2'
            ? json({ total_count: 2, jobs: [
              { id: runId + 1, run_id: runId, run_attempt: run.run_attempt,
                head_sha: sha, name: runId === 100 ? 'CI' : 'All builds',
                status: 'completed', conclusion: 'success' },
            ] })
            : json({ total_count: 2, jobs: [{ id: 1, name: 'unrelated' }] }, nextPage());
        }
        if (path === root + '/git/matching-refs/heads/automation/pr-ready/18') {
          assert.equal(options.method, 'GET');
          return url.searchParams.get('page') === '2'
            ? json([...refs].filter(([ref]) => !ref.includes('/180-'))
              .map(([ref, sha]) => ({ ref, object: { sha } })))
            : json([{ ref: prefix + '180-100.1-200.1', object: { sha } }], nextPage());
        }
        if (path === root + '/git/refs') {
          assert.equal(options.method, 'POST');
          const body = JSON.parse(options.body);
          assert.ok(body.ref.startsWith(prefix + '18-'));
          assert.equal(body.sha, sha);
          assert.equal(refs.has(body.ref), false);
          refs.set(body.ref, body.sha);
          refWrites.push('create');
          return json({ ref: body.ref, object: { sha } });
        }
        if (path.startsWith(root + '/git/refs/heads/automation/pr-ready/18-')) {
          const ref = 'refs/' + path.slice((root + '/git/refs/').length);
          assert.ok(refs.has(ref));
          if (options.method === 'DELETE') {
            refs.delete(ref);
            refWrites.push('delete');
            return new Response(null, { status: 204 });
          }
          assert.equal(options.method, 'PATCH');
          assert.deepEqual(JSON.parse(options.body), { sha, force: true });
          refs.set(ref, sha);
          refWrites.push('update');
          return json({ ref, object: { sha } });
        }
        if (path === root + '/issues/18/labels') {
          assert.equal(options.method, 'POST');
          assert.deepEqual(JSON.parse(options.body), { labels: ['ready-to-merge'] });
          additions++;
          pr.labels.push({ name: 'ready-to-merge' });
          return json(pr.labels);
        }
        if (path === root + '/issues/18/labels/ready-to-merge') {
          assert.equal(options.method, 'DELETE');
          pr.labels = pr.labels.filter(label => label.name !== 'ready-to-merge');
          return json(pr.labels);
        }
        assert.fail('Unexpected SDK request: ' + options.method + ' ' + url);
      },
    } });
    const context = {
      repo: { owner: 'vercel-labs', repo: 'postgresbuild' },
      eventName, payload: {
        action: 'completed',
        repository: { default_branch: 'main' },
        workflow_run: { id: 100, event: 'pull_request', head_sha: sha, head_branch: 'main' },
        inputs: { pr_number: '18', head_sha: sha },
      },
    };
    const args = { github, context, core: { info: () => {} } };
    assert.equal((await gate(args)).length, 1);
    assert.equal((await gate(args)).length, 0);
    assert.equal(additions, 1);
    assert.deepEqual(refWrites, ['create']);
    // No start callback: a new completed attempt must still wake the notifier.
    runs['postgresbuild.yml'].run_attempt = 2;
    assert.equal((await gate(args)).length, 1);
    assert.equal((await gate(args)).length, 0);
    assert.deepEqual(refWrites, ['create', 'create', 'delete']);
    assert.equal(additions, 1);
    // Old successful jobs cannot establish readiness while the OTHER run is active.
    runs['postgresbuild.yml'].status = 'in_progress';
    assert.equal((await gate(args)).length, 0);
    assert.deepEqual(pr.labels, [{ name: 'dependencies' }]);
    assert.deepEqual([...refs.keys()], [prefix + '180-100.1-200.1']);
    runs['postgresbuild.yml'].status = 'completed';
    assert.equal((await gate(args)).length, 1);
    pr.draft = true;
    assert.equal((await gate(args)).length, 0);
    assert.deepEqual([...refs.keys()], [prefix + '180-100.1-200.1']);
  }
};
