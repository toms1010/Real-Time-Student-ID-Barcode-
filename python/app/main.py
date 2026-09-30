"""FastAPI application factory.

Startup sequence (brief section 31)::

    Load Configuration -> Initialise Logger -> Check Database -> Start Python API
    -> Initialise Camera (lazy) -> Initialise Barcode Decoder -> Serve UI

The camera is opened **lazily** by the vision pipeline rather than during
start-up, because the service must be able to start (for administration and
reports) on a machine with no camera attached.  ``GET /api/scanner/state``
reports the camera error instead.

Run it with::

    source .venv/bin/activate
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import ALL_ROUTERS
from app.utils.config import AppConfig, get_config
from app.utils.errors import AppError
from app.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)

DESCRIPTION = """
Local API for the **Real-Time Student ID Barcode Detection and Verification
System**.  The C++ scanner calls `POST /api/scans` for every decoded barcode; the
browser UI reads the same data through the remaining routes.

* Runs entirely on `127.0.0.1` - no cloud service, no internet connection.
* `POST /api/scans` is the hot path; everything else is administration.
* Interactive documentation is at `/docs`, the OpenAPI schema at `/openapi.json`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: AppConfig = app.state.config
    logger.info("=" * 72)
    logger.info("%s v%s starting", config.application.name, config.application.version)
    logger.info("environment=%s  bind=%s  database=%s",
                config.application.environment,
                f"{config.api.host}:{config.api.port}",
                config.database.path)

    # 1. database (created and migrated if necessary)
    try:
        from app.database.connection import init_database
        from app.database.migrations import apply_migrations
        from app.database.repository import Repository

        database = init_database(config)
        applied = apply_migrations(database)
        if applied:
            logger.info("applied database migrations: %s", ", ".join(applied))
        users = Repository(database).users.count()
        if users == 0:
            logger.warning(
                "no user accounts exist yet - run 'python -m app.cli create-admin' "
                "or './scripts/setup.sh' before signing in"
            )
    except AppError:
        logger.exception("database initialisation failed; the API will report 503 on /api/health")
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error during start-up")

    # 2. vision pipeline (camera opened lazily on first use)
    if config.scanner.stream_source == "python" and config.application.environment != "testing":
        try:
            from app.vision.pipeline import get_pipeline

            pipeline = get_pipeline()
            pipeline.start()
        except Exception:  # noqa: BLE001
            logger.exception("could not start the vision pipeline; "
                             "the rest of the API stays available")

    logger.info("API ready on http://%s:%d", config.api.host, config.api.port)
    logger.info("=" * 72)
    try:
        yield
    finally:
        try:
            from app.vision.pipeline import get_pipeline

            get_pipeline().stop()
        except Exception:  # noqa: BLE001
            pass
        logger.info("shutdown complete")


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or get_config()
    setup_logging(cfg.application.log_level, cfg.application.log_format,
                  log_file=cfg.application.log_file, force=True)

    app = FastAPI(
        title=cfg.application.name,
        version=cfg.application.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.config = cfg

    # --- CORS: off by default (same-origin UI) -----------------------------
    if cfg.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.api.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
        )
        logger.info("CORS enabled for: %s", ", ".join(cfg.api.cors_origins))

    # --- uniform error envelope -------------------------------------------
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("%s %s -> %s", request.method, request.url.path, exc.code)
        else:
            logger.info("%s %s -> %s", request.method, request.url.path, exc.code)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a traceback, SQL text or a file path to the client.

        The full traceback goes to the log; the client gets the same stable
        ``UNKNOWN_ERROR`` body the C++ scanner knows how to parse.
        """
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "UNKNOWN_ERROR",
                    "message": "An unexpected error occurred. Please contact the administrator.",
                    "details": {},
                },
            },
        )

    @app.middleware("http")
    async def timing_middleware(request: Request, call_next):
        """Log slow requests; a barcode POST should be well under 100 ms."""
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000.0
        response.headers["X-Response-Time-ms"] = f"{elapsed:.2f}"
        if elapsed > 500 and request.url.path not in {"/api/health"}:
            logger.warning("slow request: %s %s took %.1f ms",
                           request.method, request.url.path, elapsed)
        return response

    # --- routers -----------------------------------------------------------
    for router in ALL_ROUTERS:
        app.include_router(router)

    # --- frontend ----------------------------------------------------------
    if cfg.web.mount_frontend:
        _mount_frontend(app, cfg)

    logger.debug("application configured (environment=%s)", cfg.application.environment)
    return app


def _mount_frontend(app: FastAPI, cfg: AppConfig) -> None:
    """Serve the built React app from ``frontend/dist``.

    ``/api`` is registered first, so the SPA fallback can never shadow an API
    route.  A missing build produces a helpful page instead of a 404, because
    "npm run build" is a step people forget.
    """
    dist = Path(cfg.frontend_dist)
    index = dist / "index.html"

    if not index.is_file():
        @app.get("/", include_in_schema=False, response_model=None)
        def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "message": "The web interface has not been built yet.",
                    "how_to_fix": [
                        "cd frontend",
                        "npm install",
                        "npm run build",
                        "then reload this page",
                    ],
                    "api_docs": "/docs",
                },
            )
        logger.warning("frontend build not found at %s - run 'npm run build' in frontend/", dist)
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False, response_model=None)
    def index_page() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa_fallback(full_path: str) -> FileResponse | JSONResponse:
        """Client-side routing: any unknown non-API path returns the SPA."""
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404,
                                content={"success": False, "message": "Unknown endpoint"})
        candidate = dist / full_path
        if full_path and candidate.is_file() and _within(dist, candidate):
            return FileResponse(candidate)
        return FileResponse(index)

    logger.info("serving the web interface from %s", dist)


def _within(base: Path, candidate: Path) -> bool:
    try:
        base = base.resolve()
        return base == candidate.resolve() or base in candidate.resolve().parents
    except OSError:  # pragma: no cover
        return False


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    config = get_config()
    uvicorn.run(
        "app.main:app",
        host=config.api.host,
        port=config.api.port,
        log_level=config.application.log_level.lower(),
        access_log=config.application.environment == "development",
    )
