'use strict';

// Octokit is supplied by actions/github-script; no credentials or dependencies
// are installed from the PR. Only metadata is used; no PR code is executed.
const REQUIRED_WORKFLOWS = [
  { workflow: 'ci.yml', check: 'CI' },
  { workflow: 'postgresbuild.yml', check: 'All builds' },
];
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
  if (event.action !== 'completed' || event.workflow_run?.event !== 'pull_request') {
    return [];
  }
  const sha = event.workflow_run.head_sha;
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

async function latestRun(github, context, workflow, sha) {
  const runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
    ...context.repo, workflow_id: workflow, event: 'pull_request', head_sha: sha,
    per_page: 100,
  });
  const latest = runs.filter(run => run.event === 'pull_request' && run.head_sha === sha)
    .sort((a, b) => b.id - a.id)[0];
  if (latest && (!Number.isSafeInteger(latest.id) || latest.id <= 0 ||
      !Number.isSafeInteger(latest.run_attempt) || latest.run_attempt <= 0)) {
    throw new Error('Workflow run has an invalid ID or attempt');
  }
  return latest;
}

async function readiness(github, context, number, sha) {
  let { data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: number,
  });
  const ineligible = 'PR is closed, draft, retargeted, or stale';
  if (!current(pr, context, sha)) {
    return { ready: false, pr, reason: ineligible };
  }
  const runs = [];
  let reason;
  for (const { workflow, check } of REQUIRED_WORKFLOWS) {
    // Check BOTH workflows: the other one may be rerunning while its previous
    // passing aggregate is still visible. Never filter the runs by conclusion.
    const run = await latestRun(github, context, workflow, sha);
    if (!run || run.status !== 'completed') {
      reason = `${workflow} is missing, queued, or running`;
      break;
    }
    const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRunAttempt, {
      ...context.repo, run_id: run.id, attempt_number: run.run_attempt, per_page: 100,
    });
    const gate = jobs.filter(job => job.name === check)
      .sort((a, b) => b.id - a.id)[0];
    if (!gate || gate.run_id !== run.id || gate.run_attempt !== run.run_attempt ||
        gate.head_sha !== sha || gate.status !== 'completed' || gate.conclusion !== 'success') {
      reason = `${check} is missing or not successful in the current attempt`;
      break;
    }
    runs.push({ workflow, id: run.id, attempt: run.run_attempt });
  }
  // Catch a rerun/new run admitted during the job reads, including a new run ID
  // for the same commit. The notifier independently repeats these checks.
  if (!reason) {
    for (const run of runs) {
      const latest = await latestRun(github, context, run.workflow, sha);
      if (!latest || latest.id !== run.id || latest.run_attempt !== run.attempt ||
          latest.status !== 'completed') {
        reason = 'Required workflow changed while reading readiness';
        break;
      }
    }
  }
  // Refresh on failure too: an old completion must not clear a newer head's
  // label or signal if a push happened during the workflow/job reads.
  ({ data: pr } = await github.rest.pulls.get({
    ...context.repo, pull_number: number,
  }));
  if (!current(pr, context, sha)) reason = ineligible;
  return { ready: !reason, pr, reason, runs };
}

async function signal(github, context, number, sha, state) {
  const prefix = `${SIGNAL_PREFIX}${number}`;
  const matching = await github.paginate(github.rest.git.listMatchingRefs, {
    ...context.repo, ref: prefix, per_page: 100,
  });
  // Match only this PR's dedicated signal refs. Keep support for cleaning up
  // the legacy ref, and never touch another PR's refs (e.g. 18 versus 180).
  const pattern = new RegExp(`^refs/${prefix}(?:-[1-9][0-9]*\\.[1-9][0-9]*-[1-9][0-9]*\\.[1-9][0-9]*)?$`);
  const existing = matching.filter(ref => pattern.test(ref.ref));
  let changed = false;
  let target;
  if (state.ready) {
    // Completed attempts get distinct wakeups even without a rerun-start event.
    // Replays or another workflow's delayed completion reuse the same pair.
    target = `refs/${prefix}-${state.runs.map(run => `${run.id}.${run.attempt}`).join('-')}`;
    const same = existing.find(ref => ref.ref === target);
    if (!same) {
      await github.rest.git.createRef({ ...context.repo, ref: target, sha });
      changed = true;
    } else if (same.object.sha !== sha) {
      await github.rest.git.updateRef({
        ...context.repo, ref: target.slice(5), sha, force: true,
      });
      changed = true;
    }
  }
  // Publish before cleanup so a partial failure/retry never duplicates a
  // successful wakeup. At most one signal is retained after reconciliation.
  for (const ref of existing) {
    if (ref.ref !== target) {
      await github.rest.git.deleteRef({ ...context.repo, ref: ref.ref.slice(5) });
    }
  }
  return changed;
}

async function gate({ github, context, core }) {
  const results = [];
  for (const { number, sha } of await candidates(github, context)) {
    const state = await readiness(github, context, number, sha);
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
    if (await signal(github, context, number, sha, state)) {
      results.push({ number, sha });
    }
    core.info(`PR #${number}: ${state.ready ? `${READY_LABEL} (${sha})` : state.reason}`);
  }
  return results;
}

module.exports = { candidates, gate, identity, readiness };
