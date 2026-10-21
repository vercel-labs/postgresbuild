"""Vercel Blob adapter using project credentials plus ``BLOB_STORE_ID``."""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid

import httpx2
from vercel.blob.errors import BlobNotFoundError
from vercel.blob.sync import get_blob


class VercelBlobStore:
    def __init__(self) -> None:
        self.store_id = os.environ["BLOB_STORE_ID"].removeprefix("store_")
        self.token = (
            os.environ.get("VERCEL_OIDC_TOKEN")
            or os.environ["BLOB_READ_WRITE_TOKEN"]
        )

    def _url(self, path: str) -> str:
        encoded = urllib.parse.quote(path, safe="/")
        return (
            f"https://{self.store_id}.public.blob.vercel-storage.com/{encoded}"
        )

    def public_url(self, path: str) -> str:
        return self._url(path)

    def _api_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "x-api-blob-request-attempt": "0",
            "x-api-blob-request-id": f"{self.store_id}:{uuid.uuid4().hex}",
            "x-api-version": "12",
            "x-vercel-blob-store-id": self.store_id,
        }

    def _get(self, url: str) -> bytes:
        separator = "&" if "?" in url else "?"
        value = get_blob(
            f"{url}{separator}v={uuid.uuid4().hex}", access="public"
        )
        return value.body

    def _strong_etag(self, url: str) -> str:
        response = httpx2.get(
            "https://vercel.com/api/blob",
            params={"url": url},
            headers=self._api_headers(),
            timeout=30,
            follow_redirects=True,
        )
        response.raise_for_status()
        metadata = response.json()
        etag = metadata.get("etag") if isinstance(metadata, dict) else None
        if not isinstance(etag, str) or not etag or etag.startswith("W/"):
            raise TypeError("Vercel Blob metadata response has no strong ETag")
        return etag

    def get(self, path: str) -> tuple[bytes, str] | None:
        url = self._url(path)
        try:
            content = self._get(url)
        except BlobNotFoundError:
            return None
        return content, self._strong_etag(url)

    def _put(
        self,
        path: str,
        content: bytes,
        *,
        etag: str | None,
        overwrite: bool,
    ) -> httpx2.Response:
        # The Python SDK does not yet expose Blob's OIDC store-id or if-match
        # arguments. Match its wire protocol for these two options only.
        headers = {
            **self._api_headers(),
            "content-type": "application/octet-stream",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1" if overwrite else "0",
            "x-cache-control-max-age": "0",
            "x-content-type": "application/json",
            "x-vercel-blob-access": "public",
        }
        if etag is not None:
            headers["x-if-match"] = etag
        return httpx2.put(
            "https://vercel.com/api/blob",
            params={"pathname": path},
            headers=headers,
            content=content,
            timeout=30,
            follow_redirects=True,
        )

    def put_immutable(self, path: str, content: bytes) -> None:
        existing = self.get(path)
        if existing is not None:
            if existing[0] != content:
                raise ValueError("immutable Blob fragment conflicts")
            return
        try:
            response = self._put(path, content, etag=None, overwrite=False)
            response.raise_for_status()
        except httpx2.HTTPStatusError:
            existing = self.get(path)
            if existing is None or existing[0] != content:
                raise

    def artifact_url(self, tag: str, name: str) -> str:
        return self.public_url(f"releases/{tag}/{name}")

    def publish_artifact(self, tag: str, name: str, content: bytes) -> None:
        path = f"releases/{tag}/{name}"
        self.put_immutable(path, content)

    def put_cas(self, path: str, content: bytes, etag: str | None) -> bool:
        response = self._put(
            path,
            content,
            etag=etag,
            overwrite=etag is not None,
        )
        if response.status_code in {409, 412}:
            return False
        response.raise_for_status()
        return True

    def list_fragments(self) -> list[bytes]:
        result: list[bytes] = []
        cursor = None
        while True:
            response = httpx2.get(
                "https://vercel.com/api/blob",
                params={
                    "prefix": "snapshots/",
                    **({"cursor": cursor} if cursor is not None else {}),
                },
                headers=self._api_headers(),
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, dict) or not isinstance(
                page.get("blobs"), list
            ):
                raise TypeError("Vercel Blob list response is malformed")
            for item in page["blobs"]:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("pathname"), str)
                    and item["pathname"].endswith(".json")
                    and isinstance(item.get("url"), str)
                ):
                    content = self._get(item["url"])
                    # Parse here so a corrupt fragment fails before any write.
                    json.loads(content)
                    result.append(content)
            if not page.get("hasMore"):
                return result
            cursor = page.get("cursor")
            if not isinstance(cursor, str) or not cursor:
                raise TypeError("Vercel Blob list cursor is malformed")
