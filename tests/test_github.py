from __future__ import annotations

import json

import httpx2
import pytest

from postgresbuild.github import GitHubRepository


@pytest.mark.parametrize("workflows", [False, True])
def test_repository_follows_pagination_and_preserves_filters(
    *, workflows: bool
) -> None:
    pages = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.url.params["per_page"] == "100"
        page = int(request.url.params["page"])
        pages.append(page)
        if workflows:
            assert request.url.path == "/repos/owner/repo/actions/runs"
            assert request.url.params["head_sha"] == "head"
            assert request.url.params["event"] == "pull_request"
        else:
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
            json={"workflow_runs": items} if workflows else items,
            headers=headers,
        )

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    with GitHubRepository("owner/repo", "token", client=client) as repository:
        values = (
            repository.get_workflow_runs(head_sha="head", event="pull_request")
            if workflows
            else repository.get_pulls(state="open", base="main")
        )
        assert list(values) == [{"id": 1}, {"id": 2}]
    assert pages == [1, 2]
    assert client.is_closed


def test_repository_reads_policy_evidence_and_posts_status() -> None:
    paths = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/repos/owner/repo/")
        paths.append(path)
        if path == "pulls/1":
            return httpx2.Response(200, json={"number": 1})
        if path == "compare/main...head":
            return httpx2.Response(200, json={"behind_by": 0})
        if path == "pulls/1/commits":
            return httpx2.Response(200, json=[{"sha": "head"}])
        if path == "pulls/1/files":
            return httpx2.Response(200, json=[{"filename": "src/recipe.py"}])
        if path == "contents/src/recipe.py":
            assert request.url.params["ref"] == "head"
            return httpx2.Response(
                200, json={"encoding": "base64", "content": "ZmlsZQ=="}
            )
        if path == "commits/head/statuses":
            return httpx2.Response(200, json=[])
        assert path == "statuses/head"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "state": "success",
            "context": "Dependency maintenance",
            "description": "passed",
            "target_url": "https://github.com/owner/repo/actions",
        }
        return httpx2.Response(201, json={})

    with GitHubRepository(
        "owner/repo",
        client=httpx2.Client(transport=httpx2.MockTransport(respond)),
    ) as repository:
        assert repository.get_pull(1) == {"number": 1}
        assert repository.compare("main", "head") == {"behind_by": 0}
        assert list(repository.get_commits(1)) == [{"sha": "head"}]
        assert list(repository.get_files(1)) == [{"filename": "src/recipe.py"}]
        assert repository.get_contents("src/recipe.py", ref="head") == {
            "encoding": "base64",
            "content": "ZmlsZQ==",
        }
        assert list(repository.get_statuses("head")) == []
        repository.create_status(
            "head",
            "success",
            context="Dependency maintenance",
            description="passed",
            target_url="https://github.com/owner/repo/actions",
        )
    assert len(paths) == 7


def test_repository_surfaces_http_errors() -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(403))
    with GitHubRepository(
        "owner/repo", client=httpx2.Client(transport=transport)
    ) as repository:
        with pytest.raises(httpx2.HTTPStatusError):
            list(repository.get_pulls(state="open"))
