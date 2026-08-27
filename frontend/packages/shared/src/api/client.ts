// 统一 REST client：所有 /api 调用走这里（规格 §4）
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${url}`);
  return (await res.json()) as T;
}

export function apiGet<T = any>(url: string): Promise<T> {
  return request<T>(url, { headers: { Accept: "application/json" } });
}

export function apiPost<T = any>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
