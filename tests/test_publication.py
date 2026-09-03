from __future__ import annotations

import base64
import hashlib
import json
import operator
import time
import urllib.parse
from typing import Any, cast

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import main
from postgresbuild import publication
from postgresbuild.publication import (
    CHECKSUM_NAME,
    SNAPSHOT_NAME,
    PublicationPolicy,
    canonical_json,
    fetch_valid_snapshot,
    materialize,
    project_index,
    validate_index,
    validate_snapshot,
    verify_github_oidc,
    versions_ndjson,
)

REPOSITORY = "example/postgresbuild"
POLICY = PublicationPolicy(REPOSITORY)


WORKFLOW_REF = POLICY.workflow_ref
TAG = "202601010000"


def _url(name: str) -> str:
    return (
        f"https://github.com/{REPOSITORY}/releases/download/{TAG}/"
        f"{urllib.parse.quote(name, safe='')}"
    )


def test_publication_policy_is_loaded_from_environment() -> None:
    policy = PublicationPolicy.from_environment(
        {
            "PUBLICATION_REPOSITORY": "owner/repository",
            "PUBLICATION_REF": "refs/heads/stable",
            "PUBLICATION_WORKFLOW_PATH": ".github/workflows/release.yml",
            "PUBLICATION_ENVIRONMENT": "production",
        }
    )
    assert policy.owner == "owner"
    assert policy.workflow_ref == (
        "owner/repository/.github/workflows/release.yml@refs/heads/stable"
    )
    assert policy.subject == "repo:owner/repository:environment:production"
    assert (
        policy.immutable_subject(
            {"repository_owner_id": "123", "repository_id": "456"}
        )
        == "repo:owner@123/repository@456:environment:production"
    )
    assert policy.immutable_subject({}) is None

    with pytest.raises(ValueError, match="PUBLICATION_REPOSITORY"):
        PublicationPolicy.from_environment({})


def _file(
    name: str,
    role: str,
    content: bytes = b"evidence",
    *,
    version: str = "17.1",
    target: str = "target",
    tag: str = TAG,
) -> dict[str, object]:
    result: dict[str, object] = {
        "encoding": "zstd"
        if role in {"primary-archive", "dbgsym"}
        else "identity",
        "logical_name": name,
        "media_type": (
            "application/x-tar"
            if role in {"primary-archive", "dbgsym"}
            else "application/octet-stream"
        ),
        "role": role,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    if role in {"primary-archive", "dbgsym"}:
        suffix = "-dbgsym" if role == "dbgsym" else ""
        release_name = f"postgresql-{version}+{tag}-{target}{suffix}.tar.zst"
        result.update(
            {
                "release_name": release_name,
                "url": (
                    f"https://github.com/{REPOSITORY}/releases/download/"
                    f"{tag}/{urllib.parse.quote(release_name, safe='')}"
                ),
            }
        )
    return result


def _record(
    version: str = "17.1", target: str = "target", tag: str = TAG
) -> dict[str, object]:
    files = [
        _file(
            "archive.tar.zst",
            "primary-archive",
            b"archive",
            version=version,
            target=target,
            tag=tag,
        ),
        _file(
            "archive.dbgsym.tar.zst",
            "dbgsym",
            b"symbols",
            version=version,
            target=target,
            tag=tag,
        ),
        _file("archive.metadata.json", "metadata"),
        _file("archive.test-result.json", "test-result"),
    ]
    return {
        "coordinate": {
            "package": "postgresql",
            "source_version": version,
            "target": target,
        },
        "files": sorted(files, key=operator.itemgetter("logical_name")),
        "test_result": {
            "artifact_sha256": hashlib.sha256(b"archive").hexdigest(),
            "recipe": "postgresbuild.postgresql:PostgreSQL",
            "target": target,
            "version": version,
        },
    }


def _snapshot(tag: str = TAG, version: str = "17.1") -> dict[str, object]:
    record = _record(version, tag=tag)
    files = cast("list[dict[str, Any]]", record["files"])
    checksum = "".join(
        f"{item['sha256']}  {item['release_name']}\n"
        for item in sorted(
            (item for item in files if "release_name" in item),
            key=operator.itemgetter("release_name"),
        )
    ).encode()
    checksum_url = (
        f"https://github.com/{REPOSITORY}/releases/download/"
        f"{tag}/{CHECKSUM_NAME}"
    )
    return {
        "checksum": {
            "media_type": "text/plain",
            "release_name": CHECKSUM_NAME,
            "sha256": hashlib.sha256(checksum).hexdigest(),
            "size": len(checksum),
            "url": checksum_url,
        },
        "expected_coordinates": [record["coordinate"]],
        "format": "ggbuild-snapshot-v1",
        "repository": REPOSITORY,
        "run": {
            "attempt": "1",
            "event": "push",
            "id": "1",
            "started_at": "2026-01-01T00:00:00Z",
            "workflow_ref": WORKFLOW_REF,
        },
        "source_commit": "b" * 40,
        "successful": [record],
        "tag": tag,
    }


def _assets(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    successful = cast("list[dict[str, Any]]", snapshot["successful"])
    files = cast("list[dict[str, Any]]", successful[0]["files"])
    result = {
        item["release_name"]: {
            "browser_download_url": item["url"],
            "name": item["release_name"],
            "size": item["size"],
        }
        for item in files
        if "release_name" in item
    }
    checksum = cast("dict[str, Any]", snapshot["checksum"])
    result[CHECKSUM_NAME] = {
        "browser_download_url": checksum["url"],
        "name": CHECKSUM_NAME,
        "size": checksum["size"],
    }
    result[SNAPSHOT_NAME] = {
        "browser_download_url": _url(SNAPSHOT_NAME),
        "name": SNAPSHOT_NAME,
        "size": len(canonical_json(snapshot)),
    }
    return result


def _checksum_bytes(snapshot: dict[str, object]) -> bytes:
    successful = cast("list[dict[str, Any]]", snapshot["successful"])
    files = [
        item
        for record in successful
        for item in cast("list[dict[str, Any]]", record["files"])
        if "release_name" in item
    ]
    return "".join(
        f"{item['sha256']}  {item['release_name']}\n"
        for item in sorted(files, key=operator.itemgetter("release_name"))
    ).encode()


class FakeResponse:
    def __init__(
        self, value: object, status_code: int = 200, content: bytes = b""
    ) -> None:
        self.value = value
        self.status_code = status_code
        self.content = content

    def json(self) -> object:
        return self.value

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))


def _jwk(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {
        "e": encode(numbers.e),
        "kid": kid,
        "kty": "RSA",
        "n": encode(numbers.n),
    }


def test_oidc_verification_caches_jwks_and_checks_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "test-key"
    claims = {
        "aud": "https://example.test/publication",
        "event_name": "push",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "iss": "https://token.actions.githubusercontent.com",
        "ref": "refs/heads/main",
        "repository": REPOSITORY,
        "repository_owner": POLICY.owner,
        "sub": POLICY.subject,
        "workflow_ref": WORKFLOW_REF,
    }
    token = jwt.encode(
        claims, private, algorithm="RS256", headers={"kid": kid}
    )

    class Session:
        calls = 0

        def get(self, _url: str, timeout: int) -> FakeResponse:
            assert timeout == 10
            self.calls += 1
            return FakeResponse({"keys": [_jwk(private.public_key(), kid)]})

    publication._jwks_cache.clear()
    session = Session()
    assert (
        verify_github_oidc(token, claims["aud"], POLICY, fetch=session)[
            "repository"
        ]
        == REPOSITORY
    )
    assert (
        verify_github_oidc(token, claims["aud"], POLICY, fetch=session)[
            "repository"
        ]
        == REPOSITORY
    )
    assert session.calls == 1

    invalid = {
        **claims,
        "workflow_ref": (
            f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/main"
        ),
    }
    bad_token = jwt.encode(
        invalid, private, algorithm="RS256", headers={"kid": kid}
    )
    with pytest.raises(ValueError, match="workflow_ref"):
        verify_github_oidc(bad_token, claims["aud"], POLICY, fetch=session)

    invalid = {
        **claims,
        "sub": f"repo:{REPOSITORY}:ref:refs/heads/main",
    }
    bad_token = jwt.encode(
        invalid, private, algorithm="RS256", headers={"kid": kid}
    )
    with pytest.raises(ValueError, match="release environment"):
        verify_github_oidc(bad_token, claims["aud"], POLICY, fetch=session)

    immutable = {
        **claims,
        "repository_owner_id": "123",
        "repository_id": "456",
        "sub": "repo:example@123/postgresbuild@456:environment:release",
    }
    immutable_token = jwt.encode(
        immutable, private, algorithm="RS256", headers={"kid": kid}
    )
    assert (
        verify_github_oidc(
            immutable_token, claims["aud"], POLICY, fetch=session
        )["sub"]
        == immutable["sub"]
    )

    immutable["repository_id"] = "457"
    mismatched_token = jwt.encode(
        immutable, private, algorithm="RS256", headers={"kid": kid}
    )
    with pytest.raises(ValueError, match="release environment"):
        verify_github_oidc(
            mismatched_token, claims["aud"], POLICY, fetch=session
        )


def test_unknown_oidc_key_refreshes_once() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "aud": "audience",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "iss": "https://token.actions.githubusercontent.com",
        "sub": "subject",
    }
    token = jwt.encode(
        claims, private, algorithm="RS256", headers={"kid": "missing"}
    )

    class Session:
        calls = 0

        def get(self, _url: str, timeout: int) -> FakeResponse:
            self.calls += 1
            return FakeResponse({"keys": []})

    publication._jwks_cache.clear()
    session = Session()
    with pytest.raises(ValueError, match="unknown key"):
        verify_github_oidc(token, "audience", POLICY, fetch=session)
    assert session.calls == 2


def test_github_client_downloads_private_release_assets_through_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_url = _url(SNAPSHOT_NAME)
    api_url = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/123"

    calls: list[tuple[str, dict[str, str] | None, int]] = []

    def get(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int,
    ) -> FakeResponse:
        calls.append((url, headers, timeout))
        if url.endswith(f"/releases/tags/{TAG}"):
            return FakeResponse(
                {
                    "assets": [
                        {
                            "browser_download_url": browser_url,
                            "url": api_url,
                        }
                    ]
                }
            )
        assert url == api_url
        return FakeResponse({}, content=b"private asset")

    session = requests.Session()
    monkeypatch.setattr(session, "get", get)
    client = publication.GitHubReleaseClient("token", session)
    assert client.release_by_tag(REPOSITORY, TAG) is not None
    assert client.get_bytes(browser_url) == b"private asset"
    assert session.headers["Accept"] == "application/vnd.github+json"
    assert session.headers["Authorization"] == "Bearer token"
    assert calls[1] == (
        api_url,
        {"Accept": "application/octet-stream"},
        30,
    )


def test_snapshot_validation_rejects_release_asset_mismatch() -> None:
    snapshot = _snapshot()
    assert (
        validate_snapshot(
            snapshot,
            policy=POLICY,
            tag=TAG,
            release_assets=_assets(snapshot),
        )
        == snapshot
    )
    assets = _assets(snapshot)
    assets["unexpected"] = {
        "browser_download_url": "https://example.test/unexpected",
        "name": "unexpected",
        "size": 1,
    }
    with pytest.raises(ValueError, match="not declared"):
        validate_snapshot(
            snapshot,
            policy=POLICY,
            tag=TAG,
            release_assets=assets,
        )


def test_snapshot_validation_rejects_incomplete_success() -> None:
    snapshot = _snapshot()
    expected = cast("list[dict[str, str]]", snapshot["expected_coordinates"])
    expected.append(
        {
            "package": "postgresql",
            "source_version": "16.5",
            "target": "target",
        }
    )
    expected.sort(
        key=operator.itemgetter("package", "source_version", "target")
    )
    with pytest.raises(ValueError, match="do not match expected"):
        validate_snapshot(
            snapshot,
            policy=POLICY,
            tag=TAG,
            release_assets=_assets(snapshot),
        )


def test_fetch_uses_exact_tag_and_ignores_non_public_releases() -> None:
    snapshot = _snapshot()
    assets = list(_assets(snapshot).values())

    class GitHub:
        def __init__(self, *, draft: bool = False) -> None:
            self.draft = draft
            self.lookups: list[tuple[str, str]] = []

        def release_by_tag(self, repository: str, tag: str) -> dict[str, Any]:
            self.lookups.append((repository, tag))
            return {
                "assets": assets,
                "draft": self.draft,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "name": TAG,
                "tag_name": TAG,
            }

        def get_bytes(self, url: str) -> bytes:
            if url == _url(SNAPSHOT_NAME):
                return canonical_json(snapshot)
            if url == _url(CHECKSUM_NAME):
                return _checksum_bytes(snapshot)
            if url.endswith("-dbgsym.tar.zst"):
                return b"symbols"
            return b"archive"

    github = GitHub()
    tag = TAG
    assert fetch_valid_snapshot(REPOSITORY, tag, github, POLICY) == (
        snapshot,
        "2026-01-01T00:00:00Z",
    )
    assert github.lookups == [(REPOSITORY, tag)]
    assert (
        fetch_valid_snapshot(REPOSITORY, tag, GitHub(draft=True), POLICY)
        is None
    )
    assert (
        fetch_valid_snapshot("fork/postgresbuild", tag, github, POLICY) is None
    )


@pytest.mark.parametrize("corrupt", [CHECKSUM_NAME, "distribution"])
def test_fetch_rejects_checksum_or_distribution_byte_mismatch(
    corrupt: str,
) -> None:
    snapshot = _snapshot()
    assets = list(_assets(snapshot).values())

    class GitHub:
        def release_by_tag(self, repository: str, tag: str) -> dict[str, Any]:
            return {
                "assets": assets,
                "draft": False,
                "name": tag,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "tag_name": tag,
            }

        def get_bytes(self, url: str) -> bytes:
            if url == _url(SNAPSHOT_NAME):
                return canonical_json(snapshot)
            if url == _url(CHECKSUM_NAME):
                return (
                    b"corrupt"
                    if corrupt == CHECKSUM_NAME
                    else _checksum_bytes(snapshot)
                )
            if corrupt == "distribution" and url.endswith(".tar.zst"):
                return b"corrupt"
            return (
                b"symbols" if url.endswith("-dbgsym.tar.zst") else b"archive"
            )

    with pytest.raises(ValueError, match=r"SHA256SUMS|distribution"):
        fetch_valid_snapshot(REPOSITORY, TAG, GitHub(), POLICY)


def test_fetch_publishes_verified_primary_artifact_only() -> None:
    snapshot = _snapshot()
    assets = list(_assets(snapshot).values())
    calls: list[tuple[str, str, bytes]] = []

    class GitHub:
        def release_by_tag(self, repository: str, tag: str) -> dict[str, Any]:
            return {
                "assets": assets,
                "draft": False,
                "name": tag,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "tag_name": tag,
            }

        def get_bytes(self, url: str) -> bytes:
            if url == _url(SNAPSHOT_NAME):
                return canonical_json(snapshot)
            if url == _url(CHECKSUM_NAME):
                return _checksum_bytes(snapshot)
            if url.endswith("-dbgsym.tar.zst"):
                return b"symbols"
            return b"archive"

    assert fetch_valid_snapshot(
        REPOSITORY,
        TAG,
        GitHub(),
        POLICY,
        lambda tag, name, content: calls.append((tag, name, content)),
    ) == (snapshot, "2026-01-01T00:00:00Z")
    assert calls == [
        (
            TAG,
            f"postgresql-17.1+{TAG}-target.tar.zst",
            b"archive",
        )
    ]


class MemoryBlob:
    def __init__(self, *, race_once: bool = False) -> None:
        self.values: dict[str, tuple[bytes, str]] = {}
        self.race_once = race_once
        self.cas_calls = 0

    def get(self, path: str) -> tuple[bytes, str] | None:
        return self.values.get(path)

    def put_immutable(self, path: str, content: bytes) -> None:
        existing = self.values.get(path)
        if existing is not None and existing[0] != content:
            raise ValueError("immutable Blob fragment conflicts")
        self.values.setdefault(path, (content, "fragment-etag"))

    def put_cas(self, path: str, content: bytes, etag: str | None) -> bool:
        self.cas_calls += 1
        current = self.values.get(path)
        if (current[1] if current else None) != etag:
            return False
        if self.race_once:
            self.race_once = False
            return False
        self.values[path] = (content, f"etag-{self.cas_calls}")
        return True

    def list_fragments(self) -> list[bytes]:
        return [
            content
            for path, (content, _etag) in self.values.items()
            if path.startswith("snapshots/")
        ]


def test_materialize_is_idempotent_retries_cas_and_rebuilds() -> None:
    store = MemoryBlob(race_once=True)
    tag = TAG
    first = materialize(
        store, _snapshot(tag), "2026-01-01T00:00:00Z", REPOSITORY
    )
    assert store.cas_calls == 2
    assert (
        validate_index(json.loads(store.values["index.json"][0]), REPOSITORY)
        == first
    )
    assert (
        materialize(store, _snapshot(tag), "2026-01-01T00:00:00Z", REPOSITORY)
        == first
    )
    assert store.cas_calls == 2
    del store.values["index.json"]
    assert (
        materialize(store, _snapshot(tag), "2026-01-01T00:00:00Z", REPOSITORY)
        == first
    )


def test_materialize_rejects_immutable_conflict_and_corrupt_index() -> None:
    store = MemoryBlob()
    tag = TAG
    materialize(store, _snapshot(tag), "2026-01-01T00:00:00Z", REPOSITORY)
    with pytest.raises(ValueError, match="immutable"):
        materialize(
            store,
            _snapshot(tag, "17.2"),
            "2026-01-02T00:00:00Z",
            REPOSITORY,
        )
    store.values["index.json"] = (b"{}", "corrupt")
    with pytest.raises(ValueError, match="malformed"):
        materialize(
            store,
            _snapshot("202601020000"),
            "2026-01-02T00:00:00Z",
            REPOSITORY,
        )
    assert store.values["index.json"] == (b"{}", "corrupt")


def test_projection_orders_history_and_supersedes_latest() -> None:
    first = {
        "format": "postgresbuild-snapshot-fragment-v1",
        "published_at": "2026-01-01T00:00:00Z",
        "repository": REPOSITORY,
        "source_commit": "a" * 40,
        "successful": [_record()],
        "tag": TAG,
    }
    second = {
        **first,
        "published_at": "2026-01-02T00:00:00Z",
        "source_commit": "b" * 40,
        "tag": "202601020000",
    }
    index = project_index([first, second])
    assert [item["tag"] for item in index["snapshots"]] == [
        "202601020000",
        TAG,
    ]
    assert index["latest"][0]["tag"] == "202601020000"


def test_public_index_reads_blob_without_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)
    index = project_index([])

    class Store:
        def get(self, path: str) -> tuple[bytes, str]:
            assert path == "index.json"
            return canonical_json(index), "etag"

        def artifact_url(self, tag: str, name: str) -> str:
            return f"https://blob.test/{tag}/{name}"

    monkeypatch.setattr(main, "VercelBlobStore", Store)
    response = TestClient(main.app).get("/index.json")
    assert response.status_code == 200
    assert response.json() == index
    assert response.headers["cache-control"] == (
        "public, s-maxage=300, stale-while-revalidate=3600"
    )


def test_versions_ndjson_groups_orders_and_advertises_primary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "format": "postgresbuild-snapshot-fragment-v1",
        "published_at": "2026-01-02T00:00:00Z",
        "repository": REPOSITORY,
        "source_commit": "a" * 40,
        "successful": [
            _record("16.9", "z-target", "202601020000"),
            _record("17.1", "b-target", "202601020000"),
            _record("17.1", "a-target", "202601020000"),
        ],
        "tag": "202601020000",
    }
    second = {
        "format": "postgresbuild-snapshot-fragment-v1",
        "published_at": "2026-01-01T00:00:00Z",
        "repository": REPOSITORY,
        "source_commit": "b" * 40,
        "successful": [_record("18.2")],
        "tag": TAG,
    }
    index = project_index([second, first])
    records = [
        json.loads(line) for line in versions_ndjson(index).splitlines()
    ]
    assert [item["version"] for item in records] == [
        "17.1+202601020000",
        "16.9+202601020000",
        f"18.2+{TAG}",
    ]
    assert [item["platform"] for item in records[0]["artifacts"]] == [
        "a-target",
        "b-target",
    ]
    assert all(
        artifact["variant"] == "install_only"
        and artifact["archive_format"] == "tar.zst"
        and all("dbgsym" not in url for url in artifact["urls"])
        for record in records
        for artifact in record["artifacts"]
    )
    artifact = records[0]["artifacts"][0]
    filename = "postgresql-17.1+202601020000-a-target.tar.zst"
    assert artifact["urls"] == [
        (
            "https://postgresbuild.labs.vercel.dev/artifacts/"
            "202601020000/"
            "postgresql-17.1%2B202601020000-a-target.tar.zst"
        ),
        (
            "https://postgresbuild.labs.vercel.dev/artifact-fallback/"
            f"202601020000/{filename.replace('+', '%2B')}"
        ),
    ]
    assert "url" not in artifact
    assert "fallback_urls" not in artifact

    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)

    class Store:
        def get(self, path: str) -> tuple[bytes, str]:
            assert path == "index.json"
            return canonical_json(index), "etag"

    monkeypatch.setattr(main, "VercelBlobStore", Store)
    response = TestClient(main.app).get("/versions.ndjson")
    assert response.status_code == 200
    assert response.content == versions_ndjson(index)
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == (
        "public, s-maxage=300, stale-while-revalidate=3600"
    )


def test_consumer_artifact_url_encodes_tag_and_filename_segments() -> None:
    assert publication._consumer_artifact_url(
        "artifacts",
        "tag+candidate/1",
        "postgresql 18+build/file?.tar.zst",
    ) == (
        "https://postgresbuild.labs.vercel.dev/artifacts/"
        "tag%2Bcandidate%2F1/postgresql%2018%2Bbuild%2Ffile%3F.tar.zst"
    )


def test_publication_route_authenticates_and_materializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)
    snapshot = _snapshot()
    calls: list[object] = []

    class GitHub:
        def __init__(self, token: str) -> None:
            calls.append(("release-token", token))

    monkeypatch.setattr(
        main,
        "verify_github_oidc",
        lambda token, audience, policy: calls.append(
            (token, audience, policy.repository)
        ),
    )
    monkeypatch.setattr(main, "GitHubReleaseClient", GitHub)

    class Store:
        def publish_artifact(
            self, tag: str, name: str, content: bytes
        ) -> None:
            calls.append(("publish", tag, name, content))

    monkeypatch.setattr(main, "VercelBlobStore", Store)
    monkeypatch.setattr(
        main,
        "fetch_valid_snapshot",
        lambda repository, tag, github, policy, publish: (
            calls.append("mirror") or (snapshot, "2026-01-01T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        main,
        "materialize",
        lambda store, value, published_at, repository: {
            "format": "postgresbuild-index-v1",
            "latest": [],
            "snapshots": [],
        },
    )
    response = TestClient(main.app).post(
        "/api/publication",
        headers={
            "authorization": "Bearer github-oidc-token",
            "x-github-token": "github-release-token",
        },
        json={"repository": REPOSITORY, "tag": "build-1"},
    )
    assert response.status_code == 200
    assert calls == [
        (
            "github-oidc-token",
            "https://postgresbuild.labs.vercel.dev/api/publication",
            REPOSITORY,
        ),
        ("release-token", "github-release-token"),
        "mirror",
    ]


def test_publication_route_requires_release_token_after_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)
    monkeypatch.setattr(main, "verify_github_oidc", lambda *_args: None)
    response = TestClient(main.app).post(
        "/api/publication",
        headers={"authorization": "Bearer github-oidc-token"},
        json={"repository": REPOSITORY, "tag": TAG},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "missing GitHub release token"}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ValueError("GitHub OIDC claim workflow_ref is not authorized"),
            "workflow_ref",
        ),
        (
            ValueError("GitHub OIDC subject is not the release environment"),
            "subject",
        ),
        (RuntimeError("sensitive detail"), "RuntimeError"),
    ],
)
def test_publication_route_reports_safe_oidc_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason: str,
) -> None:
    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)

    def reject(*_args: object) -> None:
        raise error

    monkeypatch.setattr(main, "verify_github_oidc", reject)
    response = TestClient(main.app).post(
        "/api/publication",
        headers={"authorization": "Bearer github-token"},
        json={"repository": REPOSITORY, "tag": TAG},
    )
    assert response.status_code == 401
    assert response.json() == {
        "detail": f"unauthorized GitHub workflow: {reason}"
    }
    assert "sensitive detail" not in response.text


def test_publication_route_rejects_incomplete_snapshot_without_blob_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLICATION_REPOSITORY", REPOSITORY)
    snapshot = _snapshot()
    expected = cast("list[dict[str, str]]", snapshot["expected_coordinates"])
    expected.append(
        {
            "package": "postgresql",
            "source_version": "16.5",
            "target": "target",
        }
    )
    expected.sort(
        key=operator.itemgetter("package", "source_version", "target")
    )
    assets = list(_assets(snapshot).values())

    class GitHub:
        def __init__(self, _token: str) -> None:
            pass

        def release_by_tag(self, repository: str, tag: str) -> dict[str, Any]:
            assert repository == REPOSITORY
            return {
                "assets": assets,
                "draft": False,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "name": tag,
                "tag_name": tag,
            }

        def get_bytes(self, url: str) -> bytes:
            if url == _url(SNAPSHOT_NAME):
                return canonical_json(snapshot)
            if url == _url(CHECKSUM_NAME):
                return _checksum_bytes(snapshot)
            if url.endswith("-dbgsym.tar.zst"):
                return b"symbols"
            return b"archive"

    blob_created = False

    def blob_store() -> object:
        nonlocal blob_created
        blob_created = True
        return object()

    monkeypatch.setattr(main, "verify_github_oidc", lambda *args: None)
    monkeypatch.setattr(main, "GitHubReleaseClient", GitHub)
    monkeypatch.setattr(main, "VercelBlobStore", blob_store)
    response = TestClient(main.app).post(
        "/api/publication",
        headers={
            "authorization": "Bearer github-token",
            "x-github-token": "github-release-token",
        },
        json={
            "repository": REPOSITORY,
            "tag": TAG,
        },
    )
    assert response.status_code == 400
    assert "do not match expected" in response.json()["detail"]
    assert not blob_created
