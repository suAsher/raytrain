"""
In-memory store for Workspaces / DevSessions.

M2 uses this to get the orchestration loop working end-to-end. M4 swaps the
implementation for Postgres behind the same interface (``WorkspaceStore``
protocol) without touching the API layer.

Thread-safety: guarded by a single lock. The control plane is low-QPS
(human-driven), so a global lock is fine for v1.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class WorkspaceRecord:
    id: str
    user: str
    tenant: str
    name: str
    image: str
    cpu: int
    memory_gi: int
    pvc_gi: int
    state: str = "creating"   # creating|running|stopped|deleting|error
    pod_name: str = ""
    pvc_name: str = ""
    service_name: str = ""
    ide_urls: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class WorkspaceStore:
    """In-memory CRUD. Replace with Postgres in M4."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, WorkspaceRecord] = {}

    def create(self, **kwargs) -> WorkspaceRecord:
        with self._lock:
            wid = kwargs.pop("id", None) or uuid.uuid4().hex[:10]
            rec = WorkspaceRecord(id=wid, **kwargs)
            self._items[wid] = rec
            return rec

    def get(self, wid: str) -> Optional[WorkspaceRecord]:
        with self._lock:
            return self._items.get(wid)

    def update(self, wid: str, **changes) -> Optional[WorkspaceRecord]:
        with self._lock:
            rec = self._items.get(wid)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            return rec

    def delete(self, wid: str) -> bool:
        with self._lock:
            return self._items.pop(wid, None) is not None

    def list_for_user(self, user: str, is_admin: bool = False) -> list[WorkspaceRecord]:
        with self._lock:
            vals = list(self._items.values())
        if is_admin:
            return vals
        return [r for r in vals if r.user == user]

    def count_for_user(self, user: str) -> int:
        with self._lock:
            return sum(1 for r in self._items.values() if r.user == user)


# Process-wide singleton (M2). M4 replaces with a DB-backed instance via
# configure_stores().
_store: WorkspaceStore | None = None


def get_workspace_store() -> WorkspaceStore:
    global _store
    if _store is None:
        _store = WorkspaceStore()
    return _store


def set_workspace_store(s) -> None:
    global _store
    _store = s


# --------------------------------------------------------------------------- #
# DevSession (M3)
# --------------------------------------------------------------------------- #


@dataclass
class DevSessionRecord:
    id: str
    workspace_id: str
    user: str
    tenant: str
    image: str
    gpu_type: str
    gpu_count: int
    pvc_name: str
    state: str = "creating"   # creating|running|stopping|expired
    pod_name: str = ""
    service_name: str = ""
    ide_urls: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    # idle reclaim: server kills the pod after this many seconds without a
    # heartbeat. Hard cap separately enforced via created_at.
    idle_timeout_s: int = 4 * 3600
    max_lifetime_s: int = 24 * 3600

    def to_dict(self) -> dict:
        return asdict(self)

    def is_expired(self, now: float | None = None) -> bool:
        now = now or time.time()
        if now - self.last_seen_at > self.idle_timeout_s:
            return True
        if now - self.created_at > self.max_lifetime_s:
            return True
        return False


class DevSessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, DevSessionRecord] = {}

    def create(self, **kwargs) -> DevSessionRecord:
        with self._lock:
            sid = kwargs.pop("id", None) or uuid.uuid4().hex[:10]
            rec = DevSessionRecord(id=sid, **kwargs)
            self._items[sid] = rec
            return rec

    def get(self, sid: str) -> Optional[DevSessionRecord]:
        with self._lock:
            return self._items.get(sid)

    def update(self, sid: str, **changes) -> Optional[DevSessionRecord]:
        with self._lock:
            rec = self._items.get(sid)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            return rec

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._items.pop(sid, None) is not None

    def list_for_user(self, user: str, is_admin: bool = False) -> list[DevSessionRecord]:
        with self._lock:
            vals = list(self._items.values())
        if is_admin:
            return vals
        return [r for r in vals if r.user == user]

    def gpu_in_use_by_user(self, user: str) -> int:
        with self._lock:
            return sum(
                r.gpu_count for r in self._items.values()
                if r.user == user and r.state in ("creating", "running")
            )

    def expired(self, now: float | None = None) -> list[DevSessionRecord]:
        with self._lock:
            return [r for r in self._items.values() if r.is_expired(now)]


_dev_store: DevSessionStore | None = None


def get_devsession_store() -> DevSessionStore:
    global _dev_store
    if _dev_store is None:
        _dev_store = DevSessionStore()
    return _dev_store


def set_devsession_store(s) -> None:
    global _dev_store
    _dev_store = s


# --------------------------------------------------------------------------- #
# Dataset registry (M3)
# --------------------------------------------------------------------------- #


@dataclass
class DatasetRecord:
    id: str
    name: str
    type: str                 # lance | parquet | dir
    uri: str
    owner: str
    tenant: str
    version: str = "latest"
    visibility: str = "private"   # private | tenant | public
    schema_json: dict = field(default_factory=dict)
    rows: int = 0
    size_bytes: int = 0
    tags: list = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, DatasetRecord] = {}

    def create(self, **kwargs) -> DatasetRecord:
        with self._lock:
            did = kwargs.pop("id", None) or uuid.uuid4().hex[:10]
            rec = DatasetRecord(id=did, **kwargs)
            self._items[did] = rec
            return rec

    def get(self, did: str) -> Optional[DatasetRecord]:
        with self._lock:
            return self._items.get(did)

    def update(self, did: str, **changes) -> Optional[DatasetRecord]:
        with self._lock:
            rec = self._items.get(did)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            return rec

    def delete(self, did: str) -> bool:
        with self._lock:
            return self._items.pop(did, None) is not None

    def visible_to(self, user: str, tenant: str, is_admin: bool = False) -> list[DatasetRecord]:
        """Datasets the caller can see: own (private) + same-tenant + public."""
        with self._lock:
            vals = list(self._items.values())
        if is_admin:
            return vals
        out = []
        for r in vals:
            if r.visibility == "public":
                out.append(r)
            elif r.visibility == "tenant" and r.tenant == tenant:
                out.append(r)
            elif r.owner == user:
                out.append(r)
        return out

    def can_access(self, rec: DatasetRecord, user: str, tenant: str, is_admin: bool) -> bool:
        if is_admin or rec.owner == user:
            return True
        if rec.visibility == "public":
            return True
        if rec.visibility == "tenant" and rec.tenant == tenant:
            return True
        return False


_dataset_store: DatasetStore | None = None


def get_dataset_store() -> DatasetStore:
    global _dataset_store
    if _dataset_store is None:
        _dataset_store = DatasetStore()
    return _dataset_store


def set_dataset_store(s) -> None:
    global _dataset_store
    _dataset_store = s
