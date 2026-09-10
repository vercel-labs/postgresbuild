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


def test_public_blob_read_uses_strong_control_plane_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    calls: list[tuple[str, str]] = []
    metadata_calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(url: str, *, token: str) -> SimpleNamespace:
        calls.append((url, token))
        return SimpleNamespace(content=b"index", etag='W/"etag"')

    def request_get(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
    ) -> SimpleNamespace:
        assert timeout == 30
        metadata_calls.append((url, params, headers))
        return SimpleNamespace(
            json=lambda: {"etag": '"etag"'},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(blob, "get", get)
    monkeypatch.setattr(blob.requests, "get", request_get)

    assert blob.VercelBlobStore().get("index.json") == (
        b"index",
        '"etag"',
    )
    parsed = urlparse(calls[0][0])
    assert parsed.path == "/index.json"
    assert len(parse_qs(parsed.query)["v"][0]) == 32
    assert calls[0][1] == "managed-token"
    assert metadata_calls[0][0] == "https://vercel.com/api/blob"
    assert metadata_calls[0][1] == {
        "url": "https://example.public.blob.vercel-storage.com/index.json"
    }
    assert metadata_calls[0][2]["x-api-version"] == "12"
    assert metadata_calls[0][2]["x-vercel-blob-store-id"] == "example"


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
