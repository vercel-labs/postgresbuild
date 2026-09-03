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


def publish_release(
    request: PublicationRequest,
    token: str,
    policy: PublicationPolicy,
) -> dict[str, object] | None:
    github = GitHubReleaseClient(token)
    store: VercelBlobStore | None = None

    def publish_artifact(tag: str, name: str, content: bytes) -> None:
        nonlocal store
        store = store or VercelBlobStore()
        store.publish_artifact(tag, name, content)

    fetched = fetch_valid_snapshot(
        request.repository,
        request.tag,
        github,
        policy,
        publish_artifact,
    )
    if fetched is None:
        return None
    snapshot, published_at = fetched
    store = store or VercelBlobStore()
    return materialize(store, snapshot, published_at, policy.repository)


def oidc_failure_reason(error: Exception) -> str:
    message = str(error)
    fixed_reasons = {
        "GitHub OIDC header is not RS256 with a key ID": "header",
        "GitHub OIDC token uses an unknown key": "key",
        "GitHub OIDC event is not authorized": "event_name",
        "GitHub OIDC repository owner is not authorized": "repository_owner",
        "GitHub OIDC subject is not the release environment": "subject",
    }
    if message in fixed_reasons:
        return fixed_reasons[message]
    prefix = "GitHub OIDC claim "
    suffix = " is not authorized"
    if message.startswith(prefix) and message.endswith(suffix):
        claim = message.removeprefix(prefix).removesuffix(suffix)
        if claim in {"repository", "ref", "workflow_ref"}:
            return claim
    return type(error).__name__


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
    store = VercelBlobStore()
    existing = store.get("index.json")
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index not found")
    try:
        value = validate_index(json.loads(existing[0]), policy.repository)
        content = versions_ndjson(value, store.artifact_url)
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
    x_github_token: Annotated[str | None, Header()] = None,
) -> Response:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token"
        )
    audience = os.environ.get(
        "PUBLICATION_AUDIENCE",
        "https://postgresbuild.labs.vercel.dev/api/publication",
    )
    policy = PublicationPolicy.from_environment()
    try:
        verify_github_oidc(
            authorization.removeprefix("Bearer "), audience, policy
        )
    except Exception as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"unauthorized GitHub workflow: {oidc_failure_reason(error)}",
        ) from error
    if not x_github_token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing GitHub release token"
        )
    try:
        index = publish_release(request, x_github_token, policy)
        if index is None:
            return json_response({"ignored": True}, status_code=202)
    except (
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return json_response(index)
