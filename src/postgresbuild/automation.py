"""Trusted-main dependency checks for human-reviewed maintenance PRs."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from github import Auth, Github, GithubException

from postgresbuild.maintenance import (
    MARKER,
    Candidate,
    declarations,
    fixture_substitutions,
    rewrite_fixtures,
    staged_workflow,
    sync_pins,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from github.PullRequest import PullRequest
    from github.Repository import Repository
    from github.WorkflowRun import WorkflowRun

STATUS = "Dependency maintenance"
WORKFLOWS = {".github/workflows/ci.yml", ".github/workflows/postgresbuild.yml"}
# Verified against the installed App and its published Factory PRs.
FACTORY_BOT_ID = "314297673"


def authenticated_factory(pr: PullRequest, repository: str) -> bool:
    return bool(
        str(pr.user.id) == FACTORY_BOT_ID
        and pr.user.type == "Bot"
        and pr.head.repo is not None
        and pr.head.repo.full_name == repository
        and pr.head.ref.startswith("factory/")
        and pr.base.ref == "main"
        and not pr.draft
        and any(label.name == "dependencies" for label in pr.labels)
    )


def workflow_state(runs: Iterable[WorkflowRun], head: str) -> tuple[str, str]:
    latest: dict[str, WorkflowRun] = {}
    for run in runs:
        path = run.path.split("@", 1)[0]
        if (
            path in WORKFLOWS
            and run.head_sha == head
            and run.event == "pull_request"
        ):
            previous = latest.get(path)
            if previous is None or (run.id, run.run_attempt) > (
                previous.id,
                previous.run_attempt,
            ):
                latest[path] = run
    if latest.keys() != WORKFLOWS:
        return (
            "pending",
            "Waiting for CI and the full PostgreSQL build/test workflow",
        )
    if any(run.status != "completed" for run in latest.values()):
        return "pending", "Required workflows are still running"
    if any(run.conclusion != "success" for run in latest.values()):
        return "failure", "A required workflow did not succeed"
    return "success", "CI and all PostgreSQL builds/tests passed for this head"


def verify_changes(
    files: dict[str, tuple[str, str]], body: str
) -> list[Candidate]:
    """Permit declarations, matching pins, patches, and version fixtures."""
    candidates = []
    fixtures: dict[str, str] = {}
    replacements: list[tuple[str, str, str]] = []
    for path, (before, after) in files.items():
        if not re.fullmatch(
            r"src/postgresbuild/[a-z0-9_]+/__init__\.py", path
        ):
            continue
        old_name, old, _ = declarations(before)
        name, new, _ = declarations(after)
        if old == new:
            continue
        if not old or not new or old_name != name:
            raise ValueError("recipe declaration class changed or disappeared")
        previous = tuple(v for v, _ in old)
        current = tuple(v for v, _ in new)
        if previous == current:
            raise ValueError("checksum-only changes require human review")
        for version, digest in new:
            if version in dict(old) and dict(old)[version] != digest:
                raise ValueError("checksum of an existing release changed")
        candidate = Candidate(
            path.split("/")[2], previous, current, "routine", ()
        )
        if candidate.digest not in MARKER.findall(body):
            raise ValueError("missing candidate digest in PR body")
        candidates.append(candidate)
        fixtures.update(fixture_substitutions(candidate, dict(old), dict(new)))
        if len(old) == len(new) == 1:
            replacements.append((name, previous[0], current[0]))
    if not candidates:
        raise ValueError("no release update found")
    packages = {c.package for c in candidates}
    for path, (before, after) in files.items():
        if re.fullmatch(r"src/postgresbuild/[a-z0-9_]+/__init__\.py", path):
            expected = before
            for recipe, old_version, new_version in replacements:
                expected = sync_pins(
                    expected, recipe, old_version, new_version
                )
            _, _, old_tree = declarations(expected)
            _, _, new_tree = declarations(after)
            if ast.dump(old_tree) != ast.dump(new_tree):
                raise ValueError(f"non-maintenance recipe change: {path}")
        elif re.fullmatch(r"tests/test_[a-z0-9_]+\.py", path):
            expected = rewrite_fixtures(before, fixtures)
            if ast.dump(ast.parse(expected)) != ast.dump(ast.parse(after)):
                raise ValueError(f"test logic changed: {path}")
        elif path == ".github/workflows/postgresbuild.yml":
            continue  # Independently regenerated below, using trusted code.
        elif (
            re.fullmatch(
                r"src/postgresbuild/[a-z0-9_]+/patches/[\w./-]+\.patch", path
            )
            and ".." not in Path(path).parts
            and path.split("/")[2] in packages
        ):
            continue
        else:
            raise ValueError(f"file is outside maintenance scope: {path}")
    return candidates


def verify_generated(files: dict[str, tuple[str, str]]) -> None:
    # Never check out or execute a PR branch with the checker's credentials.
    # Recipe ASTs have already been verified against trusted main. Tests and
    # upstream patches are data here and are never executed by this process.
    staged_workflow(
        {path: after for path, (_, after) in files.items()}, check=True
    )


def read_content(repository: Repository, path: str, ref: str) -> str:
    content = repository.get_contents(path, ref=ref)
    if isinstance(content, list) or content.encoding != "base64":
        raise ValueError(f"cannot read {path} at {ref}")
    return content.decoded_content.decode()


def qualify(repository: Repository, pr: PullRequest) -> None:
    sha = pr.head.sha
    comparison = repository.compare("main", sha)
    if comparison.behind_by != 0 or pr.mergeable is not True:
        raise ValueError("branch must be updated with current main")
    commits = list(pr.get_commits())
    if not commits or any(
        c.author is None
        or str(c.author.id) != FACTORY_BOT_ID
        or not c.commit.verification.verified
        for c in commits
    ):
        raise ValueError("all commits must be signed and authored by Factory")
    changed = list(pr.get_files())
    if any(f.status in {"renamed", "removed"} for f in changed):
        raise ValueError("renames and removals require human review")
    files = {
        f.filename: (
            ""
            if f.status == "added"
            else read_content(repository, f.filename, pr.base.sha),
            read_content(repository, f.filename, sha),
        )
        for f in changed
    }
    verify_changes(files, pr.body or "")
    verify_generated(files)


def set_status(
    repository: Repository, sha: str, state: str, description: str
) -> None:
    commit = repository.get_commit(sha)
    existing = next(
        (s for s in commit.get_statuses() if s.context == STATUS), None
    )
    if (
        existing
        and existing.state == state
        and existing.description == description[:140]
    ):
        return
    commit.create_status(
        state,
        context=STATUS,
        description=description[:140],
        target_url=f"https://github.com/{repository.full_name}/actions",
    )


def check(repository: Repository) -> None:
    for summary in repository.get_pulls(state="open", base="main"):
        pr = repository.get_pull(summary.number)
        sha = pr.head.sha
        runs = repository.get_workflow_runs(head_sha=sha, event="pull_request")
        state, description = workflow_state(runs, sha)
        try:
            if state == "success" and authenticated_factory(
                pr, repository.full_name
            ):
                qualify(repository, pr)
        except (
            OSError,
            ValueError,
            GithubException,
            subprocess.CalledProcessError,
        ) as exc:
            state, description = "failure", str(exc)
        # Re-read after policy checks. Status is bound to this head.
        fresh = repository.get_pull(pr.number)
        if fresh.head.sha != sha or fresh.state != "open":
            continue
        set_status(repository, sha, state, description)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.parse_args()
    with Github(
        auth=Auth.Token(os.environ["GH_TOKEN"]),
        per_page=100,
        timeout=60,
        lazy=True,
    ) as client:
        check(client.get_repo(os.environ["GITHUB_REPOSITORY"]))


if __name__ == "__main__":
    main()
