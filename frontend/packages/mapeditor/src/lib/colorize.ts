// 灰度像素 → 颜色分类。口径与后端 LLM/maps/mapserver.py 的 occupancy()/classify_pixel() 一致：
//   occ = negate ? pixel/255 : 1 - pixel/255      // 默认（negate=0）黑=占用、白=空闲、灰=未知
//   occ > occupied_thresh → occupied（黑）
//   occ < free_thresh     → free（白）
//   其余                   → unknown（灰 205 → 中灰）

export type PixelKind = "occupied" | "free" | "unknown";

export interface ColorizeOpts {
  negate: number | boolean;
  occupied_thresh: number;
  free_thresh: number;
}

export const UNKNOWN_GRAY = 205;

/** 单像素占用概率（与 ROS map_server 一致）。 */
export function occupancy(pixel: number, maxval: number, negate: number | boolean): number {
  const p = maxval ? Number(pixel) / Number(maxval) : 0;
  return negate ? p : 1 - p;
}

/** 单像素分类：occupied / free / unknown。 */
export function classifyPixel(pixel: number, maxval: number, opts: ColorizeOpts): PixelKind {
  const occ = occupancy(pixel, maxval, opts.negate);
  if (occ > opts.occupied_thresh) return "occupied";
  if (occ < opts.free_thresh) return "free";
  return "unknown";
}

/** 分类 → RGBA 颜色（占用深色 / 空闲浅色 / 未知中灰）。 */
export function colorFor(kind: PixelKind): [number, number, number, number] {
  if (kind === "occupied") return [28, 28, 32, 255]; // 深色 = 占用（原色须与后端 image_png 的灰度语义对应）
  if (kind === "free") return [246, 246, 240, 255]; // 浅色 = 空闲
  return [UNKNOWN_GRAY, UNKNOWN_GRAY, UNKNOWN_GRAY, 255]; // 中灰 = 未知
}
