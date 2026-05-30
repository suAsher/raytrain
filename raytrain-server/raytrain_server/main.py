"""
raytrain Platform — control plane entry point.

Composition root: pulls together the routers + middleware + lifespan hooks.
This is what ``uvicorn raytrain_server.main:app`` boots up.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import auth as auth_api
from .api import admin_users as admin_users_api
from .api import code as code_api
from .api import datasets as datasets_api
from .api import devsessions as devsessions_api
from .api import health as health_api
from .api import jobs as jobs_api
from .api import workspaces as workspaces_api
from .core.bootstrap import configure_persistence
from .core.reclaim import ReclaimLoop
from .core.settings import Settings, get_settings
from .core.store import get_devsession_store


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Easier to test with custom settings than mutating
    a module-level singleton."""
    s = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Wire SQL persistence if configured (else in-memory).
        configure_persistence(s)
        # Start the DevSession reclaim loop. reclaim_once is a no-op when
        # nothing has expired, so it's safe to always run.
        loop = ReclaimLoop(get_devsession_store(), s, interval_s=60)
        loop.start()
        app.state.reclaim_loop = loop
        try:
            yield
        finally:
            loop.stop()

    app = FastAPI(
        title="raytrain Platform API",
        version="0.1.0",
        description=(
            "raytrain Platform control plane. Translates user requests "
            "(Web UI / CLI) into Ray Job Submission API calls against "
            "long-lived RayClusters."
        ),
        lifespan=lifespan,
    )
    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health_api.router)
    app.include_router(auth_api.router)
    app.include_router(admin_users_api.router)
    app.include_router(code_api.router)
    app.include_router(jobs_api.router)
    app.include_router(workspaces_api.router)
    app.include_router(devsessions_api.router)
    app.include_router(datasets_api.router)

    return app


# Module-level app for ``uvicorn raytrain_server.main:app``
app = create_app()


def run() -> None:  # pragma: no cover
    """Entry point for the ``raytrain-server`` console script."""
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "raytrain_server.main:app",
        host=s.server_host,
        port=s.server_port,
        log_level=s.log_level.lower(),
        reload=False,
    )
