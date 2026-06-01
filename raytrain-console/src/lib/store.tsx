import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import type { Job, JobStatus, QuotaSummary } from "./types";
import { whoami, getToken, clearToken, type WhoAmI } from "./api";
import {
  fetchJobs,
  cancelJobApi,
  retryJobApi,
  fetchMyQuota,
  fetchProjects,
} from "./consoleApi";

interface StoreValue {
  jobs: Job[];
  loading: boolean;
  error: string;
  refresh: () => void;
  project: string;
  setProject: (p: string) => void;
  projects: string[]; // includes the "All projects" sentinel at index 0
  me: WhoAmI | null;
  logout: () => void;
  getJob: (id: string) => Job | undefined;
  cancelJob: (id: string) => Promise<void>;
  retryJob: (id: string) => Promise<string>; // returns new job id
  quota: QuotaSummary;
}

const ALL = "All projects";
const EMPTY_QUOTA: QuotaSummary = {
  gpu: { used: 0, total: 0 },
  cpu: { used: 0, total: 0 },
  memGi: { used: 0, total: 0 },
};

const Ctx = createContext<StoreValue | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [project, setProject] = useState<string>(ALL);
  const [projectList, setProjectList] = useState<string[]>([]);
  const [me, setMe] = useState<WhoAmI | null>(null);
  const [quota, setQuota] = useState<QuotaSummary>(EMPTY_QUOTA);

  // Resolve the caller's identity + quota + project list from the backend.
  useEffect(() => {
    if (!getToken()) return;
    let alive = true;
    whoami().then((w) => alive && setMe(w)).catch(() => {});
    fetchMyQuota()
      .then((q) => {
        if (!alive) return;
        setQuota({
          gpu: { used: q.usage.gpus, total: q.quota.max_gpus },
          cpu: { used: q.usage.cpus, total: q.quota.max_cpus },
          memGi: { used: q.usage.memory_gi, total: q.quota.max_memory_gi },
        });
      })
      .catch(() => {});
    fetchProjects().then((ps) => alive && setProjectList(ps)).catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const refresh = useCallback(() => {
    let alive = true;
    setLoading(true);
    setError("");
    fetchJobs()
      .then((js) => alive && setJobs(js))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    const stop = refresh();
    return stop;
  }, [refresh]);

  const value = useMemo<StoreValue>(() => {
    return {
      jobs,
      loading,
      error,
      refresh,
      project,
      setProject,
      projects: [ALL, ...projectList],
      me,
      quota,
      logout: () => {
        clearToken();
        location.href = "/login";
      },
      getJob: (id) => jobs.find((j) => j.id === id),
      cancelJob: async (id) => {
        await cancelJobApi(id);
        refresh();
      },
      retryJob: async (id) => {
        const created = await retryJobApi(id);
        setJobs((prev) => [created, ...prev]);
        return created.id;
      },
    };
  }, [jobs, loading, error, refresh, project, projectList, me, quota]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): StoreValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useStore must be used within StoreProvider");
  return v;
}

export type { JobStatus };
