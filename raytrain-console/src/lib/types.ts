// Domain model for the training console. These mirror the shapes returned by
// raytrain-server's /v1/console/* endpoints (all real data).

export type JobStatus =
  | "Running"
  | "Queued"
  | "Failed"
  | "Succeeded"
  | "Cancelled"
  | "Starting";

export type GpuType = "H20" | "A100" | "CPU-only";

export interface TimelinePhase {
  key: string;
  label: string;
  at: string | null; // ISO timestamp, null = not reached
  state: "done" | "current" | "pending" | "error";
}

export interface FailureInfo {
  category: string; // e.g. "OOMKilled", "ImagePullBackOff", "QuotaExceeded"
  summary: string; // human-readable one-liner
  detail: string; // longer explanation / hint
  container?: string;
  logAnchor?: number; // line number in logs to jump to
}

export interface PodInfo {
  name: string;
  role: "head" | "worker" | "submitter";
  phase: "Running" | "Pending" | "Succeeded" | "Failed" | "Terminating";
  node: string;
  restarts: number;
  gpu: number;
  ageSec: number;
  ip: string;
  lastEvent?: string;
}

export interface K8sEvent {
  ts: string;
  type: "Normal" | "Warning";
  reason: string;
  object: string;
  message: string; // human-readable (already translated)
  raw?: string; // original k8s reason for the debug-minded
}

export interface LogLine {
  ts: string;
  container: string;
  level: "INFO" | "WARN" | "ERROR" | "DEBUG";
  text: string;
}

export interface MetricSeries {
  t: string; // label (HH:MM)
  [key: string]: number | string;
}

export interface Artifact {
  name: string;
  kind: "checkpoint" | "model" | "log" | "eval";
  size: string;
  path: string;
  createdAt: string;
}

export interface JobResources {
  gpuType: GpuType;
  nodes: number;
  gpusPerNode: number;
  cpuPerGpu: number;
  memPerGpuGi: number;
  headCpu: number;
  headMemGi: number;
  rdma: boolean;
}

export interface JobMounts {
  dataset: { path: string; uri: string; mode: "ro" | "rw" };
  checkpoint: { path: string; uri: string; mode: "ro" | "rw"; shared: boolean };
  scratch: { path: string; sizeGi: number };
}

export interface Job {
  id: string;
  name: string;
  status: JobStatus;
  project: string;
  queue: string;
  quotaGroup: string;
  priority: "low" | "normal" | "high";
  image: string;
  entrypoint: string;
  workingDir: string;
  gitRef?: string;
  env: { key: string; value: string }[];
  creator: string;
  createdAt: string; // ISO
  startedAt?: string;
  durationSec: number;
  resources: JobResources;
  mounts: JobMounts;
  failure?: FailureInfo;
  description?: string;
  // rich detail payloads (populated by the Job Detail endpoint)
  timeline?: TimelinePhase[];
  pods?: PodInfo[];
  events?: K8sEvent[];
  logs?: LogLine[];
  metrics?: {
    gpuUtil: MetricSeries[];
    gpuMem: MetricSeries[];
    cpu: MetricSeries[];
    mem: MetricSeries[];
    objStore: MetricSeries[];
    throughput: MetricSeries[];
  };
  artifacts?: Artifact[];
  rayJobYaml?: string;
  // set by the backend when the job was really submitted to a Ray cluster
  live?: boolean;
  submissionId?: string;
  // "k8s" when pods/events are read from the live cluster, "unavailable" when
  // the job isn't live (no synthesized pods/events — Req 14.5).
  pods_source?: "k8s" | "unavailable";
  // "minio" when artifacts are listed from object storage, "unavailable" when
  // there's no checkpoint URI / store (no synthesized artifacts — Req 14.6).
  artifacts_source?: "minio" | "unavailable";
}

export interface Queue {
  name: string;
  clusterQueue: string;
  gpuType: GpuType;
  nominal: number; // nominal quota (GPUs)
  used: number;
  pending: number;
  admitted: number;
  avgWaitMin: number;
  health: "healthy" | "degraded" | "down";
  recentJobs: { id: string; name: string; status: JobStatus }[];
}

export interface ResourcePool {
  name: GpuType;
  totalGpu: number;
  usedGpu: number;
  nodes: number;
  health: "healthy" | "degraded" | "down";
}

export interface QuotaSummary {
  gpu: { used: number; total: number };
  cpu: { used: number; total: number };
  memGi: { used: number; total: number };
}

export interface Experiment {
  id: string;
  name: string;
  project: string;
  runs: number;
  bestMetric: string;
  lastRunAt: string;
  baselineJobId: string;
}

export interface AdminEntity {
  id: string;
  name: string;
  meta: string;
  detail: string;
  status?: string;
}
