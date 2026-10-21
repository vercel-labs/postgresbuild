from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from vercel.api import session
from vercel.blob.errors import BlobNotFoundError

from postgresbuild import blob


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

    def get_blob(url: str, *, access: str) -> SimpleNamespace:
        calls.append((url, access))
        return SimpleNamespace(body=b"index")

    def request_get(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
        follow_redirects: bool,
    ) -> SimpleNamespace:
        assert timeout == 30
        assert follow_redirects
        metadata_calls.append((url, params, headers))
        return SimpleNamespace(
            json=lambda: {"etag": '"etag"'},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(blob, "get_blob", get_blob)
    monkeypatch.setattr(blob.httpx2, "get", request_get)

    assert blob.VercelBlobStore().get("index.json") == (
        b"index",
        '"etag"',
    )
    parsed = urlparse(calls[0][0])
    assert parsed.path == "/index.json"
    assert len(parse_qs(parsed.query)["v"][0]) == 32
    assert calls[0][1] == "public"
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

    def get_blob(url: str, *, access: str) -> None:
        raise BlobNotFoundError

    monkeypatch.setattr(blob, "get_blob", get_blob)

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


@pytest.mark.parametrize("status", [200, 404])
def test_public_blob_download_uses_current_sync_sdk(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "example.public.blob.vercel-storage.com"
        assert request.url.path == "/index.json"
        assert len(request.url.params["v"]) == 32
        return httpx2.Response(status, content=b"index")

    client = httpx2.Client(transport=httpx2.MockTransport(respond))
    store = blob.VercelBlobStore()
    monkeypatch.setattr(store, "_strong_etag", lambda _: '"etag"')
    with session(httpx_client_factory=lambda: client):
        assert store.get("index.json") == (
            (b"index", '"etag"') if status == 200 else None
        )
    assert client.is_closed


@pytest.mark.parametrize("status", [201, 409, 412])
def test_conditional_blob_upload_preserves_bytes_and_preconditions(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    body = b"\x00\xffbinary archive\r\n"

    def put(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        content: bytes,
        timeout: int,
        follow_redirects: bool,
    ) -> httpx2.Response:
        assert url == "https://vercel.com/api/blob"
        assert params == {"pathname": "index.json"}
        assert headers["x-if-match"] == '"etag"'
        assert headers["x-allow-overwrite"] == "1"
        assert headers["authorization"] == "Bearer managed-token"
        assert content == body
        assert timeout == 30
        assert follow_redirects
        return httpx2.Response(status, request=httpx2.Request("PUT", url))

    monkeypatch.setattr(blob.httpx2, "put", put)
    assert blob.VercelBlobStore().put_cas("index.json", body, '"etag"') == (
        status == 201
    )


def test_immutable_blob_upload_recovers_from_a_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOB_STORE_ID", "store_example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "managed-token")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    store = blob.VercelBlobStore()
    reads = iter([None, (b"fragment", '"etag"')])
    monkeypatch.setattr(store, "get", lambda _: next(reads))
    response = httpx2.Response(
        409, request=httpx2.Request("PUT", "https://vercel.com/api/blob")
    )
    monkeypatch.setattr(store, "_put", lambda *args, **kwargs: response)

    store.put_immutable("snapshots/fragment.json", b"fragment")
