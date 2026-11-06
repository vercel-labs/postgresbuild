'use strict';

// Verify signal ref reads and writes through the action's actual SDK.
const assert = require('node:assert/strict');
const { signal } = require('./release-published.cjs');

module.exports = async function testOctokit(Octokit) {
  const root = '/repos/vercel-labs/postgresbuild';
  const tag = '202611051830';
  const sha = 'a'.repeat(40);
  let ref = null;
  let creations = 0;
  const github = new Octokit({ auth: 'test-token', request: {
    fetch: async (input, options) => {
      const url = new URL(input);
      assert.equal(url.origin, 'https://api.github.com');
      const path = decodeURIComponent(url.pathname);
      const json = (data, status = 200) => new Response(JSON.stringify(data), {
        status, headers: { 'content-type': 'application/json' },
      });
      if (path === `${root}/git/ref/heads/automation/release-published/${tag}`) {
        assert.equal(options.method, 'GET');
        return ref ? json({ object: { sha: ref } }) : json({ message: 'Not Found' }, 404);
      }
      if (path === `${root}/git/refs`) {
        assert.equal(options.method, 'POST');
        assert.deepEqual(JSON.parse(options.body), {
          ref: `refs/heads/automation/release-published/${tag}`, sha,
        });
        ref = sha;
        creations++;
        return json({ object: { sha } }, 201);
      }
      assert.fail(`Unexpected SDK request: ${options.method} ${url}`);
    },
  } });
  const args = {
    github, tag, core: { info: () => {} },
    context: { repo: { owner: 'vercel-labs', repo: 'postgresbuild' },
      eventName: 'push', ref: 'refs/heads/main', sha, payload: {} },
    fetchIndex: async () => new Response(JSON.stringify({
      format: 'postgresbuild-index-v1', snapshots: [{ tag, source_commit: sha,
        successful: [{ coordinate: { package: 'postgresql', source_version: '18.6' } }] }],
    })),
  };
  assert.deepEqual(await signal(args), { tag, sha, versions: ['18.6'] });
  assert.equal(await signal(args), null);
  assert.equal(creations, 1);
};
