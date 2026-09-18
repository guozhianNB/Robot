// 构建入口：`pnpm build` → 本脚本 → 真正的 `vite build`
//
// 为什么不是直接写 "vite build"：在**受限沙箱**里 Node 子进程被禁止使用管道 stdio，
// 于是 esbuild 的服务进程起不来（spawn EPERM），而 pnpm 自己捕获输出也走管道 ——
// 直接跑 vite 会连着 pnpm 一起报错。这里改用「文件句柄 stdio」把 vite 的输出接出来，
// 正常开发机上与 admin 的 `vite build` 完全等价（同一个 vite.js、同一个 cwd）。
// 撞上沙箱 EPERM 时**不静默兜底**，而是明确失败并提示改用 `build:sandbox`。
//
// 相关：`scripts/verify-build.mjs` 是沙箱内的等价构建（同一个 vite.config.ts），
// 实测产物与 `vite build` 逐字节相同（SHA256 一致）。

import { spawn } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

{
  const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const viteBin = path.join(pkgRoot, "node_modules", "vite", "bin", "vite.js");

  const tmp = mkdtempSync(path.join(os.tmpdir(), "mapeditor-build-"));
  const inFile = path.join(tmp, "in.txt");
  const outFile = path.join(tmp, "out.txt");
  const errFile = path.join(tmp, "err.txt");
  writeFileSync(inFile, "");
  const inFd = openSync(inFile, "r");
  const outFd = openSync(outFile, "w");
  const errFd = openSync(errFile, "w");

  const code = await new Promise((resolve) => {
    const child = spawn(process.execPath, [viteBin, "build"], {
      cwd: pkgRoot,
      stdio: [inFd, outFd, errFd],
    });
    // 启动失败（vite 没装、路径不对）必须**有话说**，否则用户只看到一个空白的 exit 1
    child.on("error", (e) => {
      process.stderr.write(`[build] 无法启动 vite：${e && e.message}\n`);
      resolve(1);
    });
    child.on("exit", (c) => resolve(c === null ? 1 : c));
  });

  closeSync(inFd);
  closeSync(outFd);
  closeSync(errFd);
  const out = readFileSync(outFile, "utf8");
  const err = readFileSync(errFile, "utf8");
  rmSync(tmp, { recursive: true, force: true });

  const sandboxed = /spawn EPERM/i.test(err) || /spawn EPERM/i.test(out);
  if (code === 0 || !sandboxed) {
    process.stdout.write(out);
    process.stderr.write(err);
    process.exit(code);
  }

  // 走到这里说明是**受限沙箱**（Node 子进程不能用管道 stdio → esbuild 服务进程起不来）。
  // 刻意不静默兜底：让"走了非默认构建路径"这件事显式可见，由开发者决定是否用 build:sandbox。
  process.stderr.write(out);
  process.stderr.write(err);
  process.stderr.write(
    "\n[build] 检测到沙箱禁止子进程管道 stdio（spawn EPERM），esbuild 服务进程无法启动。\n" +
      "        正常情况下请在**真实开发机**上跑 pnpm build（= vite build）。\n" +
      "        若确实在受限沙箱里，请显式改用：pnpm --filter mapeditor build:sandbox\n" +
      "        （同一份 vite.config.ts 的等价构建，实测产物与 vite build 逐字节相同）\n",
  );
  process.exit(1);
}
