"""GitHub REST operations used by the maintenance tooling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
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
        self, path: str, **params: str | int
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            response = self._get(path, **params, per_page=100, page=page)
            items = response.json()
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
