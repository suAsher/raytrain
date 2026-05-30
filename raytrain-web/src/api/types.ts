// Mirror the server's response shapes (raytrain_server/api/*.py).

export interface WhoAmI {
  user: string;
  tenant: string;
  role: "user" | "admin";
  issued_at: number;
  expires_at: number;
}

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

export interface Dataset {
  id: string;
  name: string;
  type: string;
  uri: string;
  version: string;
  visibility: "private" | "tenant" | "public";
  owner: string;
  tenant: string;
  rows: number;
  size_bytes: number;
  arrow_schema: Record<string, string>;
  tags: string[];
  description: string;
}

export interface JobInfo {
  submission_id: string;
  status: string;
  metadata: Record<string, string>;
}

export interface SubmitJobResponse {
  submission_id: string;
  code_uri: string | null;
  cluster_address: string;
  runtime_env: Record<string, unknown>;
}

// ---------------- platform users / quota (admin) ----------------

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

export interface CreateUserResponse {
  user: PlatformUser;
  token: string | null;
  token_expires_at: number | null;
}

export interface MyQuota {
  user: string;
  quota: UserQuota;
  usage: { gpus: number; cpus: number; memory_gi: number; jobs: number };
  remaining: {
    gpus: number | null;
    cpus: number | null;
    memory_gi: number | null;
    jobs: number | null;
  };
}
