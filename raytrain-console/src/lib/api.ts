// Thin API layer for the console.
//
// Real data only: every call hits raytrain-server's /v1/* endpoints. Failures
// surface as ApiError (carrying the backend FriendlyError code/message/hint);
// empty results render as empty states. There is no mock fallback — the UI must
// never present synthesized data as if it were real (Req 14.5/14.6).

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

export class ApiError extends Error {
  status: number;
  code: string;
  hint: string;
  constructor(status: number, message: string, code = "", hint = "") {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
  }
}

// Surface an API error consistently. Prefer the structured FriendlyError
// message (+ hint); fall back to a generic message.
export function errMsg(e: unknown): string {
  if (e instanceof ApiError) {
    return e.hint ? `${e.message}（${e.hint}）` : e.message;
  }
  if (e instanceof Error) return e.message;
  return "unexpected error";
}

// Extract the FriendlyError parts for locale-aware rendering (see
// i18n/localizeError). Non-ApiError values degrade to a bare message.
export function errInfo(e: unknown): { code?: string; message: string; hint?: string } {
  if (e instanceof ApiError) {
    return { code: e.code || undefined, message: e.message, hint: e.hint || undefined };
  }
  if (e instanceof Error) return { message: e.message };
  return { message: "unexpected error" };
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
    throw new ApiError(401, "unauthorized", "UNAUTHORIZED");
  }
  if (!res.ok) {
    let message = res.statusText;
    let code = "";
    let hint = "";
    try {
      const body = await res.json();
      if (body && body.error) {
        // FriendlyError contract: { error: { code, message, hint } }
        message = body.error.message || message;
        code = body.error.code || "";
        hint = body.error.hint || "";
      } else if (body && body.detail) {
        // legacy FastAPI HTTPException: { detail }
        message = typeof body.detail === "string" ? body.detail : message;
      }
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, message, code, hint);
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
