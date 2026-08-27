import ts from "typescript";
import { defineConfig } from "vitest/config";

// 沙箱环境禁止 fork/pipe 子进程（EPERM）：
//  - pool: "threads" —— worker_threads 同进程线程，绕开 tinypool fork
//  - esbuild: false + 进程内 typescript 转译插件 —— 绕开 esbuild spawn
//  - deps.optimizer 全关 —— 绕开依赖预构建（esbuild）
// 本配置为纯 JS（.mjs），vite 原生 import 加载，不走 esbuild。
const tsTranspilePlugin = {
  name: "ts-transpile-in-process",
  enforce: "pre",
  transform(code, id) {
    if (!/\.[cm]?tsx?$/.test(id)) return null;
    const out = ts.transpileModule(code, {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ESNext,
        jsx: ts.JsxEmit.Preserve,
        esModuleInterop: true,
        sourceMap: false,
      },
      fileName: id,
    });
    return { code: out.outputText, map: null };
  },
};

export default defineConfig({
  esbuild: false,
  plugins: [tsTranspilePlugin],
  test: {
    environment: "node",
    pool: "threads",
    deps: {
      optimizer: { ssr: { enabled: false }, web: { enabled: false } },
    },
  },
});
