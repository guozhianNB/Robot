// 新告警提示音（规格 §5.2）：WebAudio **现场合成**——
// 不引入音频文件、不引入第三方库（全局约束）。
//
// 浏览器要求"用户手势"后才允许出声：未解锁时 `resume()` 会挂起/被拒，
// 这里一律 try/catch 静默失败 —— 提示音是锦上添花，绝不能把异常抛到界面上。
let ctx: AudioContext | null = null;

/** 蜂鸣 `times` 声（约 880Hz、每声 0.18s，声间隔 0.12s）。 */
export function beep(times = 2): void {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;                                   // 老浏览器没有 WebAudio → 静默跳过
    ctx = ctx ?? new Ctor();
    if (ctx.state === "suspended") void ctx.resume().catch(() => { /* 未解锁：静默 */ });
    const start = ctx.currentTime;
    for (let i = 0; i < times; i++) {
      const at = start + i * 0.3;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      // 两端淡入淡出，避免爆音（exponentialRamp 不能从 0 起，故用极小值）
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.25, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at);
      osc.stop(at + 0.18);
    }
  } catch { /* 静默失败：提示音不可用不影响面板 */ }
}
