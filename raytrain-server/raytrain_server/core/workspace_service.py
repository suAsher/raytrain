"""
WorkspaceService — derive a Workspace/DevSession's REAL display state from the
cluster, and own the create/stop/start lifecycle so we never report a fake
"running" (Req 1/2/3/4).

Why
---
The old code set DB state to "running" right after create_pod, so an
ImagePullBackOff pod still showed as running. Here we separate:
  - DB "intent" markers: creating / stopping / stopped  (persisted lifecycle)
  - display state: derived live from pod_phase + container status

derive_state is pure (no I/O beyond the injected K8sClient) and is the unit we
test exhaustively (Property 1).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .errors import Codes, FriendlyError
from .k8s_client import K8sClient
from .settings import Settings
from .store import WorkspaceRecord, WorkspaceStore
from . import workspace as ws

log = logging.getLogger(__name__)

# container waiting/terminated reasons that mean "not healthy"
_BAD_WAITING = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
                "CreateContainerConfigError", "InvalidImageName"}

_IMAGE_RE = re.compile(r"^[\w./:-]+(@sha256:[0-9a-f]{64})?$")


def validate_image(image: str) -> None:
    """Raise FriendlyError(400) if the image reference is empty/malformed."""
    if not image or not _IMAGE_RE.match(image):
        raise FriendlyError(
            400, Codes.INVALID_IMAGE,
            f"镜像地址非法: {image!r}",
            hint="请填写合法的容器镜像引用，如 registry/repo:tag",
        )


@dataclass
class DerivedState:
    state: str            # creating|starting|running|stopping|stopped|error
    pod_phase: str | None
    reason: str | None


def derive_state(
    rec: WorkspaceRecord,
    k8s: K8sClient,
    namespace: str,
) -> DerivedState:
    """Map real pod phase + container status to a display state (Property 1).

    Intent markers on the record take precedence where they must:
      - rec.state == 'stopping' and pod still exists → stopping
      - rec.state == 'stopped'  → stopped (don't probe)
    Otherwise derive from the live pod.
    """
    if rec.state == "stopped":
        return DerivedState("stopped", "NotFound", None)

    if not rec.pod_name:
        return DerivedState(rec.state or "creating", None, None)

    phase = k8s.pod_phase(rec.pod_name, namespace)

    if phase == "NotFound":
        # stopping intent + gone → fully stopped; else genuinely stopped
        return DerivedState("stopped", "NotFound", None)

    if rec.state == "stopping":
        # pod still terminating
        return DerivedState("stopping", phase, None)

    # inspect container health to distinguish error causes
    kind, reason = k8s.pod_container_status(rec.pod_name, namespace)

    if kind == "waiting" and reason in _BAD_WAITING:
        return DerivedState("error", phase, reason)
    if kind == "terminated":
        return DerivedState("error", phase, reason)

    if phase == "Pending":
        return DerivedState("starting", "Pending", None)
    if phase == "Running":
        if kind == "ready":
            return DerivedState("running", "Running", None)
        # running pod but containers not all ready yet
        return DerivedState("starting", "Running", None)
    if phase == "Failed":
        return DerivedState("error", "Failed", reason or "PodFailed")
    if phase == "Succeeded":
        # a workspace pod shouldn't normally Succeed; treat as stopped
        return DerivedState("stopped", "Succeeded", None)

    return DerivedState("starting", phase, None)


class WorkspaceService:
    """Owns Workspace lifecycle with real-state awareness. Inject K8sClient."""

    def __init__(self, store: WorkspaceStore, k8s: K8sClient, settings: Settings):
        self._store = store
        self._k8s = k8s
        self._s = settings

    @property
    def _ns(self) -> str:
        return self._s.workspace_namespace

    def derive(self, rec: WorkspaceRecord) -> DerivedState:
        return derive_state(rec, self._k8s, self._ns)

    def start_after_terminating(self, rec: WorkspaceRecord, spec: ws.WorkspaceSpec) -> None:
        """Wait for any old (Terminating) pod to disappear, then create.

        Raises FriendlyError(409) if the old pod won't clear within the window,
        or if create still hits a 409 conflict (Req 4.3/4.4/4.5).
        """
        if self._k8s.pod_phase(spec.pod_name, self._ns) != "NotFound":
            ok = self._k8s.wait_pod_deleted(
                spec.pod_name, self._ns, timeout_s=self._s.workspace_start_wait_s
            )
            if not ok:
                raise FriendlyError(
                    409, Codes.WORKSPACE_TERMINATING,
                    "上一个开发机实例仍在终止，暂时无法启动",
                    hint=f"请等待约 {self._s.workspace_start_wait_s}s 后重试",
                )
        try:
            self._k8s.create_pod(ws.build_pod_manifest(spec), self._ns)
        except RuntimeError as exc:
            # K8sClient.create_pod raises RuntimeError on 409
            if "already exists" in str(exc):
                raise FriendlyError(
                    409, Codes.WORKSPACE_TERMINATING,
                    "上一个开发机实例仍在终止，请稍后重试",
                    hint="实例尚未完全释放",
                ) from exc
            raise
