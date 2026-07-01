from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, HttpUrl, field_validator

from . import opml
from .common import feed_path, parse_delay
from .common.storage import S3Storage, Storage
from .config import Settings, get_settings
from .podcasts import enqueue_create

logger = logging.getLogger(__name__)


class PodcastRequest(BaseModel):
    feed_url: HttpUrl
    title: str | None = None
    delay: str | None = None

    @field_validator("delay")
    @classmethod
    def _validate_delay(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_delay(value)
        return value


def create_app(
    *,
    settings: Settings | None = None,
    storage: Storage | None = None,
    queue: asyncio.Queue | None = None,
    lifespan=None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage or S3Storage(settings)
    app.state.queue = queue if queue is not None else asyncio.Queue()

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> PlainTextResponse:
        return PlainTextResponse("Bad Request", status_code=400)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/podcast")
    async def create_podcast(
        payload: PodcastRequest, request: Request
    ) -> dict[str, str]:
        feed_id = await enqueue_create(
            request.app.state.queue,
            feed_url=str(payload.feed_url),
            title=payload.title,
            delay=payload.delay,
        )
        logger.info("podcast create: feed_id=%s url=%s", feed_id, payload.feed_url)
        return {"feed_id": feed_id}

    @app.get("/podcast/{feed_id}")
    async def get_podcast(feed_id: str, request: Request) -> Response:
        key = feed_path(feed_id)
        if await request.app.state.storage.head(key) is None:
            logger.info("podcast fetch: unknown feed_id=%s", feed_id)
            return Response(status_code=404)
        await request.app.state.queue.put({"feed_id": feed_id, "requested": True})
        logger.info("podcast fetch: feed_id=%s; queued refresh", feed_id)
        audio_base = request.app.state.settings.public_storage_url.rstrip("/")
        return RedirectResponse(url=f"{audio_base}/{key}", status_code=307)

    @app.get("/opml")
    async def opml_export(request: Request) -> Response:
        if not request.app.state.settings.enable_opml:
            return Response(status_code=404)
        document = await opml.export_opml(
            request.app.state.storage, request.app.state.settings
        )
        return Response(content=document, media_type="text/x-opml")

    @app.post("/opml")
    async def opml_import(request: Request) -> Response:
        if not request.app.state.settings.enable_opml:
            return Response(status_code=404)
        try:
            await opml.import_opml(
                request.app.state.storage,
                request.app.state.queue,
                await request.body(),
                request.app.state.settings,
            )
        except ValueError:
            return PlainTextResponse("Bad Request", status_code=400)
        return Response(status_code=202)

    return app
