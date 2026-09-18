// ★ SSE 事件协议唯一事实来源（规格 §4.1）—— 后端 bus.publish 的事件类型与此对照
export interface ReminderEvent {
  type: "reminder";
  id: number;
  uid: string;
  kind: string;
  title: string;
  content: string;
  status: string;
  missed: boolean;
  silent: boolean;
  time: string;
}

export interface ReminderConfirmedEvent {
  type: "reminder_confirmed";
  id: number;       // 与后端 reminder.py:151 publish("reminder_confirmed", id=rid, ...) 一致（同 reminder 事件用 id 键）
  uid?: string;
  title?: string;
}

export interface AlarmEvent {
  type: "alarm";
  level: string;
  alarm_type?: string;  // sos / fall / health / no_activity ...（不能叫 type，会与 bus.publish 的事件类型键冲突）
  uid?: string;
  message?: string;
}

export interface ChatNewEvent {
  type: "chat_new";
  uid: string;
  user: string;
  assistant: string;
}

export interface ChatPartialEvent {
  type: "chat_partial";
  uid: string;
  delta: string;   // 助手回复实时增量（LLM content delta），kiosk 打字机式追加
}

export interface VoiceStateEvent {
  type: "voice_state";
  state: string;     // idle / listening / recognized / speaking / asr_partial（实时识别字幕，带 text）
  uid?: string;
  text?: string;
}

export interface UserChangedEvent {
  type: "user_changed";
  uid: string;
  locked: boolean;
  /** manual | voiceprint | password | auth_disabled | logout | expired | location | auth_reenabled … */
  source: string;
  role?: "admin" | "ward" | "elder";
  slot?: "kiosk" | "admin";
  ward_uid?: string;
}

export interface VoiceStatusEvent {
  type: "voice_status";
  status: string;   // running / degraded / disabled / stopped（worker._report 广播）
  error?: string;
  retries?: number;
}

/** 管理员会话过期（TTL 到点）——该槽位回落到集体层，前端应退回登录门。 */
export interface SessionExpiredEvent {
  type: "session_expired";
  slot: "kiosk" | "admin";
}

/** 管理员口令门开关变化（开/关都会作废已有 admin 会话）。 */
export interface AdminAuthChangedEvent {
  type: "admin_auth_changed";
  required: boolean;
}

/** 病房数据变化：action = upsert | zone | assign | manual … */
export interface WardChangedEvent {
  type: "ward_changed";
  uid: string;
  action: string;
}

export type BusEvent =
  | ReminderEvent
  | ReminderConfirmedEvent
  | AlarmEvent
  | ChatNewEvent
  | ChatPartialEvent
  | VoiceStateEvent
  | UserChangedEvent
  | VoiceStatusEvent
  | SessionExpiredEvent
  | AdminAuthChangedEvent
  | WardChangedEvent;

const KNOWN_TYPES = new Set([
  "reminder",
  "reminder_confirmed",
  "alarm",
  "chat_new",
  "chat_partial",
  "voice_state",
  "user_changed",
  "voice_status",
  "session_expired",
  "admin_auth_changed",
  "ward_changed",
]);

/** 解析 SSE 原始帧（"data: {...}" 或心跳注释行）→ BusEvent | null */
export function parseBusEvent(raw: string): BusEvent | null {
  if (!raw.startsWith("data:")) return null;       // 心跳注释行等
  return parseBusPayload(raw.slice(5));
}

/** 解析已剥离 "data:" 前缀的 payload JSON（EventSource 的 msg.data 场景）→ BusEvent | null */
export function parseBusPayload(raw: string): BusEvent | null {
  try {
    const payload = JSON.parse(raw.trim()) as Record<string, unknown>;
    const type = payload["type"];
    if (typeof type !== "string" || !KNOWN_TYPES.has(type)) return null;
    return payload as unknown as BusEvent;
  } catch {
    return null;                                    // 坏帧容错
  }
}

/** 将 SSE 流按 \n\n 切帧并解析（供 connectEvents 复用） */
export function parseSseChunk(chunk: string): BusEvent[] {
  const out: BusEvent[] = [];
  for (const frame of chunk.split("\n\n")) {
    const ev = parseBusEvent(frame.trim());
    if (ev) out.push(ev);
  }
  return out;
}
