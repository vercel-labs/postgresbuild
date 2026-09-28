#!/usr/bin/env python3
"""Watch a pull request's checks and review feedback with GitHub CLI."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import count
from operator import itemgetter
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PR_FIELDS = "number,url,state,headRefOid,mergeable,reviewDecision"
CHECK_FIELDS = "name,state,bucket,link,workflow"
PENDING_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
PASS_STATES = {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True)
class CheckSummary:
    total: int
    passed: int
    skipped: int
    pending: tuple[dict[str, Any], ...]
    failed: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Snapshot:
    pr: dict[str, Any]
    checks: CheckSummary
    feedback: tuple[dict[str, str], ...]


def run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["gh", *args]
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("GitHub CLI (gh) is required") from error


def gh_json(*args: str) -> object:
    result = run_gh(*args)
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or f"gh {' '.join(args)} failed"
        )
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON from gh {' '.join(args)}") from error
    return payload


def mapping(value: object, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object from {source}")
    return cast("dict[str, Any]", value)


def records(value: object, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list from {source}")
    return [mapping(item, source) for item in value]


def read_pr(spec: str) -> dict[str, Any]:
    target = [] if spec == "auto" else [spec]
    return mapping(
        gh_json("pr", "view", *target, "--json", PR_FIELDS), "pr view"
    )


def repo_from_pr(pr: dict[str, Any]) -> str:
    url = urlsplit(str(pr.get("url") or ""))
    parts = url.path.strip("/").split("/")
    if url.hostname != "github.com" or len(parts) != 4 or parts[2] != "pull":
        raise RuntimeError(f"unexpected pull request URL: {url.geturl()}")
    return f"{parts[0]}/{parts[1]}"


def read_checks(pr: dict[str, Any]) -> list[dict[str, Any]]:
    result = run_gh(
        "pr",
        "checks",
        str(pr["number"]),
        "-R",
        repo_from_pr(pr),
        "--json",
        CHECK_FIELDS,
    )
    if not result.stdout.strip():
        if "no checks reported" in result.stderr.lower():
            return []
        raise RuntimeError(result.stderr.strip() or "gh pr checks failed")
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("invalid JSON from gh pr checks") from error
    # gh pr checks exits nonzero for failed or pending checks.
    return records(payload, "pr checks")


def classify_checks(checks: list[dict[str, Any]]) -> CheckSummary:
    passed = skipped = 0
    pending: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for check in checks:
        bucket = str(check.get("bucket") or "").lower()
        state = str(check.get("state") or "").upper()
        if bucket == "pending" or state in PENDING_STATES:
            pending.append(check)
        elif bucket == "skipping" or state == "SKIPPED":
            skipped += 1
        elif bucket == "pass" or state in PASS_STATES:
            passed += 1
        else:
            failed.append(check)
    return CheckSummary(
        len(checks), passed, skipped, tuple(pending), tuple(failed)
    )


def api_records(endpoint: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in count(1):
        items = records(
            gh_json("api", f"{endpoint}?per_page=100&page={page}"),
            endpoint,
        )
        result.extend(items)
        if len(items) < 100:
            return result
    raise RuntimeError("unreachable pagination state")


def author(item: dict[str, Any]) -> str:
    user = item.get("user")
    return str(user.get("login") or "") if isinstance(user, dict) else ""


def read_feedback(pr: dict[str, Any]) -> tuple[dict[str, str], ...]:
    base = f"repos/{repo_from_pr(pr)}"
    number = str(pr["number"])
    reviews = api_records(f"{base}/pulls/{number}/reviews")
    pending_reviews = {
        str(item.get("id"))
        for item in reviews
        if str(item.get("state") or "").upper() == "PENDING"
    }
    feedback: list[dict[str, str]] = []

    def add(kind: str, item: dict[str, Any], body: str) -> None:
        if body:
            feedback.append(
                {
                    "kind": kind,
                    "id": str(item.get("id") or ""),
                    "author": author(item),
                    "body": body,
                    "url": str(item.get("html_url") or ""),
                    "time": str(
                        item.get("submitted_at")
                        or item.get("created_at")
                        or ""
                    ),
                }
            )

    for item in api_records(f"{base}/issues/{number}/comments"):
        add("comment", item, str(item.get("body") or ""))
    for item in reviews:
        state = str(item.get("state") or "").upper()
        if state != "PENDING":
            add("review", item, str(item.get("body") or state))
    for item in api_records(f"{base}/pulls/{number}/comments"):
        review_id = str(item.get("pull_request_review_id") or "")
        if review_id not in pending_reviews:
            add("inline", item, str(item.get("body") or ""))
    feedback.sort(key=itemgetter("time", "kind", "id"))
    return tuple(feedback)


def read_snapshot(spec: str) -> Snapshot:
    pr = read_pr(spec)
    return Snapshot(pr, classify_checks(read_checks(pr)), read_feedback(pr))


def report(snapshot: Snapshot, seen: set[tuple[str, str]]) -> None:
    pr = snapshot.pr
    checks = snapshot.checks
    print(
        f"PR #{pr['number']} {str(pr['headRefOid'])[:12]}: "
        f"{checks.passed} passed, {checks.skipped} skipped, "
        f"{len(checks.pending)} pending, {len(checks.failed)} failed "
        f"(mergeable={pr.get('mergeable')}, "
        f"review={pr.get('reviewDecision')})",
        flush=True,
    )
    for item in snapshot.feedback:
        key = (item["kind"], item["id"])
        if key in seen:
            continue
        seen.add(key)
        summary = " ".join(item["body"].split())[:200]
        print(
            f"  {item['kind']} from {item['author']}: {summary} {item['url']}",
            flush=True,
        )


def watch(spec: str, interval: int, timeout: int, *, follow: bool) -> int:
    started = time.monotonic()
    seen: set[tuple[str, str]] = set()
    last_green_key: tuple[str, int, int, int] | None = None
    last_green_sha = ""
    errors = 0
    while True:
        try:
            snapshot = read_snapshot(spec)
        except (RuntimeError, TypeError) as error:
            errors += 1
            if errors >= 3:
                raise
            print(
                f"GitHub status unavailable ({errors}/3): {error}", flush=True
            )
            time.sleep(interval)
            continue
        errors = 0
        report(snapshot, seen)
        pr = snapshot.pr
        state = str(pr.get("state") or "").upper()
        if state != "OPEN":
            print(f"PR is {state.lower()}: {pr['url']}", flush=True)
            return 0 if state == "MERGED" else 2
        if str(pr.get("reviewDecision") or "") == "CHANGES_REQUESTED":
            print(
                "Review changes requested; inspect the feedback above.",
                flush=True,
            )
            return 1
        if str(pr.get("mergeable") or "") == "CONFLICTING":
            print("PR has merge conflicts.", flush=True)
            return 1
        if snapshot.checks.failed:
            for check in snapshot.checks.failed:
                print(
                    f"FAILED {check.get('name')}: {check.get('link')}",
                    flush=True,
                )
            return 1
        green = snapshot.checks.total > 0 and not snapshot.checks.pending
        if green and str(pr.get("mergeable") or "") == "MERGEABLE":
            sha = str(pr["headRefOid"])
            green_key = (
                sha,
                snapshot.checks.total,
                snapshot.checks.passed,
                snapshot.checks.skipped,
            )
            if last_green_key == green_key:
                current = read_pr(spec)
                if str(current.get("headRefOid") or "") == sha:
                    if last_green_sha != sha:
                        print(
                            f"SUCCESS: all checks passed for {sha}", flush=True
                        )
                        last_green_sha = sha
                    if not follow:
                        return 0
            else:
                print("All current checks passed; confirming...", flush=True)
            last_green_key = green_key
        else:
            last_green_key = None
        if timeout and time.monotonic() - started >= timeout:
            print(f"Timed out after {timeout} seconds.", flush=True)
            return 3
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pr", nargs="?", default="auto", help="PR number or URL"
    )
    parser.add_argument(
        "--once", action="store_true", help="Print one snapshot"
    )
    parser.add_argument(
        "--follow", action="store_true", help="Keep watching after checks pass"
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument(
        "--timeout", type=int, default=0, help="Seconds; 0 disables"
    )
    args = parser.parse_args()
    if args.interval < 1 or args.timeout < 0 or (args.once and args.follow):
        parser.error("use a positive interval and nonnegative timeout")
    try:
        if args.once:
            report(read_snapshot(args.pr), set())
            return 0
        return watch(args.pr, args.interval, args.timeout, follow=args.follow)
    except (RuntimeError, TypeError) as error:
        print(f"PR watcher error: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
