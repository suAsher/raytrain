// React Query hooks wrapping the API. Keeps components declarative.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  CreateUserResponse,
  Dataset,
  DevSession,
  JobInfo,
  MyQuota,
  PlatformUser,
  SubmitJobResponse,
  UserQuota,
  Workspace,
  WhoAmI,
} from "./types";

// ---------------- auth ----------------

export function useWhoAmI() {
  return useQuery({
    queryKey: ["whoami"],
    queryFn: async () => (await api.get<WhoAmI>("/v1/auth/me")).data,
    retry: false,
  });
}

// ---------------- workspaces ----------------

export function useWorkspaces() {
  return useQuery({
    queryKey: ["workspaces"],
    queryFn: async () =>
      (await api.get<Workspace[]>("/v1/workspaces")).data,
    refetchInterval: 5000,
  });
}

export function useCreateWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      name: string;
      cpu?: number;
      memory_gi?: number;
      pvc_gi?: number;
    }) => (await api.post<Workspace>("/v1/workspaces", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}

export function useWorkspaceAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (args: { id: string; action: "start" | "stop" | "delete" }) => {
      if (args.action === "delete") {
        await api.delete(`/v1/workspaces/${args.id}`);
      } else {
        await api.post(`/v1/workspaces/${args.id}/${args.action}`);
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}

// ---------------- dev sessions ----------------

export function useDevSessions() {
  return useQuery({
    queryKey: ["devsessions"],
    queryFn: async () =>
      (await api.get<DevSession[]>("/v1/dev-sessions")).data,
    refetchInterval: 5000,
  });
}

export function useCreateDevSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      workspace_id: string;
      gpu_type: string;
      gpu_count: number;
    }) => (await api.post<DevSession>("/v1/dev-sessions", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["devsessions"] }),
  });
}

export function useDeleteDevSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await api.delete(`/v1/dev-sessions/${id}`);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["devsessions"] }),
  });
}

// ---------------- datasets ----------------

export function useDatasets() {
  return useQuery({
    queryKey: ["datasets"],
    queryFn: async () => (await api.get<Dataset[]>("/v1/datasets")).data,
  });
}

export function useRegisterDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      name: string;
      type: string;
      uri: string;
      visibility: string;
      tags: string[];
      description: string;
    }) => (await api.post<Dataset>("/v1/datasets", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["datasets"] }),
  });
}

// ---------------- jobs ----------------

export function useJobs(gpuType: string) {
  return useQuery({
    queryKey: ["jobs", gpuType],
    queryFn: async () =>
      (await api.get<JobInfo[]>("/v1/jobs", { params: { gpu_type: gpuType } }))
        .data,
    refetchInterval: 5000,
  });
}

export function useStopJob(gpuType: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (submissionId: string) => {
      await api.delete(`/v1/jobs/${submissionId}`, {
        params: { gpu_type: gpuType },
      });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useSubmitJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: {
      repo: string;
      exp_name: string;
      gpu_type: string;
      num_nodes: number;
      gpus_per_node: number;
      entrypoint: string;
      code_uri?: string | null;
    }) => (await api.post<SubmitJobResponse>("/v1/jobs", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}


// ---------------- admin: users + quota ----------------

export function useUsers() {
  return useQuery({
    queryKey: ["admin-users"],
    queryFn: async () =>
      (await api.get<PlatformUser[]>("/v1/admin/users")).data,
  });
}

export interface CreateUserBody {
  user: string;
  tenant?: string;
  role?: "user" | "admin";
  quota?: Partial<UserQuota>;
  projects?: string[];
  queues?: string[];
  datasets?: string[];
  image_prefixes?: string[];
  issue_token?: boolean;
  token_days?: number;
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateUserBody) =>
      (await api.post<CreateUserResponse>("/v1/admin/users", body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export interface UpdateUserBody {
  tenant?: string;
  role?: "user" | "admin";
  quota?: Partial<UserQuota>;
  projects?: string[];
  queues?: string[];
  datasets?: string[];
  image_prefixes?: string[];
  enabled?: boolean;
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (args: { user: string; body: UpdateUserBody }) =>
      (await api.patch<PlatformUser>(`/v1/admin/users/${args.user}`, args.body))
        .data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (user: string) => {
      await api.delete(`/v1/admin/users/${user}`);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-users"] }),
  });
}

export function useMyQuota() {
  return useQuery({
    queryKey: ["my-quota"],
    queryFn: async () => (await api.get<MyQuota>("/v1/quota")).data,
    refetchInterval: 10000,
  });
}
