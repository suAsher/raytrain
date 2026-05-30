"""
Wire persistence on startup.

If ``settings.database_url`` is set, create a Database, init the schema, and
swap the in-memory stores + audit for SQL-backed ones. Otherwise leave the
in-memory defaults (dev / tests).

Called from the FastAPI lifespan.
"""
from __future__ import annotations

import logging

from .audit import AuditLog, set_audit
from .db import Database
from .settings import Settings
from .sql_store import (
    SqlDatasetStore,
    SqlDevSessionStore,
    SqlUserStore,
    SqlWorkspaceStore,
)
from .store import (
    set_dataset_store,
    set_devsession_store,
    set_workspace_store,
)
from .users import set_user_store

log = logging.getLogger(__name__)


def configure_persistence(settings: Settings) -> Database | None:
    """Return the Database if one was configured, else None (in-memory mode)."""
    if not settings.database_url:
        log.info("no database_url; using in-memory stores (state lost on restart)")
        return None

    db = Database(settings.database_url)
    db.init_schema()

    set_workspace_store(SqlWorkspaceStore(db))
    set_devsession_store(SqlDevSessionStore(db))
    set_dataset_store(SqlDatasetStore(db))
    set_user_store(SqlUserStore(db))
    set_audit(AuditLog(db))

    log.info("persistence configured: %s", settings.database_url.split("@")[-1])
    return db
