'use strict';

// Octokit is supplied by actions/github-script; no credentials or dependencies
// are installed from the PR. Only metadata is read, and no PR code is executed.
const REQUIRED_CHECKS = ['CI', 'All builds'];
const NOTICE_WORKFLOW = 'pr-ready.yml';

function identity(number, sha) {
  if (!/^[1-9][0-9]*$/.test(String(number)) ||
      !Number.isSafeInteger(Number(number)) || !/^[0-9a-f]{40}$/.test(sha)) {
    throw new Error('Expected a positive PR number and full lowercase commit SHA');
  }
  return { number: Number(number), sha };
}

function notificationName(number, sha) {
  return `PR #${number} checks passed (${sha})`;
}

async function candidates(github, context) {
  const event = context.payload;
  if (context.eventName === 'workflow_dispatch') {
    return [identity(event.inputs.pr_number, event.inputs.head_sha)];
  }
  if (context.eventName === 'pull_request_target') {
    return [identity(event.number, event.pull_request.head.sha)];
  }
  if (event.workflow_run && event.workflow_run.event !== 'pull_request') {
    return [];
  }
  const sha = event.workflow_run?.head_sha;
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    throw new Error('Event does not identify a commit');
  }
  const pulls = await github.paginate(
    github.rest.repos.listPullRequestsAssociatedWithCommit,
    { ...context.repo, commit_sha: sha, per_page: 100 },
  );
  return pulls.map(pr => identity(pr.number, sha));
}

function current(pr, context, sha) {
  return pr.state === 'open' && !pr.draft && pr.head.sha === sha &&
    pr.base.ref === context.payload.repository.default_branch &&
    pr.base.repo.full_name === `${context.repo.owner}/${context.repo.repo}`;
}

async function readiness(github, context, number, sha) {
  const { data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: number,
  });
  if (!current(pr, context, sha)) {
    return { ready: false, reason: 'PR is closed, draft, retargeted, or stale' };
  }
  const checks = [];
  for (const check_name of REQUIRED_CHECKS) {
    const matching = await github.paginate(github.rest.checks.listForRef, {
      ...context.repo, ref: sha, check_name, filter: 'latest', per_page: 100,
    });
    const latest = matching.filter(check => check.name === check_name &&
      check.head_sha === sha && check.app?.slug === 'github-actions')
      .sort((a, b) => b.id - a.id)[0];
    if (!latest || latest.status !== 'completed' || latest.conclusion !== 'success') {
      return { ready: false, reason: `${check_name} is missing or not successful` };
    }
    checks.push(latest);
  }
  return { ready: true, pr, checks };
}

async function notification(github, context, pr, sha) {
  const title = notificationName(pr.number, sha);
  for await (const page of github.paginate.iterator(
    github.rest.actions.listWorkflowRuns,
    {
      ...context.repo, workflow_id: NOTICE_WORKFLOW, event: 'workflow_dispatch',
      branch: context.payload.repository.default_branch,
      created: `>=${pr.created_at}`, per_page: 100,
    },
  )) {
    const found = page.data.find(run => run.display_title === title);
    if (found) return found;
  }
  return null;
}

async function gate({ github, context, core, pause = ms =>
  new Promise(resolve => setTimeout(resolve, ms)) }) {
  const results = [];
  for (const { number, sha } of await candidates(github, context)) {
    const state = await readiness(github, context, number, sha);
    if (!state.ready) {
      core.info(`PR #${number}: ${state.reason}`);
      continue;
    }
    if (await notification(github, context, state.pr, sha)) {
      core.info(`PR #${number}: already notified for ${sha}`);
      continue;
    }
    // Revalidate after reading notification history to catch pushes/reruns that
    // happened during those reads. Dispatch always executes the default branch.
    const fresh = await readiness(github, context, number, sha);
    if (!fresh.ready) {
      core.info(`PR #${number}: ${fresh.reason}`);
      continue;
    }
    await github.rest.actions.createWorkflowDispatch({
      ...context.repo, workflow_id: NOTICE_WORKFLOW,
      ref: context.payload.repository.default_branch,
      inputs: { pr_number: String(number), head_sha: sha },
    });
    // Keep the per-commit gate occupied until GitHub exposes the accepted run,
    // so the next queued callback can observe it instead of dispatching twice.
    let found;
    for (let attempt = 0; attempt < 5; attempt++) {
      found = await notification(github, context, fresh.pr, sha);
      if (found) break;
      await pause(1000 * (attempt + 1));
    }
    if (!found) {
      throw new Error('Dispatch accepted but not visible; inspect runs before retrying');
    }
    core.info(`PR #${number}: notification ${found.html_url}`);
    results.push(found);
  }
  return results;
}

async function notice({ github, context, core }) {
  const { number, sha } = identity(
    context.payload.inputs.pr_number, context.payload.inputs.head_sha,
  );
  const state = await readiness(github, context, number, sha);
  if (!state.ready) {
    await core.summary.addHeading(`PR #${number}: readiness changed`)
      .addRaw(`${state.reason}. Recheck the PR before reviewing or merging.`)
      .write();
    return;
  }
  await core.summary.addHeading(`PR #${number} checks passed`)
    .addLink(`Review PR #${number}`, state.pr.html_url)
    .addRaw(`\n\nValidated commit: \`${sha}\`. Maintainers review and merge the PR.\n\n`)
    .addTable([
      [{ data: 'Check', header: true }, { data: 'Successful run', header: true }],
      ...state.checks.map(check => [check.name, check.html_url]),
    ]).write();
}

module.exports = { candidates, gate, identity, notice, notificationName, readiness };
