"""GitHub REST operations used by the maintenance tooling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast
from urllib.parse import quote

import httpx2

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType


class GitHubRepository:
    def __init__(
        self,
        full_name: str,
        token: str = "",
        *,
        client: httpx2.Client | None = None,
    ) -> None:
        self.full_name = full_name
        self._url = (
            f"https://api.github.com/repos/{quote(full_name, safe='/')}"
        )
        self.client = client or httpx2.Client(
            timeout=60, follow_redirects=True
        )
        self.client.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.client.headers["Authorization"] = f"Bearer {token}"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.client.close()

    def _get(self, path: str, **params: str | int) -> httpx2.Response:
        response = self.client.get(f"{self._url}/{path}", params=params)
        response.raise_for_status()
        return response

    def _pages(
        self, path: str, *, key: str | None = None, **params: str | int
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            response = self._get(path, **params, per_page=100, page=page)
            value = response.json()
            items = value[key] if key is not None else value
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise ValueError(f"invalid GitHub list response: {path}")
            yield from items
            if "next" not in response.links:
                return
            page += 1

    def get_pulls(
        self, *, state: str, base: str | None = None
    ) -> Iterator[dict[str, Any]]:
        params = {"state": state}
        if base is not None:
            params["base"] = base
        return self._pages("pulls", **params)

    def get_pull(self, number: int) -> dict[str, Any]:
        return cast("dict[str, Any]", self._get(f"pulls/{number}").json())

    def get_workflow_runs(
        self, *, head_sha: str, event: str
    ) -> Iterator[dict[str, Any]]:
        return self._pages(
            "actions/runs", key="workflow_runs", head_sha=head_sha, event=event
        )

    def compare(self, base: str, head: str) -> dict[str, Any]:
        path = f"compare/{quote(base, safe='')}...{quote(head, safe='')}"
        return cast("dict[str, Any]", self._get(path).json())

    def get_commits(self, number: int) -> Iterator[dict[str, Any]]:
        return self._pages(f"pulls/{number}/commits")

    def get_files(self, number: int) -> Iterator[dict[str, Any]]:
        return self._pages(f"pulls/{number}/files")

    def get_contents(self, path: str, *, ref: str) -> Any:
        return self._get(f"contents/{quote(path, safe='/')}", ref=ref).json()

    def get_statuses(self, sha: str) -> Iterator[dict[str, Any]]:
        return self._pages(f"commits/{quote(sha, safe='')}/statuses")

    def create_status(self, sha: str, state: str, **fields: str) -> None:
        response = self.client.post(
            f"{self._url}/statuses/{quote(sha, safe='')}",
            json={"state": state, **fields},
        )
        response.raise_for_status()
