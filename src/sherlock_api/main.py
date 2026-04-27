from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from sherlock_api import __version__
from sherlock_api.api.v1.router import router as v1_router
from sherlock_api.config import get_settings
from sherlock_api.db.notify import get_notify_hub, shutdown_notify_hub
from sherlock_api.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger("app")
    settings = get_settings()
    log.info("app.start", env=settings.app_env, version=__version__)

    try:
        await get_notify_hub().start()
    except Exception:
        log.exception("app.notify_hub_start_failed")

    dispatcher = None
    if settings.dispatcher_in_app:
        from sherlock_api.dispatcher import DispatcherManager

        dispatcher = DispatcherManager(settings=settings)
        await dispatcher.start()
        app.state.dispatcher = dispatcher
        log.info("dispatcher.embedded_started", workers=dispatcher.worker_count)

    try:
        yield
    finally:
        if dispatcher is not None:
            await dispatcher.stop()
        await shutdown_notify_hub()
        log.info("app.stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sherlock API",
        version=__version__,
        description="REST API wrapper over the Sherlock Telegram bot.",
        lifespan=lifespan,
    )

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    app.include_router(v1_router, prefix="/v1")
    return app


app = create_app()
