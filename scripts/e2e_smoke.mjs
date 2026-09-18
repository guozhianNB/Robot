// 无头浏览器冒烟测试（CDP over WebSocket）——只读页面，不写仓库文件。
// 用法: node scripts/e2e_smoke.mjs <url> [<url> ...]
//
// ⚠️ 已知限制：**在 DSH 受限沙箱里跑不起来** —— 浏览器进程会被直接杀掉
// （实测 headless Edge 退出码 0x80000003），与脚本本身无关。请在真实开发机上跑，例如：
//   node scripts/e2e_smoke.mjs "http://127.0.0.1:8000/mapeditor/" ^
//     "http://127.0.0.1:8000/mapeditor/pixel-editor.html?map=my_map"
//
// 为什么不用 shell 管道：本沙箱禁止 Node 子进程用管道 stdio（spawn EPERM），
// 所以浏览器进程必须 stdio:'ignore' 启动，输出改从 CDP（WebSocket）拿。
// 为什么自己实现一个极简 CDP 客户端：npm 无网，装不了 puppeteer/playwright。
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

const EDGE_CANDIDATES = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
];

function findBrowser() {
  for (const p of EDGE_CANDIDATES) if (fs.existsSync(p)) return p;
  throw new Error("找不到 Edge/Chrome");
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function getJson(port, urlPath) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port, path: urlPath, timeout: 4000 }, (res) => {
      let b = "";
      res.on("data", (c) => (b += c));
      res.on("end", () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error("timeout")); });
  });
}

async function waitForCdp(port, ms = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { return await getJson(port, "/json/version"); } catch { await sleep(300); }
  }
  throw new Error("CDP 未就绪");
}

/** 极简 CDP 客户端：WebSocket 握手 + 收发文本帧（不处理分片以外的高级特性）。 */
class Cdp {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.id = 0; this.pending = new Map(); this.events = []; }

  connect() {
    const u = new URL(this.wsUrl);
    return new Promise((resolve, reject) => {
      const key = crypto.randomBytes(16).toString("base64");
      const req = http.request({
        host: u.hostname, port: u.port, path: u.pathname + u.search,
        headers: {
          Connection: "Upgrade", Upgrade: "websocket",
          "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13",
        },
      });
      req.on("upgrade", (res, socket) => {
        this.socket = socket;
        socket.on("data", (buf) => this._onData(buf));
        socket.on("error", reject);
        resolve();
      });
      req.on("error", reject);
      req.end();
    });
  }

  _onData(buf) {
    this._buf = this._buf ? Buffer.concat([this._buf, buf]) : buf;
    for (;;) {
      const b = this._buf;
      if (b.length < 2) return;
      const op = b[0] & 0x0f;
      let len = b[1] & 0x7f;
      let off = 2;
      if (len === 126) { if (b.length < 4) return; len = b.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (b.length < 10) return; len = Number(b.readBigUInt64BE(2)); off = 10; }
      if (b.length < off + len) return;
      const payload = b.subarray(off, off + len);
      this._buf = b.subarray(off + len);
      if (op === 8) { this.socket.end(); return; }
      if (op === 1) {
        let msg;
        try { msg = JSON.parse(payload.toString("utf8")); } catch { continue; }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve, reject } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
        } else if (msg.method) {
          this.events.push(msg);
        }
      }
    }
  }

  send(method, params = {}) {
    const id = ++this.id;
    const data = Buffer.from(JSON.stringify({ id, method, params }), "utf8");
    const mask = crypto.randomBytes(4);
    let head;
    if (data.length < 126) head = Buffer.from([0x81, 0x80 | data.length]);
    else if (data.length < 65536) { head = Buffer.alloc(4); head[0] = 0x81; head[1] = 0xfe; head.writeUInt16BE(data.length, 2); }
    else { head = Buffer.alloc(10); head[0] = 0x81; head[1] = 0xff; head.writeBigUInt64BE(BigInt(data.length), 2); }
    const masked = Buffer.alloc(data.length);
    for (let i = 0; i < data.length; i++) masked[i] = data[i] ^ mask[i % 4];
    this.socket.write(Buffer.concat([head, mask, masked]));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  close() { try { this.socket.end(); } catch {} }
}

async function checkUrl(browser, port, url) {
  const tabs = await getJson(port, "/json/list");
  const page = tabs.find((t) => t.type === "page");
  const cdp = new Cdp(page.webSocketDebuggerUrl);
  await cdp.connect();
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Page.enable");
  await cdp.send("Page.navigate", { url });
  await sleep(7000);

  const res = await cdp.send("Runtime.evaluate", {
    expression: `(() => ({
      title: document.title,
      text: (document.body ? document.body.innerText : '').slice(0, 3000),
      canvases: document.querySelectorAll('canvas').length,
      imgs: Array.from(document.querySelectorAll('img')).map(i => i.naturalWidth + 'x' + i.naturalHeight),
      links: Array.from(document.querySelectorAll('a')).length,
      readyState: document.readyState,
    }))()`,
    returnByValue: true,
  });
  const errs = cdp.events
    .filter((e) => (e.method === "Runtime.exceptionThrown") ||
                   (e.method === "Log.entryAdded" && e.params.entry.level === "error") ||
                   (e.method === "Runtime.consoleAPICalled" && e.params.type === "error"))
    .map((e) => JSON.stringify(e.params).slice(0, 400));
  cdp.close();
  return { url, ...res.result.value, errors: errs };
}

const urls = process.argv.slice(2);
if (!urls.length) { console.error("用法: node scripts/e2e_smoke.mjs <url> [...]"); process.exit(2); }

const port = 9222 + Math.floor(Math.random() * 500);
const profile = path.join(os.tmpdir(), `smoke-${port}`);
fs.mkdirSync(profile, { recursive: true });
const browser = spawn(findBrowser(), [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-background-networking",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore", detached: false });

let failed = false;
try {
  await waitForCdp(port);
  for (const u of urls) {
    const r = await checkUrl(browser, port, u);
    console.log("=".repeat(70));
    console.log("URL   :", r.url);
    console.log("title :", r.title, "| readyState:", r.readyState, "| canvas:", r.canvases,
                "| imgs:", JSON.stringify(r.imgs));
    console.log("text  :", r.text.replace(/\n+/g, " | "));
    console.log("errors:", r.errors.length ? r.errors : "（无）");
    const bad = /NaN|undefined|\[object Object\]/.test(r.text);
    if (r.errors.length || bad || (r.canvases === 0 && r.imgs.every((s) => s.startsWith("0x")))) {
      failed = true;
      console.log("!! 判定：有问题（错误/NaN/没有画布且图未加载）");
    } else {
      console.log("OK 判定：无 JS 错误、无 NaN/undefined、渲染要素存在");
    }
  }
} catch (e) {
  failed = true;
  console.error("冒烟测试异常：", e.message);
} finally {
  try { browser.kill(); } catch {}
}
process.exit(failed ? 1 : 0);
