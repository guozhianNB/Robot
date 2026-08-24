# -*- coding: utf-8 -*-
r"""
LLM 端一键启动程序。

在项目根目录执行（Windows / Linux 通用）：
    python start.py

流程：
  1. 检查根目录 .env 是否配置 DEEPSEEK_API_KEY
  2. 检查 8000 端口是否被占用（占用则询问是否结束旧进程重启）
  3. 用虚拟环境解释器启动 uvicorn（LLM.server:app）
  4. 轮询 /api/health 确认后端就绪
  5. 自动打开前端 UI/index.html
  6. 常驻前台，Ctrl+C 优雅停止

只依赖标准库（subprocess / socket / urllib / webbrowser），与项目
"依赖用 stdlib" 的风格一致。
"""
import os
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
UI_INDEX = BASE_DIR / "UI" / "index.html"
HEALTH_TIMEOUT = 30.0        # 健康检查最长等待秒数
POLL_INTERVAL = 0.5          # 健康检查轮询间隔

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


def _fmt(msg, color=None):
    if color:
        msg = _c(msg, color)
    return msg


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


def health_ready():
    """GET /api/health，返回是否就绪。"""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    print()
    info(_c("===== LLM 端一键启动 =====", _BOLD))
    info("项目根目录：{}".format(BASE_DIR))

    # ---- 1. 检查 .env ----
    step("检查 .env 配置")
    if not ENV_FILE.is_file():
        fail("未找到 {}".format(ENV_FILE))
        info("请在项目根目录创建 .env，并写入 DEEPSEEK_API_KEY=你的密钥")
        return 1
    api_key = parse_env("DEEPSEEK_API_KEY")
    if not api_key:
        fail(".env 中未配置 DEEPSEEK_API_KEY（或值为空）")
        info("请编辑 {} 补充：DEEPSEEK_API_KEY=你的密钥".format(ENV_FILE))
        return 1
    ok("DEEPSEEK_API_KEY 已配置（{}...）".format(api_key[:6]))

    # ---- 2. 端口检查 ----
    step("检查端口 {} 占用情况".format(PORT))
    pid = None
    if port_in_use(PORT):
        pid = find_pid_on_port(PORT)
        warn("端口 {} 已被占用".format(PORT) + ("（PID {}）".format(pid) if pid else ""))
        answer = input(_c("  是否结束该进程并重启后端？[y/N] ", _YELLOW)).strip().lower()
        if answer not in ("y", "yes"):
            info("已取消启动，请自行处理占用 {} 端口的进程后重试".format(PORT))
            return 1
        if pid:
            step("结束旧进程 PID {}".format(pid))
            kill_pid(pid)
            time.sleep(1.0)
            if port_in_use(PORT):
                fail("端口 {} 仍被占用，无法启动".format(PORT))
                return 1
            ok("旧进程已结束")
        else:
            fail("未能定位占用进程，请手动关闭占用 {} 端口的程序".format(PORT))
            return 1
    else:
        ok("端口 {} 空闲".format(PORT))

    # ---- 3. 选择解释器并启动 uvicorn ----
    python = pick_python()
    if python:
        info("使用虚拟环境解释器：{}".format(python))
    else:
        warn("未找到 .venv 解释器，改用系统 Python：{}".format(sys.executable))
        warn("若依赖未安装，后端会启动失败，请先创建虚拟环境并安装依赖")
        python = sys.executable

    cmd = [python, "-m", "uvicorn", "LLM.server:app", "--host", HOST, "--port", str(PORT)]
    step("启动后端：{}".format(" ".join(cmd)))
    proc = subprocess.Popen(cmd, cwd=BASE_DIR)  # 输出透传到当前终端
    info("后端进程 PID：{}".format(proc.pid))
    info("服务地址：http://127.0.0.1:{}/api/health".format(PORT))

    # ---- 4. 健康检查 ----
    step("等待后端就绪（最长 {} 秒）".format(int(HEALTH_TIMEOUT)))
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_report = 0.0
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fail("后端进程提前退出，退出码：{}".format(proc.returncode))
            info("请查看上方 uvicorn 日志确认失败原因（常见：依赖缺失 / 端口被占 / .env 配置错误）")
            return 1
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
    else:
        ok("LLM 端已就绪：http://127.0.0.1:{}/api/health".format(PORT))

    # ---- 5. 打开前端 ----
    if UI_INDEX.is_file():
        step("打开前端页面")
        uri = UI_INDEX.resolve().as_uri()
        try:
            webbrowser.open(uri)
            ok("已在浏览器打开：{}".format(uri))
        except Exception as exc:
            warn("自动打开浏览器失败（{}），请手动打开：{}".format(exc, uri))
    else:
        warn("未找到前端文件 {}，跳过自动打开".format(UI_INDEX))

    # ---- 6. 常驻前台 ----
    info("服务运行中。按 Ctrl+C 停止后端……")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print()
        info("收到停止信号，正在结束后端进程……")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        ok("后端已停止")
    if proc.returncode not in (0, -15):
        fail("后端进程退出，退出码：{}".format(proc.returncode))
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，reconfigure 为 UTF-8 防止中文/符号打印报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
