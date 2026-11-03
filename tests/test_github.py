from __future__ import annotations

from unittest import mock

import httpx2
import pytest

from postgresbuild import github


def release_link(tag: str) -> str:
    return (
        f'<span><a class="Link--primary Link" '
        f'href="/owner/repo/releases/tag/{tag}">{tag}</a></span>'
    )


@pytest.mark.parametrize("renamed", [False, True])
def test_latest_uses_designated_release_without_rest_api(
    *, renamed: bool
) -> None:
    repository = "new-owner/repo" if renamed else "owner/repo"

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "github.com"
        assert "Authorization" not in request.headers
        assert request.method == "HEAD"
        if renamed and request.url.path == "/owner/repo/releases/latest":
            return httpx2.Response(
                301, headers={"Location": f"/{repository}/releases/latest"}
            )
        if request.url.path.endswith("/latest"):
            return httpx2.Response(
                302, headers={"Location": f"/{repository}/releases/tag/v1.2.3"}
            )
        assert request.url.path == f"/{repository}/releases/tag/v1.2.3"
        return httpx2.Response(200)

    client = httpx2.Client(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    )
    with mock.patch.object(github.httpx2, "Client", return_value=client):
        assert (
            github.latest_release(
                {
                    "type": "github-release",
                    "repository": "owner/repo",
                    "tag": "v{version}",
                }
            )
            == "1.2.3"
        )
    assert client.is_closed


@pytest.mark.parametrize("tag", ["v1.3.0beta1", "other-1.2.3", ""])
def test_invalid_latest_release_is_a_failure(tag: str) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/latest") and tag:
            return httpx2.Response(
                302, headers={"Location": f"/owner/repo/releases/tag/{tag}"}
            )
        return httpx2.Response(200)

    client = httpx2.Client(
        transport=httpx2.MockTransport(respond), follow_redirects=True
    )
    with (
        mock.patch.object(github.httpx2, "Client", return_value=client),
        pytest.raises(ValueError, match="latest release"),
    ):
        github.latest_release(
            {
                "type": "github-release",
                "repository": "owner/repo",
                "tag": "v{version}",
            }
        )


def test_major_policy_follows_pages_and_selects_greatest_stable_version() -> (
    None
):
    pages = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "github.com"
        assert request.url.path == "/owner/repo/releases"
        page = int(request.url.params["page"])
        pages.append(page)
        html = (
            release_link("v4.0.0")
            + release_link("v3.6.2")
            + '<a rel="next" href="/owner/repo/releases?page=2">Next</a>'
            if page == 1
            else release_link("v3.9.0beta1")
            + release_link("v3.5.9")
            + release_link("v3.7.1")
            # A release-note link is not a published release heading.
            + '<a href="/owner/repo/releases/tag/v3.99.0">Planned version</a>'
        )
        return httpx2.Response(200, text=html)

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    with mock.patch.object(github.httpx2, "Client", return_value=client):
        assert (
            github.latest_release(
                {
                    "type": "github-release",
                    "repository": "owner/repo",
                    "tag": "v{version}",
                    "major": "3",
                }
            )
            == "3.7.1"
        )
    assert pages == [1, 2]


@pytest.mark.parametrize("failure", ["http", "empty", "repeated", "no-match"])
def test_failed_or_incomplete_major_listing_is_not_current(
    failure: str,
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        if failure == "no-match":
            return httpx2.Response(200, text=release_link("v4.0.0"))
        if page == 1:
            return httpx2.Response(
                200, text=release_link("v3.5.7") + '<a rel="next">Next</a>'
            )
        if failure == "http":
            return httpx2.Response(403)
        return httpx2.Response(
            200, text=release_link("v3.5.7") if failure == "repeated" else ""
        )

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    with (
        mock.patch.object(github.httpx2, "Client", return_value=client),
        pytest.raises((httpx2.HTTPError, ValueError)),
    ):
        github.latest_release(
            {
                "type": "github-release",
                "repository": "owner/repo",
                "tag": "v{version}",
                "major": "3",
            }
        )


def test_major_policy_preserves_the_first_hundred_release_limit() -> None:
    pages = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        assert page <= 10
        html = "".join(
            release_link(f"v3.5.{page * 10 + n}") for n in range(10)
        )
        return httpx2.Response(200, text=html + '<a rel="next">Next</a>')

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    with mock.patch.object(github.httpx2, "Client", return_value=client):
        assert (
            github.latest_release(
                {
                    "type": "github-release",
                    "repository": "owner/repo",
                    "tag": "v{version}",
                    "major": "3",
                }
            )
            == "3.5.109"
        )
    assert pages == list(range(1, 11))
