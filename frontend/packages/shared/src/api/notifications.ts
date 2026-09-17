// 通知中心 REST 客户端（需求文档模块 11 / 护士台数据面）。
//
// 后端契约（`LLM/server.py` 通知路由段 + `LLM/agent/notify.py`）：
//   POST   /api/notifications            —— **免鉴权**投递口（小车/视觉/雷达/任意模块）
//   GET    /api/notifications            —— 列表 + 计数（**仅管理员**）
//   POST   /api/notifications/{id}/ack   —— 标记已处理（仅管理员）
//   POST   /api/notifications/ack-all    —— 全量标记已处理（仅管理员）
//   DELETE /api/notifications/{id}       —— 删单条（仅管理员）
//
// 读/确认/删除必须带 `X-Surface: admin`（后端 `_notice_admin()` 校验槽位角色，非 admin → 403），
// 故这里统一走 `shared/src/api/client.ts` 的既有封装（不自己 new fetch）。
import { apiDelete, apiGet, apiPost } from "./client";

/** 列表一条通知。字段名与后端 `SELECT * FROM notifications` 的列名一一对应
 *  （`uid_name` 由路由层 `_notice_payload()` 补齐，「姓名 · 床号」）。 */
export interface Notice {
  id: number;
  /** info / warning / critical */
  level: string;
  /** kiosk / llm / vision / radar / cart / reminder / system */
  source: string;
  /** **通知类型**（sos / fall / task_done / patrol_done …）—— 表列名就叫 `type`；
   *  注意总线事件里同一语义的键是 `kind`（规格 D6 陷阱）。 */
  type: string;
  uid: string;
  uid_name: string;
  title: string;
  body: string;
  ref: string;
  /** 去重合并计数（同 key 窗口内重复上报 → count+1） */
  count: number;
  created_at: string;
  last_at: string;
  /** 空串 = 未处理 */
  ack_at: string;
  ack_by: string;
}

export interface NoticeCounts {
  unread: number;
  critical: number;
}

export interface NoticeListQuery {
  state?: "all" | "unread";
  limit?: number;
  before_id?: number;
}

export interface NoticeList {
  ok: boolean;
  items: Notice[];
  counts: NoticeCounts;
}

/** 通知列表（默认最近 50 条；`state="unread"` 只回未处理）。 */
export function listNotices(opts: NoticeListQuery = {}): Promise<NoticeList> {
  const q = new URLSearchParams();
  q.set("state", opts.state ?? "all");
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.before_id) q.set("before_id", String(opts.before_id));
  return apiGet<NoticeList>(`/api/notifications?${q.toString()}`, "admin");
}

/** 标记一条已处理（其他屏幕会收到 `notification_ack` 广播同步）。 */
export async function ackNotice(id: number): Promise<void> {
  await apiPost<{ ok: boolean; id: number }>(`/api/notifications/${id}/ack`, {}, "admin");
}

/** 全量标记已处理，返回被处理的条数。 */
export async function ackAllNotices(): Promise<number> {
  const r = await apiPost<{ ok: boolean; acked: number }>(
    "/api/notifications/ack-all", {}, "admin");
  return r.acked ?? 0;
}

/** 删除单条（误报清理）。 */
export async function removeNotice(id: number): Promise<void> {
  await apiDelete<{ ok: boolean }>(`/api/notifications/${id}`, "admin");
}

export interface NoticeIngest {
  source?: string;
  level?: string;
  type: string;
  uid?: string;
  title?: string;
  /** 对应后端 `body` 列（模块 11 原文的字段名是 `message`） */
  message?: string;
  ref?: string;
}

/** 投递一条通知（该口**免鉴权**，供将来的小车侧脚本 / 前端调试用）。
 *  返回 `{ok, id, deduped, level}`；`deduped=true` 表示合并进了已有未处理通知。 */
export function ingestNotice(payload: NoticeIngest) {
  return apiPost<{ ok: boolean; id: number; deduped: boolean; level: string }>(
    "/api/notifications", payload);
}
