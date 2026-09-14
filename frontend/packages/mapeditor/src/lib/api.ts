// 地图编辑器 REST 封装（不引 axios，只用 fetch）。
// 后端口径：查询降级返回 {ok:true, status:"unavailable", reason}；写失败返回 {ok:false, error}；
// 冲突返回 HTTP 409 + {need_confirm:true, affects}。这里统一成 ApiResult，页面按需展示，绝不吞错。

export interface ApiResult<T> {
  ok: boolean;
  http: number;
  data: T;
  error: string;
}

function build<T>(res: Response): Promise<ApiResult<T>> {
  return res
    .json()
    .catch(() => ({}))
    .then((data: any) => {
      const obj = (data ?? {}) as any;
      const error =
        (typeof obj.error === "string" && obj.error) ||
        (obj.ok === false ? `接口返回失败（HTTP ${res.status}）` : "");
      return { ok: res.ok && obj.ok !== false, http: res.status, data: obj as T, error };
    });
}

/** 统一请求入口：网络异常不抛出，转成 http=0 + error 文本交给页面显示。 */
export async function api<T = any>(url: string, init?: RequestInit): Promise<ApiResult<T>> {
  try {
    const res = await fetch(url, {
      ...init,
      headers: { Accept: "application/json", ...(init?.headers || {}) },
    });
    return await build<T>(res);
  } catch (e: any) {
    return { ok: false, http: 0, data: {} as T, error: `请求失败：${e?.message || e}` };
  }
}

export function getJson<T = any>(url: string): Promise<ApiResult<T>> {
  return api<T>(url, { method: "GET" });
}

export function postJson<T = any>(url: string, body?: unknown): Promise<ApiResult<T>> {
  return api<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function deleteJson<T = any>(url: string): Promise<ApiResult<T>> {
  return api<T>(url, { method: "DELETE" });
}

// ---------------------------------------------------------------- 常用拼接
export const enc = (s: string) => encodeURIComponent(s);

export const mapImageUrl = (name: string) => `/api/map/${enc(name)}/image.png`;
export const mapDownloadUrl = (name: string, file: "yaml" | "pgm" | "tags") =>
  `/api/map/${enc(name)}/download?file=${file}`;
export const mapTagsUrl = (name: string) => `/api/map/${enc(name)}/tags`;
export const destinationsUrl = (name: string) => `/api/destinations?map=${enc(name)}`;
export const zonesUrl = (name: string) => `/api/zones?map=${enc(name)}`;
export const pixelEditorUrl = (name: string) =>
  `/mapeditor/pixel-editor.html?map=${enc(name)}`;

/** 换图命令（改完地图必须重启导航才生效）。 */
export const navCommand = (name: string) => `~/tools/nav_screen.sh nav ${name}`;
