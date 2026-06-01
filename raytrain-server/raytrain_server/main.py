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
from .api import admin_resources as admin_resources_api
from .api import code as code_api
from .api import console as console_api
from .api import datasets as datasets_api
from .api import devsessions as devsessions_api
from .api import health as health_api
from .api import jobs as jobs_api
from .api import workspaces as workspaces_api
from .core.bootstrap import configure_persistence
from .core.errors import install_error_handlers
from .core.reclaim import ReclaimLoop
from .core.settings import Settings, get_settings
from .core.store import get_devsession_store


def _bootstrap_admin(s: Settings) -> None:
    """Create a username/password admin on startup if configured and absent."""
    if not s.bootstrap_admin_password:
        return
    import logging as _logging
    from .core.users import UserRecord, get_user_store, hash_password

    store = get_user_store()
    if store.get(s.bootstrap_admin_user) is not None:
        return
    store.create(
        UserRecord(
            user=s.bootstrap_admin_user,
            tenant="default",
            role="admin",
            password_hash=hash_password(s.bootstrap_admin_password),
        )
    )
    _logging.getLogger(__name__).info(
        "bootstrapped admin user %r (change the password after first login)",
        s.bootstrap_admin_user,
    )


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
        # Bootstrap a username/password admin so a fresh platform has a login
        # without exec-ing into the pod (no-op if disabled or already present).
        _bootstrap_admin(s)
        # Seed demo job records so the console isn't empty on first boot —
        # only in in-memory mode. With a real database we never write demo data
        # (Req 14.6: no seed data masquerading as real).
        if s.seed_demo and not s.database_url:
            from .core.seed import seed_demo_jobs

            seed_demo_jobs()
        # Start the DevSession reclaim loop. reclaim_once is a no-op when
        # nothing has expired, so it's safe to always run.
        loop = ReclaimLoop(get_devsession_store(), s, interval_s=60)
        loop.start()
        app.state.reclaim_loop = loop
        # Start the background status reconciler so live job statuses advance
        # without a user opening the list/detail page (Req 5.7).
        from .core.jobs_store import get_job_store
        from .core.status_reconciler import StatusReconcileLoop
        from .core.submission_service import get_submission_service

        rloop = StatusReconcileLoop(
            get_job_store(), get_submission_service(),
            interval_s=s.status_reconcile_interval_s,
        )
        rloop.start()
        app.state.status_reconcile_loop = rloop
        try:
            yield
        finally:
            loop.stop()
            rloop.stop()

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

    install_error_handlers(app)

    app.include_router(health_api.router)
    app.include_router(auth_api.router)
    app.include_router(admin_users_api.router)
    app.include_router(admin_resources_api.router)
    app.include_router(console_api.router)
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
