# -*- coding: utf-8 -*-
r"""档案侧的手脚：读档案 / 算下一个 uid / 按姓名反查 uid / 调 `scripts/update_elder.py`。

为什么改档案要走 `update_elder.py` 子进程、而不是直接 `db.upsert_profile`
------------------------------------------------------------------------
`db.upsert_profile` 是**全量覆盖**：只传 uid + name 去改姓名，会把病史/用药/偏好/
备注/style 全部清空。`scripts/update_elder.py` 已经处理好了"安全读改写"，还会写审计
（`memory_change`）。GUI 复用它 = 与手工敲命令**完全同一条路径**，行为可预期、可复现。

为什么删除也要走它：`update_elder.py <uid> --delete --yes` 会先备份数据库，再删掉
**所有带 uid 列的表**（自动发现）+ 人脸的样本目录 + 声纹 npz，并清掉指向该 uid 的
会话主体。这些"连根删"的细节不该在 GUI 里重写一遍。

uid 规则（`next_uid`）与姓名反查（`find_people`）是纯函数，可脱离摄像头与 Tk 单测。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "update_elder.py"

# uid 里不许出现的字符：路径分隔符与 Windows 文件名保留字符（uid 会变成目录名）
BAD_CHARS = set("\\/:*?<>|") | {chr(34)}
UID_MAX_LEN = 24


# ---------------------------------------------------------------------------
# 档案读取
# ---------------------------------------------------------------------------
def profiles():
    """全部档案（含老人/护士/管理员；`kind=""` = 不过滤）。"""
    from LLM import db
    try:
        return db.list_profiles(kind="")
    except Exception:                                    # noqa: BLE001
        return []


def profile_of(uid):
    """单个档案（不存在返回 None）。"""
    from LLM import db
    try:
        return db.get_profile(str(uid))
    except Exception:                                    # noqa: BLE001
        return None


def name_map(rows=None):
    """uid → 显示名（优先"称呼"，其次姓名，都没有才回落 uid）。

    与 admin 端 `elderLabel()`、`scripts/face_check.py::name_map()` 同一优先级 ——
    三处显示的必须是同一个名字，否则"测试认出来了但界面上叫另一个人"。
    """
    out = {}
    for r in (profiles() if rows is None else rows):
        uid = str(r.get("uid") or "")
        if uid:
            out[uid] = r.get("nickname") or r.get("name") or uid
    return out


def label(uid, names=None):
    """uid → `fuze（000）`；空 → `未知`。"""
    if not uid:
        return "未知"
    names = name_map() if names is None else names
    nm = names.get(uid)
    return "%s（%s）" % (nm, uid) if nm and nm != uid else str(uid)


# ---------------------------------------------------------------------------
# uid 生成 / 校验
# ---------------------------------------------------------------------------
def next_uid(rows=None):
    """下一个 uid = 现有**纯数字** uid 的最大值 +1（没有数字 uid 就从 000 起）。

    只认纯数字 uid：仓库里可能有人用 `elder_001` 这类命名，它不该把自增带偏。
    补零宽度至少 3 位（与现有 000/001 风格一致）。
    """
    nums = []
    for r in (profiles() if rows is None else rows):
        u = str(r.get("uid") or "")
        if u.isdigit():
            nums.append(int(u))
    n = (max(nums) + 1) if nums else 0
    return "%0*d" % (max(3, len(str(n))), n)


def check_uid(uid):
    """uid 合法性；返回 `(ok, 说明)`。"""
    s = str(uid or "")
    if not s:
        return False, "uid 不能为空"
    if s != s.strip() or " " in s:
        return False, "uid 不能含空格"
    if s == "admin":
        return False, "uid 不能用 admin（那是管理员专用值，会被特殊处理）"
    hit = sorted(set(s) & BAD_CHARS)
    if hit:
        return False, "uid 不能含这些字符：%s" % " ".join(hit)
    if len(s) > UID_MAX_LEN:
        return False, "uid 太长（最多 %d 个字符）" % UID_MAX_LEN
    if s in (".", ".."):
        return False, "uid 不能用 . 或 .."
    return True, ""


# ---------------------------------------------------------------------------
# 按姓名反查 uid
# ---------------------------------------------------------------------------
def find_people(text, rows=None):
    """按姓名/称呼反查档案，返回命中的档案列表（含 `_how` 说明命中方式）。

    命中优先级：uid 全等 > 姓名/称呼全等 > 姓名/称呼包含（忽略大小写）。
    只返回**最好的一档**，避免"输入张"时把全部姓张的都倒出来 ——
    找不到人时调用方可以把 all_names() 摊给用户看。
    """
    q = str(text or "").strip()
    if not q:
        return []
    rows = profiles() if rows is None else rows
    ql = q.casefold()
    exact, part = [], []
    for r in rows:
        uid = str(r.get("uid") or "")
        nm = str(r.get("name") or "")
        nk = str(r.get("nickname") or "")
        if uid == q:
            exact.append({**r, "_how": "uid 全等"})
        elif nm == q or nk == q:
            exact.append({**r, "_how": "姓名全等"})
        elif ql and (ql in nm.casefold() or ql in nk.casefold()
                     or ql in uid.casefold()):
            part.append({**r, "_how": "姓名包含"})
    return exact or part


def all_names(rows=None):
    """`fuze（000）、guo（001）` —— 找不到人时提示用户可选谁。"""
    rows = profiles() if rows is None else rows
    return "、".join(label(r.get("uid"), name_map(rows)) for r in rows) or "（库里还没有成员）"


# ---------------------------------------------------------------------------
# 调 scripts/update_elder.py
# ---------------------------------------------------------------------------
def create_cmd(uid, name, nickname="", bed="", age=""):
    """拼 `update_elder.py` 的参数（只带非空字段，避免把空串写进档案）。

    年龄**不做 int() 转换**：`update_elder.py` 的 `--age` 是 `type=int`，写错了它会
    用 argparse 报错并以非 0 退出 —— 让子进程去拒绝，比在这里悄悄把字段丢掉好
    （界面那层已经先挡了一道"必须填数字"）。
    """
    args = [str(uid), "--name", str(name)]
    if str(nickname or "").strip():
        args += ["--nickname", str(nickname).strip()]
    if str(bed or "").strip():
        args += ["--bed", str(bed).strip()]
    if str(age or "").strip():
        args += ["--age", str(age).strip()]
    return args


def delete_cmd(uid):
    """拼删除参数（与手册里那条命令逐字一致）。"""
    return [str(uid), "--delete", "--yes"]


def cmd_text(args):
    """给人看的命令行文本：`python scripts\\update_elder.py 002 --name 张桂芳`。"""
    rel = SCRIPT.relative_to(REPO)
    body = " ".join('"%s"' % a if " " in str(a) else str(a) for a in args)
    return "python %s %s" % (str(rel).replace("/", os.sep), body)


def run_update_elder(args, timeout=180):
    """跑一次 `update_elder.py`，返回 `(returncode, 输出文本)`。

    输出走**临时文件**而不是管道：管道在某些受限环境下会被拒绝，而且子进程输出
    很多时管道写满就死锁。临时文件两种毛病都没有，还能顺便留下完整输出给界面显示。
    """
    cmd = [sys.executable, str(SCRIPT)] + [str(a) for a in args]
    env = {**os.environ, "PYTHONUTF8": "1"}
    fd, path = tempfile.mkstemp(prefix="vtest_elder_", suffix=".log")
    try:
        with os.fdopen(fd, "wb") as fh:
            proc = subprocess.run(cmd, cwd=str(REPO), stdout=fh,
                                  stderr=subprocess.STDOUT, env=env, timeout=timeout)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            out = fh.read()
        return int(proc.returncode), out
    except subprocess.TimeoutExpired:
        return -9, "超时（超过 %s 秒）：%s" % (timeout, cmd_text(args))
    except Exception as e:                               # noqa: BLE001
        return -1, "执行失败：%r" % (e,)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 概况（一级小窗显示用）
# ---------------------------------------------------------------------------
def library_stats():
    """人脸样本库概况（缺 numpy 等依赖时返回带 error 的字典，不抛）。"""
    try:
        from LLM import face_api
        return face_api.library()
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def model_status():
    """检测器/识别器可用性（不碰摄像头）。"""
    try:
        from LLM import face_api
        st = face_api.status()
    except Exception as e:                               # noqa: BLE001
        return {"detector": {"available": False, "reason": str(e)[:160]},
                "embedder": {"available": False, "reason": str(e)[:160]}}
    return st


def summary_lines(rows=None):
    """一级小窗的"库里已有谁"两行文本。"""
    rows = profiles() if rows is None else rows
    lib = library_stats()
    per = (lib.get("per_uid") or {}) if isinstance(lib, dict) else {}
    names = name_map(rows)
    detail = "、".join("%s %d 张" % (label(u, names), n)
                       for u, n in sorted(per.items())) or "（还没有人脸样本）"
    return [
        "成员：%d 位档案" % len(rows),
        "人脸样本：%s 位 / %s 张 —— %s"
        % (lib.get("uids", "?"), lib.get("samples", "?"), detail),
    ]
