// Thin API layer for the console.
//
// The console is designed ahead of the backend: some pages have real endpoints
// on raytrain-server, others (Queues/Experiments/Artifacts/Job-detail
// pods·events·metrics) do not yet. Every data call therefore tries the real
// API first and falls back to mock data on failure, flipping a global
// `usingMock` flag that the UI surfaces as a "demo data" banner.

const TOKEN_KEY = "raytrain.console.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

// Global mock indicator (read by the shell to show a banner).
let _usingMock = false;
const listeners = new Set<(v: boolean) => void>();
export function usingMock(): boolean {
  return _usingMock;
}
export function onMockChange(fn: (v: boolean) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function setUsingMock(v: boolean) {
  if (_usingMock !== v) {
    _usingMock = v;
    listeners.forEach((fn) => fn(v));
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Surface an API error's detail message consistently.
export function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "unexpected error";
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (res.status === 401) {
    clearToken();
    if (location.pathname !== "/login") location.href = "/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface WhoAmI {
  user: string;
  tenant: string;
  role: "user" | "admin";
  issued_at: number;
  expires_at: number;
}

export async function whoami(): Promise<WhoAmI> {
  return apiFetch<WhoAmI>("/v1/auth/me");
}

export interface LoginResult {
  token: string;
  expires_at: number;
  user: string;
  tenant: string;
  role: string;
}

// Username/password login → stores the returned JWT for subsequent requests.
export async function login(username: string, password: string): Promise<LoginResult> {
  const res = await apiFetch<LoginResult>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(res.token);
  return res;
}

// Try a real API call; on any failure, return the mock value and flag demo mode.
export async function withFallback<T>(
  real: () => Promise<T>,
  mock: () => T
): Promise<T> {
  try {
    const v = await real();
    setUsingMock(false);
    return v;
  } catch {
    setUsingMock(true);
    return mock();
  }
}
