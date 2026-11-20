const API_BASE = import.meta.env.VITE_API_URL || "";

export function getToken() {
  return localStorage.getItem("crm_token") || "";
}

export function setToken(token: string) {
  localStorage.setItem("crm_token", token);
}

export function clearToken() {
  localStorage.removeItem("crm_token");
}

/** Turn API / gateway error bodies into a short toast-safe message. Never returns HTML. */
export function formatApiError(status: number, body: string, fallback = "Request failed"): string {
  const text = (body || "").trim();
  const looksHtml =
    /^<!DOCTYPE/i.test(text) ||
    /^<html[\s>]/i.test(text) ||
    /<\/html>/i.test(text) ||
    /<title>/i.test(text) ||
    /Bad Gateway/i.test(text) ||
    /nginx\//i.test(text);

  if (looksHtml || !text) {
    if (status === 502) return "Server unavailable (502)";
    if (status === 503) return "Server unavailable (503)";
    if (status === 504) return "Server timeout (504)";
    if (status === 500) return "Server error (500)";
    if (status > 0) return `${fallback} (${status})`;
    return fallback;
  }

  try {
    const json = JSON.parse(text) as {
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };
    if (typeof json.detail === "string" && json.detail.trim()) return json.detail.trim();
    if (Array.isArray(json.detail)) {
      const parts = json.detail
        .map((d) => {
          if (typeof d === "string") return d;
          if (d && typeof d === "object" && "msg" in d) return String((d as { msg: unknown }).msg);
          return "";
        })
        .filter(Boolean);
      if (parts.length) return parts.join("; ");
    }
    if (typeof json.message === "string" && json.message.trim()) return json.message.trim();
    if (typeof json.error === "string" && json.error.trim()) return json.error.trim();
  } catch {
    /* not JSON — fall through */
  }

  if (text.includes("<") || text.length > 180) {
    return status > 0 ? `${fallback} (${status})` : fallback;
  }
  return text;
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers = new Headers(opts.headers || {});
  if (!headers.has("Content-Type") && opts.body && !(opts.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(formatApiError(res.status, text, res.statusText || "Request failed"));
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

/** Like api(), but rejects after `ms` so a hung upstream cannot block Promise.all forever. */
export async function apiWithTimeout<T>(path: string, ms: number, opts: RequestInit = {}): Promise<T> {
  const ac = new AbortController();
  const timer = window.setTimeout(() => ac.abort(), Math.max(1, ms));
  const parent = opts.signal;
  const onParentAbort = () => ac.abort();
  if (parent) {
    if (parent.aborted) ac.abort();
    else parent.addEventListener("abort", onParentAbort, { once: true });
  }
  try {
    return await api<T>(path, { ...opts, signal: ac.signal });
  } catch (e) {
    if (ac.signal.aborted && !(parent && parent.aborted)) {
      throw new Error(`Request timed out after ${ms}ms`);
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
    if (parent) parent.removeEventListener("abort", onParentAbort);
  }
}

export async function login(email: string, password: string) {
  const body = new URLSearchParams();
  body.set("username", email);
  body.set("password", password);
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(formatApiError(res.status, text, "Login failed"));
  }
  return res.json();
}
