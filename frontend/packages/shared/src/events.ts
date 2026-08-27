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

export interface VoiceStateEvent {
  type: "voice_state";
  state: string;     // idle / wake / listening / recognized / speaking
  uid?: string;
  text?: string;
}

export interface UserChangedEvent {
  type: "user_changed";
  uid: string;
  locked: boolean;
  source: "manual" | "voiceprint";
}

export type BusEvent =
  | ReminderEvent
  | ReminderConfirmedEvent
  | AlarmEvent
  | ChatNewEvent
  | VoiceStateEvent
  | UserChangedEvent;

const KNOWN_TYPES = new Set([
  "reminder",
  "reminder_confirmed",
  "alarm",
  "chat_new",
  "voice_state",
  "user_changed",
]);

/** 解析 SSE 原始帧（"data: {...}" 或心跳注释行）→ BusEvent | null */
export function parseBusEvent(raw: string): BusEvent | null {
  if (!raw.startsWith("data:")) return null;       // 心跳注释行等
  try {
    const payload = JSON.parse(raw.slice(5).trim()) as Record<string, unknown>;
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
