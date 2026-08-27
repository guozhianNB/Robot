import { apiGet, apiPost } from "./client";

export interface SessionUser {
  ok: boolean;
  uid: string | null;
  locked: boolean;
  source: "manual" | "voiceprint" | "none";
}

export function getSessionUser(): Promise<SessionUser> {
  return apiGet<SessionUser>("/api/session/user");
}

export function setSessionUser(uid: string, locked: boolean): Promise<SessionUser> {
  return apiPost<SessionUser>("/api/session/user", { uid, locked });
}
