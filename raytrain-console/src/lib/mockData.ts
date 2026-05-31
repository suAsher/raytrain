import type {
  AdminEntity,
  Experiment,
  Job,
  JobStatus,
  Queue,
  QuotaSummary,
  ResourcePool,
} from "./types";
import {
  buildArtifacts,
  buildEvents,
  buildLogs,
  buildMetrics,
  buildPods,
  buildTimeline,
  iso,
  rayJobYaml,
} from "./mockGen";

export const PROJECTS = ["pointcept", "sslod26", "occ-world", "nuscenes-det"];
export const TENANTS = ["research", "platform"];
export const CREATORS = ["zhangsan", "lisi", "wangwu", "asher", "intern-01"];
export const QUEUES = ["h20-research", "h20-shared", "a100-research", "cpu-batch"];

interface Seed {
  name: string;
  status: JobStatus;
  project: string;
  queue: string;
  gpuType: Job["resources"]["gpuType"];
  nodes: number;
  gpusPerNode: number;
  creator: string;
  createdMinAgo: number;
  image: string;
  failureCat?: string;
}

const SEEDS: Seed[] = [
  { name: "sslod26-pretrain-base", status: "Running", project: "sslod26", queue: "h20-research", gpuType: "H20", nodes: 2, gpusPerNode: 8, creator: "asher", createdMinAgo: 32, image: "raytrain/sslod26:cu124-v3" },
  { name: "pointcept-scannet-semseg", status: "Running", project: "pointcept", queue: "h20-shared", gpuType: "H20", nodes: 1, gpusPerNode: 8, creator: "zhangsan", createdMinAgo: 65, image: "raytrain/pointcept:cu124-v3" },
  { name: "occ-world-bevformer-large", status: "Queued", project: "occ-world", queue: "a100-research", gpuType: "A100", nodes: 4, gpusPerNode: 8, creator: "lisi", createdMinAgo: 12, image: "raytrain/occworld:cu121-v2" },
  { name: "nuscenes-det-centerpoint", status: "Failed", project: "nuscenes-det", queue: "h20-shared", gpuType: "H20", nodes: 2, gpusPerNode: 8, creator: "wangwu", createdMinAgo: 48, image: "raytrain/nusc:cu124-v1", failureCat: "OOMKilled" },
  { name: "pointcept-s3dis-ablation-2", status: "Failed", project: "pointcept", queue: "h20-shared", gpuType: "H20", nodes: 1, gpusPerNode: 4, creator: "intern-01", createdMinAgo: 90, image: "raytrain/pointcept:bad-tag", failureCat: "ImagePullBackOff" },
  { name: "sslod26-finetune-nuscenes", status: "Succeeded", project: "sslod26", queue: "h20-research", gpuType: "H20", nodes: 2, gpusPerNode: 8, creator: "asher", createdMinAgo: 240, image: "raytrain/sslod26:cu124-v3" },
  { name: "occ-world-baseline", status: "Succeeded", project: "occ-world", queue: "a100-research", gpuType: "A100", nodes: 1, gpusPerNode: 8, creator: "lisi", createdMinAgo: 320, image: "raytrain/occworld:cu121-v2" },
  { name: "pointcept-scannet-sweep-lr", status: "Cancelled", project: "pointcept", queue: "h20-shared", gpuType: "H20", nodes: 1, gpusPerNode: 8, creator: "zhangsan", createdMinAgo: 150, image: "raytrain/pointcept:cu124-v3" },
  { name: "nuscenes-det-pillarnext", status: "Queued", project: "nuscenes-det", queue: "h20-shared", gpuType: "H20", nodes: 2, gpusPerNode: 8, creator: "wangwu", createdMinAgo: 7, image: "raytrain/nusc:cu124-v1" },
  { name: "sslod26-pretrain-large", status: "Running", project: "sslod26", queue: "h20-research", gpuType: "H20", nodes: 4, gpusPerNode: 8, creator: "asher", createdMinAgo: 180, image: "raytrain/sslod26:cu124-v3" },
  { name: "cpu-preprocess-lance", status: "Running", project: "pointcept", queue: "cpu-batch", gpuType: "CPU-only", nodes: 1, gpusPerNode: 0, creator: "intern-01", createdMinAgo: 22, image: "raytrain/preprocess:v1" },
  { name: "occ-world-debug-smoke", status: "Succeeded", project: "occ-world", queue: "a100-research", gpuType: "A100", nodes: 1, gpusPerNode: 1, creator: "lisi", createdMinAgo: 410, image: "raytrain/occworld:cu121-v2" },
];

function makeJob(seed: Seed, idx: number): Job {
  const failure =
    seed.status === "Failed"
      ? seed.failureCat === "ImagePullBackOff"
        ? {
            category: "ImagePullBackOff",
            summary: "镜像拉取失败：tag 不存在或需要认证",
            detail:
              "ray-head 无法拉取镜像 " + seed.image + "。该 tag 在仓库中不存在。请在 Step 2 选择有效镜像后重试。",
            container: "ray-head",
            logAnchor: 2,
          }
        : {
            category: "OOMKilled",
            summary: "worker-0 内存超限被杀 (exit 137)",
            detail:
              "GPU 显存达到 78.2/80.0 GiB 后 CUDA OOM，进程被 OOMKilled。建议降低 batch size，或把 memory-per-gpu 调高后 Retry。",
            container: "worker-0",
            logAnchor: 12,
          }
      : undefined;

  const withError = seed.status === "Failed" && seed.failureCat === "OOMKilled";
  const durationSec =
    seed.status === "Queued"
      ? 0
      : seed.status === "Running"
      ? seed.createdMinAgo * 60
      : Math.round(seed.createdMinAgo * 0.7 * 60);

  return {
    id: `job-${String(idx + 1).padStart(4, "0")}`,
    name: seed.name,
    status: seed.status,
    project: seed.project,
    queue: seed.queue,
    quotaGroup: seed.project + "-qg",
    priority: idx % 5 === 0 ? "high" : "normal",
    image: seed.image,
    entrypoint:
      seed.gpuType === "CPU-only"
        ? "python tools/preprocess.py --src /data --dst /scratch"
        : "python tools/train.py --config configs/" + seed.project + "/default.py",
    workingDir: "/workspace/" + seed.project,
    gitRef: "main@" + (0xa1f3 + idx).toString(16),
    env: [
      { key: "NCCL_IB_DISABLE", value: seed.nodes > 1 ? "0" : "1" },
      { key: "OMP_NUM_THREADS", value: "8" },
    ],
    creator: seed.creator,
    createdAt: iso(seed.createdMinAgo),
    startedAt: seed.status === "Queued" ? undefined : iso(seed.createdMinAgo - 4),
    durationSec,
    resources: {
      gpuType: seed.gpuType,
      nodes: seed.nodes,
      gpusPerNode: seed.gpusPerNode,
      cpuPerGpu: 8,
      memPerGpuGi: seed.failureCat === "OOMKilled" ? 64 : 96,
      headCpu: 4,
      headMemGi: 16,
      rdma: seed.nodes > 1,
    },
    mounts: {
      dataset: { path: "/data", uri: "minio://datasets/" + seed.project, mode: "ro" },
      checkpoint: {
        path: "/checkpoints",
        uri: "minio://checkpoints/" + seed.name,
        mode: "rw",
        shared: true,
      },
      scratch: { path: "/scratch", sizeGi: 200 },
    },
    failure,
    description:
      "Training run for " + seed.project + ". Submitted from the console create wizard.",
    timeline: buildTimeline(seed.status, seed.createdMinAgo),
    pods: buildPods(seed.status, seed.nodes, seed.gpusPerNode, withError),
    events: buildEvents(seed.status, seed.failureCat),
    logs: buildLogs(seed.status, seed.nodes, withError),
    metrics: seed.status === "Queued" ? buildMetrics(0) : buildMetrics(30),
    artifacts: buildArtifacts(seed.status),
    rayJobYaml: rayJobYaml({
      name: seed.name,
      image: seed.image,
      entrypoint:
        "python tools/train.py --config configs/" + seed.project + "/default.py",
      resources: {
        gpuType: seed.gpuType,
        nodes: seed.nodes,
        gpusPerNode: seed.gpusPerNode,
        cpuPerGpu: 8,
        memPerGpuGi: 96,
        headCpu: 4,
        headMemGi: 16,
        rdma: seed.nodes > 1,
      },
    }),
  };
}

export const JOBS: Job[] = SEEDS.map(makeJob);

export const QUEUE_DATA: Queue[] = [
  {
    name: "h20-research",
    clusterQueue: "cq-h20",
    gpuType: "H20",
    nominal: 64,
    used: 48,
    pending: 8,
    admitted: 6,
    avgWaitMin: 4,
    health: "healthy",
    recentJobs: [
      { id: "job-0001", name: "sslod26-pretrain-base", status: "Running" },
      { id: "job-0010", name: "sslod26-pretrain-large", status: "Running" },
      { id: "job-0006", name: "sslod26-finetune-nuscenes", status: "Succeeded" },
    ],
  },
  {
    name: "h20-shared",
    clusterQueue: "cq-h20",
    gpuType: "H20",
    nominal: 64,
    used: 56,
    pending: 16,
    admitted: 5,
    avgWaitMin: 12,
    health: "degraded",
    recentJobs: [
      { id: "job-0002", name: "pointcept-scannet-semseg", status: "Running" },
      { id: "job-0009", name: "nuscenes-det-pillarnext", status: "Queued" },
      { id: "job-0004", name: "nuscenes-det-centerpoint", status: "Failed" },
    ],
  },
  {
    name: "a100-research",
    clusterQueue: "cq-a100",
    gpuType: "A100",
    nominal: 32,
    used: 8,
    pending: 32,
    admitted: 2,
    avgWaitMin: 26,
    health: "degraded",
    recentJobs: [
      { id: "job-0003", name: "occ-world-bevformer-large", status: "Queued" },
      { id: "job-0007", name: "occ-world-baseline", status: "Succeeded" },
    ],
  },
  {
    name: "cpu-batch",
    clusterQueue: "cq-cpu",
    gpuType: "CPU-only",
    nominal: 512,
    used: 96,
    pending: 0,
    admitted: 1,
    avgWaitMin: 0,
    health: "healthy",
    recentJobs: [{ id: "job-0011", name: "cpu-preprocess-lance", status: "Running" }],
  },
];

export const POOLS: ResourcePool[] = [
  { name: "H20", totalGpu: 128, usedGpu: 104, nodes: 16, health: "degraded" },
  { name: "A100", totalGpu: 32, usedGpu: 8, nodes: 4, health: "healthy" },
  { name: "CPU-only", totalGpu: 0, usedGpu: 0, nodes: 8, health: "healthy" },
];

export const QUOTA: QuotaSummary = {
  gpu: { used: 26, total: 48 },
  cpu: { used: 312, total: 768 },
  memGi: { used: 1840, total: 4096 },
};

export const EXPERIMENTS: Experiment[] = [
  { id: "exp-1", name: "sslod26 pretrain sweep", project: "sslod26", runs: 7, bestMetric: "mIoU 0.732", lastRunAt: iso(60), baselineJobId: "job-0001" },
  { id: "exp-2", name: "scannet semseg ablation", project: "pointcept", runs: 12, bestMetric: "mIoU 0.711", lastRunAt: iso(150), baselineJobId: "job-0002" },
  { id: "exp-3", name: "occ-world bevformer scale", project: "occ-world", runs: 4, bestMetric: "IoU 0.401", lastRunAt: iso(320), baselineJobId: "job-0007" },
];

export const ADMIN: Record<string, AdminEntity[]> = {
  Projects: [
    { id: "p1", name: "pointcept", meta: "3 members · 2 queues", detail: "owner: asher", status: "active" },
    { id: "p2", name: "sslod26", meta: "4 members · 1 queue", detail: "owner: asher", status: "active" },
    { id: "p3", name: "occ-world", meta: "2 members · 1 queue", detail: "owner: lisi", status: "active" },
    { id: "p4", name: "nuscenes-det", meta: "3 members · 1 queue", detail: "owner: wangwu", status: "active" },
  ],
  QuotaGroups: [
    { id: "q1", name: "pointcept-qg", meta: "H20 × 32, A100 × 0", detail: "used 18/32", status: "active" },
    { id: "q2", name: "sslod26-qg", meta: "H20 × 48", detail: "used 40/48", status: "active" },
    { id: "q3", name: "occ-world-qg", meta: "A100 × 16", detail: "used 8/16", status: "active" },
  ],
  "Users / Roles": [
    { id: "u1", name: "asher", meta: "admin", detail: "research tenant", status: "enabled" },
    { id: "u2", name: "zhangsan", meta: "user", detail: "pointcept", status: "enabled" },
    { id: "u3", name: "lisi", meta: "user", detail: "occ-world", status: "enabled" },
    { id: "u4", name: "intern-01", meta: "user", detail: "pointcept (restricted)", status: "disabled" },
  ],
  "Resource Profiles": [
    { id: "r1", name: "h20-single-node", meta: "H20 × 8, 1 node", detail: "cpu 64, mem 768Gi", status: "active" },
    { id: "r2", name: "h20-multi-2n", meta: "H20 × 16, 2 nodes, RDMA", detail: "cpu 128, mem 1.5Ti", status: "active" },
    { id: "r3", name: "a100-debug", meta: "A100 × 1", detail: "cpu 8, mem 96Gi", status: "active" },
  ],
  Queues: [
    { id: "qa", name: "h20-research", meta: "cq-h20 · nominal 64", detail: "borrowing: on", status: "healthy" },
    { id: "qb", name: "h20-shared", meta: "cq-h20 · nominal 64", detail: "borrowing: on", status: "degraded" },
    { id: "qc", name: "a100-research", meta: "cq-a100 · nominal 32", detail: "borrowing: off", status: "degraded" },
  ],
  "Runtime Images": [
    { id: "i1", name: "raytrain/pointcept:cu124-v3", meta: "CUDA 12.4 · Ray 2.54", detail: "torch 2.4, pointcept", status: "active" },
    { id: "i2", name: "raytrain/sslod26:cu124-v3", meta: "CUDA 12.4 · Ray 2.54", detail: "torch 2.4, sslod26", status: "active" },
    { id: "i3", name: "raytrain/occworld:cu121-v2", meta: "CUDA 12.1 · Ray 2.54", detail: "torch 2.3", status: "active" },
  ],
  "Node ResourceFlavors": [
    { id: "f1", name: "h20-flavor", meta: "gpu=h20 · 8/node", detail: "16 nodes, taint: gpu=h20", status: "healthy" },
    { id: "f2", name: "a100-flavor", meta: "gpu=a100 · 8/node", detail: "4 nodes", status: "healthy" },
    { id: "f3", name: "cpu-flavor", meta: "cpu-only", detail: "8 nodes", status: "healthy" },
  ],
};
