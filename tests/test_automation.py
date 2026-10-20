from __future__ import annotations

import base64
import copy
import subprocess
from typing import Any
from unittest import mock

import pytest
from github import GithubException
from github.Commit import Commit
from github.ContentFile import ContentFile
from github.File import File
from github.GithubObject import GithubObject
from github.PullRequest import PullRequest
from github.Repository import Repository
from github.WorkflowRun import WorkflowRun
from packaging.version import Version

from postgresbuild import automation, maintenance

SHA = "a" * 40
CURRENT_SSL = maintenance.recipes()["openssl"][-1].source_version
_SSL = Version(CURRENT_SSL)
NEXT_SSL = f"{_SSL.major}.{_SSL.minor}.{_SSL.micro + 1}"


def pull() -> dict[str, Any]:
    return {
        "number": 1,
        "node_id": "PR_1",
        "draft": False,
        "state": "open",
        "title": "OpenSSL: security update",
        "html_url": "https://github.com/vercel-labs/postgresbuild/pull/1",
        "user": {"id": int(automation.FACTORY_BOT_ID), "type": "Bot"},
        "head": {
            "sha": SHA,
            "ref": "factory/task-1",
            "repo": {"full_name": "vercel-labs/postgresbuild"},
        },
        "base": {"ref": "main", "sha": "b" * 40},
        "labels": [{"name": "dependencies"}],
        "mergeable": True,
    }


def runs() -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "run_attempt": 1,
            "path": path,
            "head_sha": SHA,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "html_url": f"https://github.com/vercel-labs/postgresbuild/actions/runs/{i}",
        }
        for i, path in enumerate(sorted(automation.WORKFLOWS), start=1)
    ]


def resource[T: GithubObject](kind: type[T], data: dict[str, Any]) -> T:
    return kind(mock.Mock(), {}, data)


def pull_request(data: dict[str, Any] | None = None) -> PullRequest:
    return resource(PullRequest, pull() if data is None else data)


def workflow_runs(data: list[dict[str, Any]]) -> list[WorkflowRun]:
    return [resource(WorkflowRun, item) for item in data]


@pytest.mark.parametrize(
    "alteration",
    [
        {"user": {"id": 43, "type": "Bot"}},
        {"user": {"id": int(automation.FACTORY_BOT_ID), "type": "User"}},
        {"draft": True},
        {"labels": []},
        {
            "head": {
                "ref": "factory/task-1",
                "repo": {"full_name": "someone/fork"},
            }
        },
        {
            "head": {
                "ref": "maintenance/task-1",
                "repo": {"full_name": "vercel-labs/postgresbuild"},
            }
        },
    ],
)
def test_factory_identity_cannot_be_replaced_by_a_label(
    alteration: dict[str, Any],
) -> None:
    pr = pull_request({**pull(), **alteration})
    assert not automation.authenticated_factory(
        pr, "vercel-labs/postgresbuild"
    )


def test_existing_factory_bot_is_recognized_without_configuration() -> None:
    assert automation.authenticated_factory(
        pull_request(), "vercel-labs/postgresbuild"
    )


@pytest.mark.parametrize(
    "state,conclusion,expected",
    [
        ("completed", "success", "success"),
        ("in_progress", None, "pending"),
        ("queued", None, "pending"),
        ("completed", "failure", "failure"),
        ("completed", "cancelled", "failure"),
        ("completed", "skipped", "failure"),
    ],
)
def test_complete_workflow_gates(
    state: str, conclusion: str | None, expected: str
) -> None:
    results = runs()
    results[1].update(status=state, conclusion=conclusion)
    assert (
        automation.workflow_state(workflow_runs(results), SHA)[0] == expected
    )


def test_missing_stale_or_manually_dispatched_runs_do_not_satisfy_gate() -> (
    None
):
    assert (
        automation.workflow_state(workflow_runs(runs()[:1]), SHA)[0]
        == "pending"
    )
    assert (
        automation.workflow_state(workflow_runs(runs()), "c" * 40)[0]
        == "pending"
    )
    results = runs()
    results[1]["event"] = "workflow_dispatch"
    assert (
        automation.workflow_state(workflow_runs(results), SHA)[0] == "pending"
    )


def test_newer_rerun_invalidates_earlier_success() -> None:
    results = runs()
    rerun = {
        **results[1],
        "run_attempt": 2,
        "status": "in_progress",
        "conclusion": None,
    }
    assert (
        automation.workflow_state(workflow_runs([*results, rerun]), SHA)[0]
        == "pending"
    )


def bump_files() -> tuple[dict[str, tuple[str, str]], str]:
    before = (
        maintenance.ROOT / "src/postgresbuild/openssl/__init__.py"
    ).read_text()
    _, registered, _ = maintenance.declarations(before)
    after = before.replace(f'"{CURRENT_SSL}",', f'"{NEXT_SSL}",').replace(
        'sha256="' + registered[0][1] + '"',
        'sha256="' + "a" * 64 + '"',
    )
    change = maintenance.Candidate(
        "openssl", (CURRENT_SSL,), (NEXT_SSL,), "security", ()
    )
    return {
        "src/postgresbuild/openssl/__init__.py": (before, after)
    }, f"<!-- postgresbuild-candidate:{change.digest} -->"


def test_policy_allows_declarations_and_matching_exact_pins() -> None:
    files, body = bump_files()
    before = (
        maintenance.ROOT / "src/postgresbuild/postgresql/__init__.py"
    ).read_text()
    files["src/postgresbuild/postgresql/__init__.py"] = (
        before,
        maintenance.sync_pins(before, "OpenSSL", CURRENT_SSL, NEXT_SSL),
    )
    assert [c.package for c in automation.verify_changes(files, body)] == [
        "openssl"
    ]


def test_policy_accepts_a_batch_with_one_marker_per_package() -> None:
    grouped = maintenance.recipes()
    files, ssl_marker = bump_files()
    markers = [ssl_marker]
    for package in ("zstd", "postgresql"):
        path = f"src/postgresbuild/{package}/__init__.py"
        before = (maintenance.ROOT / path).read_text()
        _, registered, _ = maintenance.declarations(before)
        after = before
        current = []
        for old, sha in registered:
            parsed = Version(old)
            new = (
                old
                if parsed.is_prerelease
                else (
                    f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}"
                    if package == "zstd"
                    else f"{parsed.major}.{parsed.minor + 1}"
                )
            )
            current.append(new)
            if new != old:
                after = after.replace(f'"{old}",', f'"{new}",').replace(
                    sha, "a" * 64
                )
        change = maintenance.Candidate(
            package,
            tuple(v for v, _ in registered),
            tuple(current),
            "upstream",
            (),
        )
        markers.append(f"<!-- postgresbuild-candidate:{change.digest} -->")
        files[path] = before, after
    pg_path = "src/postgresbuild/postgresql/__init__.py"
    before, after = files[pg_path]
    after = maintenance.sync_pins(after, "OpenSSL", CURRENT_SSL, NEXT_SSL)
    zstd = grouped["zstd"][-1].source_version
    parsed = Version(zstd)
    after = maintenance.sync_pins(
        after,
        "Zstandard",
        zstd,
        f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}",
    )
    files[pg_path] = before, after
    assert {
        c.package for c in automation.verify_changes(files, "\n".join(markers))
    } == {"openssl", "zstd", "postgresql"}
    with pytest.raises(ValueError, match="missing candidate digest"):
        automation.verify_changes(files, "\n".join(markers[:2]))


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        ".vc/automations/config.yml",
        ".github/workflows/ci.yml",
        "src/postgresbuild/automation.py",
        "src/postgresbuild/openldap/patches/unrelated.patch",
    ],
)
def test_policy_blocks_unrelated_changes(path: str) -> None:
    files, body = bump_files()
    files[path] = ("before", "after")
    with pytest.raises(ValueError, match="outside maintenance"):
        automation.verify_changes(files, body)


def test_policy_blocks_recipe_code_execution_and_test_logic_changes() -> None:
    files, body = bump_files()
    path = "src/postgresbuild/openssl/__init__.py"
    before, after = files[path]
    files[path] = (
        before,
        after + "\nraise RuntimeError('changed build logic')\n",
    )
    with pytest.raises(ValueError, match="non-maintenance"):
        automation.verify_changes(files, body)
    files, body = bump_files()
    files["tests/test_project.py"] = ("assert True\n", "assert False\n")
    with pytest.raises(ValueError, match="test logic"):
        automation.verify_changes(files, body)


def test_policy_requires_digest_and_rejects_existing_checksum_changes() -> (
    None
):
    files, _ = bump_files()
    with pytest.raises(ValueError, match="digest"):
        automation.verify_changes(files, "not a candidate")
    before = (
        maintenance.ROOT / "src/postgresbuild/postgresql/__init__.py"
    ).read_text()
    _, registered, _ = maintenance.declarations(before)
    files, body = bump_files()
    files["src/postgresbuild/postgresql/__init__.py"] = (
        before,
        before.replace(
            registered[0][1],
            "a" * 64,
        ),
    )
    with pytest.raises(ValueError, match="checksum-only"):
        automation.verify_changes(files, body)


def test_generated_workflow_is_independently_validated() -> None:
    workflow = (
        maintenance.ROOT / ".github/workflows/postgresbuild.yml"
    ).read_text()
    tampered = workflow.replace(
        "uv run ggbuild ci publish-github", "echo altered-publication"
    )
    assert tampered != workflow
    with pytest.raises(subprocess.CalledProcessError):
        automation.verify_generated(
            {".github/workflows/postgresbuild.yml": (workflow, tampered)}
        )


@pytest.mark.parametrize(
    "successful,error,expected",
    [
        (True, None, "success"),
        (True, ValueError("blocked"), "failure"),
        (
            True,
            GithubException(403, {"message": "API unavailable"}),
            "failure",
        ),
        (False, None, "failure"),
    ],
)
def test_checks_report_eligibility_without_merging_or_app_credentials(
    *,
    successful: bool,
    error: Exception | None,
    expected: str,
) -> None:
    repository = mock.Mock(spec=Repository)
    repository.full_name = "vercel-labs/postgresbuild"
    pr = pull_request()
    results = runs()
    if not successful:
        results[1]["conclusion"] = "failure"
    repository.get_pulls.return_value = [pr]
    repository.get_pull.return_value = pr
    repository.get_workflow_runs.return_value = workflow_runs(results)
    commit = repository.get_commit.return_value
    commit.get_statuses.return_value = []
    with mock.patch.object(automation, "qualify", side_effect=error):
        automation.check(repository)
    repository.get_pulls.assert_called_once_with(state="open", base="main")
    repository.get_workflow_runs.assert_called_once_with(
        head_sha=SHA, event="pull_request"
    )
    # Even a passing PR only receives status; the workflow cannot merge it.
    repository.get_commit.assert_called_once_with(SHA)
    assert commit.create_status.call_count == 1
    assert commit.create_status.call_args.args == (expected,)
    assert (
        commit.create_status.call_args.kwargs["context"] == automation.STATUS
    )


@pytest.mark.parametrize(
    "author,verified",
    [
        ({"id": 42}, True),
        ({"id": int(automation.FACTORY_BOT_ID)}, False),
        (None, True),
    ],
)
def test_unsigned_or_human_authored_commits_cannot_qualify(
    author: dict[str, Any] | None, *, verified: bool
) -> None:
    repository = mock.Mock(spec=Repository)
    repository.compare.return_value.behind_by = 0
    commit = resource(
        Commit,
        {
            "author": author,
            "commit": {"verification": {"verified": verified}},
        },
    )
    pr = pull_request()
    with mock.patch.object(pr, "get_commits", return_value=[commit]):
        with pytest.raises(ValueError, match="signed"):
            automation.qualify(repository, pr)


def test_stale_base_prevents_qualification() -> None:
    repository = mock.Mock(spec=Repository)
    repository.compare.return_value.behind_by = 1
    pr = pull_request()
    with mock.patch.object(pr, "get_commits") as commits:
        with pytest.raises(ValueError, match="current main"):
            automation.qualify(repository, pr)
    commits.assert_not_called()


def test_head_change_during_validation_prevents_success_status() -> None:
    repository = mock.Mock(spec=Repository)
    repository.full_name = "vercel-labs/postgresbuild"
    changed = copy.deepcopy(pull())
    changed["head"]["sha"] = "c" * 40
    pr = pull_request()
    repository.get_pulls.return_value = [pr]
    repository.get_pull.side_effect = [pr, pull_request(changed)]
    repository.get_workflow_runs.return_value = workflow_runs(runs())
    with mock.patch.object(automation, "qualify"):
        automation.check(repository)
    repository.get_commit.assert_not_called()


def test_qualification_uses_sdk_commit_verification_and_file_contents() -> (
    None
):
    files, body = bump_files()
    repository = mock.Mock(spec=Repository)
    repository.compare.return_value.behind_by = 0
    pr = pull_request({**pull(), "body": body})
    commit = resource(
        Commit,
        {
            "author": {"id": int(automation.FACTORY_BOT_ID)},
            "commit": {"verification": {"verified": True}},
        },
    )
    changed = [
        resource(File, {"filename": path, "status": "modified"})
        for path in files
    ]

    def contents(path: str, *, ref: str) -> ContentFile:
        before, after = files[path]
        text = after if ref == SHA else before
        return resource(
            ContentFile,
            {
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            },
        )

    repository.get_contents.side_effect = contents
    with (
        mock.patch.object(pr, "get_commits", return_value=[commit]),
        mock.patch.object(pr, "get_files", return_value=changed),
        mock.patch.object(automation, "verify_generated") as generated,
    ):
        automation.qualify(repository, pr)
    repository.compare.assert_called_once_with("main", SHA)
    generated.assert_called_once_with(files)
    path = next(iter(files))
    assert repository.get_contents.call_args_list == [
        mock.call(path, ref=pr.base.sha),
        mock.call(path, ref=SHA),
    ]


def test_status_is_not_reposted_when_the_result_is_unchanged() -> None:
    repository = mock.Mock(spec=Repository)
    commit = repository.get_commit.return_value
    status = mock.Mock(
        context=automation.STATUS, state="success", description="passed"
    )
    commit.get_statuses.return_value = [status]
    automation.set_status(repository, SHA, "success", "passed")
    commit.create_status.assert_not_called()


def test_file_read_rejects_a_directory() -> None:
    repository = mock.Mock(spec=Repository)
    repository.get_contents.return_value = []
    with pytest.raises(ValueError, match="cannot read"):
        automation.read_content(repository, "src", SHA)
