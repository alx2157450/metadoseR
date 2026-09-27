// 与后端通信：同源部署（生产）或经 Vite 代理（开发）。
export type Json = any;

const TOKEN_KEY = "reviewdesk.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(t: string | null) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 浏览器禁用存储时忽略 */
  }
}

export class ApiError extends Error {
  status: number;
  detail: Json;
  constructor(status: number, detail: Json) {
    super(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request(method: string, path: string, body?: Json, raw = false): Promise<Json> {
  const headers: Record<string, string> = {};
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(path, { method, headers, body: payload });
  if (!res.ok) {
    let detail: Json = res.statusText;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* 非 JSON 错误 */
    }
    if (res.status === 401 && t) setToken(null);
    throw new ApiError(res.status, detail);
  }
  if (raw) return res;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

export const api = {
  get: (p: string) => request("GET", p),
  post: (p: string, b?: Json) => request("POST", p, b ?? {}),
  put: (p: string, b?: Json) => request("PUT", p, b ?? {}),
  patch: (p: string, b?: Json) => request("PATCH", p, b ?? {}),
  del: (p: string) => request("DELETE", p),
  upload: (p: string, fd: FormData) => request("POST", p, fd),
  async download(p: string, filename: string, method = "GET", body?: Json) {
    const res: Response = await request(method, p, body, true);
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  },
};

export async function waitJob(id: number, onProgress?: (j: Json) => void): Promise<Json> {
  for (;;) {
    const j = await api.get(`/api/jobs/${id}`);
    onProgress?.(j);
    if (j.status === "done" || j.status === "failed") return j;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

export const pct = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
