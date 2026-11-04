'use strict';

// Exercise the production gate through the action's real REST and pagination
// client, intercepting only HTTP. This catches SDK contracts hidden by mocks.
const assert = require('node:assert/strict');
const { gate, notificationName } = require('./pr-ready.cjs');

module.exports = async function testOctokit(Octokit) {
  for (const eventName of ['workflow_run', 'workflow_dispatch']) {
    const sha = 'a'.repeat(40);
    const root = '/repos/vercel-labs/postgresbuild';
    const pr = { number: 18, state: 'open', draft: false,
      head: { sha },
      base: { ref: 'main', repo: { full_name: 'vercel-labs/postgresbuild' } },
      created_at: '2026-11-01T12:00:00Z' };
    let dispatches = 0;
    const notices = [];
    const github = new Octokit({ auth: 'test-token', request: {
      fetch: async (input, options) => {
        const url = new URL(input);
        assert.equal(url.origin, 'https://api.github.com');
        const json = (data, headers = {}) => {
          const response = new Response(JSON.stringify(data), {
            status: 200, headers: { 'content-type': 'application/json', ...headers },
          });
          Object.defineProperty(response, 'url', { value: String(input) });
          return response;
        };
        if (url.pathname === `${root}/pulls/18`) return json(pr);
        if (url.pathname === `${root}/commits/${sha}/pulls`) return json([pr]);
        if (url.pathname === `${root}/commits/${sha}/check-runs`) {
          assert.equal(url.searchParams.get('filter'), 'latest');
          const name = url.searchParams.get('check_name');
          assert.ok(['CI', 'All builds'].includes(name));
          return json({ total_count: 1, check_runs: [
            { id: 1, name, head_sha: sha, app: { slug: 'github-actions' },
              status: 'completed', conclusion: 'success' },
          ] });
        }
        if (url.pathname === `${root}/actions/workflows/pr-ready.yml/dispatches`) {
          assert.equal(options.method, 'POST');
          const body = JSON.parse(options.body);
          assert.equal(body.ref, 'main');
          assert.deepEqual(body.inputs, { pr_number: '18', head_sha: sha });
          dispatches++;
          notices.push({ display_title: notificationName(18, sha), html_url: 'https://github.com/run/100' });
          return new Response(null, { status: 204 });
        }
        if (url.pathname === `${root}/actions/workflows/pr-ready.yml/runs`) {
          if (url.searchParams.get('page') === '2') {
            return json({ total_count: notices.length + 1, workflow_runs: notices });
          }
          url.searchParams.set('page', '2');
          return json({ total_count: notices.length + 1,
            workflow_runs: [{ display_title: 'Unrelated PR' }] },
          { link: `<${url}>; rel="next"` });
        }
        assert.fail(`Unexpected SDK request: ${options.method} ${url}`);
      },
    } });
    const context = {
      repo: { owner: 'vercel-labs', repo: 'postgresbuild' },
      eventName, payload: {
        repository: { default_branch: 'main' },
        workflow_run: { event: 'pull_request', head_sha: sha },
        inputs: { pr_number: '18', head_sha: sha },
      },
    };
    const args = { github, context, core: { info: () => {} },
      pause: async () => { assert.fail('Dispatch should already be visible'); } };
    assert.equal((await gate(args)).length, 1);
    assert.equal((await gate(args)).length, 0);
    assert.equal(dispatches, 1);
  }
};
