// 米 ↔ 像素 换算 + 点包含判断。
// 与后端 LLM/maps/mapserver.py 的 meters_to_pixel / pixel_to_meters **严格同口径**：
//   x_m = origin_x + px * resolution
//   y_m = origin_y + (H - 1 - py) * resolution
//   px  = (x_m - origin_x) / resolution
//   py  = H - 1 - (y_m - origin_y) / resolution
// py 是**图像行号（从上往下）**，所以 y 轴必须翻转 —— 这是最高频错误，改动务必对拍后端。

export type Point = [number, number];
export type Polygon = number[][];

/** 米坐标 → 图像像素坐标（浮点，py 为行号）。 */
export function metersToPixel(
  x: number,
  y: number,
  resolution: number,
  origin: number[] | null | undefined,
  height: number,
): Point {
  const ox = Number(origin?.[0] ?? 0);
  const oy = Number(origin?.[1] ?? 0);
  const res = Number(resolution) || 1; // 防 0 除
  return [(x - ox) / res, height - 1 - (y - oy) / res];
}

/** 图像像素坐标（py 为行号）→ 米坐标。 */
export function pixelToMeters(
  px: number,
  py: number,
  resolution: number,
  origin: number[] | null | undefined,
  height: number,
): Point {
  const ox = Number(origin?.[0] ?? 0);
  const oy = Number(origin?.[1] ?? 0);
  const res = Number(resolution) || 1;
  return [ox + px * res, oy + (height - 1 - py) * res];
}

/**
 * 射线法判断点是否在多边形内（多边形顶点为米坐标，可顺/逆时针）。
 * 传入像素或米坐标均可，只要两套坐标一致。边界上的点结果不作保证。
 */
export function pointInPolygon(x: number, y: number, polygon: Polygon): boolean {
  const n = Array.isArray(polygon) ? polygon.length : 0;
  if (n < 3) return false;
  let inside = false;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const pi = polygon[i];
    const pj = polygon[j];
    if (!pi || !pj) continue;
    const xi = Number(pi[0]);
    const yi = Number(pi[1]);
    const xj = Number(pj[0]);
    const yj = Number(pj[1]);
    // 检测水平射线是否穿过边 (j → i)
    const cross = yi > y !== yj > y;
    if (cross && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
