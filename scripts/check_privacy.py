# -*- coding: utf-8 -*-
r"""隐私闸门：确保**人脸照片与指纹**（顺带声纹）永远不会进 git、更不会被推到 GitHub。

为什么需要它（`.gitignore` 不够）
--------------------------------
`.gitignore` 只对**未跟踪**的文件生效 —— 对已经 `git add` 过的文件完全无效，对历史提交
更无效（2026-09-19 实测：`LLM/data/speakers/*.npz` 就是规则写好之前提交进去的，规则再全也
拦不住，文件至今还躺在 `origin/main` 里）。所以真正的保证要**主动检查"git 看得见什么"**：

    ① 索引（`git ls-files`）        = 下一次 commit 会带上的
    ② 未跟踪且未被忽略              = 下一次 `git add -A` 会带上的
    ③ 历史对象（`git rev-list --all --objects`） = 已经推到 GitHub 的东西

三处任何一处出现人脸样本 → **红色失败**（退出码 1）。手机上"我拍了一张脸"这件事只要落在
仓库里，就会被这三条之一逮住 —— 因为人脸样本的文件名是固定格式
（`face_lib.add_sample()` 生成 `<年月日>_<时分秒>_<微秒>.jpg/.npz`），
**不依赖路径**也能认出来（`FACE_DIR` 被重定位到仓库内任何地方都拦得住）。

用法
----
    python scripts/check_privacy.py              # 人看：三条全查（含历史，0.1 秒）
    python scripts/check_privacy.py --quiet      # 只出错才说话（钩子用）
    python scripts/check_privacy.py --strict     # 把声纹等"警告"也当失败
    python scripts/check_privacy.py --no-history # 跳过历史（提交时求快）

装成 git 钩子（一次设置，之后每次 commit/push 自动查）：
    git config core.hooksPath .githooks
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---- 红色规则：一旦命中就是"人脸/生物特征要进仓库了" -------------------------
FATAL_RULES = (
    ("人脸样本目录（照片+指纹）", re.compile(r"(^|/)LLM/data/faces/")),
    # face_lib.add_sample() 的命名：20260918_224758_985347.jpg / .npz
    ("人脸样本文件（时间戳命名）",
     re.compile(r"(^|/)\d{8}_\d{6}_\d{6}\.(jpg|jpeg|png|bmp|webp|npz)$", re.I)),
    ("人脸样本（faces 目录下的图片/指纹）",
     re.compile(r"(^|/)faces?/[^/]+\.(jpg|jpeg|png|bmp|webp|npz)$", re.I)),
    ("检测画框输出（vision.face --out 落的盘）",
     re.compile(r"(^|/)det_[^/]*\.(jpg|jpeg|png|bmp|webp)$", re.I)),
)

# ---- 黄色规则①：同样是隐私问题，但属于"已知遗留"，用 --strict 才算失败 ----
PRIVACY_WARN_RULES = (
    ("声纹样本（生物特征）", re.compile(r"(^|/)LLM/data/speakers/")),
    ("人声录音文件", re.compile(r"\.(wav|mp3|m4a|flac)$", re.I)),
    ("运行时数据库（含姓名/床位等个人信息）", re.compile(r"(^|/)LLM/data/[^/]+\.db")),
    ("审计日志", re.compile(r"(^|/)LLM/data/audit\.jsonl$")),
)

# ---- 黄色规则②：体积/仓库卫生，只报数量（node_modules 几千条，别刷屏） ----
BLOAT_WARN_RULES = (
    ("语音/视觉模型权重（体积）",
     re.compile(r"(^|/)(LLM/models|vision/models|\.research_sherpa|LLM/data/chroma)/")),
    ("node_modules（体积）", re.compile(r"(^|/)node_modules/")),
)


class GitError(RuntimeError):
    pass


def git(*args):
    """跑一条 git 命令，返回 stdout 行列表。"""
    try:
        p = subprocess.run(["git", "-C", str(REPO)] + list(args),
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError as e:                       # 没装 git
        raise GitError("找不到 git 命令：%s" % e) from e
    if p.returncode != 0:
        raise GitError("git %s 失败：%s" % (" ".join(args), (p.stderr or "").strip()))
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def is_repo():
    try:
        return git("rev-parse", "--is-inside-work-tree") == ["true"]
    except GitError:
        return False


def _match(path, rules):
    for name, rx in rules:
        if rx.search(path):
            return name
    return None


def scan(paths, rules):
    """把路径列表过一遍规则，返回 `[(规则名, 路径), …]`（同一规则只报前若干条）。"""
    hits, per_rule = [], {}
    for p in paths:
        why = _match(p, rules)
        if not why:
            continue
        per_rule[why] = per_rule.get(why, 0) + 1
        if per_rule[why] <= 5:                           # 免得刷屏
            hits.append((why, p))
    return hits, per_rule


def indexed():
    """索引里的文件 = 下一次 commit 会带上的。"""
    return git("ls-files")


def untracked_not_ignored():
    """未跟踪且**未被忽略** = 下一次 `git add -A` 会带上的（最需要盯的一类）。"""
    return git("ls-files", "--others", "--exclude-standard")


def history_paths():
    """所有 ref 可达的对象路径 = 已经推到 GitHub 的东西。"""
    out = []
    for line in git("rev-list", "--all", "--objects"):
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip():
            out.append(parts[1].strip())
    return out


def ignored_samples():
    """工作区里**被正确忽略**的样本（列出来给用户吃颗定心丸）。"""
    try:
        return git("ls-files", "--others", "--ignored", "--exclude-standard",
                   "--", "LLM/data/faces", "LLM/data/speakers")
    except GitError:
        return []


def fmt(hits, per_rule):
    lines = []
    for why, path in hits:
        lines.append("    ❌ %s：%s" % (why, path))
    for why, n in per_rule.items():
        if n > 5:
            lines.append("    … %s 另有 %d 条同类" % (why, n - 5))
    return lines


def fmt_warn(hits):
    """警告：按规则归并，每条规则最多举 3 个例子（node_modules 几千条，别刷屏）。"""
    per = {}
    for why, path in hits:
        per.setdefault(why, []).append(path)
    lines = []
    for why in sorted(per):
        paths = sorted(per[why])
        sample = "、".join(paths[:3]) + ("…" if len(paths) > 3 else "")
        lines.append("    ⚠️ %s：%d 条（%s）" % (why, len(paths), sample))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python scripts/check_privacy.py",
        description="确保人脸照片/指纹（与声纹）不会进 git、不会被推到 GitHub")
    ap.add_argument("--strict", action="store_true", help="把警告也算失败")
    ap.add_argument("--quiet", action="store_true", help="只出错才输出（git 钩子用）")
    ap.add_argument("--no-history", action="store_true", help="跳过历史扫描（提交时求快）")
    args = ap.parse_args(argv)

    if not is_repo():
        print("[隐私闸门] 不在 git 仓库里，跳过。")
        return 0

    def say(*a):
        if not args.quiet:
            print(*a)

    fatal, warn, bloat = [], [], []

    say("=== 隐私闸门：人脸照片/指纹会不会进 GitHub ===\n")

    def one_pass(paths, label):
        """对一组路径跑三类规则，把结果分别堆进 fatal / warn / bloat。

        红色命中这里只报数量，具体路径放到文末统一列（那里还带处理办法）；
        黄/蓝色只在非 --quiet 时说（钩子每次提交都跑，别刷屏）。
        """
        hits, per = scan(paths, FATAL_RULES)
        fatal.extend(hits)
        say("%s：%s" % (label, "✅ 干净" if not hits
                        else "❌ 命中 %d 条（清单见文末）" % len(per)))
        hits, _ = scan(paths, PRIVACY_WARN_RULES)
        warn.extend(hits)
        for ln in fmt_warn(hits):
            say(ln)
        hits, _ = scan(paths, BLOAT_WARN_RULES)
        bloat.extend(hits)
        for ln in fmt_warn(hits):
            say(ln)

    # ① 索引
    try:
        one_pass(indexed(), "① 即将提交的内容（git 索引）")

        # ② 未跟踪且未被忽略（`git add -A` 会带上的）
        one_pass(untracked_not_ignored(), "② 未跟踪但未被忽略（`git add -A` 会带上的）")

        # ③ 历史（= 已经在 GitHub 上的）
        if args.no_history:
            say("③ git 历史：已跳过（--no-history）")
        else:
            one_pass(history_paths(), "③ git 历史里的对象（= 已经推到 GitHub 的东西）")

        # ④ 工作区里的样本（应当是"git 看不见"）
        ign = ignored_samples()
        faces = [p for p in ign if "/faces/" in p]
        spk = [p for p in ign if "/speakers/" in p]
        say("④ 盘上已被正确忽略的样本（git 看不见 → 不会被提交）：")
        say("    人脸样本 %d 个文件、声纹样本 %d 个文件" % (len(faces), len(spk)))
        say("    位置：%s" % (str(REPO / "LLM" / "data" / "faces") + " 与 …/speakers"))
    except GitError as e:
        print("[隐私闸门] %s" % e)
        return 2

    say("")
    fatal = sorted(set(fatal))
    warn = sorted(set(warn))
    bloat = sorted(set(bloat))
    if fatal:
        print("❌ 失败：下面这些**人脸样本/生物特征**在 git 看得见的地方（绝不能推上去）：")
        for why, path in fatal:
            print("    %s：%s" % (why, path))
        print("\n怎么处理：")
        print("  · 已被跟踪 → git rm --cached <路径> 后再提交（并把规则补进 .gitignore）")
        print("  · 已经在历史里 → 用 git filter-repo 清史 + 强推（见 docs/log.md 2026-09-19）")
        print("  · 只是未跟踪 → 确认 .gitignore 覆盖它，别用 git add -f")
        return 1
    if args.strict and (warn or bloat):
        print("❌ --strict：这些警告按失败处理：")
        for ln in fmt_warn(warn + bloat):
            print(ln)
        return 1
    if warn or bloat:
        say("⚠️ 警告（不阻断；加 --strict 可当失败）：")
        for ln in fmt_warn(warn):
            say(ln)
        for ln in fmt_warn(bloat):
            say(ln)
        if warn:
            say("  说明：声纹/wav/数据库 属于「已知遗留」（见 docs/log.md 2026-09-19）；"
                "本次要保证的**人脸照片**是干净的。")
    say("✅ 通过：人脸照片与指纹没有出现在索引、未忽略的未跟踪文件、或 git 历史里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
