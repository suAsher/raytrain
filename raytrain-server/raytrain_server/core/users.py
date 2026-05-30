"""
User + per-user quota/grants store (platform-level RBAC, NOT K8s RBAC).

Why this exists
---------------
The platform deliberately does NOT hand users a kubeconfig. A user only holds a
platform JWT (identity: who you are). What a user is *allowed* to do — which GPU
budget, which projects / datasets / images / queues — is a **business** decision
the control plane owns, expressed here and persisted in the DB. K8s namespace
RBAC cannot express "alice may use at most 4 H20 GPUs" or "alice may use dataset
X" — so it lives at the platform layer.

Model (per your decisions)
--------------------------
- Quota is **per single user** (not per team/quota-group).
- Grants + quota are persisted in the **DB** (sql_store) — token carries only
  identity, so changing a user's quota/grants takes effect immediately without
  re-issuing tokens.
- Admin **creates a user with grants/quota**, and can **update** them later.

Same dual-backend pattern as the rest of core: an in-memory ``UserStore`` and a
SQL-backed ``SqlUserStore`` (in sql_store.py) behind one interface, selected at
bootstrap.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class UserQuota:
    """Per-user hard caps. 0 / empty means 'use the platform default / unset'.

    ``-1`` means **unlimited** (admin opt-out of a particular cap). Anything
    >= 0 is a hard ceiling enforced at submit time against current usage.
    """

    max_gpus: int = 0          # total concurrent GPUs across running jobs
    max_jobs: int = 0          # max concurrent (running/pending) jobs
    max_cpus: int = 0          # total concurrent CPUs
    max_memory_gi: int = 0     # total concurrent memory (GiB)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UserRecord:
    """A platform user + their grants. ``user`` is the unique key (JWT ``sub``)."""

    user: str
    tenant: str = "default"
    role: str = "user"                 # user | admin
    quota: UserQuota = field(default_factory=UserQuota)
    # business grants — what the user may reference when submitting
    projects: list = field(default_factory=list)
    queues: list = field(default_factory=list)
    datasets: list = field(default_factory=list)
    image_prefixes: list = field(default_factory=list)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserStore:
    """In-memory user store. SQL-backed equivalent lives in sql_store.py."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, UserRecord] = {}

    def create(self, rec: UserRecord) -> UserRecord:
        with self._lock:
            if rec.user in self._items:
                raise ValueError(f"user {rec.user!r} already exists")
            self._items[rec.user] = rec
            return rec

    def get(self, user: str) -> Optional[UserRecord]:
        with self._lock:
            return self._items.get(user)

    def update(self, user: str, **changes) -> Optional[UserRecord]:
        with self._lock:
            rec = self._items.get(user)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            rec.updated_at = time.time()
            return rec

    def delete(self, user: str) -> bool:
        with self._lock:
            return self._items.pop(user, None) is not None

    def list_all(self) -> list:
        with self._lock:
            return list(self._items.values())


_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def set_user_store(s) -> None:
    global _user_store
    _user_store = s
