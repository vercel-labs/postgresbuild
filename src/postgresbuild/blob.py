"""Vercel Blob adapter using project credentials plus ``BLOB_STORE_ID``."""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid

import requests
from vercel.blob import get
from vercel.blob.errors import BlobNotFoundError


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

    def _get(self, url: str) -> tuple[bytes, str]:
        separator = "&" if "?" in url else "?"
        value = get(f"{url}{separator}v={uuid.uuid4().hex}", token=self.token)
        return value.content, value.etag

    def get(self, path: str) -> tuple[bytes, str] | None:
        try:
            return self._get(self._url(path))
        except BlobNotFoundError:
            return None

    def _put(
        self,
        path: str,
        content: bytes,
        *,
        etag: str | None,
        overwrite: bool,
    ) -> requests.Response:
        # The Python SDK does not yet expose Blob's OIDC store-id or if-match
        # arguments. Match its wire protocol for these two options only.
        headers = {
            "authorization": f"Bearer {self.token}",
            "content-type": "application/octet-stream",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1" if overwrite else "0",
            "x-api-blob-request-attempt": "0",
            "x-api-blob-request-id": f"{self.store_id}:{uuid.uuid4().hex}",
            "x-api-version": "12",
            "x-cache-control-max-age": "0",
            "x-content-type": "application/json",
            "x-vercel-blob-access": "public",
            "x-vercel-blob-store-id": self.store_id,
        }
        if etag is not None:
            headers["x-if-match"] = etag
        return requests.put(
            "https://vercel.com/api/blob",
            params={"pathname": path},
            headers=headers,
            data=content,
            timeout=30,
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
        except requests.HTTPError:
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
            response = requests.get(
                "https://vercel.com/api/blob",
                params={
                    "prefix": "snapshots/",
                    **({"cursor": cursor} if cursor is not None else {}),
                },
                headers={
                    "authorization": f"Bearer {self.token}",
                    "x-api-blob-request-attempt": "0",
                    "x-api-blob-request-id": (
                        f"{self.store_id}:{uuid.uuid4().hex}"
                    ),
                    "x-api-version": "12",
                    "x-vercel-blob-store-id": self.store_id,
                },
                timeout=30,
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
                    content, _ = self._get(item["url"])
                    # Parse here so a corrupt fragment fails before any write.
                    json.loads(content)
                    result.append(content)
            if not page.get("hasMore"):
                return result
            cursor = page.get("cursor")
            if not isinstance(cursor, str) or not cursor:
                raise TypeError("Vercel Blob list cursor is malformed")
