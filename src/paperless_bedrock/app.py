"""HTTP interface: the paperless workflow webhook and a health endpoint."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from paperless_bedrock.jobs import JobQueue


def create_app(
    queue: JobQueue,
    webhook_token: str | None,
    on_startup: Callable[[], None] | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> Starlette:
    async def analyze(request: Request) -> JSONResponse:
        if webhook_token:
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {webhook_token}"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            document_id = int(body["document_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return JSONResponse(
                {"error": 'expected JSON body {"document_id": <int>}'}, status_code=400
            )
        queued = queue.enqueue(document_id)
        return JSONResponse(
            {"document_id": document_id, "queued": queued}, status_code=202 if queued else 200
        )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "jobs": queue.counts()})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if on_startup:
            on_startup()
        try:
            yield
        finally:
            if on_shutdown:
                on_shutdown()

    return Starlette(
        routes=[
            Route("/analyze", analyze, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
