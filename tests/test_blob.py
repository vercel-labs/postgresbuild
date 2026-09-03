from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from vercel.blob.errors import BlobNotFoundError

from postgresbuild import blob

if TYPE_CHECKING:
    import pytest


def test_blob_store_uses_managed_token_when_oidc_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "managed-token"
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", credential)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    assert blob.VercelBlobStore().token == credential


def test_blob_store_prefers_ambient_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "oidc-token"
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", credential)

    assert blob.VercelBlobStore().token == credential


def test_public_blob_read_busts_cache_without_private_origin_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    calls: list[tuple[str, str]] = []

    def get(url: str, *, token: str) -> SimpleNamespace:
        calls.append((url, token))
        return SimpleNamespace(content=b"index", etag="etag")

    monkeypatch.setattr(blob, "get", get)

    assert blob.VercelBlobStore().get("index.json") == (b"index", "etag")
    parsed = urlparse(calls[0][0])
    assert parsed.path == "/index.json"
    assert len(parse_qs(parsed.query)["v"][0]) == 32
    assert calls[0][1] == "managed-token"


def test_public_blob_read_maps_missing_object_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    def get(url: str, *, token: str) -> None:
        raise BlobNotFoundError

    monkeypatch.setattr(blob, "get", get)

    assert blob.VercelBlobStore().get("index.json") is None


def test_artifact_url_is_public_and_encodes_release_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    url = blob.VercelBlobStore().artifact_url(
        "202601010000", "postgresql-18.4+build-aarch64.tar.zst"
    )
    assert url == (
        "https://example.public.blob.vercel-storage.com/releases/"
        "202601010000/postgresql-18.4%2Bbuild-aarch64.tar.zst"
    )
