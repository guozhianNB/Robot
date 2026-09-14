// esbuild 的纯 JS 替身（**仅用于 `pnpm build:sandbox` 的自验**，不参与浏览器产物）。
//
// 为什么需要它：某些受限沙箱禁止 Node 子进程使用管道 stdio（`spawn` 直接 EPERM），
// 而 esbuild 的服务进程完全依赖 stdin/stdout 管道协议 —— 于是 Vite 的配置打包与 TS
// 转译都会挂在 "spawn EPERM"。这里用一个等价的纯 JS 实现顶上：
//   * transform()/transformSync() → Node 原生类型剥离 + **define 常量替换**
//   * build()                     → 只做 Vite 的配置文件打包（单文件，保留外部 import）
// 正常开发机上 `pnpm build`（vite build）会走真正的 esbuild，与本文件无关。
//
// ⚠️ 教训（2026-09-14 实测踩到，代价是"整页全黑"）：**`define` 必须实现，不能只剥离类型。**
// Vite 把 `define` 交给 esbuild 的 transform 去做；垫片忽略 `options.define` 时，
// vue 的 esm-bundler 产物里 `process.env.NODE_ENV` / `__VUE_PROD_DEVTOOLS__` 会原样留在
// bundle 里（实测 220 处），而浏览器没有 `process` → 模块求值即 ReferenceError → 全黑，
// 且比"构建失败"难查得多。对照判据：产物里 `process.env` 出现次数应为 0。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const IMPL = `
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { fileURLToPath } from "node:url";

function toPath(spec) {
  return spec.startsWith("file:") ? fileURLToPath(spec) : spec;
}
function strip(code) {
  return stripTypeScriptTypes(code, { mode: "strip" });
}
// --- define 替换（esbuild 的核心职责之一，垫片必须等价支持）---
// 规则：按 key 长度倒序的全局字面替换，与 esbuild 行为一致（足够覆盖 process.env.* / __VUE_*）。
// 跳过 import 说明符那种整行字符串的风险很低：这些 builtin 常量在实践中不会被裹进字符串里。
function applyDefine(code, define) {
  if (!define) return code;
  const keys = Object.keys(define).sort((a, b) => b.length - a.length);
  let out = code;
  for (const k of keys) {
    if (!k) continue;
    const v = define[k];
    const lit = typeof v === "string" ? v : JSON.stringify(v);
    if (out.includes(k)) out = out.split(k).join(lit);
  }
  return out;
}
// Vite 有若干分支即使 sourcemap=false 也会 JSON.parse(result.map)，
// 所以只要调用方显式传了 sourcemap 字段，就一律给一个合法 map 桩。
function wantMap(options) {
  return Object.prototype.hasOwnProperty.call(options, "sourcemap");
}
function makeMap(input, options) {
  return JSON.stringify({
    version: 3,
    sources: [options.sourcefile || "unknown"],
    sourcesContent: [String(input)],
    mappings: "",
    names: [],
  });
}
function convert(input, options = {}) {
  const loader = options.loader || "js";
  let code = String(input);
  if (loader === "ts" || loader === "tsx" || loader === "jsx") code = strip(code);
  code = applyDefine(code, options.define);
  const map = wantMap(options) ? makeMap(input, options) : null;
  return { code, map, warnings: [], errors: [] };
}
export function transform(input, options = {}) {
  return Promise.resolve(convert(input, options));
}
export function transformSync(input, options = {}) {
  return convert(input, options);
}
export function build(options = {}) {
  const inputs = options.entryPoints || [];
  const entry = inputs.length === 1 && typeof inputs[0] === "string"
    ? inputs[0]
    : (inputs.find && inputs.find((v) => typeof v === "string"));
  if (!entry) throw new Error("esbuild-shim: 只支持单入口配置打包");
  const file = toPath(entry);
  const src = readFileSync(file, "utf8");
  let code = /\\.m?ts$/.test(file) ? strip(src) : src;
  code = applyDefine(code, options.define);
  const deps = [];
  const seen = new Set();
  for (const m of code.matchAll(/(?:from\\s*|import\\s*\\(\\s*)["']([^"']+)["']/g)) {
    const spec = m[1];
    if (spec.startsWith(".") || spec.startsWith("/") || spec.startsWith("node:")) continue;
    if (seen.has(spec)) continue;
    seen.add(spec);
    deps.push(spec);
  }
  return Promise.resolve({
    errors: [],
    warnings: [],
    outputFiles: [{ path: entry, text: code }],
    metafile: { inputs: { [entry]: { bytes: code.length, imports: [] } } },
    dependencies: deps,
  });
}
export function buildSync() {
  throw new Error("esbuild-shim: 不支持同步 build（Vite 构建链不会用到）");
}
export function formatMessages(messages) {
  return Promise.resolve((messages || []).map((m) => String(m.text || "")));
}
export function formatMessagesSync(messages) {
  return (messages || []).map((m) => String(m.text || ""));
}
export function analyzeMetafile() { return Promise.resolve(""); }
export function analyzeMetafileSync() { return ""; }
export function context(options) {
  return build(options).then((r) => ({
    rebuild: async () => r,
    watch: async () => {},
    cancel: async () => {},
    dispose: async () => {},
  }));
}
export function stop() { return Promise.resolve(); }
export const version = "0.21.3";
`;

const SHIM_URL = "esbuild-shim:esbuild";
const SHIM_CODE =
  IMPL +
  "\nconst __impl = { transform, transformSync, build, buildSync, formatMessages, " +
  "formatMessagesSync, analyzeMetafile, analyzeMetafileSync, context, stop, version };\n" +
  "export default __impl;\n";

function isEsbuild(spec) {
  const s = spec.startsWith("file:") ? fileURLToPath(spec) : spec;
  return s === "esbuild" || /(^|[/\\])esbuild([/\\]|$)/.test(s);
}

export async function resolve(specifier, context, nextResolve) {
  if (isEsbuild(specifier)) {
    return { url: SHIM_URL, shortCircuit: true, format: "module" };
  }
  return nextResolve(specifier, context);
}

export async function load(url, context, nextLoad) {
  if (url === SHIM_URL) {
    return { format: "module", shortCircuit: true, source: SHIM_CODE };
  }
  return nextLoad(url, context);
}
