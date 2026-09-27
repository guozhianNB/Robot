// 统一 REST client：所有 /api 调用走这里（规格 §4）。
// 分层用户体系：带 X-Surface 头区分端槽位（kiosk=车前/语音，admin=管理台），
// 后端据此返回**该槽位的角色**——role 绝不由前端指定（后端红线 R1）。
export type Surface = "kiosk" | "admin";

export class ApiError<TBody = unknown> extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: TBody | undefined;

  constructor(status: number, url: string, body?: TBody) {
    super(`API ${status}: ${url}`);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.body = body;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      // HTTP 状态仍是主错误；空响应或非 JSON 错误页不应掩盖它。
    }
    throw new ApiError(res.status, url, body);
  }
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

/** FastAPI 校验错误的单条结构（`{"detail":[{"msg":"...","loc":[...]}]}`）。 */
interface ValidationIssue {
  msg?: string;
}

/**
 * 后端错误正文里能给人看的那句话。
 *
 * `ApiError.message` 只有 `API 422: /api/plans` 这种定位信息，真正的原因在 body 里：
 * FastAPI 校验失败是 `{"detail":[{...}]}`，路由自己抛 `HTTPException(422, detail="...")`
 * 是 `{"detail":"当前地图未知"}`。业务代码统一用这个函数取原因——否则用户只能看到
 * 一个没有任何解释的状态码（2026-09-27 用户实测踩过）。
 */
export function apiErrorMessage(error: unknown, fallback = "请求失败"): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error && error.message ? error.message : fallback;
  }
  const detail = (error.body as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as ValidationIssue | null)?.msg)
      .filter((msg): msg is string => typeof msg === "string" && !!msg.trim());
    if (messages.length) return `参数不合法：${messages.join("；")}`;
  }
  return error.message;
}
