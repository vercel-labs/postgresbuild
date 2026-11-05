'use strict';

// Octokit is supplied by actions/github-script; no credentials or dependencies
// are installed from the PR. Only metadata is used; no PR code is executed.
const REQUIRED_CHECKS = ['CI', 'All builds'];
const READY_LABEL = 'ready-to-merge';
const SIGNAL_PREFIX = 'heads/automation/pr-ready/';

function identity(number, sha) {
  if (!/^[1-9][0-9]*$/.test(String(number)) ||
      !Number.isSafeInteger(Number(number)) || !/^[0-9a-f]{40}$/.test(sha)) {
    throw new Error('Expected a positive PR number and full lowercase commit SHA');
  }
  return { number: Number(number), sha };
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
  let { data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: number,
  });
  if (!current(pr, context, sha)) {
    return { ready: false, pr, reason: 'PR is closed, draft, retargeted, or stale' };
  }
  if (context.eventName === 'workflow_run') {
    // A rerun can start before its new aggregate check replaces the old pass.
    // Read current state: events can arrive after completion or during a rerun.
    const { data: run } = await github.rest.actions.getWorkflowRun({
      ...context.repo, run_id: context.payload.workflow_run.id,
    });
    if (run.head_sha !== sha) throw new Error('Workflow run does not match event commit');
    if (run.status !== 'completed') {
      return { ready: false, pr, reason: 'A required workflow is queued or running' };
    }
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
      return { ready: false, pr, reason: `${check_name} is missing or not successful` };
    }
    checks.push(latest);
  }
  // Checks take multiple reads; catch a push, draft conversion or closure that
  // happened while reading them before changing readiness.
  ({ data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: number,
  }));
  if (!current(pr, context, sha)) {
    return { ready: false, pr, reason: 'PR is closed, draft, retargeted, or stale' };
  }
  return { ready: true, pr, checks };
}

async function signal(github, context, number, sha, ready) {
  const ref = `${SIGNAL_PREFIX}${number}`;
  let existing;
  try {
    ({ data: existing } = await github.rest.git.getRef({ ...context.repo, ref }));
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  if (!ready) {
    if (existing) await github.rest.git.deleteRef({ ...context.repo, ref });
    return false;
  }
  if (existing?.object.sha === sha) return false;
  if (existing) {
    // Only this dedicated signal ref may be rewritten; never a PR or main ref.
    await github.rest.git.updateRef({ ...context.repo, ref, sha, force: true });
  } else {
    await github.rest.git.createRef({ ...context.repo, ref: `refs/${ref}`, sha });
  }
  return true;
}

async function gate({ github, context, core }) {
  const results = [];
  for (const { number, sha } of await candidates(github, context)) {
    const state = await readiness(github, context, number, sha);
    // An old commit's completion must not remove a newer head's readiness.
    if (state.pr.head.sha !== sha) {
      core.info(`PR #${number}: ${state.reason}`);
      continue;
    }
    const labelled = state.pr.labels.some(label => label.name === READY_LABEL);
    if (!state.ready && labelled) {
      await github.rest.issues.removeLabel({
        ...context.repo, issue_number: number, name: READY_LABEL,
      });
    } else if (state.ready && !labelled) {
      await github.rest.issues.addLabels({
        ...context.repo, issue_number: number, labels: [READY_LABEL],
      });
    }
    // Reconcile independently of the label, including after a partial failure
    // or when migrating an already-labelled PR. An unchanged ref emits no push.
    if (await signal(github, context, number, sha, state.ready)) {
      results.push({ number, sha });
    }
    core.info(`PR #${number}: ${state.ready ? `${READY_LABEL} (${sha})` : state.reason}`);
  }
  return results;
}

module.exports = { candidates, gate, identity, readiness };
