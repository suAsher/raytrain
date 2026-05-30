"""
Background reclaim loop: kills DevSession pods whose idle / lifetime budget is
exhausted.

Runs as a daemon thread started in the FastAPI lifespan. Low frequency (every
60s) — the control plane is human-paced so we don't need anything fancier.

Kept separate from the API layer so it can be unit-tested by calling
``reclaim_once`` directly with injected stores + a fake K8s client.
"""
from __future__ import annotations

import logging
import threading
import time

from .k8s_client import K8sClient
from .settings import Settings
from .store import DevSessionStore

log = logging.getLogger(__name__)


def reclaim_once(
    store: DevSessionStore,
    k8s: K8sClient,
    namespace: str,
    now: float | None = None,
) -> list[str]:
    """Reclaim all expired DevSessions. Returns the list of reclaimed ids."""
    reclaimed: list[str] = []
    for rec in store.expired(now):
        try:
            if rec.pod_name:
                k8s.delete_pod(rec.pod_name, namespace)
            if rec.service_name:
                k8s.delete_service(rec.service_name, namespace)
            store.delete(rec.id)
            reclaimed.append(rec.id)
            log.info("reclaim: killed dev session %s (user=%s)", rec.id, rec.user)
        except Exception as exc:  # noqa: BLE001
            log.warning("reclaim: failed to kill %s: %r", rec.id, exc)
    return reclaimed


class ReclaimLoop:
    """Daemon thread that calls reclaim_once on an interval."""

    def __init__(
        self,
        store: DevSessionStore,
        settings: Settings,
        interval_s: int = 60,
    ) -> None:
        self._store = store
        self._settings = settings
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="raytrain-reclaim", daemon=True
        )
        self._thread.start()
        log.info("reclaim loop started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Construct K8sClient inside the thread so config load happens once.
        k8s = K8sClient(in_cluster=self._settings.in_cluster)
        ns = self._settings.devsession_namespace
        while not self._stop.wait(self._interval):
            try:
                reclaim_once(self._store, k8s, ns)
            except Exception as exc:  # noqa: BLE001
                log.warning("reclaim loop iteration failed: %r", exc)
