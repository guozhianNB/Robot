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
      // 后端错误有两种形态：自家 _err() 给 {ok:false, error}；FastAPI HTTPException 给 {detail}
      // （例如"未知地图源"的 400）。两种都要能透出，否则用户只看到一句通用失败提示。
      const detail =
        typeof obj.detail === "string" ? obj.detail
          : Array.isArray(obj.detail)
            ? obj.detail.map((d: any) => d?.msg || JSON.stringify(d)).join("; ")
            : "";
      const error =
        (typeof obj.error === "string" && obj.error) ||
        detail ||
        (obj.ok === false ? `接口返回失败（HTTP ${res.status}）` : "");
      return { ok: res.ok && obj.ok !== false, http: res.status, data: obj as T, error };
    });
}

// ---------------------------------------------------------------- 地图源（贯穿所有请求）
// 设计：源是"编辑器自己的旋钮"（运行时可切换、每个浏览器各记一份，存 localStorage）。
// 不放进后端 settings：PC 上跑的后端要 ssh 到板卡、板卡上跑的后端要 local，
// 全局一份会互相覆盖（用户 2026-09-14 定：ROS 端与编辑器解耦）。
const SOURCE_KEY = "mapeditor.source";
let currentSource = (typeof localStorage !== "undefined" && localStorage.getItem(SOURCE_KEY)) || "";

/** 当前地图源 id；空 = 用后端默认源（mapsources.default）。 */
export const getSource = () => currentSource;

export function setSource(id: string) {
  currentSource = id || "";
  try {
    if (currentSource) localStorage.setItem(SOURCE_KEY, currentSource);
    else localStorage.removeItem(SOURCE_KEY);
  } catch {
    /* 隐私模式等：记不住就算了，本次会话内仍生效 */
  }
}

/** 给任意 URL 追加/覆盖 `?source=`。空源不追加（保持旧口径与 URL 干净）。 */
export function withSource(url: string): string {
  if (!currentSource) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}source=${enc(currentSource)}`;
}

/** 统一请求入口：网络异常不抛出，转成 http=0 + error 文本交给页面显示。
 *  ``noSource``：源注册表自身的接口（/api/map/sources*）不该带 ?source=。 */
export async function api<T = any>(
  url: string,
  init?: RequestInit & { noSource?: boolean },
): Promise<ApiResult<T>> {
  const { noSource, ...rest } = init || {};
  const target = noSource ? url : withSource(url);
  try {
    const res = await fetch(target, {
      ...rest,
      headers: { Accept: "application/json", ...(rest.headers || {}) },
    });
    return await build<T>(res);
  } catch (e: any) {
    return { ok: false, http: 0, data: {} as T, error: `请求失败：${e?.message || e}` };
  }
}

export function getJson<T = any>(url: string, init?: RequestInit & { noSource?: boolean }): Promise<ApiResult<T>> {
  return api<T>(url, { ...(init || {}), method: "GET" });
}

export function postJson<T = any>(
  url: string,
  body?: unknown,
  init?: RequestInit & { noSource?: boolean },
): Promise<ApiResult<T>> {
  return api<T>(url, {
    ...(init || {}),
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function deleteJson<T = any>(url: string, init?: RequestInit & { noSource?: boolean }): Promise<ApiResult<T>> {
  return api<T>(url, { ...(init || {}), method: "DELETE" });
}

// ---------------------------------------------------------------- 常用拼接
export const enc = (s: string) => encodeURIComponent(s);

export const mapImageUrl = (name: string, bust = false) =>
  withSource(`/api/map/${enc(name)}/image.png${bust ? `?_=${Date.now()}` : ""}`);
export const mapDownloadUrl = (name: string, file: "yaml" | "pgm" | "tags") =>
  withSource(`/api/map/${enc(name)}/download?file=${file}`);
export const mapTagsUrl = (name: string) => withSource(`/api/map/${enc(name)}/tags`);
export const destinationsUrl = (name: string) => withSource(`/api/destinations?map=${enc(name)}`);
export const zonesUrl = (name: string) => withSource(`/api/zones?map=${enc(name)}`);
export const mapListUrl = () => withSource("/api/map/list");
export const mapMetaUrl = (name: string) => withSource(`/api/map/${enc(name)}/meta`);
export const editorStatusUrl = () => withSource("/api/mapeditor/status");
/** 列表/状态/元数据之外的写接口：调用处自行 withSource()。 */
export const sourcesUrl = () => "/api/map/sources";
export const pixelEditorUrl = (name: string) =>
  withSource(`/mapeditor/pixel-editor.html?map=${enc(name)}`);

/** 换图命令（改完地图必须重启导航才生效）。 */
export const navCommand = (name: string) => `~/tools/nav_screen.sh nav ${name}`;
