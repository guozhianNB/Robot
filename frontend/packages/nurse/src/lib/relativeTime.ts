// 相对时间（规格 §5.2）："3 分钟前" / "2 小时前" / "1 天前"。
//
// 口径：用**后端给的时间戳**与**本地当前时间**直接求差，不做任何时区换算花活
// （服务器与护士站 PC 在同一局域网、同一时区；后端 `db.now_iso()` 落的是
// `YYYY-MM-DD HH:MM:SS`，无时区标记，按本地时间解析即正确）。
export function relativeTime(iso: string, nowMs: number = Date.now()): string {
  if (!iso) return "";
  // 补 "T"：Safari / 部分 WebView 不接受 "YYYY-MM-DD HH:MM:SS" 这种空格分隔写法
  const t = Date.parse(iso.includes("T") ? iso : iso.replace(" ", "T"));
  if (Number.isNaN(t)) return "";             // 解析不了就不显示，不抛异常
  const diffS = Math.max(0, Math.floor((nowMs - t) / 1000));   // 服务器时钟略快 → 当"刚刚"
  if (diffS < 60) return "刚刚";
  if (diffS < 3600) return `${Math.floor(diffS / 60)} 分钟前`;
  if (diffS < 86400) return `${Math.floor(diffS / 3600)} 小时前`;
  return `${Math.floor(diffS / 86400)} 天前`;
}
