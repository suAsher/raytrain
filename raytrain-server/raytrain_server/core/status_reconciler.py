"""
StatusReconciler — background loop that keeps Live_Job statuses in sync with
the real Ray cluster, so a job's status advances (and failures get recorded)
even when nobody has the list/detail page open (Req 5.7).

Mirrors ReclaimLoop's daemon-thread pattern. ``reconcile_once`` is pure enough
to unit-test by injecting a fake SubmissionService + JobStore.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_TERMINAL = ("Succeeded", "Failed", "Cancelled")


def reconcile_once(job_store, submission_service) -> list[str]:
    """Reconcile every non-terminal live job. Returns ids whose status changed.

    Property 7: terminal jobs are never re-polled / re-written.
    """
    changed: list[str] = []
    # admin view = all jobs; we only touch live, non-terminal ones
    jobs = job_store.list_visible("", "", is_admin=True)
    for j in jobs:
        if not j.submission_id or j.status in _TERMINAL:
            continue
        before = j.status
        updated = submission_service.reconcile(j)
        if updated and updated.status != before:
            changed.append(j.id)
    return changed


class StatusReconcileLoop:
    """Daemon thread that calls reconcile_once on an interval."""

    def __init__(self, job_store, submission_service, interval_s: int = 30) -> None:
        self._job_store = job_store
        self._svc = submission_service
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="raytrain-status-reconcile", daemon=True
        )
        self._thread.start()
        log.info("status reconcile loop started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                reconcile_once(self._job_store, self._svc)
            except Exception as exc:  # noqa: BLE001
                log.warning("status reconcile iteration failed: %r", exc)
