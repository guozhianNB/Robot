// 统一 REST client：所有 /api 调用走这里（规格 §4）。
// 分层用户体系：带 X-Surface 头区分端槽位（kiosk=车前/语音，admin=管理台），
// 后端据此返回**该槽位的角色**——role 绝不由前端指定（后端红线 R1）。
export type Surface = "kiosk" | "admin";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${url}`);
  return (await res.json()) as T;
}

export function apiGet<T = any>(url: string, surface?: Surface): Promise<T> {
  return request<T>(url, {
    headers: { Accept: "application/json", ...(surface ? { "X-Surface": surface } : {}) },
  });
}

export function apiPost<T = any>(url: string, body?: unknown, surface?: Surface): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(surface ? { "X-Surface": surface } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function apiDelete<T = any>(url: string, surface?: Surface): Promise<T> {
  return request<T>(url, {
    method: "DELETE",
    headers: { Accept: "application/json", ...(surface ? { "X-Surface": surface } : {}) },
  });
}
