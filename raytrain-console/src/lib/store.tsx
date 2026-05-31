import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import type { Job, JobStatus } from "./types";
import { JOBS, PROJECTS, QUOTA } from "./mockData";
import { buildTimeline } from "./mockGen";
import { whoami, getToken, clearToken, type WhoAmI } from "./api";
import { fetchJobs, cancelJobApi, retryJobApi } from "./consoleApi";

interface StoreValue {
  jobs: Job[];
  loading: boolean;
  refresh: () => void;
  project: string;
  setProject: (p: string) => void;
  me: WhoAmI | null;
  logout: () => void;
  getJob: (id: string) => Job | undefined;
  cancelJob: (id: string) => void;
  retryJob: (id: string) => Promise<string>; // returns new job id
  cloneJob: (id: string) => Job; // returns prefilled draft (not persisted)
  addJob: (j: Job) => string;
  quota: typeof QUOTA;
}

const Ctx = createContext<StoreValue | null>(null);

let seq = JOBS.length;

export function StoreProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [project, setProject] = useState<string>("All projects");
  const [me, setMe] = useState<WhoAmI | null>(null);

  // Resolve the caller's identity from the real backend. If it fails we keep a
  // sensible placeholder so the (demo-data) UI still renders.
  useEffect(() => {
    if (!getToken()) return;
    let alive = true;
    whoami()
      .then((w) => alive && setMe(w))
      .catch(
        () =>
          alive &&
          setMe({ user: "demo", tenant: "research", role: "admin", issued_at: 0, expires_at: 0 })
      );
    return () => {
      alive = false;
    };
  }, []);

  const refresh = useCallback(() => {
    let alive = true;
    setLoading(true);
    fetchJobs()
      .then((js) => alive && setJobs(js))
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
      refresh,
      project,
      setProject,
      me,
      quota: QUOTA,
      logout: () => {
        clearToken();
        location.href = "/login";
      },
      getJob: (id) => jobs.find((j) => j.id === id),
      cancelJob: (id) => {
        // optimistic local update + fire real API (ignore failure → demo mode)
        setJobs((prev) =>
          prev.map((j) =>
            j.id === id && (j.status === "Running" || j.status === "Queued" || j.status === "Starting")
              ? { ...j, status: "Cancelled" as JobStatus }
              : j
          )
        );
        cancelJobApi(id).catch(() => {});
      },
      retryJob: async (id) => {
        const src = jobs.find((j) => j.id === id);
        try {
          const created = await retryJobApi(id);
          setJobs((prev) => [created, ...prev]);
          return created.id;
        } catch {
          // backend unavailable → local demo clone
          if (!src) return id;
          seq += 1;
          const newId = `job-${String(seq).padStart(4, "0")}`;
          const clone: Job = {
            ...src,
            id: newId,
            name: src.name.replace(/-retry\d*$/, "") + "-retry",
            status: "Queued",
            createdAt: new Date().toISOString(),
            startedAt: undefined,
            durationSec: 0,
            failure: undefined,
            timeline: buildTimeline("Queued", 1),
          };
          setJobs((prev) => [clone, ...prev]);
          return newId;
        }
      },
      cloneJob: (id) => {
        const src = jobs.find((j) => j.id === id);
        return src as Job;
      },
      addJob: (j) => {
        seq += 1;
        const newId = `job-${String(seq).padStart(4, "0")}`;
        const withId = { ...j, id: newId };
        setJobs((prev) => [withId, ...prev]);
        return newId;
      },
    };
  }, [jobs, loading, refresh, project, me]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): StoreValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useStore must be used within StoreProvider");
  return v;
}

export const ALL_PROJECTS = ["All projects", ...PROJECTS];
