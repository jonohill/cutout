from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl, ValidationError, field_validator

from . import dashboard, opml
from .common import feed_path, parse_delay
from .common.storage import S3Storage, Storage
from .config import Settings, get_settings
from .podcasts import delete_feed, enqueue_create

logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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

    async def _render_feeds(request: Request, notice: str | None) -> Response:
        """Render the #feeds partial that every dashboard mutation swaps in."""
        data = await dashboard.gather(
            request.app.state.storage, request.app.state.settings
        )
        return _TEMPLATES.TemplateResponse(
            request, "_feeds.html", {"data": data, "notice": notice}
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request) -> Response:
        if not request.app.state.settings.enable_dashboard:
            return Response(status_code=404)
        data = await dashboard.gather(
            request.app.state.storage, request.app.state.settings
        )
        return _TEMPLATES.TemplateResponse(
            request, "dashboard.html", {"data": data, "notice": None}
        )

    @app.post("/dashboard/podcast", response_class=HTMLResponse)
    async def dashboard_add(
        request: Request,
        feed_url: str = Form(...),
        title: str = Form(""),
        delay: str = Form(""),
    ) -> Response:
        if not request.app.state.settings.enable_dashboard:
            return Response(status_code=404)
        # Reuse the JSON endpoint's validation (URL shape + delay grammar).
        try:
            payload = PodcastRequest(
                feed_url=feed_url, title=title or None, delay=delay or None
            )
        except ValidationError:
            return await _render_feeds(request, "Invalid feed URL or delay.")
        feed_id = await enqueue_create(
            request.app.state.queue,
            feed_url=str(payload.feed_url),
            title=payload.title,
            delay=payload.delay,
        )
        logger.info("dashboard add: feed_id=%s url=%s", feed_id, payload.feed_url)
        return await _render_feeds(
            request, f"Queued {payload.feed_url} — appears once processed."
        )

    @app.post("/dashboard/podcast/{feed_id}/delete", response_class=HTMLResponse)
    async def dashboard_delete(feed_id: str, request: Request) -> Response:
        if not request.app.state.settings.enable_dashboard:
            return Response(status_code=404)
        removed = await delete_feed(request.app.state.storage, feed_id)
        logger.info("dashboard delete: feed_id=%s objects=%d", feed_id, removed)
        notice = (
            f"Removed podcast {feed_id} ({removed} object(s))."
            if removed
            else f"Nothing to remove for {feed_id}."
        )
        return await _render_feeds(request, notice)

    @app.get("/dashboard/opml")
    async def dashboard_opml_export(request: Request) -> Response:
        if not request.app.state.settings.enable_dashboard:
            return Response(status_code=404)
        document = await opml.export_opml(
            request.app.state.storage, request.app.state.settings
        )
        return Response(
            content=document,
            media_type="text/x-opml",
            headers={"Content-Disposition": "attachment; filename=cutout.opml"},
        )

    @app.post("/dashboard/opml", response_class=HTMLResponse)
    async def dashboard_opml_import(
        request: Request, file: UploadFile = File(...)
    ) -> Response:
        if not request.app.state.settings.enable_dashboard:
            return Response(status_code=404)
        try:
            created = await opml.import_opml(
                request.app.state.storage,
                request.app.state.queue,
                await file.read(),
                request.app.state.settings,
            )
        except ValueError:
            return await _render_feeds(request, "That doesn't look like valid OPML.")
        return await _render_feeds(
            request, f"Imported {created} new podcast(s) — appear once processed."
        )

    return app
