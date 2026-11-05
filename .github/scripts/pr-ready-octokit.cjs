'use strict';

// Exercise the production gate through the action's real REST and pagination
// client, intercepting only HTTP. This catches SDK contracts hidden by mocks.
const assert = require('node:assert/strict');
const { gate } = require('./pr-ready.cjs');

module.exports = async function testOctokit(Octokit) {
  for (const eventName of ['workflow_run', 'workflow_dispatch']) {
    const sha = 'a'.repeat(40);
    const root = '/repos/vercel-labs/postgresbuild';
    const pr = { number: 18, state: 'open', draft: false,
      head: { sha },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      labels: [{ name: 'dependencies' }] };
    let additions = 0;
    let signalSha = null;
    const refWrites = [];
    const run = { head_sha: sha, status: 'completed' };
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
        if (url.pathname === `${root}/pulls/18`) return json(pr);
        if (url.pathname === `${root}/commits/${sha}/pulls`) return json([pr]);
        if (url.pathname === `${root}/actions/runs/100`) return json(run);
        if (path === `${root}/git/ref/heads/automation/pr-ready/18`) {
          assert.equal(options.method, 'GET');
          return signalSha ? json({ object: { sha: signalSha } })
            : json({ message: 'Not Found' }, {}, 404);
        }
        if (url.pathname === `${root}/git/refs`) {
          assert.equal(options.method, 'POST');
          assert.equal(signalSha, null);
          assert.deepEqual(JSON.parse(options.body), {
            ref: 'refs/heads/automation/pr-ready/18', sha,
          });
          signalSha = sha;
          refWrites.push('create');
          return json({ object: { sha } });
        }
        if (path === `${root}/git/refs/heads/automation/pr-ready/18`) {
          assert.ok(signalSha);
          if (options.method === 'DELETE') {
            signalSha = null;
            refWrites.push('delete');
            return new Response(null, { status: 204 });
          }
          assert.equal(options.method, 'PATCH');
          assert.deepEqual(JSON.parse(options.body), { sha, force: true });
          signalSha = sha;
          refWrites.push('update');
          return json({ object: { sha } });
        }
        if (url.pathname === `${root}/commits/${sha}/check-runs`) {
          assert.equal(url.searchParams.get('filter'), 'latest');
          const name = url.searchParams.get('check_name');
          assert.ok(['CI', 'All builds'].includes(name));
          return json({ total_count: 1, check_runs: [
            { id: 1, name, head_sha: sha, app: { slug: 'github-actions' },
              status: 'completed', conclusion: 'success' },
          ] });
        }
        if (url.pathname === `${root}/issues/18/labels`) {
          assert.equal(options.method, 'POST');
          const body = JSON.parse(options.body);
          assert.deepEqual(body, { labels: ['ready-to-merge'] });
          additions++;
          pr.labels.push({ name: 'ready-to-merge' });
          return json(pr.labels);
        }
        if (url.pathname === `${root}/issues/18/labels/ready-to-merge`) {
          assert.equal(options.method, 'DELETE');
          pr.labels = pr.labels.filter(label => label.name !== 'ready-to-merge');
          return json(pr.labels);
        }
        assert.fail(`Unexpected SDK request: ${options.method} ${url}`);
      },
    } });
    const context = {
      repo: { owner: 'vercel-labs', repo: 'postgresbuild' },
      eventName, payload: {
        action: 'completed',
        repository: { default_branch: 'main' },
        workflow_run: { id: 100, event: 'pull_request', head_sha: sha },
        inputs: { pr_number: '18', head_sha: sha },
      },
    };
    const args = { github, context, core: { info: () => {} } };
    assert.equal((await gate(args)).length, 1);
    assert.equal((await gate(args)).length, 0);
    assert.equal(additions, 1);
    assert.deepEqual(refWrites, ['create']);
    if (eventName === 'workflow_run') {
      context.payload.action = 'in_progress';
      run.status = 'in_progress';
      assert.equal((await gate(args)).length, 0);
      assert.deepEqual(pr.labels, [{ name: 'dependencies' }]);
      assert.equal(signalSha, null);
      context.payload.action = 'completed';
      run.status = 'completed';
      assert.equal((await gate(args)).length, 1);
      assert.equal(additions, 2);
      assert.deepEqual(refWrites, ['create', 'delete', 'create']);
    }
    signalSha = 'b'.repeat(40);
    assert.equal((await gate(args)).length, 1);
    assert.equal(refWrites.at(-1), 'update');
    pr.draft = true;
    assert.equal((await gate(args)).length, 0);
    assert.equal(refWrites.at(-1), 'delete');
    assert.equal(signalSha, null);
  }
};
