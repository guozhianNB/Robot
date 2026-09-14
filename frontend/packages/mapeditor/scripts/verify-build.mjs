// 受限沙箱下的构建自验：`pnpm build:sandbox`
//
// 这个脚本不改任何源码、也不改 node_modules，只在当前 Node 进程里做两件事：
//   1. 适配被沙箱禁掉的「子进程管道 stdio」（Vite 会执行 `net use` 探测网络盘）；
//   2. 把 esbuild 换成等价的纯 JS 实现（沙箱里 esbuild 服务进程起不来）。
// 然后调用 Vite 的 build() API，测试环境（sandbox）与参数与 vite.config.ts 一致。
//
// 正常开发机请直接用 `pnpm build`（vite build），不需要本脚本。
// 本脚本只在打包**之前**替换模块解析，产物与正常构建一致（同一个 vite.config.ts）。
//
// ⚠️ 教训（2026-09-14 实测踩到，很隐蔽）：走 `configFile: false` + 自己拼配置时，
// **真 `vite build` 里那套 `define` 替换不会自动生效**。结果是 vue 的 esm-bundler 产物里
// `process.env.NODE_ENV` / `__VUE_PROD_DEVTOOLS__` 原样留在 bundle 中（实测 220 处），
// 而浏览器里没有 `process` → 模块一求值就 ReferenceError → **整页全黑**（比构建失败更难查）。
// 所以下面必须显式补上同一套 define；对照判据：产物里 `process.env` 出现次数应为 0
// （admin 的真 vite 产物就是 0）。

import { register } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import fs from "node:fs";
import path from "node:path";

import "./child-process-stdio.mjs";

register("./esbuild-shim.mjs", pathToFileURL(import.meta.filename));

const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// 先让 Node 原生类型剥离把 vite.config.ts 读进来（绕开 esbuild 的配置打包）
const cfgTs = path.join(pkgRoot, "vite.config.ts");
const cfgModule = await import(pathToFileURL(cfgTs).href);
const cfg = cfgModule.default;

const viteEntry = path.join(pkgRoot, "node_modules", "vite", "dist", "node", "index.js");
const vite = await import(pathToFileURL(viteEntry).href);

// 与真 `vite build` 等价的编译期常量（缺一个就可能把 dev 代码带进产物）
const DEFINE = {
  "process.env.NODE_ENV": JSON.stringify("production"),
  __VUE_OPTIONS_API__: "true",
  __VUE_PROD_DEVTOOLS__: "false",
  __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false",
};

await vite.build({
  ...cfg,
  root: pkgRoot,
  configFile: false,
  envFile: false,
  logLevel: "info",
  define: { ...(cfg.define || {}), ...DEFINE },
  plugins: [...(cfg.plugins || []), { name: "mapeditor:assert-no-process-globals", apply: "build" }],
});

// 产物自检：bundle 里不能残留 process.env / __VUE_* 常量，否则浏览器必然白屏
const outDir = path.resolve(pkgRoot, cfg.build?.outDir || "dist");
const assetsDir = path.join(outDir, "assets");
const problems = [];
for (const f of fs.existsSync(assetsDir) ? fs.readdirSync(assetsDir) : []) {
  if (!f.endsWith(".js")) continue;
  const text = fs.readFileSync(path.join(assetsDir, f), "utf8");
  for (const bad of ["process.env", "__VUE_PROD_DEVTOOLS__", "__VUE_OPTIONS_API__",
                     "__VUE_PROD_HYDRATION_MISMATCH_DETAILS__"]) {
    const n = text.split(bad).length - 1;
    if (n > 0) problems.push(`${f}: 残留 ${bad} × ${n}`);
  }
}
if (problems.length) {
  console.error("\n[build:sandbox] ❌ 产物自检失败——浏览器会因 process/常量未替换而整页白屏：");
  for (const p of problems) console.error("  - " + p);
  console.error("  请检查 define 是否生效（对照：真 vite 产物里 process.env 出现次数应为 0）");
  process.exit(1);
}
console.log("[build:sandbox] ✅ 产物自检通过：define 已生效，无 process.env / __VUE_* 残留");
