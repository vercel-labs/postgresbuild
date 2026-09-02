from __future__ import annotations

import json
import os
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel

from postgresbuild.blob import VercelBlobStore
from postgresbuild.publication import (
    GitHubReleaseClient,
    PublicationPolicy,
    canonical_json,
    fetch_valid_snapshot,
    materialize,
    validate_index,
    verify_github_oidc,
    versions_ndjson,
)

app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)


class PublicationRequest(BaseModel):
    repository: str
    tag: str


def json_response(
    value: object, *, status_code: int = 200, cache_control: str = "no-store"
) -> Response:
    return Response(
        canonical_json(value),
        status_code=status_code,
        media_type="application/json",
        headers={"Cache-Control": cache_control},
    )


@app.get("/index.json", response_class=Response)
def get_index() -> Response:
    policy = PublicationPolicy.from_environment()
    existing = VercelBlobStore().get("index.json")
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index not found")
    try:
        value = validate_index(json.loads(existing[0]), policy.repository)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "stored index is corrupt"
        ) from error
    return json_response(
        value,
        cache_control="public, s-maxage=300, stale-while-revalidate=3600",
    )


@app.get("/versions.ndjson", response_class=Response)
def get_versions() -> Response:
    policy = PublicationPolicy.from_environment()
    existing = VercelBlobStore().get("index.json")
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index not found")
    try:
        value = validate_index(json.loads(existing[0]), policy.repository)
        content = versions_ndjson(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "stored index is corrupt"
        ) from error
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": (
                "public, s-maxage=300, stale-while-revalidate=3600"
            )
        },
    )


@app.post("/api/publication", response_class=Response)
def ingest_publication(
    request: PublicationRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token"
        )
    audience = os.environ.get(
        "PUBLICATION_AUDIENCE",
        "https://postgresbuild.vercel.app/api/publication",
    )
    policy = PublicationPolicy.from_environment()
    try:
        verify_github_oidc(
            authorization.removeprefix("Bearer "), audience, policy
        )
    except Exception as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "unauthorized GitHub workflow"
        ) from error
    try:
        fetched = fetch_valid_snapshot(
            request.repository, request.tag, GitHubReleaseClient(), policy
        )
        if fetched is None:
            return json_response({"ignored": True}, status_code=202)
        snapshot, published_at = fetched
        index = materialize(
            VercelBlobStore(), snapshot, published_at, policy.repository
        )
    except (
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return json_response(index)
