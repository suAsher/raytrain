// Thin axios wrapper around the raytrain Platform API.
// The bearer token is kept in localStorage and injected on every request.

import axios, { AxiosInstance } from "axios";

const TOKEN_KEY = "raytrain.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export const api: AxiosInstance = axios.create({
  baseURL: "/",
  timeout: 30000,
});

api.interceptors.request.use((config) => {
  const t = getToken();
  if (t) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${t}`;
  }
  return config;
});

// On 401 clear the token so the app bounces back to login.
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401) {
      clearToken();
      if (location.pathname !== "/login") {
        location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

// Helper to surface API error detail consistently.
export function errMsg(e: unknown): string {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const anyE = e as any;
  return (
    anyE?.response?.data?.detail ||
    anyE?.message ||
    "unexpected error"
  );
}
