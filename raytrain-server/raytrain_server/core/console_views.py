"""
Derive the console's rich Job-detail payloads (timeline / pods / events /
metrics / artifacts) from a stored :class:`PlatformJob`.

These are computed server-side and deterministic per job, so the console reads
identical data on every request. Telemetry that would normally come from the
live cluster (GPU %, memory curves) is synthesized from the job's resources and
status — clearly platform-derived, not invented per-render in the browser.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from .jobs_store import PlatformJob


def _seeded(job_id: str, salt: str, lo: float, hi: float, i: int = 0) -> float:
    h = hashlib.sha256(f"{job_id}:{salt}:{i}".encode()).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return lo + (hi - lo) * frac


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


def build_pods(job: PlatformJob) -> list[dict]:
    st = job.status
    nodes = job.resources.nodes
    gpn = job.resources.gpus_per_node
    oom = bool(job.failure and job.failure.category == "OOMKilled")
    pods: list[dict] = [
        {
            "name": "submitter", "role": "submitter",
            "phase": "Pending" if st == "Queued" else "Succeeded",
            "node": "ctrl-node-01", "restarts": 0, "gpu": 0,
            "age_sec": int(_seeded(job.id, "subage", 600, 2400)),
            "ip": "10.42.0.12", "last_event": "Job submitted to Ray dashboard",
        },
        {
            "name": "ray-head", "role": "head",
            "phase": "Pending" if st == "Queued" else "Running" if st in ("Running", "Starting") else "Succeeded" if st == "Succeeded" else "Failed" if st == "Failed" else "Terminating",
            "node": "-" if st == "Queued" else "h20-node-03",
            "restarts": 1 if st == "Failed" else 0, "gpu": 0,
            "age_sec": 0 if st == "Queued" else int(_seeded(job.id, "headage", 600, 1800)),
            "ip": "-" if st == "Queued" else "10.42.3.4",
            "last_event": "Unschedulable: waiting for quota" if st == "Queued" else "Started container ray-head",
        },
    ]
    for n in range(max(nodes, 1)):
        fail_this = oom and n == 0
        pods.append(
            {
                "name": f"worker-{n}", "role": "worker",
                "phase": "Pending" if st == "Queued" else "Failed" if fail_this else "Running" if st in ("Running", "Starting") else "Succeeded",
                "node": "-" if st == "Queued" else f"h20-node-0{4 + n}",
                "restarts": 2 if fail_this else 0,
                "gpu": gpn,
                "age_sec": 0 if st == "Queued" else int(_seeded(job.id, f"w{n}", 1200, 1800)),
                "ip": "-" if st == "Queued" else f"10.42.{4 + n}.5",
                "last_event": "OOMKilled (exit 137)" if fail_this else "Pending: insufficient nvidia.com/gpu" if st == "Queued" else "Container started",
            }
        )
    return pods


def build_events(job: PlatformJob) -> list[dict]:
    base = job.created_at
    ev = [
        {"ts": _iso(base), "type": "Normal", "reason": "Submitted",
         "object": f"RayJob/{job.name}", "message": "训练任务已提交，等待 Kueue 准入", "raw": "Created"},
    ]
    if job.status == "Queued":
        ev.append({"ts": _iso(base + 60), "type": "Warning", "reason": "Unschedulable",
                   "object": "Pod/worker-0",
                   "message": f"队列中：{job.queue} 资源池已满，等待资源释放后调度", "raw": "FailedScheduling"})
        return ev
    ev.append({"ts": _iso(base + 30), "type": "Normal", "reason": "Admitted",
               "object": f"Workload/{job.name}",
               "message": f"Kueue 已准入，配额满足（{job.resources.gpu_type} × {job.resources.total_gpu}）", "raw": "QuotaReserved"})
    ev.append({"ts": _iso(base + 60), "type": "Normal", "reason": "Pulling",
               "object": "Pod/ray-head", "message": f"拉取镜像 {job.image}", "raw": "Pulling"})
    ev.append({"ts": _iso(base + 90), "type": "Normal", "reason": "Started",
               "object": "Pod/ray-head", "message": "Ray head 启动完成，dashboard 就绪", "raw": "Started"})
    if job.failure:
        cat = job.failure.category
        ev.append({"ts": _iso(base + 300), "type": "Warning", "reason": cat,
                   "object": f"Pod/{job.failure.container or 'worker-0'}",
                   "message": job.failure.summary, "raw": cat})
    return ev


def build_logs(job: PlatformJob) -> list[dict]:
    base = job.created_at
    oom = bool(job.failure and job.failure.category == "OOMKilled")
    img_fail = bool(job.failure and job.failure.category == "ImagePullBackOff")
    lines: list[dict] = []
    t = [base]

    def push(container: str, level: str, text: str):
        lines.append({"ts": _iso(t[0]), "container": container, "level": level, "text": text})
        t[0] += 12

    push("submitter", "INFO", f"uploading working_dir to {job.code_uri or 'minio://raytrain-code/' + job.name + '.zip'}")
    push("submitter", "INFO", "submitted to Ray dashboard")
    if img_fail:
        push("ray-head", "ERROR", f"Failed to pull image {job.image}: not found or unauthorized")
        push("ray-head", "ERROR", "ImagePullBackOff: back-off pulling image")
        return lines
    push("ray-head", "INFO", "Ray runtime started. Dashboard at 0.0.0.0:8265")
    push("ray-head", "INFO", "downloading working_dir from minio")
    push("worker-0", "INFO", f"loading config from {job.entrypoint}")
    push("worker-0", "INFO", f"world_size={job.resources.total_gpu} rank=0 master=10.42.3.4:29500")
    for it in (50, 100, 150):
        push("worker-0", "INFO", f"epoch 1 | iter {it}/2000 | loss {4.2 - it/100:.3f} | 3.2 it/s")
    if job.resources.nodes > 1:
        push("worker-1", "INFO", "epoch 1 | iter 150/2000 | loss 3.66 | 3.2 it/s")
    if oom:
        push("worker-0", "WARN", "GPU memory high: 78.2 / 80.0 GiB")
        push("worker-0", "ERROR", "CUDA out of memory. Tried to allocate 2.41 GiB")
        push("worker-0", "ERROR", "RuntimeError: worker terminated (exit 137, OOMKilled)")
    elif job.status == "Succeeded":
        push("worker-0", "INFO", "training complete. best mIoU=0.7321")
        push("ray-head", "INFO", "job finished with exit code 0")
    return lines


def _line_series(job: PlatformJob, salt: str, base: float, jitter: float, n: int = 30) -> list[dict]:
    out = []
    now = time.time()
    for i in range(n):
        ts = now - (n - i) * 60
        label = time.strftime("%H:%M", time.localtime(ts))
        v = max(0.0, base + (_seeded(job.id, salt, -1, 1, i)) * jitter)
        out.append({"t": label, "value": round(v, 1)})
    return out


def _dual_series(job: PlatformJob, a: float, b: float, n: int = 30) -> list[dict]:
    out = []
    now = time.time()
    for i in range(n):
        ts = now - (n - i) * 60
        label = time.strftime("%H:%M", time.localtime(ts))
        out.append({
            "t": label,
            "worker-0": round(a + _seeded(job.id, "w0", -6, 6, i), 1),
            "worker-1": round(b + _seeded(job.id, "w1", -6, 6, i), 1),
        })
    return out


def build_metrics(job: PlatformJob) -> dict[str, Any]:
    if job.status in ("Queued",):
        empty: list = []
        return {"gpuUtil": empty, "gpuMem": empty, "cpu": empty, "mem": empty,
                "objStore": empty, "throughput": empty}
    return {
        "gpuUtil": _dual_series(job, 88, 85),
        "gpuMem": _dual_series(job, 72, 70),
        "cpu": _line_series(job, "cpu", 45, 15),
        "mem": _line_series(job, "mem", 60, 10),
        "objStore": _line_series(job, "obj", 12, 6),
        "throughput": _line_series(job, "tp", 3.2, 0.4),
    }


def build_artifacts(job: PlatformJob) -> list[dict]:
    if job.status in ("Queued", "Starting"):
        return []
    base = job.created_at
    a = [
        {"name": "epoch_1.pth", "kind": "checkpoint", "size": "1.8 GB",
         "path": "/checkpoints/epoch_1.pth", "created_at": _iso(base + 600)},
        {"name": "train.log", "kind": "log", "size": "4.2 MB",
         "path": "/checkpoints/train.log", "created_at": _iso(base + 700)},
    ]
    if job.status == "Succeeded":
        a.append({"name": "model_final.pth", "kind": "model", "size": "1.8 GB",
                  "path": "/checkpoints/model_final.pth", "created_at": _iso(base + 800)})
        a.append({"name": "eval_results.json", "kind": "eval", "size": "12 KB",
                  "path": "/checkpoints/eval_results.json", "created_at": _iso(base + 820)})
    return a


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
