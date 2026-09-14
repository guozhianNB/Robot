// 地图编辑器前端类型（与后端 pydantic 模型一一对应，字段名禁止改动）

/** 画布绘制模式（App / MapCanvas / ZonePanel 共用） */
export type DrawMode = "idle" | "point" | "polygon" | "rect";

export interface MapMetaFields {
  name: string;
  width: number | null;
  height: number | null;
  resolution: number | null;
  origin: number[] | null;
  negate: number | null;
  occupied_thresh: number | null;
  free_thresh: number | null;
  meta_ok?: boolean;
  problems?: string[];
  unknown_ratio?: number | null;
  counts?: { occupied?: number; free?: number; unknown?: number; total?: number } | null;
  bounds?: { min_x: number; max_x: number; min_y: number; max_y: number } | null;
  stale?: boolean;
  cached_at?: number | string | null;
}

export interface MapCounts {
  destinations: number;
  zones: number;
}

/** /api/map/list 的每一项（map_info 的字段 + 文件态） */
export interface MapListItem {
  name: string;
  has_pgm?: boolean;
  has_yaml?: boolean;
  has_tags?: boolean;
  tags_exists?: boolean;
  width?: number | null;
  height?: number | null;
  resolution?: number | null;
  origin?: number[] | null;
  unknown_ratio?: number | null;
  meta_ok?: boolean;
  problems?: string[];
  stale?: boolean;
  cached_at?: number | string | null;
  counts?: MapCounts;
  status?: string;
  current?: boolean;
}

export interface MapListResp {
  ok: boolean;
  status?: string;
  reason?: string;
  maps?: MapListItem[];
  mode?: string;
  root?: string;
  current_map?: string;
}

export interface MetaResp {
  ok: boolean;
  error?: string;
  meta?: MapMetaFields;
  counts?: MapCounts;
  tags_exists?: boolean;
  tags_warnings?: string[];
  tags_path?: string;
  fingerprint?: { changed?: boolean; reasons?: string[] } | null;
}

export interface IoStatus {
  ok?: boolean;
  mode?: string;
  root?: string;
  available?: boolean;
  reason?: string;
  transport?: string;
  stale?: boolean;
  cached_at?: number | string | null;
  host?: string;
  // 2026-09-14「地图源」新增
  source?: string;
  source_label?: string;
  source_kind?: string;
}

/** 一条地图源（`GET /api/map/sources` 的 items 元素）。界面只读展示、只允许"选"。 */
export interface SourceItem {
  id: string;
  label: string;
  kind: "local" | "ssh";
  note?: string;
  root?: string;
  host?: string;
  user?: string;
  port?: number;
  /** 人类可读的目标：local 是目录、ssh 是 user@host:root */
  target?: string;
  /** local 源的本机路径是否存在；ssh 源为 null（要 /test 才知道） */
  path_ok?: boolean | null;
  is_default?: boolean;
  available?: boolean;
  reason?: string;
}

export interface SourcesResp {
  ok: boolean;
  default: string;
  items: SourceItem[];
  path?: string;
  warnings?: string[];
  env_robot_ip?: string;
  readonly_note?: string;
}

export interface PoseResp {
  ok: boolean;
  status?: "ok" | "unavailable";
  reason?: string;
  source?: string | null;
  x?: number | null;
  y?: number | null;
  yaw?: number | null;
  suspect?: boolean;
  note?: string;
}

export interface CurrentMapResp {
  ok: boolean;
  source?: "map_topic" | "unknown";
  name?: string | null;
  detail?: string;
  /** 多项命中时后端给的是**命中地图名列表**（不是布尔） */
  ambiguous?: string[] | boolean;
}

export interface Destination {
  uid: string;
  name?: string;
  aliases?: string[] | string;
  aliases_list?: string[];
  x?: number | null;
  y?: number | null;
  yaw_deg?: number | null;
  risk?: string;
  elder_allowed?: number;
  note?: string;
  learned_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface DestinationIn {
  map_name: string;
  name: string;
  aliases: string[];
  x: number;
  y: number;
  yaw_deg: number | null;
  risk: string;
  elder_allowed: number;
  note: string;
  learned_by: string;
}

export interface Zone {
  uid: string;
  name?: string;
  kind?: string;
  shape?: string;
  polygon?: number[][];
  parent?: string;
  note?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ZoneIn {
  map_name: string;
  name: string;
  kind: string;
  shape: string;
  polygon: number[][];
  parent: string;
  note: string;
}

/** POST /api/destinations/validate 的载荷 */
export interface ValidateIn {
  map_name: string;
  x: number;
  y: number;
  margin_m?: number;
}

export interface ValidateResp {
  ok: boolean;
  error?: string;
  map_name?: string;
  x?: number;
  y?: number;
  on_obstacle?: boolean;
  on_unknown?: boolean;
  in_bounds?: boolean;
  clearance_m?: number | null;
  edge_margin_m?: number | null;
  pixel?: number[];
  pixel_kind?: string;
  reasons?: string[];
}
