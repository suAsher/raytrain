// Data access for the console pages, backed by /v1/console/* on raytrain-server.
// Each call uses withFallback: real backend first, mock on failure (which flips
// the global "demo data" banner). The backend returns shapes already aligned to
// the console's domain types (see raytrain_server/api/console.py).

import { apiFetch, withFallback } from "./api";
import type { Job, Queue, Experiment, ResourcePool, JobStatus } from "./types";
import { JOBS, QUEUE_DATA, POOLS, EXPERIMENTS } from "./mockData";

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
  return withFallback(
    () => apiFetch<Job[]>("/v1/console/jobs"),
    () => JOBS
  );
}

export async function fetchJob(id: string): Promise<Job | undefined> {
  return withFallback(
    () => apiFetch<Job>(`/v1/console/jobs/${id}`),
    () => JOBS.find((j) => j.id === id)
  );
}

export async function fetchOverview(): Promise<{
  counts: Record<string, number>;
  pools: ResourcePool[];
  recentFailed: Job[];
  recent: Job[];
}> {
  return withFallback(
    async () => {
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
    },
    () => {
      const counts: Record<string, number> = {};
      JOBS.forEach((j) => (counts[j.status] = (counts[j.status] || 0) + 1));
      return {
        counts,
        pools: POOLS,
        recentFailed: JOBS.filter((j) => j.status === "Failed"),
        recent: [...JOBS].slice(0, 6),
      };
    }
  );
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
  recentJobs: { id: string; name: string; status: JobStatus }[];
}

export async function fetchQueues(): Promise<Queue[]> {
  return withFallback(
    async () => {
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
    },
    () => QUEUE_DATA
  );
}

export async function fetchExperiments(): Promise<Experiment[]> {
  return withFallback(
    () => apiFetch<Experiment[]>("/v1/console/experiments"),
    () => EXPERIMENTS
  );
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
  return withFallback(
    () => apiFetch<ArtifactRow[]>("/v1/console/artifacts"),
    () => {
      const out: ArtifactRow[] = [];
      JOBS.forEach((j) =>
        (j.artifacts || []).forEach((a) =>
          out.push({
            name: a.name,
            kind: a.kind,
            size: a.size,
            path: a.path,
            created_at: a.createdAt,
            jobId: j.id,
            jobName: j.name,
          })
        )
      );
      return out;
    }
  );
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
// Workspaces + DevSessions (开发机) — real endpoints, with mock fallback so the
// pages render even before a cluster is wired.
// --------------------------------------------------------------------------- //

export interface Workspace {
  id: string;
  user: string;
  tenant: string;
  name: string;
  image: string;
  state: string;
  pod_phase?: string | null;
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

const MOCK_WORKSPACES: Workspace[] = [
  { id: "ws-demo01", user: "asher", tenant: "research", name: "ws-pointcept", image: "raytrain/workspace:cpu-base-v1", state: "running", pod_phase: "Running", ide_urls: { jupyter: "#", code: "#", ssh: "ssh://ws-demo01:22" }, cpu: 4, memory_gi: 8, pvc_gi: 100 },
];
const MOCK_DEVSESSIONS: DevSession[] = [
  { id: "dev-demo01", workspace_id: "ws-demo01", user: "asher", gpu_type: "h20", gpu_count: 1, state: "running", pod_phase: "Running", ide_urls: { jupyter: "#" } },
];

export async function fetchWorkspaces(): Promise<Workspace[]> {
  return withFallback(() => apiFetch<Workspace[]>("/v1/workspaces"), () => MOCK_WORKSPACES);
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
  return withFallback(() => apiFetch<DevSession[]>("/v1/dev-sessions"), () => MOCK_DEVSESSIONS);
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
