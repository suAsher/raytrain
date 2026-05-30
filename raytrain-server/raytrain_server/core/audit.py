"""
Audit log: who did what, when, and whether it succeeded.

Backed by the same Database as the stores. When no DB is configured we fall
back to structured stdout logging (still captured by `kubectl logs`).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .db import Database

log = logging.getLogger("raytrain.audit")


class AuditLog:
    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    def record(
        self,
        user: str,
        action: str,
        resource: str = "",
        result: str = "ok",
        detail: str = "",
    ) -> None:
        ts = time.time()
        if self._db is not None:
            try:
                self._db.execute(
                    'INSERT INTO audit_log (ts, "user", action, resource, result, detail) '
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, user, action, resource, result, detail),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("audit db write failed: %r", exc)
        # always also emit structured stdout (defense in depth)
        log.info(
            "audit user=%s action=%s resource=%s result=%s detail=%s",
            user, action, resource, result, detail,
        )

    def search(
        self,
        user: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 200,
    ) -> list[dict]:
        if self._db is None:
            return []
        where = []
        params: list = []
        if user:
            where.append('"user" = ?')
            params.append(user)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        return self._db.query_all(
            f'SELECT ts, "user", action, resource, result, detail '
            f"FROM audit_log{clause} ORDER BY ts DESC LIMIT ?",
            params,
        )


_audit: AuditLog | None = None


def get_audit() -> AuditLog:
    global _audit
    if _audit is None:
        _audit = AuditLog(None)  # default: stdout-only until DB wired
    return _audit


def set_audit(audit: AuditLog) -> None:
    global _audit
    _audit = audit
