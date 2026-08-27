import { apiPost } from "./client";

export interface AlarmResult {
  ok: boolean;
}

export function reportAlarm(type: string, uid: string, message: string): Promise<AlarmResult> {
  return apiPost<AlarmResult>("/api/alarm", { type, uid, message });
}
