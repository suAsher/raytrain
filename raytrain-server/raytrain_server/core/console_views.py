"""
Derive the console's Job-detail timeline + RayJob YAML preview from a stored
:class:`PlatformJob`.

Pods/events come from the live cluster (see ``api/console._pods_and_events``),
logs/metrics from Loki/Prometheus, and artifacts from object storage
(``core/artifact_store``) — none of those are synthesized here. This module only
provides:
  - ``build_timeline``: phase timeline derived from the job's own timestamps
  - ``render_rayjob_yaml``: a read-only manifest preview
both deterministic per job so the console reads identical data on every request.
"""
from __future__ import annotations

import time

from .jobs_store import PlatformJob


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def build_timeline(job: PlatformJob) -> list[dict]:
    base = job.created_at
    order = ["Submitted", "Queued", "Admitted", "Starting", "Running"]
    phases: list[dict] = []
    for i, label in enumerate(order):
        phases.append(
            {"key": label.lower(), "label": label, "at": _iso(base + i * 30), "state": "done"}
        )
    st = job.status
    if st == "Queued":
        phases[1]["state"] = "current"
        for k in (2, 3, 4):
            phases[k]["at"] = None
            phases[k]["state"] = "pending"
    elif st == "Starting":
        phases[3]["state"] = "current"
        phases[4]["at"] = None
        phases[4]["state"] = "pending"
    elif st == "Running":
        phases[4]["state"] = "current"
    elif st == "Succeeded":
        phases.append({"key": "succeeded", "label": "Succeeded",
                       "at": _iso(job.finished_at or base + 600), "state": "done"})
    elif st == "Failed":
        phases.append({"key": "failed", "label": "Failed",
                       "at": _iso(job.finished_at or base + 300), "state": "error"})
    elif st == "Cancelled":
        phases[4]["state"] = "current"
        phases.append({"key": "cancelled", "label": "Cancelled",
                       "at": _iso(job.finished_at or base + 120), "state": "error"})
    return phases


def render_rayjob_yaml(job: PlatformJob) -> str:
    r = job.resources
    return f"""apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: {job.name}
  labels:
    raytrain.io/gpu-type: {r.gpu_type.lower()}
spec:
  entrypoint: {job.entrypoint}
  runtimeEnvYAML: |
    working_dir: "{job.code_uri or 'minio://raytrain-code/' + job.name + '.zip'}"
  rayClusterSpec:
    headGroupSpec:
      rayStartParams: {{ num-gpus: "0" }}
      template:
        spec:
          containers:
            - name: ray-head
              image: {job.image}
    workerGroupSpecs:
      - groupName: gpu-workers
        replicas: {r.nodes}
        rayStartParams: {{ num-gpus: "{r.gpus_per_node}" }}
        template:
          spec:
            containers:
              - name: ray-worker
                image: {job.image}
                resources:
                  limits: {{ nvidia.com/gpu: "{r.gpus_per_node}" }}"""
