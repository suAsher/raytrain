// Data access for the console pages, backed by /v1/console/* on raytrain-server.
// Real data only — no mock fallback. Empty results render as empty states and
// failures surface as FriendlyError messages (Req 14.5/14.6). The backend
// returns shapes already aligned to the console's domain types
// (see raytrain_server/api/console.py).

import { apiFetch } from "./api";
import type { Job, Queue, Experiment, ResourcePool, JobStatus } from "./types";

interface OverviewResp {
  counts: Record<string, number>;
  pools: {
    name: string;
    total_gpu: number;
    used_gpu: number;
    nodes: number;
    health: string;
  }[];
  recentFailed: Partial<Job>[];
  recent: Partial<Job>[];
}

export async function fetchJobs(): Promise<Job[]> {
  return apiFetch<Job[]>("/v1/console/jobs");
}

export async function fetchJob(id: string): Promise<Job | undefined> {
  return apiFetch<Job>(`/v1/console/jobs/${id}`);
}

export async function fetchOverview(): Promise<{
  counts: Record<string, number>;
  pools: ResourcePool[];
  recentFailed: Job[];
  recent: Job[];
}> {
  const r = await apiFetch<OverviewResp>("/v1/console/overview");
  return {
    counts: r.counts,
    pools: r.pools.map((p) => ({
      name: p.name as ResourcePool["name"],
      totalGpu: p.total_gpu,
      usedGpu: p.used_gpu,
      nodes: p.nodes,
      health: p.health as ResourcePool["health"],
    })),
    recentFailed: r.recentFailed as Job[],
    recent: r.recent as Job[],
  };
}

interface QueueResp {
  name: string;
  cluster_queue: string;
  gpu_type: string;
  nominal: number;
  used: number;
  pending: number;
  admitted: number;
  avg_wait_min: number;
  health: string;
  source?: string;
  recentJobs: { id: string; name: string; status: JobStatus }[];
}

export async function fetchQueues(): Promise<Queue[]> {
  const rows = await apiFetch<QueueResp[]>("/v1/console/queues");
  return rows.map((q) => ({
    name: q.name,
    clusterQueue: q.cluster_queue,
    gpuType: q.gpu_type as Queue["gpuType"],
    nominal: q.nominal,
    used: q.used,
    pending: q.pending,
    admitted: q.admitted,
    avgWaitMin: q.avg_wait_min,
    health: q.health as Queue["health"],
    recentJobs: q.recentJobs || [],
  }));
}

export async function fetchExperiments(): Promise<Experiment[]> {
  return apiFetch<Experiment[]>("/v1/console/experiments");
}

// --------------------------------------------------------------------------- //
// Caller's own quota + usage (/v1/quota) — drives the top-bar resource summary.
// max_* == 0 means "unlimited" on the backend.
// --------------------------------------------------------------------------- //

export interface MyQuota {
  user: string;
  quota: { max_gpus: number; max_jobs: number; max_cpus: number; max_memory_gi: number };
  usage: { gpus: number; cpus: number; memory_gi: number; jobs: number };
  remaining: Record<string, number | null>;
}

export async function fetchMyQuota(): Promise<MyQuota> {
  return apiFetch<MyQuota>("/v1/quota");
}

// Runtime images registered by admins (/v1/admin/resources/runtime_image) —
// the Create-Job wizard's image dropdown. Read path is open to any user.
export async function fetchRuntimeImages(): Promise<string[]> {
  const rows = await apiFetch<{ name: string; enabled: boolean }[]>(
    "/v1/admin/resources/runtime_image"
  );
  return rows.filter((r) => r.enabled).map((r) => r.name);
}

// Projects registered by admins — the Create-Job wizard's project dropdown.
export async function fetchProjects(): Promise<string[]> {
  const rows = await apiFetch<{ name: string; enabled: boolean }[]>(
    "/v1/admin/resources/project"
  );
  return rows.filter((r) => r.enabled).map((r) => r.name);
}

// --------------------------------------------------------------------------- //
// Real training logs (Loki) + metrics (Prometheus) for the Job Detail page.
// Both endpoints return an explicit `source` ("loki"/"prometheus"/"unavailable")
// and a `reason` when data isn't available — never synthesized (Req 8/10/14.5).
// --------------------------------------------------------------------------- //

export interface LogLineResp {
  ts?: string;
  container?: string;
  level?: string;
  text: string;
}

export interface JobLogsResp {
  lines: LogLineResp[];
  next_cursor: string | null;
  source: "loki" | "unavailable";
  reason?: string;
}

export async function fetchJobLogs(id: string, container?: string): Promise<JobLogsResp> {
  const qs = container && container !== "all" ? `?container=${encodeURIComponent(container)}` : "";
  return apiFetch<JobLogsResp>(`/v1/console/jobs/${id}/logs${qs}`);
}

export interface MetricPoint {
  t: string;
  value: number;
}

export interface MetricSeriesResp {
  metric: string; // gpu_util | gpu_mem | throughput
  unit: string;
  points: MetricPoint[];
  source: "prometheus" | "unavailable";
}

export interface JobMetricsResp {
  series: MetricSeriesResp[];
  source: "prometheus" | "unavailable";
  reason?: string;
}

export async function fetchJobMetrics(id: string): Promise<JobMetricsResp> {
  return apiFetch<JobMetricsResp>(`/v1/console/jobs/${id}/metrics`);
}

export interface ArtifactRow {
  name: string;
  kind: "checkpoint" | "model" | "log" | "eval";
  size: string;
  path: string;
  created_at: string;
  jobId: string;
  jobName: string;
}

export async function fetchArtifacts(): Promise<ArtifactRow[]> {
  return apiFetch<ArtifactRow[]>("/v1/console/artifacts");
}

// mutations
export async function createJob(body: unknown): Promise<Job> {
  return apiFetch<Job>("/v1/console/jobs", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function cancelJobApi(id: string): Promise<void> {
  await apiFetch(`/v1/console/jobs/${id}/cancel`, { method: "POST" });
}

export async function retryJobApi(id: string): Promise<Job> {
  return apiFetch<Job>(`/v1/console/jobs/${id}/retry`, { method: "POST" });
}

// --------------------------------------------------------------------------- //
// Admin: platform users (/v1/admin/users) — real CRUD, no mock fallback.
// --------------------------------------------------------------------------- //

export interface UserQuota {
  max_gpus: number;
  max_jobs: number;
  max_cpus: number;
  max_memory_gi: number;
}

export interface PlatformUser {
  user: string;
  tenant: string;
  role: "user" | "admin";
  quota: UserQuota;
  projects: string[];
  queues: string[];
  datasets: string[];
  image_prefixes: string[];
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export interface CreateUserBody {
  user: string;
  tenant?: string;
  role?: "user" | "admin";
  password?: string;
  quota?: Partial<UserQuota>;
  projects?: string[];
  datasets?: string[];
  image_prefixes?: string[];
}

export interface UpdateUserBody {
  role?: "user" | "admin";
  enabled?: boolean;
  password?: string;
  quota?: Partial<UserQuota>;
  projects?: string[];
  datasets?: string[];
  image_prefixes?: string[];
}

export async function fetchUsers(): Promise<PlatformUser[]> {
  return apiFetch<PlatformUser[]>("/v1/admin/users");
}

export async function createUser(body: CreateUserBody): Promise<{ user: PlatformUser }> {
  return apiFetch<{ user: PlatformUser }>("/v1/admin/users", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateUser(user: string, body: UpdateUserBody): Promise<PlatformUser> {
  return apiFetch<PlatformUser>(`/v1/admin/users/${user}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteUser(user: string): Promise<void> {
  await apiFetch(`/v1/admin/users/${user}`, { method: "DELETE" });
}

// --------------------------------------------------------------------------- //
// Admin: catalog resources (projects / quota_groups / runtime_images) + queues.
// --------------------------------------------------------------------------- //

export type ResourceKind = "project" | "quota_group" | "runtime_image";

export interface ResourceRecord {
  id: string;
  kind: string;
  name: string;
  spec: Record<string, string | number>;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export async function fetchResources(kind: ResourceKind): Promise<ResourceRecord[]> {
  return apiFetch<ResourceRecord[]>(`/v1/admin/resources/${kind}`);
}

export async function createResource(
  kind: ResourceKind,
  body: { name: string; spec: Record<string, string | number>; enabled?: boolean }
): Promise<ResourceRecord> {
  return apiFetch<ResourceRecord>(`/v1/admin/resources/${kind}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateResource(
  kind: ResourceKind,
  id: string,
  body: { name: string; spec: Record<string, string | number>; enabled: boolean }
): Promise<ResourceRecord> {
  return apiFetch<ResourceRecord>(`/v1/admin/resources/${kind}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteResource(kind: ResourceKind, id: string): Promise<void> {
  await apiFetch(`/v1/admin/resources/${kind}/${id}`, { method: "DELETE" });
}

export interface AdminQueue {
  name: string;
  cluster_queue: string;
  gpu_type: string;
  nominal: number;
  used: number;
  pending: number;
  admitted: number;
  avg_wait_min: number;
  health: string;
}

export async function fetchAdminQueues(): Promise<AdminQueue[]> {
  return apiFetch<AdminQueue[]>("/v1/admin/queues");
}

export async function createQueue(body: {
  name: string;
  cluster_queue: string;
  gpu_type: string;
  nominal: number;
}): Promise<AdminQueue> {
  return apiFetch<AdminQueue>("/v1/admin/queues", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteQueue(name: string): Promise<void> {
  await apiFetch(`/v1/admin/queues/${name}`, { method: "DELETE" });
}

// --------------------------------------------------------------------------- //
// Workspaces + DevSessions (开发机) — real endpoints only. State is derived by
// the backend from the live cluster; empty lists render as empty states.
// --------------------------------------------------------------------------- //

export interface Workspace {
  id: string;
  user: string;
  tenant: string;
  name: string;
  image: string;
  state: string;
  pod_phase?: string | null;
  reason?: string | null;
  ide_urls: Record<string, string>;
  cpu: number;
  memory_gi: number;
  pvc_gi: number;
}

export interface DevSession {
  id: string;
  workspace_id: string;
  user: string;
  gpu_type: string;
  gpu_count: number;
  state: string;
  pod_phase?: string | null;
  ide_urls: Record<string, string>;
}

export async function fetchWorkspaces(): Promise<Workspace[]> {
  // Req 14.5/14.6 — real data only; empty list shows an empty state, never mock.
  return apiFetch<Workspace[]>("/v1/workspaces");
}

export async function createWorkspace(body: {
  name: string;
  cpu?: number;
  memory_gi?: number;
  pvc_gi?: number;
}): Promise<Workspace> {
  return apiFetch<Workspace>("/v1/workspaces", { method: "POST", body: JSON.stringify(body) });
}

export async function workspaceAction(id: string, action: "start" | "stop" | "delete"): Promise<void> {
  if (action === "delete") {
    await apiFetch(`/v1/workspaces/${id}`, { method: "DELETE" });
  } else {
    await apiFetch(`/v1/workspaces/${id}/${action}`, { method: "POST" });
  }
}

export async function fetchDevSessions(): Promise<DevSession[]> {
  // Real data only (no mock masquerade).
  return apiFetch<DevSession[]>("/v1/dev-sessions");
}

export async function createDevSession(body: {
  workspace_id: string;
  gpu_type: string;
  gpu_count: number;
}): Promise<DevSession> {
  return apiFetch<DevSession>("/v1/dev-sessions", { method: "POST", body: JSON.stringify(body) });
}

export async function deleteDevSession(id: string): Promise<void> {
  await apiFetch(`/v1/dev-sessions/${id}`, { method: "DELETE" });
}
