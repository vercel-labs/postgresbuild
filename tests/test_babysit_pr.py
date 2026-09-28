from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

    import pytest


def load_watcher() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/babysit-pr.py"
    spec = importlib.util.spec_from_file_location("babysit_pr", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sample_pr() -> dict[str, Any]:
    return {
        "number": 7,
        "url": "https://github.com/vercel-labs/postgresbuild/pull/7",
        "state": "OPEN",
        "headRefOid": "abc123",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
    }


def test_checks_distinguish_pending_failure_and_skips() -> None:
    watcher = load_watcher()
    summary = watcher.classify_checks(
        [
            {"name": "build", "bucket": "pending", "state": "IN_PROGRESS"},
            {"name": "lint", "bucket": "pass", "state": "SUCCESS"},
            {"name": "test", "bucket": "fail", "state": "FAILURE"},
            {"name": "optional", "bucket": "skipping", "state": "SKIPPED"},
        ]
    )

    assert (summary.total, summary.passed, summary.skipped) == (4, 1, 1)
    assert [item["name"] for item in summary.pending] == ["build"]
    assert [item["name"] for item in summary.failed] == ["test"]


def test_checks_accept_gh_pending_exit_and_wait_for_first_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = load_watcher()
    output = subprocess.CompletedProcess(
        ["gh"], 8, '[{"name":"build","bucket":"pending"}]', ""
    )
    monkeypatch.setattr(watcher, "run_gh", lambda *args: output)
    assert watcher.read_checks(sample_pr())[0]["name"] == "build"

    output = subprocess.CompletedProcess(
        ["gh"], 1, "", "no checks reported on the branch"
    )
    monkeypatch.setattr(watcher, "run_gh", lambda *args: output)
    assert watcher.read_checks(sample_pr()) == []


def test_feedback_ignores_unpublished_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = load_watcher()

    def fake_api(endpoint: str) -> list[dict[str, Any]]:
        if endpoint.endswith("/reviews"):
            return [
                {"id": 10, "state": "PENDING", "body": "draft"},
                {
                    "id": 11,
                    "state": "COMMENTED",
                    "body": "Please check this.",
                    "user": {"login": "reviewer"},
                },
            ]
        if "/pulls/" in endpoint and endpoint.endswith("/comments"):
            return [
                {
                    "id": 20,
                    "pull_request_review_id": 10,
                    "body": "draft inline",
                },
                {
                    "id": 21,
                    "pull_request_review_id": 11,
                    "body": "Published inline",
                    "user": {"login": "reviewer"},
                },
            ]
        return []

    monkeypatch.setattr(watcher, "api_records", fake_api)
    feedback = watcher.read_feedback(sample_pr())

    assert {(item["kind"], item["id"]) for item in feedback} == {
        ("review", "11"),
        ("inline", "21"),
    }


def test_watch_requires_stable_green_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = load_watcher()
    pr = sample_pr()
    green = watcher.CheckSummary(1, 1, 0, (), ())
    snapshots = iter(
        [
            watcher.Snapshot(pr, watcher.CheckSummary(0, 0, 0, (), ()), ()),
            watcher.Snapshot(pr, green, ()),
            watcher.Snapshot(pr, green, ()),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(watcher, "read_snapshot", lambda spec: next(snapshots))
    monkeypatch.setattr(watcher, "read_pr", lambda spec: pr)
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)

    assert watcher.watch("7", 1, 0, follow=False) == 0
    assert sleeps == [1, 1]


def test_watch_waits_for_workflow_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = load_watcher()
    pr = sample_pr()
    green = watcher.CheckSummary(1, 1, 0, (), ())
    pending = {"status": "pending", "conclusion": ""}
    completed = {"status": "completed", "conclusion": "success"}
    snapshots = iter(
        [
            watcher.Snapshot(pr, green, (), (pending,)),
            watcher.Snapshot(pr, green, (), (completed,)),
            watcher.Snapshot(pr, green, (), (completed,)),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(watcher, "read_snapshot", lambda spec: next(snapshots))
    monkeypatch.setattr(watcher, "read_pr", lambda spec: pr)
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)

    assert watcher.watch("7", 1, 0, follow=False) == 0
    assert sleeps == [1, 1]
