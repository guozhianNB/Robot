// 地图编辑器「独立服务」的启停（规格 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）。
// 这三条打的是 **主后端 8000**：编辑器本身跑在 :8010，由主后端拉起/停掉。
import { apiGet, apiPost, type Surface } from "./client";

export interface MapEditorService {
  ok: boolean;
  running: boolean;
  source: "none" | "managed" | "external";
  pid: number | null;
  port: number;
  uptime_s: number | null;
  error?: string;
}

/** 编辑器服务状态（仅管理员）。 */
export function getMapEditorService(surface: Surface): Promise<MapEditorService> {
  return apiGet<MapEditorService>("/api/mapeditor/service", surface);
}

/** 拉起编辑器服务（幂等；已在跑则原样返回现状）。 */
export function startMapEditor(surface: Surface): Promise<MapEditorService> {
  return apiPost<MapEditorService>("/api/mapeditor/service/start", {}, surface);
}

/** 停掉编辑器服务（幂等；编辑器里的「保存并退出」调的是它自己的同源接口）。 */
export function stopMapEditor(surface: Surface): Promise<MapEditorService> {
  return apiPost<MapEditorService>("/api/mapeditor/service/stop", {}, surface);
}
