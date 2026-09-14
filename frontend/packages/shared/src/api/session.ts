import { apiGet, apiPost, type Surface } from "./client";

export type Role = "admin" | "ward" | "elder";

export interface SessionUser {
  ok?: boolean;
  /** 没有主体时是 null（车前屏靠它判断"没选人"） */
  uid: string | null;
  role: Role;
  locked: boolean;
  slot: Surface;
  /** default | manual | voiceprint | password | auth_disabled | expired | location | logout | auth_reenabled */
  source: string;
  /** 当前病房 uid（全局共享，空串 = 不在病房） */
  ward_uid: string;
  /** 管理员会话剩余秒数；非 admin 或无 TTL 时 null */
  ttl_remain: number | null;
  auth_required: boolean;
  autoswitch: { enabled: boolean; reason: string };
}

export interface WardZone {
  uid: string;
  name?: string;
  kind?: string;
  shape?: string;
  polygon?: number[][];
}

export interface Ward {
  uid: string;
  name: string;
  /** 病房区域所在的地图名（几何真相在地图文件夹的 <图名>.tags.json） */
  ward_map: string;
  /** 关联的区域 uid（形如 z1）；空串 = 未关联 */
  ward_zone: string;
  zone: WardZone | null;
  elders: string[];
}

export function getSessionUser(surface: Surface): Promise<SessionUser> {
  return apiGet<SessionUser>("/api/session/user", surface);
}

export function setSessionUser(uid: string, locked: boolean, surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/user", { uid, locked }, surface);   // 不传 role（R1）
}

/** 口令门登管理员：成功返回 {ok:true, role, ttl_remain}；失败/冷却返回 {ok:false, error}（HTTP 仍 200）。 */
export function login(password: string | null, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; role?: Role; ttl_remain?: number | null }>(
    "/api/session/login", { password }, surface);
}

/** 回落到该槽位的集体层主体（返回体是 principal 形状，没有 `ok` 字段）。 */
export function logout(surface: Surface): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/logout", {}, surface);
}

/** 改管理员口令（非 admin 槽位 → 403）。 */
export function changePassword(oldPw: string, newPw: string, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string }>(
    "/api/session/password", { old: oldPw, new: newPw }, surface);
}

export function getAdminAuth(surface: Surface) {
  return apiGet<{ required: boolean }>("/api/session/admin-auth", surface);
}

export function setAdminAuth(required: boolean, surface: Surface) {
  return apiPost<{ required: boolean }>("/api/session/admin-auth", { required }, surface);
}

export function listWards(surface: Surface) {
  return apiGet<{ ok: boolean; wards: Ward[] }>("/api/wards", surface);
}

export function upsertWard(uid: string, name: string, wardMap: string, wardZone: string,
                          surface: Surface) {
  return apiPost<{ ok: boolean; ward?: Ward; error?: string }>(
    "/api/wards", { uid, name, ward_map: wardMap, ward_zone: wardZone }, surface);
}

/** 便捷录入：以小车当前位姿为圆心、ward_zone_default_r 为半径，把当前房间记成病房区域。 */
export function recordWardZone(wardUid: string, surface: Surface) {
  return apiPost<{ ok: boolean; error?: string; map?: string; zone_uid?: string;
                   zone?: WardZone | null }>(`/api/wards/${encodeURIComponent(wardUid)}/zone`,
                                            {}, surface);
}

/** 把老人归入/移出病房（wardId 传空串 = 移出）。 */
export function assignElderWard(elderUid: string, wardId: string, surface: Surface) {
  return apiPost<{ ok: boolean }>(`/api/profiles/${encodeURIComponent(elderUid)}/ward`,
                                  { ward_id: wardId }, surface);
}
