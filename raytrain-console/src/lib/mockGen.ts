// Helpers to synthesize realistic detail payloads (timeline, pods, events,
// logs, metrics, artifacts) for a job so the prototype feels live.

import type {
  Artifact,
  Job,
  JobStatus,
  K8sEvent,
  LogLine,
  MetricSeries,
  PodInfo,
  TimelinePhase,
} from "./types";

export function iso(minAgo: number): string {
  return new Date(Date.now() - minAgo * 60_000).toISOString();
}

export function buildTimeline(status: JobStatus, createdMinAgo: number): TimelinePhase[] {
  const order = ["Submitted", "Queued", "Admitted", "Starting", "Running"];
  const reach = (i: number) => iso(createdMinAgo - i * 2);
  const phases: TimelinePhase[] = order.map((label, i) => ({
    key: label.toLowerCase(),
    label,
    at: reach(i),
    state: "done",
  }));

  if (status === "Queued") {
    phases[1].state = "current";
    phases[2].at = null;
    phases[2].state = "pending";
    phases[3].at = null;
    phases[3].state = "pending";
    phases[4].at = null;
    phases[4].state = "pending";
  } else if (status === "Starting") {
    phases[3].state = "current";
    phases[4].at = null;
    phases[4].state = "pending";
  } else if (status === "Running") {
    phases[4].state = "current";
  } else if (status === "Succeeded") {
    phases.push({ key: "succeeded", label: "Succeeded", at: iso(2), state: "done" });
  } else if (status === "Failed") {
    phases.push({ key: "failed", label: "Failed", at: iso(4), state: "error" });
  } else if (status === "Cancelled") {
    phases[4].state = "current";
    phases.push({ key: "cancelled", label: "Cancelled", at: iso(1), state: "error" });
  }
  return phases;
}

export function buildPods(
  status: JobStatus,
  nodes: number,
  gpusPerNode: number,
  withError = false
): PodInfo[] {
  const pods: PodInfo[] = [];
  pods.push({
    name: "submitter",
    role: "submitter",
    phase: status === "Queued" ? "Pending" : "Succeeded",
    node: "ctrl-node-01",
    restarts: 0,
    gpu: 0,
    ageSec: 1800,
    ip: "10.42.0.12",
    lastEvent: "Job submitted to Ray dashboard",
  });
  const headPhase: PodInfo["phase"] =
    status === "Queued" ? "Pending" : status === "Failed" && withError ? "Failed" : "Running";
  pods.push({
    name: "ray-head",
    role: "head",
    phase: headPhase,
    node: status === "Queued" ? "-" : "h20-node-03",
    restarts: status === "Failed" ? 1 : 0,
    gpu: 0,
    ageSec: status === "Queued" ? 0 : 1620,
    ip: status === "Queued" ? "-" : "10.42.3.4",
    lastEvent: status === "Queued" ? "Unschedulable: waiting for quota" : "Started container ray-head",
  });
  for (let n = 0; n < nodes; n++) {
    const failThis = withError && n === 0;
    pods.push({
      name: `worker-${n}`,
      role: "worker",
      phase: status === "Queued" ? "Pending" : failThis ? "Failed" : status === "Running" ? "Running" : "Succeeded",
      node: status === "Queued" ? "-" : `h20-node-0${4 + n}`,
      restarts: failThis ? 2 : 0,
      gpu: gpusPerNode,
      ageSec: status === "Queued" ? 0 : 1500 - n * 30,
      ip: status === "Queued" ? "-" : `10.42.${4 + n}.5`,
      lastEvent: failThis
        ? "OOMKilled (exit 137)"
        : status === "Queued"
        ? "Pending: insufficient nvidia.com/gpu"
        : "Container started",
    });
  }
  return pods;
}

export function buildEvents(status: JobStatus, failureCat?: string): K8sEvent[] {
  const ev: K8sEvent[] = [
    {
      ts: iso(30),
      type: "Normal",
      reason: "Submitted",
      object: "RayJob/job",
      message: "训练任务已提交，等待 Kueue 准入",
      raw: "Created",
    },
    {
      ts: iso(28),
      type: "Normal",
      reason: "Admitted",
      object: "Workload/job",
      message: "Kueue 已准入，配额满足（H20 × 16）",
      raw: "QuotaReserved",
    },
  ];
  if (status === "Queued") {
    ev.push({
      ts: iso(2),
      type: "Warning",
      reason: "Unschedulable",
      object: "Pod/worker-0",
      message: "队列中：当前 H20 资源池已满，等待 3 个任务释放后可调度",
      raw: "FailedScheduling",
    });
    return ev;
  }
  ev.push({
    ts: iso(26),
    type: "Normal",
    reason: "Pulling",
    object: "Pod/ray-head",
    message: "拉取镜像 raytrain/pointcept:cu124-v3",
    raw: "Pulling",
  });
  ev.push({
    ts: iso(25),
    type: "Normal",
    reason: "Started",
    object: "Pod/ray-head",
    message: "Ray head 启动完成，dashboard 就绪",
    raw: "Started",
  });
  if (status === "Failed") {
    if (failureCat === "OOMKilled") {
      ev.push({
        ts: iso(5),
        type: "Warning",
        reason: "OOMKilled",
        object: "Pod/worker-0",
        message: "worker-0 因内存超限被杀（exit 137）。建议降低 batch size 或提高 memory-per-gpu",
        raw: "OOMKilled",
      });
    } else if (failureCat === "ImagePullBackOff") {
      ev.push({
        ts: iso(6),
        type: "Warning",
        reason: "ImagePullBackOff",
        object: "Pod/ray-head",
        message: "镜像拉取失败：tag 不存在或仓库需要认证。请检查 image 字段",
        raw: "ImagePullBackOff",
      });
    }
  }
  return ev;
}

const LOG_SNIPPETS = [
  "loading config from configs/sslod26/pretrain_base.py",
  "building dataset: ScanNet (lance) rows=1513",
  "world_size=16 rank=0 local_rank=0 master=10.42.3.4:29500",
  "NCCL INFO Bootstrap : Using eth0:10.42.4.5",
  "epoch 1 | iter 50/2000 | loss 4.213 | lr 1.0e-4 | 3.21 it/s",
  "epoch 1 | iter 100/2000 | loss 3.987 | lr 1.0e-4 | 3.18 it/s",
  "epoch 1 | iter 150/2000 | loss 3.640 | lr 1.0e-4 | 3.25 it/s",
  "saving checkpoint to /checkpoints/epoch_1.pth",
  "epoch 2 | iter 50/2000 | loss 3.512 | lr 9.8e-5 | 3.22 it/s",
];

export function buildLogs(status: JobStatus, nodes: number, withError = false): LogLine[] {
  const lines: LogLine[] = [];
  let m = 30;
  const push = (container: string, level: LogLine["level"], text: string) => {
    lines.push({ ts: iso(m), container, level, text });
    m -= 0.2;
  };
  push("submitter", "INFO", "uploading working_dir to minio://raytrain-code/job.zip (24.1 MiB)");
  push("submitter", "INFO", "submitted to Ray dashboard http://ray-shared-h20-head:8265");
  push("ray-head", "INFO", "Ray runtime started. Dashboard at 0.0.0.0:8265");
  push("ray-head", "INFO", "downloading working_dir from minio://raytrain-code/job.zip");
  for (const s of LOG_SNIPPETS) {
    const c = `worker-0`;
    const lvl = s.includes("loss") ? "INFO" : "INFO";
    push(c, lvl, s);
  }
  if (nodes > 1) push("worker-1", "INFO", "epoch 1 | iter 150/2000 | loss 3.655 | 3.20 it/s");
  if (withError) {
    push("worker-0", "WARN", "GPU memory high: 78.2 / 80.0 GiB");
    push("worker-0", "ERROR", "CUDA out of memory. Tried to allocate 2.41 GiB");
    push("worker-0", "ERROR", "RuntimeError: worker terminated (exit code 137, OOMKilled)");
    push("ray-head", "ERROR", "job failed: worker-0 exited unexpectedly");
  } else if (status === "Succeeded") {
    push("worker-0", "INFO", "training complete. best mIoU=0.7321");
    push("ray-head", "INFO", "job finished with exit code 0");
  }
  return lines;
}

function series(n: number, base: number, jitter: number, drift = 0): MetricSeries[] {
  const out: MetricSeries[] = [];
  for (let i = 0; i < n; i++) {
    const t = new Date(Date.now() - (n - i) * 60_000);
    const label = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    const v = Math.max(0, base + drift * i + (Math.random() - 0.5) * jitter);
    out.push({ t: label, value: Math.round(v * 10) / 10 });
  }
  return out;
}

function dualSeries(n: number, aBase: number, bBase: number): MetricSeries[] {
  const out: MetricSeries[] = [];
  for (let i = 0; i < n; i++) {
    const t = new Date(Date.now() - (n - i) * 60_000);
    const label = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    out.push({
      t: label,
      "worker-0": Math.round((aBase + (Math.random() - 0.5) * 12) * 10) / 10,
      "worker-1": Math.round((bBase + (Math.random() - 0.5) * 12) * 10) / 10,
    });
  }
  return out;
}

export function buildMetrics(n = 30): Job["metrics"] {
  return {
    gpuUtil: dualSeries(n, 88, 85),
    gpuMem: dualSeries(n, 72, 70),
    cpu: series(n, 45, 15),
    mem: series(n, 60, 10),
    objStore: series(n, 12, 6, 0.2),
    throughput: series(n, 3.2, 0.4),
  };
}

export function buildArtifacts(status: JobStatus): Artifact[] {
  if (status === "Queued" || status === "Starting") return [];
  const a: Artifact[] = [
    { name: "epoch_1.pth", kind: "checkpoint", size: "1.8 GB", path: "/checkpoints/epoch_1.pth", createdAt: iso(20) },
    { name: "epoch_2.pth", kind: "checkpoint", size: "1.8 GB", path: "/checkpoints/epoch_2.pth", createdAt: iso(10) },
    { name: "train.log", kind: "log", size: "4.2 MB", path: "/checkpoints/train.log", createdAt: iso(8) },
  ];
  if (status === "Succeeded") {
    a.push({ name: "model_final.pth", kind: "model", size: "1.8 GB", path: "/checkpoints/model_final.pth", createdAt: iso(3) });
    a.push({ name: "eval_results.json", kind: "eval", size: "12 KB", path: "/checkpoints/eval_results.json", createdAt: iso(2) });
  }
  return a;
}

export function rayJobYaml(j: Pick<Job, "name" | "image" | "entrypoint" | "resources">): string {
  const { nodes, gpusPerNode, gpuType } = j.resources;
  return `apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: ${j.name}
  labels:
    raytrain.io/gpu-type: ${gpuType.toLowerCase()}
spec:
  entrypoint: ${j.entrypoint}
  runtimeEnvYAML: |
    working_dir: "minio://raytrain-code/${j.name}.zip"
  rayClusterSpec:
    headGroupSpec:
      rayStartParams: { num-gpus: "0" }
      template:
        spec:
          containers:
            - name: ray-head
              image: ${j.image}
    workerGroupSpecs:
      - groupName: gpu-workers
        replicas: ${nodes}
        rayStartParams: { num-gpus: "${gpusPerNode}" }
        template:
          spec:
            containers:
              - name: ray-worker
                image: ${j.image}
                resources:
                  limits: { nvidia.com/gpu: "${gpusPerNode}" }`;
}
