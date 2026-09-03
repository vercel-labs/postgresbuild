"""Canonical GitHub snapshot validation and materialized index projection."""

from __future__ import annotations

import hashlib
import json
import operator
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import jwt
import jwt.algorithms
import requests

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS = "https://token.actions.githubusercontent.com/.well-known/jwks"
SNAPSHOT_NAME = "ggbuild-snapshot-v1.json"
CHECKSUM_NAME = "SHA256SUMS"
SNAPSHOT_TAG = re.compile(r"\d{12}")
_JWKS_TTL = 600.0
_jwks_cache: list[tuple[float, dict[str, Any]]] = []
_jwks_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    repository: str
    ref: str = "refs/heads/main"
    workflow_path: str = ".github/workflows/postgresbuild.yml"
    environment: str = "release"

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> PublicationPolicy:
        values = os.environ if environment is None else environment
        repository = values.get("PUBLICATION_REPOSITORY", "")
        if repository.count("/") != 1 or any(
            not part for part in repository.split("/")
        ):
            raise ValueError(
                "PUBLICATION_REPOSITORY must use non-empty owner/name form"
            )
        ref = values.get("PUBLICATION_REF", "refs/heads/main")
        workflow_path = values.get(
            "PUBLICATION_WORKFLOW_PATH",
            ".github/workflows/postgresbuild.yml",
        )
        release_environment = values.get("PUBLICATION_ENVIRONMENT", "release")
        if not ref or not workflow_path or not release_environment:
            raise ValueError("publication policy values must not be empty")
        return cls(repository, ref, workflow_path, release_environment)

    @property
    def owner(self) -> str:
        return self.repository.partition("/")[0]

    @property
    def workflow_ref(self) -> str:
        return f"{self.repository}/{self.workflow_path}@{self.ref}"

    @property
    def subject(self) -> str:
        return f"repo:{self.repository}:environment:{self.environment}"

    def immutable_subject(self, claims: Mapping[str, Any]) -> str | None:
        owner_id = claims.get("repository_owner_id")
        repository_id = claims.get("repository_id")
        if not all(
            isinstance(value, str) and value.isdecimal()
            for value in (owner_id, repository_id)
        ):
            return None
        owner, _, repository = self.repository.partition("/")
        return (
            f"repo:{owner}@{owner_id}/{repository}@{repository_id}:"
            f"environment:{self.environment}"
        )


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


class HttpGetter(Protocol):
    def get(self, url: str, /, *, timeout: int) -> Any: ...


def _jwks(fetch: HttpGetter, *, force: bool = False) -> dict[str, Any]:
    with _jwks_lock:
        if (
            not force
            and _jwks_cache
            and time.monotonic() - _jwks_cache[0][0] < _JWKS_TTL
        ):
            return _jwks_cache[0][1]
        response = fetch.get(GITHUB_JWKS, timeout=10)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not isinstance(
            value.get("keys"), list
        ):
            raise TypeError("GitHub JWKS is malformed")
        _jwks_cache[:] = [(time.monotonic(), cast("dict[str, Any]", value))]
        return _jwks_cache[0][1]


def verify_github_oidc(
    token: str,
    audience: str,
    policy: PublicationPolicy,
    *,
    fetch: HttpGetter | None = None,
) -> dict[str, Any]:
    session = fetch or requests.Session()
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("GitHub OIDC header is not RS256 with a key ID")

    def select(document: Mapping[str, Any]) -> RSAPublicKey | None:
        return next(
            (
                cast(
                    "RSAPublicKey",
                    jwt.algorithms.RSAAlgorithm.from_jwk(item),
                )
                for item in document["keys"]
                if isinstance(item, dict)
                and item.get("kid") == header["kid"]
                and item.get("kty") == "RSA"
            ),
            None,
        )

    key = select(_jwks(session))
    if key is None:
        key = select(_jwks(session, force=True))
    if key is None:
        raise ValueError("GitHub OIDC token uses an unknown key")
    claims = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        audience=audience,
        issuer=GITHUB_ISSUER,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
    expected = {
        "repository": policy.repository,
        "ref": policy.ref,
        "workflow_ref": policy.workflow_ref,
    }
    for name, value in expected.items():
        if claims.get(name) != value:
            raise ValueError(f"GitHub OIDC claim {name} is not authorized")
    if claims.get("event_name") not in {"push", "workflow_dispatch"}:
        raise ValueError("GitHub OIDC event is not authorized")
    if claims.get("repository_owner") != policy.owner:
        raise ValueError("GitHub OIDC repository owner is not authorized")
    if claims.get("sub") not in {
        policy.subject,
        policy.immutable_subject(claims),
    }:
        raise ValueError("GitHub OIDC subject is not the release environment")
    return claims


class GitHubReleaseClient:
    def __init__(
        self, token: str = "", session: requests.Session | None = None
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        self._asset_api_urls: dict[str, str] = {}
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def release_by_tag(
        self, repository: str, tag: str
    ) -> dict[str, Any] | None:
        url = (
            f"https://api.github.com/repos/{repository}/releases/tags/"
            f"{urllib.parse.quote(tag, safe='')}"
        )
        response = self.session.get(url, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        release = cast("dict[str, Any]", response.json())
        for asset in release.get("assets", []):
            if not isinstance(asset, dict):
                continue
            browser_url = asset.get("browser_download_url")
            api_url = asset.get("url")
            if isinstance(browser_url, str) and isinstance(api_url, str):
                self._asset_api_urls[browser_url] = api_url
        return release

    def get_bytes(self, url: str) -> bytes:
        response = self.session.get(
            self._asset_api_urls.get(url, url),
            headers={"Accept": "application/octet-stream"},
            timeout=30,
        )
        response.raise_for_status()
        return bytes(response.content)


class ReleaseReader(Protocol):
    def release_by_tag(
        self, repository: str, tag: str
    ) -> dict[str, Any] | None: ...

    def get_bytes(self, url: str) -> bytes: ...


class ArtifactPublisher(Protocol):
    def __call__(self, tag: str, name: str, content: bytes) -> None: ...


_FILE_ROLES = {
    "primary-archive",
    "dbgsym",
    "test-data",
    "metadata",
    "test-result",
}
_PUBLIC_ROLES = {"primary-archive", "dbgsym"}


def _coordinate(value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise TypeError("snapshot coordinate is invalid")
    result = (
        value.get("package"),
        value.get("source_version"),
        value.get("target"),
    )
    if not all(isinstance(item, str) and item for item in result):
        raise ValueError("snapshot coordinate fields are invalid")
    return cast("tuple[str, str, str]", result)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _release_url(repository: str, tag: str, name: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}"
    )


def _public_name(coordinate: tuple[str, str, str], role: str, tag: str) -> str:
    package, version, target = coordinate
    stem = f"{package}-{version}+{tag}-{target}"
    return f"{stem}{'-dbgsym' if role == 'dbgsym' else ''}.tar.zst"


def _validate_record(
    record: object, *, repository: str = "", tag: str = ""
) -> tuple[tuple[str, str, str], set[str]]:
    if not isinstance(record, dict) or not isinstance(
        record.get("files"), list
    ):
        raise TypeError("snapshot successful record is invalid")
    coordinate = _coordinate(record.get("coordinate"))
    if coordinate[0] != "postgresql":
        raise ValueError("snapshot coordinate is not PostgreSQL")
    names: set[str] = set()
    logical_names: set[str] = set()
    logical_order: list[str] = []
    roles: list[str] = []
    for item in record["files"]:
        if not isinstance(item, dict):
            raise TypeError("snapshot file is invalid")
        logical_name = item.get("logical_name")
        role = item.get("role")
        if (
            not isinstance(logical_name, str)
            or not logical_name
            or logical_name in logical_names
            or logical_name.endswith(".debug.tar.zst")
            or role not in _FILE_ROLES
            or not _digest(item.get("sha256"))
            or not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not isinstance(item.get("media_type"), str)
            or not isinstance(item.get("encoding"), str)
        ):
            raise ValueError("snapshot file metadata is invalid")
        logical_names.add(logical_name)
        logical_order.append(logical_name)
        if role in _PUBLIC_ROLES:
            name = item.get("release_name")
            if (
                item.get("media_type") != "application/x-tar"
                or item.get("encoding") != "zstd"
                or not isinstance(name, str)
                or not name
                or name in names
                or name != _public_name(coordinate, role, tag)
                or item.get("url") != _release_url(repository, tag, name)
            ):
                raise ValueError(
                    "snapshot public filename or URL is not canonical"
                )
            names.add(name)
        elif "release_name" in item or "url" in item:
            raise ValueError("snapshot evidence file must remain private")
        roles.append(role)
    if roles.count("primary-archive") != 1:
        raise ValueError("snapshot coordinate needs one primary archive")
    if roles.count("dbgsym") != 1:
        raise ValueError("snapshot coordinate needs one dbgsym archive")
    if roles.count("metadata") != 1 or roles.count("test-result") != 1:
        raise ValueError("snapshot coordinate metadata is incomplete")
    if logical_order != sorted(logical_order):
        raise ValueError("snapshot files are not canonical")
    primary = next(
        item for item in record["files"] if item["role"] == "primary-archive"
    )
    result = record.get("test_result")
    if not isinstance(result, dict) or not isinstance(
        result.get("recipe"), str
    ):
        raise TypeError(
            "snapshot test result is not bound to its primary archive"
        )
    if result != {
        "artifact_sha256": primary["sha256"],
        "recipe": result["recipe"],
        "target": coordinate[2],
        "version": coordinate[1],
    }:
        raise ValueError(
            "snapshot test result is not bound to its primary archive"
        )
    return coordinate, names


def validate_snapshot(
    value: object,
    *,
    policy: PublicationPolicy,
    tag: str,
    release_assets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("format") != "ggbuild-snapshot-v1"
    ):
        raise ValueError("snapshot has the wrong format")
    snapshot = cast("dict[str, Any]", value)
    if (
        snapshot.get("repository") != policy.repository
        or snapshot.get("tag") != tag
    ):
        raise ValueError("snapshot repository or tag does not match release")
    source_commit = snapshot.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(
            character not in "0123456789abcdef" for character in source_commit
        )
    ):
        raise TypeError("snapshot source commit is invalid")
    successful = snapshot.get("successful")
    expected = snapshot.get("expected_coordinates")
    if not isinstance(successful, list) or not isinstance(expected, list):
        raise TypeError("snapshot coordinate sets are invalid")
    expected_coordinates = [_coordinate(item) for item in expected]
    if expected_coordinates != sorted(set(expected_coordinates)):
        raise ValueError("snapshot expected coordinates are not canonical")
    coordinates: set[tuple[str, str, str]] = set()
    ordered_coordinates: list[tuple[str, str, str]] = []
    release_names: set[str] = set()
    for record in successful:
        key, names = _validate_record(
            record, repository=policy.repository, tag=tag
        )
        if key in coordinates or key not in expected_coordinates:
            raise ValueError("snapshot contains a duplicate coordinate")
        coordinates.add(key)
        ordered_coordinates.append(key)
        assert isinstance(record, dict)
        for name in names:
            asset = release_assets.get(name)
            item = next(
                item
                for item in record["files"]
                if item.get("release_name") == name
            )
            if (
                asset is None
                or name in release_names
                or item.get("url") != asset.get("browser_download_url")
                or item.get("size") != asset.get("size")
            ):
                raise ValueError(
                    "snapshot file does not match its release asset"
                )
            release_names.add(name)
    if ordered_coordinates != sorted(ordered_coordinates):
        raise ValueError("snapshot successful records are not canonical")
    if coordinates != set(expected_coordinates):
        raise ValueError(
            "snapshot successful coordinates do not match expected coordinates"
        )
    run = snapshot.get("run")
    if (
        not isinstance(run, dict)
        or run.get("event") not in {"push", "workflow_dispatch"}
        or run.get("workflow_ref") != policy.workflow_ref
        or not isinstance(run.get("id"), str)
        or not run["id"].isdigit()
        or not isinstance(run.get("attempt"), str)
        or not run["attempt"].isdigit()
        or not isinstance(run.get("started_at"), str)
    ):
        raise ValueError("snapshot workflow identity is invalid")
    try:
        started = datetime.fromisoformat(run["started_at"])
    except ValueError as error:
        raise ValueError("snapshot run start time is invalid") from error
    if started.tzinfo is None:
        raise ValueError("snapshot run start time has no timezone")
    expected_tag = f"{started.astimezone(UTC):%Y%m%d%H%M}"
    if tag != expected_tag:
        raise ValueError("snapshot tag does not match its workflow run")
    checksum = snapshot.get("checksum")
    checksum_asset = release_assets.get(CHECKSUM_NAME)
    if (
        not isinstance(checksum, dict)
        or checksum.get("release_name") != CHECKSUM_NAME
        or checksum.get("url")
        != _release_url(policy.repository, tag, CHECKSUM_NAME)
        or checksum.get("media_type") != "text/plain"
        or not _digest(checksum.get("sha256"))
        or not isinstance(checksum.get("size"), int)
        or checksum["size"] < 0
        or checksum_asset is None
        or checksum_asset.get("browser_download_url") != checksum["url"]
        or checksum_asset.get("size") != checksum["size"]
    ):
        raise ValueError("snapshot checksum descriptor is invalid")
    if set(release_assets) != release_names | {CHECKSUM_NAME, SNAPSHOT_NAME}:
        raise ValueError("release has assets not declared by its snapshot")
    return snapshot


def fetch_valid_snapshot(
    repository: str,
    tag: str,
    github: ReleaseReader,
    policy: PublicationPolicy,
    publish_artifact: ArtifactPublisher | None = None,
) -> tuple[dict[str, Any], str] | None:
    if repository != policy.repository or SNAPSHOT_TAG.fullmatch(tag) is None:
        return None
    release = github.release_by_tag(repository, tag)
    if (
        release is None
        or release.get("tag_name") != tag
        or release.get("name") != tag
        or release.get("draft")
        or release.get("prerelease")
    ):
        return None
    assets: dict[str, dict[str, Any]] = {}
    for asset in release.get("assets", []):
        if not isinstance(asset, dict) or not isinstance(
            asset.get("name"), str
        ):
            raise TypeError("matching release has a malformed asset")
        if asset["name"] in assets:
            raise ValueError("matching release has duplicate asset names")
        assets[asset["name"]] = asset
    manifest = assets.get(SNAPSHOT_NAME)
    if manifest is None or not isinstance(
        manifest.get("browser_download_url"), str
    ):
        raise ValueError("matching release has no snapshot manifest")
    if manifest["browser_download_url"] != _release_url(
        repository, tag, SNAPSHOT_NAME
    ):
        raise ValueError("snapshot manifest URL is not canonical")
    raw = github.get_bytes(manifest["browser_download_url"])
    if manifest.get("size") != len(raw):
        raise ValueError("snapshot manifest size does not match release asset")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("matching release snapshot is malformed") from error
    snapshot = validate_snapshot(
        value, policy=policy, tag=tag, release_assets=assets
    )
    public_items = sorted(
        (
            item
            for record in snapshot["successful"]
            for item in record["files"]
            if item["role"] in _PUBLIC_ROLES
        ),
        key=operator.itemgetter("release_name"),
    )
    expected_checksums = "".join(
        f"{item['sha256']}  {item['release_name']}\n" for item in public_items
    ).encode()
    checksum = snapshot["checksum"]
    checksum_bytes = github.get_bytes(checksum["url"])
    if (
        len(checksum_bytes) != checksum["size"]
        or hashlib.sha256(checksum_bytes).hexdigest() != checksum["sha256"]
        or checksum_bytes != expected_checksums
    ):
        raise ValueError(
            "SHA256SUMS content does not match snapshot distributions"
        )
    for item in public_items:
        content = github.get_bytes(item["url"])
        if (
            len(content) != item["size"]
            or hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise ValueError(
                "release distribution does not match snapshot: "
                f"{item['release_name']}"
            )
        if publish_artifact is not None and item["role"] == "primary-archive":
            publish_artifact(tag, item["release_name"], content)
    published_at = release.get("published_at")
    if not isinstance(published_at, str):
        raise TypeError("published release has no publication time")
    return snapshot, published_at


def fragment(snapshot: Mapping[str, Any], published_at: str) -> dict[str, Any]:
    return {
        "format": "postgresbuild-snapshot-fragment-v1",
        "published_at": published_at,
        "repository": snapshot["repository"],
        "source_commit": snapshot["source_commit"],
        "successful": snapshot["successful"],
        "tag": snapshot["tag"],
    }


def validate_fragment(value: object, repository: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("format") != "postgresbuild-snapshot-fragment-v1"
        or not isinstance(value.get("tag"), str)
        or not isinstance(value.get("published_at"), str)
        or value.get("repository") != repository
        or not isinstance(value.get("source_commit"), str)
        or not isinstance(value.get("successful"), list)
    ):
        raise ValueError("snapshot fragment is malformed")
    fragment_value = cast("dict[str, Any]", value)
    if SNAPSHOT_TAG.fullmatch(fragment_value["tag"]) is None:
        raise ValueError("snapshot fragment tag is malformed")
    try:
        datetime.strptime(fragment_value["tag"], "%Y%m%d%H%M").replace(
            tzinfo=UTC
        )
    except ValueError as error:
        raise ValueError(
            "snapshot fragment tag is not a UTC minute"
        ) from error
    try:
        published_at = datetime.fromisoformat(fragment_value["published_at"])
    except ValueError as error:
        raise ValueError(
            "snapshot fragment publication time is malformed"
        ) from error
    if published_at.tzinfo is None:
        raise ValueError("snapshot fragment publication time has no timezone")
    source_commit = fragment_value["source_commit"]
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise ValueError("snapshot fragment source commit is malformed")
    coordinates: list[tuple[str, str, str]] = []
    for record in fragment_value["successful"]:
        coordinate, _ = _validate_record(
            record,
            repository=fragment_value["repository"],
            tag=fragment_value["tag"],
        )
        coordinates.append(coordinate)
    if coordinates != sorted(set(coordinates)):
        raise ValueError("snapshot fragment records are not canonical")
    return fragment_value


def project_index(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        fragments,
        key=operator.itemgetter("published_at", "tag"),
        reverse=True,
    )
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    snapshots = []
    for item in ordered:
        records = sorted(
            item["successful"],
            key=lambda record: (
                record["coordinate"]["package"],
                record["coordinate"]["source_version"],
                record["coordinate"]["target"],
            ),
        )
        snapshots.append(
            {
                "published_at": item["published_at"],
                "source_commit": item["source_commit"],
                "successful": records,
                "tag": item["tag"],
            }
        )
        for record in records:
            coordinate = record["coordinate"]
            key = (
                coordinate["package"],
                coordinate["source_version"],
                coordinate["target"],
            )
            latest.setdefault(key, {**record, "tag": item["tag"]})
    return {
        "format": "postgresbuild-index-v1",
        "latest": [latest[key] for key in sorted(latest)],
        "snapshots": snapshots,
    }


def validate_index(value: object, repository: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("format") != "postgresbuild-index-v1"
    ):
        raise ValueError("stored index is malformed")
    if not isinstance(value.get("snapshots"), list) or not isinstance(
        value.get("latest"), list
    ):
        raise TypeError("stored index collections are malformed")
    fragments = [
        {
            "format": "postgresbuild-snapshot-fragment-v1",
            "published_at": item["published_at"],
            "source_commit": item["source_commit"],
            "successful": item["successful"],
            "tag": item["tag"],
        }
        for item in value["snapshots"]
    ]
    if len({item["tag"] for item in fragments}) != len(fragments):
        raise ValueError("stored index contains duplicate snapshot tags")
    for fragment_value in fragments:
        validate_fragment(
            {**fragment_value, "repository": repository}, repository
        )
    projected = project_index(fragments)
    if projected != value:
        raise ValueError("stored index is not canonical")
    return cast("dict[str, Any]", value)


def _consumer_artifact_url(route: str, tag: str, name: str) -> str:
    return (
        f"https://postgresbuild.labs.vercel.dev/{route}/"
        f"{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}"
    )


def versions_ndjson(index: Mapping[str, Any]) -> bytes:
    """Project the validated v1 index into the PBS-compatible version feed."""
    lines: list[str] = []
    for snapshot in index["snapshots"]:
        by_version: dict[str, list[dict[str, Any]]] = {}
        for record in snapshot["successful"]:
            coordinate = record["coordinate"]
            if coordinate["package"] != "postgresql":
                continue
            primary = next(
                item
                for item in record["files"]
                if item["role"] == "primary-archive"
            )
            by_version.setdefault(coordinate["source_version"], []).append(
                {
                    "archive_format": "tar.zst",
                    "platform": coordinate["target"],
                    "sha256": primary["sha256"],
                    "urls": [
                        _consumer_artifact_url(
                            "artifacts",
                            snapshot["tag"],
                            primary["release_name"],
                        ),
                        _consumer_artifact_url(
                            "artifact-fallback",
                            snapshot["tag"],
                            primary["release_name"],
                        ),
                    ],
                    "variant": "install_only",
                }
            )

        def version_key(version: str) -> tuple[int, ...]:
            try:
                return tuple(int(part) for part in version.split("."))
            except ValueError as error:
                raise ValueError(
                    "index contains a non-numeric PostgreSQL version"
                ) from error

        for version in sorted(by_version, key=version_key, reverse=True):
            artifacts = sorted(
                by_version[version], key=operator.itemgetter("platform")
            )
            lines.append(
                json.dumps(
                    {
                        "artifacts": artifacts,
                        "date": snapshot["published_at"],
                        "version": f"{version}+{snapshot['tag']}",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


class BlobStore(Protocol):
    def get(self, path: str) -> tuple[bytes, str] | None: ...

    def put_immutable(self, path: str, content: bytes) -> None: ...

    def put_cas(self, path: str, content: bytes, etag: str | None) -> bool: ...

    def list_fragments(self) -> list[bytes]: ...


def materialize(
    store: BlobStore,
    snapshot: Mapping[str, Any],
    published_at: str,
    repository: str,
) -> dict[str, Any]:
    item = validate_fragment(fragment(snapshot, published_at), repository)
    payload = canonical_json(item)
    store.put_immutable(f"snapshots/{item['tag']}.json", payload)
    for _ in range(8):
        existing = store.get("index.json")
        if existing is None:
            fragments = [
                validate_fragment(json.loads(raw), repository)
                for raw in store.list_fragments()
            ]
            etag = None
        else:
            index = validate_index(json.loads(existing[0]), repository)
            fragments = [
                validate_fragment(
                    {
                        "format": "postgresbuild-snapshot-fragment-v1",
                        **snapshot,
                        "repository": repository,
                    },
                    repository,
                )
                for snapshot in index["snapshots"]
            ]
            etag = existing[1]
        by_tag = {
            fragment_value["tag"]: fragment_value
            for fragment_value in fragments
        }
        if (
            item["tag"] in by_tag
            and canonical_json(by_tag[item["tag"]]) != payload
        ):
            raise ValueError("immutable snapshot fragment conflicts")
        by_tag[item["tag"]] = item
        index = project_index(list(by_tag.values()))
        content = canonical_json(index)
        if existing is not None and existing[0] == content:
            return index
        if store.put_cas("index.json", content, etag):
            return index
    raise RuntimeError("Blob index compare-and-swap did not converge")
