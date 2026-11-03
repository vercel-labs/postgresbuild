from __future__ import annotations

import httpx2
import pytest

from postgresbuild.github import GitHubRepository


def test_repository_follows_pagination_and_preserves_filters() -> None:
    pages = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.url.params["per_page"] == "100"
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.path == "/repos/owner/repo/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["base"] == "main"
        items = [{"id": page}]
        headers = (
            {"Link": f'<{request.url.copy_set_param("page", 2)}>; rel="next"'}
            if page == 1
            else {}
        )
        return httpx2.Response(
            200,
            json=items,
            headers=headers,
        )

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    with GitHubRepository("owner/repo", "token", client=client) as repository:
        values = repository.get_pulls(state="open", base="main")
        assert list(values) == [{"id": 1}, {"id": 2}]
    assert pages == [1, 2]
    assert client.is_closed


def test_repository_surfaces_http_errors() -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(403))
    with GitHubRepository(
        "owner/repo", client=httpx2.Client(transport=transport)
    ) as repository:
        with pytest.raises(httpx2.HTTPStatusError):
            list(repository.get_pulls(state="open"))
