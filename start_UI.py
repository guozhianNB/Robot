# -*- coding: utf-8 -*-
r"""
养老陪护机器人 —— 一键启动程序（后端 + 前端双端）。

在项目根目录执行（Windows / Linux 通用）：
    python start.py                 # 生产模式：构建产物由 FastAPI 静态托管（8000 端口）
    python start.py --dev           # 开发模式：后端 8000 + Vite dev server（admin 5173 / kiosk 5174）
    python start.py --kiosk-only    # 只打开 kiosk 端页面（默认 admin + kiosk 都开）
    python start.py --no-build      # 跳过前端产物检查（默认：产物缺失时提示构建）

流程（生产模式）：
  1. 检查根目录 .env 是否配置 DEEPSEEK_API_KEY
  2. 检查 8000 端口占用（占用则询问是否结束旧进程重启）
  3. 检查前端构建产物（frontend/packages/{admin,kiosk}/dist），缺失则提示构建
  4. 用虚拟环境解释器启动 uvicorn（LLM.server:app，静态托管 /admin /kiosk）
  5. 轮询 /api/health 确认后端就绪
  6. 浏览器打开 http://127.0.0.1:8000/（admin 默认入口）与 /kiosk/
  7. 常驻前台，Ctrl+C 优雅停止

流程（开发模式 --dev）：
  1-2 同上（.env + 端口检查，额外检查 5173/5174）
  3. 启动后端 uvicorn（8000）
  4. 启动 admin/kiosk Vite dev server（5173/5174，--filter）
  5. 轮询各服务就绪
  6. 浏览器打开 http://localhost:5173/admin/ 与 http://localhost:5174/kiosk/
  7. 常驻前台，Ctrl+C 全部停止

只依赖标准库（subprocess / socket / urllib / webbrowser），与项目
"依赖用 stdlib" 的风格一致。前端构建/开发需要 pnpm（可选，缺失时降级）。
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # 项目根（与 LLM/conf.py::BASE_DIR 一致）
PORT = 8000
HOST = "0.0.0.0"
HEALTH_URL = "http://127.0.0.1:{}/api/health".format(PORT)
ENV_FILE = BASE_DIR / ".env"
HEALTH_TIMEOUT = 30.0        # 健康检查最长等待秒数
POLL_INTERVAL = 0.5          # 健康检查轮询间隔

FRONTEND_DIR = BASE_DIR / "frontend"
ADMIN_DIST = FRONTEND_DIR / "packages" / "admin" / "dist" / "index.html"
KIOSK_DIST = FRONTEND_DIR / "packages" / "kiosk" / "dist" / "index.html"
ADMIN_URL = "http://127.0.0.1:{}/".format(PORT)          # 根入口 → admin（server.py 已配）
KIOSK_URL = "http://127.0.0.1:{}/kiosk/".format(PORT)

DEV_ADMIN_PORT = 5173
DEV_KIOSK_PORT = 5174
DEV_ADMIN_URL = "http://localhost:{}/admin/".format(DEV_ADMIN_PORT)   # base=/admin/，dev 路径带前缀
DEV_KIOSK_URL = "http://localhost:{}/kiosk/".format(DEV_KIOSK_PORT)   # base=/kiosk/

# ---------------------------------------------------------------------------
# 控制台输出（ANSI 颜色，不支持时自动降级为纯文本）
# ---------------------------------------------------------------------------
_ANSI = False
try:
    if os.name == "nt":
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        _kernel32.SetConsoleMode(_kernel32.GetStdHandle(-11), 7)  # 启用 VT 序列
    _ANSI = True
except Exception:
    _ANSI = False


def _c(text, code):
    return "\033[{}m{}\033[0m".format(code, text) if _ANSI else text


_GREEN, _RED, _YELLOW, _CYAN, _BOLD = "32", "31", "33", "36", "1"

_STEP = [0]


def step(msg):
    """步骤日志：[i] 描述"""
    _STEP[0] += 1
    print(_c("[{}] ".format(_STEP[0]), _CYAN) + msg)


def ok(msg):
    """成功日志：[OK] 描述"""
    print(_c("[OK] ", _GREEN) + msg)


def fail(msg):
    """失败日志：[FAIL] 描述"""
    print(_c("[FAIL] ", _RED) + msg)


def warn(msg):
    """警告日志：[WARN] 描述"""
    print(_c("[WARN] ", _YELLOW) + msg)


def info(msg):
    """普通信息日志"""
    print(_c("[..] ", _BOLD) + msg)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def parse_env(key):
    """从 .env 读取指定键的值（简单 KEY=VALUE 解析，忽略 # 注释与空行）。"""
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def port_in_use(port):
    """探测端口是否被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_pid_on_port(port):
    """返回占用端口的进程 PID（Windows 用 netstat，Linux 用 lsof），找不到返回 None。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and ":{}".format(port) in parts[1] and parts[3] == "LISTENING":
                    return parts[-1]
        else:
            out = subprocess.run(["lsof", "-ti", "tcp:{}".format(port)],
                                 capture_output=True, text=True, timeout=10).stdout
            pids = out.split()
            return pids[0] if pids else None
    except Exception as exc:  # 工具缺失/超时等，不阻断流程
        warn("无法定位占用端口的进程：{}".format(exc))
    return None


def kill_pid(pid):
    """结束进程：Windows 用 taskkill /F，Linux 用 kill -9。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, text=True)


def pick_python():
    """选择虚拟环境解释器：Windows .venv\\Scripts\\python.exe，Linux .venv/bin/python。"""
    if os.name == "nt":
        candidates = [BASE_DIR / ".venv" / "Scripts" / "python.exe"]
    else:
        candidates = [BASE_DIR / ".venv" / "bin" / "python"]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def find_pnpm():
    """定位 pnpm 可执行文件（Windows 上可能是 pnpm.ps1 / pnpm.cmd，Linux 是 pnpm）。"""
    exe = shutil.which("pnpm")
    if exe:
        return exe
    # 常见安装位置兜底
    candidates = [
        Path.home() / "AppData" / "Local" / "pnpm" / "pnpm.ps1",   # Windows pnpm 默认
        Path.home() / "AppData" / "Local" / "pnpm" / "pnpm.cmd",
        Path("/usr/local/bin/pnpm"),
        Path("/usr/bin/pnpm"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def health_ready():
    """GET /api/health，返回是否就绪。"""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def http_ready(url, timeout=2.0):
    """任意 HTTP 端点探测（dev server 就绪检查用）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def ensure_port_free(port, label):
    """端口占用处理：询问是否结束旧进程。返回 True=可继续，False=取消。"""
    pid = None
    if port_in_use(port):
        pid = find_pid_on_port(port)
        warn("端口 {} 已被占用（{}）".format(port, label) + ("（PID {}）".format(pid) if pid else ""))
        answer = input(_c("  是否结束该进程并重启？[y/N] ", _YELLOW)).strip().lower()
        if answer not in ("y", "yes"):
            info("已取消启动，请自行处理占用 {} 端口的进程后重试".format(port))
            return False
        if pid:
            step("结束旧进程 PID {}".format(pid))
            kill_pid(pid)
            time.sleep(1.0)
            if port_in_use(port):
                fail("端口 {} 仍被占用，无法启动".format(port))
                return False
            ok("旧进程已结束")
        else:
            fail("未能定位占用进程，请手动关闭占用 {} 端口的程序".format(port))
            return False
    else:
        ok("端口 {} 空闲".format(port))
    return True


def check_env():
    """检查 .env 与 DEEPSEEK_API_KEY。返回 True=通过。"""
    step("检查 .env 配置")
    if not ENV_FILE.is_file():
        fail("未找到 {}".format(ENV_FILE))
        info("请在项目根目录创建 .env，并写入 DEEPSEEK_API_KEY=你的密钥")
        return False
    api_key = parse_env("DEEPSEEK_API_KEY")
    if not api_key:
        fail(".env 中未配置 DEEPSEEK_API_KEY（或值为空）")
        info("请编辑 {} 补充：DEEPSEEK_API_KEY=你的密钥".format(ENV_FILE))
        return False
    ok("DEEPSEEK_API_KEY 已配置（{}...）".format(api_key[:6]))
    return True


def check_frontend_build(no_build_check=False):
    """检查前端产物。返回 True=可用（或跳过检查）。"""
    if no_build_check:
        return True
    step("检查前端构建产物")
    missing = []
    if not ADMIN_DIST.is_file():
        missing.append("admin: {}".format(ADMIN_DIST))
    if not KIOSK_DIST.is_file():
        missing.append("kiosk: {}".format(KIOSK_DIST))
    if missing:
        warn("前端产物缺失：")
        for m in missing:
            warn("  - " + m)
        info("请在正常终端执行构建（本机若需代理请先设置）：")
        info("    cd frontend && pnpm install && pnpm build")
        info("或使用开发模式：python start.py --dev")
        return False
    ok("admin / kiosk 构建产物就绪")
    return True


def start_backend(python):
    """启动 uvicorn 后端，返回 Popen。"""
    cmd = [python, "-m", "uvicorn", "LLM.server:app", "--host", HOST, "--port", str(PORT)]
    step("启动后端：{}".format(" ".join(cmd)))
    proc = subprocess.Popen(cmd, cwd=str(BASE_DIR))  # 输出透传到当前终端
    info("后端进程 PID：{}".format(proc.pid))
    info("服务地址：http://127.0.0.1:{}/api/health".format(PORT))
    return proc


def wait_backend(proc):
    """轮询 /api/health。返回 True=就绪。"""
    step("等待后端就绪（最长 {} 秒）".format(int(HEALTH_TIMEOUT)))
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_report = 0.0
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fail("后端进程提前退出，退出码：{}".format(proc.returncode))
            info("请查看上方 uvicorn 日志确认失败原因（常见：依赖缺失 / 端口被占 / .env 配置错误）")
            return False
        if health_ready():
            ready = True
            break
        now = time.monotonic()
        if now - last_report >= 5.0:  # 每 5 秒报一次进度，避免刷屏
            last_report = now
            info("后端尚未就绪（已等待 {:.0f}s）……".format(now - (deadline - HEALTH_TIMEOUT)))
        time.sleep(POLL_INTERVAL)
    if not ready:
        warn("{} 秒内未探测到 /api/health，后端可能启动失败，请查看上方 uvicorn 日志".format(int(HEALTH_TIMEOUT)))
        warn("若仍在启动中可继续等待；按 Ctrl+C 停止并排查")
        return False
    ok("LLM 端已就绪：http://127.0.0.1:{}/api/health".format(PORT))
    return True


def start_dev_servers():
    """启动 admin/kiosk Vite dev server（--dev 模式）。返回 Popen 列表。"""
    pnpm = find_pnpm()
    if not pnpm:
        fail("未找到 pnpm，无法启动 Vite dev server")
        info("请安装 pnpm（npm install -g pnpm）或使用生产模式：python start.py")
        return None

    procs = []
    for name, port, url in (("admin", DEV_ADMIN_PORT, DEV_ADMIN_URL),
                            ("kiosk", DEV_KIOSK_PORT, DEV_KIOSK_URL)):
        if port_in_use(port):
            warn("端口 {} 已被占用（{} dev server），跳过启动——请确认是否是已运行的实例".format(port, name))
            continue
        cmd = [pnpm, "--filter", name, "dev"]
        step("启动 {} dev server：{}（端口 {}）".format(name, " ".join(cmd), port))
        proc = subprocess.Popen(cmd, cwd=str(FRONTEND_DIR))
        procs.append((name, port, url, proc))
        info("{} dev server PID：{}".format(name, proc.pid))

    # 轮询就绪
    for name, port, url, proc in procs:
        deadline = time.monotonic() + 30.0
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                warn("{} dev server 提前退出，退出码：{}".format(name, proc.returncode))
                break
            if http_ready(url):
                ready = True
                break
            time.sleep(POLL_INTERVAL)
        if ready:
            ok("{} dev server 就绪：{}".format(name, url))
        else:
            warn("{} dev server 未在 30 秒内就绪，请检查上方输出（Vite 首次启动可能较慢）".format(name))
    return procs


def open_pages(urls):
    """在浏览器打开页面列表。"""
    for uri in urls:
        step("打开页面：{}".format(uri))
        try:
            webbrowser.open(uri)
            ok("已在浏览器打开：{}".format(uri))
        except Exception as exc:
            warn("自动打开浏览器失败（{}），请手动打开：{}".format(exc, uri))


def stop_procs(procs):
    """优雅停止所有子进程（Ctrl+C 时调用）。"""
    for name, proc in procs:
        info("正在停止 {}（PID {}）……".format(name, proc.pid))
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    ok("全部进程已停止")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="养老陪护机器人一键启动（后端 + 前端双端）")
    parser.add_argument("--dev", action="store_true",
                        help="开发模式：后端 8000 + Vite dev server（admin 5173 / kiosk 5174）")
    parser.add_argument("--kiosk-only", action="store_true",
                        help="只打开 kiosk 端页面（默认 admin + kiosk 都打开）")
    parser.add_argument("--no-build", dest="no_build", action="store_true",
                        help="跳过前端构建产物检查（生产模式默认会检查）")
    args = parser.parse_args()

    print()
    info(_c("===== 养老陪护机器人一键启动 =====", _BOLD))
    info("模式：{}".format("开发模式（--dev）" if args.dev else "生产模式（构建产物）"))
    info("项目根目录：{}".format(BASE_DIR))

    # ---- 1. 检查 .env ----
    if not check_env():
        return 1

    # ---- 2. 端口检查 ----
    step("检查端口占用情况")
    if not ensure_port_free(PORT, "后端 uvicorn"):
        return 1

    # ---- 3. 前端就绪检查 ----
    if args.dev:
        pnpm = find_pnpm()
        if not pnpm:
            fail("未找到 pnpm，无法启动 --dev 模式")
            info("请安装 pnpm（npm install -g pnpm），或改用生产模式：python start.py")
            return 1
        if not ensure_port_free(DEV_ADMIN_PORT, "admin dev server"):
            return 1
        if not ensure_port_free(DEV_KIOSK_PORT, "kiosk dev server"):
            return 1
    else:
        if not check_frontend_build(no_build_check=args.no_build):
            return 1

    # ---- 4. 选择解释器并启动后端 ----
    python = pick_python()
    if python:
        info("使用虚拟环境解释器：{}".format(python))
    else:
        warn("未找到 .venv 解释器，改用系统 Python：{}".format(sys.executable))
        warn("若依赖未安装，后端会启动失败，请先创建虚拟环境并安装依赖")
        python = sys.executable

    backend = start_backend(python)
    procs = [("后端 uvicorn", backend)]

    # ---- 5. 健康检查 ----
    if not wait_backend(backend):
        return 1

    # ---- 6. 前端 ----
    if args.dev:
        dev = start_dev_servers()
        if dev is None:
            return 1
        procs.extend(dev)
        urls = []
        if not args.kiosk_only:
            urls.append(DEV_ADMIN_URL)
        urls.append(DEV_KIOSK_URL)
        open_pages(urls)
    else:
        urls = []
        if not args.kiosk_only:
            urls.append(ADMIN_URL)
        urls.append(KIOSK_URL)
        open_pages(urls)

    # ---- 7. 常驻前台 ----
    info("服务运行中。按 Ctrl+C 停止全部进程……")
    try:
        while True:
            time.sleep(1.0)
            # 任一后端/前端进程退出则提示（不自动退出，保持其他服务可用）
            for name, proc in procs:
                if proc.poll() is not None:
                    warn("{}（PID {}）已退出，退出码：{}".format(name, proc.pid, proc.returncode))
            procs = [(n, p) for n, p in procs if p.poll() is None]
            if not procs:
                fail("全部进程已退出")
                return 1
    except KeyboardInterrupt:
        print()
        info("收到停止信号，正在停止全部进程……")
        stop_procs(procs)
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，reconfigure 为 UTF-8 防止中文/符号打印报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
