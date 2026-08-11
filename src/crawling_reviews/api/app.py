"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import config
from ..core.errors import AppError
from ..core.logging import configure_logging, get_logger
from ..crawl.service import CrawlService
from .routes import router
from .schemas import ErrorBody, ErrorResponse

log = get_logger(__name__)


def create_app(service: CrawlService | None = None) -> FastAPI:
    configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or CrawlService()
        app.state.service.profiles.start_reaper()
        log.info("service ready", extra_fields={"port": config.port})
        yield
        # Graceful shutdown: stop the reaper and close transports rather than leaving a Chromium
        # process or an HTTP pool behind on every restart.
        app.state.service.close()
        log.info("service stopped")

    app = FastAPI(title="Review Aggregation Crawler", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        log.warning(
            "request failed",
            extra_fields={"path": request.url.path, "code": exc.code, "error": exc.message},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(
                response_code=exc.http_status,
                error=ErrorBody(message=exc.message, code=exc.code),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        # Unknown errors are our bug: log the detail, return something generic.
        log.exception("unhandled error", extra_fields={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                response_code=500,
                error=ErrorBody(message="Internal Server Error", code="INTERNAL_ERROR"),
            ).model_dump(),
        )

    app.include_router(router)
    return app
