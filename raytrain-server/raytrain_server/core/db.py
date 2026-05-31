"""
Persistence layer.

Design goal: the API/orchestration layers already talk to *Store classes
(WorkspaceStore / DevSessionStore / DatasetStore) through a small CRUD
surface. M4 swaps the in-memory dicts for a SQL-backed implementation behind
the SAME method signatures, so nothing above changes.

We support two backends through one thin DB-API wrapper:
    - sqlite3   (stdlib; default for dev / single-replica)
    - postgres  (psycopg, if RAYTRAIN_DATABASE_URL starts with postgres://)

We deliberately use raw SQL (no ORM) — the schema is tiny and stable, and it
keeps the dependency surface minimal.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Iterable

log = logging.getLogger(__name__)


class Database:
    """Minimal connection wrapper supporting sqlite + postgres.

    Connection is created lazily and guarded by a lock for sqlite (which is
    not safe across threads by default). For postgres we rely on the driver's
    own thread-safety but still serialize through the lock for simplicity at
    this QPS.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.Lock()
        self._conn: Any = None
        self._is_pg = url.startswith("postgres://") or url.startswith("postgresql://")

    # -- connection -----------------------------------------------------------

    def _connect(self) -> Any:
        if self._is_pg:
            import psycopg  # type: ignore

            return psycopg.connect(self.url, autocommit=True)
        # sqlite: strip the sqlite:/// prefix if present
        path = self.url
        for prefix in ("sqlite:///", "sqlite://"):
            if path.startswith(prefix):
                path = path[len(prefix):]
        path = path or ":memory:"
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def conn(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _ph(self) -> str:
        """Parameter placeholder: %s for postgres, ? for sqlite."""
        return "%s" if self._is_pg else "?"

    def _adapt(self, sql: str) -> str:
        """Translate ? placeholders to the backend's style.

        Note on the ``user`` column: it's a reserved word in PostgreSQL, so
        every SQL statement quotes it as ``"user"``. SQLite also accepts
        double-quoted identifiers, so the same SQL runs on both backends.
        """
        if self._is_pg:
            sql = sql.replace("?", "%s")
        return sql

    # -- execution ------------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(self._adapt(sql), tuple(params))
            if not self._is_pg:
                self.conn.commit()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(self._adapt(sql), tuple(params))
            row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_dict(cur, row)

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(self._adapt(sql), tuple(params))
            rows = cur.fetchall()
            return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cur: Any, row: Any) -> dict:
        if self._is_pg:
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        # sqlite Row supports keys()
        return {k: row[k] for k in row.keys()}

    # -- schema ---------------------------------------------------------------

    def init_schema(self) -> None:
        """Create tables if absent. JSON columns stored as TEXT for portability."""
        stmts = [
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                "user" TEXT NOT NULL,
                tenant TEXT NOT NULL,
                name TEXT NOT NULL,
                image TEXT NOT NULL,
                cpu INTEGER, memory_gi INTEGER, pvc_gi INTEGER,
                state TEXT, pod_name TEXT, pvc_name TEXT, service_name TEXT,
                ide_urls TEXT, created_at REAL, last_active_at REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dev_sessions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT, "user" TEXT, tenant TEXT, image TEXT,
                gpu_type TEXT, gpu_count INTEGER, pvc_name TEXT,
                state TEXT, pod_name TEXT, service_name TEXT, ide_urls TEXT,
                created_at REAL, last_seen_at REAL,
                idle_timeout_s INTEGER, max_lifetime_s INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT, type TEXT, uri TEXT, owner TEXT, tenant TEXT,
                version TEXT, visibility TEXT, schema_json TEXT,
                rows INTEGER, size_bytes INTEGER, tags TEXT, description TEXT,
                created_at REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, "user" TEXT, action TEXT, resource TEXT,
                result TEXT, detail TEXT
            )
            """,
            # Platform users + per-user quota/grants (NOT K8s RBAC).
            # quota / projects / queues / datasets / image_prefixes are JSON text.
            """
            CREATE TABLE IF NOT EXISTS users (
                "user" TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                role TEXT NOT NULL,
                quota TEXT,
                projects TEXT, queues TEXT, datasets TEXT, image_prefixes TEXT,
                enabled INTEGER DEFAULT 1,
                password_hash TEXT DEFAULT '',
                created_at REAL, updated_at REAL
            )
            """,
        ]
        # Postgres uses SERIAL, not SQLite's INTEGER PRIMARY KEY AUTOINCREMENT.
        # Apply the swap to whichever statement(s) contain it (currently
        # audit_log) rather than a positional index, so adding tables later
        # can't silently break this.
        if self._is_pg:
            stmts = [
                s.replace(
                    "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
                )
                for s in stmts
            ]
        for s in stmts:
            self.execute(s)
        log.info("db schema initialized (%s)", "postgres" if self._is_pg else "sqlite")


# -- json helpers (columns store JSON as text) -------------------------------


def dumps(obj: Any) -> str:
    return json.dumps(obj or {})


def loads(s: Any) -> Any:
    if s is None or s == "":
        return {}
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return {}
