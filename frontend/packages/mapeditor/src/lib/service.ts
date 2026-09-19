// 编辑器「服务自身」的两件事（规格 §4.2）：
//  1) 自停 —— 打**同源** :8010 的 /api/mapeditor/service/stop（前端因此只认识自己这个源）；
//  2) 关窗 —— window.close() 只对 window.open 打开的窗口有效，手动开的标签页关不掉，由页面提示兜底。
// 不做 serviceStatus()："服务还在不在"由 App.vue 的 statusTick 判（fetch 连不上 = http 0 = 服务没了），
// 再封一个状态函数只会变成没人用的死代码（YAGNI）。

/** 停服务（服务会先回响应、再退出）。失败返回 false，调用方负责提示"服务可能还在跑"。 */
export async function stopService(): Promise<boolean> {
  try {
    const res = await fetch("/api/mapeditor/service/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 关本窗口；关不掉（非脚本打开的标签页）时静默失败，由页面文字兜底。 */
export function closeSelf(): void {
  try {
    window.close();
  } catch {
    /* 忽略：见文件头第 2 条 */
  }
}
