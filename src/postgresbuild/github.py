"""Read public GitHub releases without the anonymous REST API quota."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import unquote

import httpx2
from packaging.version import Version

if TYPE_CHECKING:
    from ggbuild.updater import UpdatePolicy


class ReleasePage(HTMLParser):
    def __init__(self, repository: str) -> None:
        super().__init__()
        self.prefix = f"/{repository}/releases/tag/"
        self.tags: list[str] = []
        self.has_next = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href") or ""
        if (
            href.startswith(self.prefix)
            and "Link--primary" in (attributes.get("class") or "").split()
        ):
            release_tag = unquote(href.removeprefix(self.prefix))
            if release_tag not in self.tags:
                self.tags.append(release_tag)
        if "next" in (attributes.get("rel") or "").split():
            self.has_next = True


def latest_release(policy: UpdatePolicy) -> str:
    repository = policy.get("repository")
    template = policy.get("tag")
    if not repository or not template:
        raise ValueError("github-release policy needs repository and tag")
    major = policy.get("major")
    version_pattern = rf"{re.escape(major)}\..+" if major else ".+"
    pattern = re.escape(template).replace(
        re.escape("{version}"), rf"(?P<version>{version_pattern})"
    )
    url = f"https://github.com/{repository}/releases"
    with httpx2.Client(timeout=120, follow_redirects=True) as client:
        if not major:
            # Preserve GitHub's designated stable release, which need not be
            # the greatest version or newest tag.
            response = client.head(f"{url}/latest")
            response.raise_for_status()
            # Repository renames redirect before the latest-tag redirect.
            redirect = re.fullmatch(
                r"/[^/]+/[^/]+/releases/tag/(.+)", response.url.path
            )
            if response.url.host != "github.com" or redirect is None:
                raise ValueError(f"missing latest release redirect at {url}")
            tag = unquote(redirect[1])
            match = re.fullmatch(pattern, tag)
            if match is None or Version(match["version"]).is_prerelease:
                raise ValueError(f"unexpected latest release {tag!r} at {url}")
            return str(Version(match["version"]))

        # Match ggbuild's major-filtered policy: greatest stable version among
        # the first 100 published releases, ten per web page.
        tags: list[str] = []
        for page in range(1, 11):
            response = client.get(url, params={"page": page})
            response.raise_for_status()
            listing = re.fullmatch(
                r"/([^/]+/[^/]+)/releases", response.url.path
            )
            if response.url.host != "github.com" or listing is None:
                raise ValueError(
                    f"unexpected release listing at {response.url}"
                )
            parsed = ReleasePage(listing[1])
            parsed.feed(response.text)
            if not parsed.tags or set(tags).intersection(parsed.tags):
                raise ValueError(
                    f"incomplete release listing at {response.url}"
                )
            tags.extend(parsed.tags)
            if len(tags) >= 100 or not parsed.has_next:
                break
        else:
            raise ValueError(f"incomplete release listing at {url}")

    versions = {
        Version(match["version"])
        for tag in tags[:100]
        if (match := re.fullmatch(pattern, tag)) is not None
        and not Version(match["version"]).is_prerelease
    }
    if not versions:
        raise ValueError(f"no major {major} releases found at {url}")
    return str(max(versions))
