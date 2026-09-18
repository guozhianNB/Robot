// 子进程 stdio 适配（**仅用于 `pnpm verify:build` 的自验**，不参与浏览器产物）。
//
// 受限沙箱禁止 Node 子进程使用管道 stdio，而 Vite 在 Windows 上会执行
// `exec("net use")`（safeRealpathSync 探测网络盘）。这里用「文件句柄 + 轮询」
// 自己实现 exec/execFile，让这类调用能正常返回；正常开发机不会用到本文件。

import cp from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const isWin = process.platform === "win32";
let seq = 0;
const opened = [];

function makeTemp() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "build-verify-"));
  const p = (n) => path.join(dir, `dsh-${seq}-${n}`);
  seq += 1;
  return { inF: p("in.txt"), outF: p("out.txt"), errF: p("err.txt") };
}

function track(fds) {
  opened.push(fds);
  if (opened.length > 400) {
    const old = opened.shift();
    for (const fd of old) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
}

function execAsync(file, args, options, callback) {
  const opts = options && typeof options === "object" ? options : {};
  const cb = typeof options === "function" ? options : callback;
  const { inF, outF, errF } = makeTemp();
  fs.writeFileSync(inF, "");
  const inFd = fs.openSync(inF, "r");
  const outFd = fs.openSync(outF, "w");
  const errFd = fs.openSync(errF, "w");
  track([inFd, outFd, errFd]);

  const child = cp.spawn(file, args, {
    cwd: opts.cwd,
    env: opts.env,
    windowsHide: opts.windowsHide,
    stdio: [inFd, outFd, errFd],
  });

  const cleanup = () => {
    for (const fd of [inFd, outFd, errFd]) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  };

  const timer = setInterval(() => {
    const done = child.exitCode !== null || child.signalCode !== null;
    if (!done) return;
    clearInterval(timer);
    const stdout = fs.readFileSync(outF, "utf8");
    const stderr = fs.readFileSync(errF, "utf8");
    cleanup();
    if (cb) cb(null, stdout, stderr);
  }, 25);

  child.on("error", (err) => {
    clearInterval(timer);
    cleanup();
    if (cb) cb(err, "", "");
  });
  return child;
}

function normalizeExecArgs(command, options, callback) {
  let opts = options;
  let cb = callback;
  if (typeof options === "function") {
    cb = options;
    opts = undefined;
  }
  opts = opts && typeof opts === "object" ? { ...opts } : {};
  const shell = opts.shell || (isWin ? process.env.comspec || "cmd.exe" : "/bin/sh");
  // cmd 的引号规则很坑：变量放在最外层，再用 /d /s /c 整体引号包住
  const args = isWin ? ["/d", "/s", "/c", `"${String(command)}"`] : ["-c", String(command)];
  opts.windowsVerbatimArguments = true;
  return { file: shell, args, opts, cb };
}

cp.exec = function exec(command, options, callback) {
  const { file, args, opts, cb } = normalizeExecArgs(command, options, callback);
  return execAsync(file, args, opts, cb);
};

cp.execFile = function execFile(file, args, options, callback) {
  let a = args;
  let o = options;
  let cb = callback;
  if (typeof a === "function") {
    cb = a;
    a = [];
    o = undefined;
  } else if (typeof o === "function") {
    cb = o;
    o = undefined;
  }
  return execAsync(file, Array.isArray(a) ? a : [], o, cb);
};
