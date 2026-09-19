# -*- coding: utf-8 -*-
r"""改老人档案字段（姓名 / 称呼 / 床位 / 年龄 / 性别 / 备注 / 风格）—— 安全读改写。

为什么需要这个脚本
----------------
`POST /api/profiles` 是 **upsert 全量覆盖**：

    ON CONFLICT(uid) DO UPDATE SET name=excluded.name, nickname=..., bed=...,
      age=..., gender=..., birthday=..., profile_json=excluded.profile_json,
      style=..., preferences_json=excluded.preferences_json, notes=...

也就是说**只传 uid + name 去改姓名，会把病史/用药/偏好/备注/style 全部清空**。
本脚本先读出当前档案，只改你点名的字段，再把其余字段原样回写 —— 改姓名不会丢别的资料。
（`kind` / `ward_id` / `ward_map` / `ward_zone` 这几个字段 upsert 本来就不碰，病房归属与角色安全。）

改姓名**不影响**其他任何东西：人脸/声纹样本、记忆、提醒、对话历史都挂在 **uid** 上，
uid 不变就都不用重拍、不用重录。

用法
----
    python scripts/update_elder.py --list                          # 列出全部档案
    python scripts/update_elder.py elder_001 --dry-run --name 张桂芳  # 只看会改成什么（不写库）
    python scripts/update_elder.py elder_001 --name 张桂芳 --nickname 张奶奶
    python scripts/update_elder.py elder_001 --bed 12 --age 78
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LLM import db                      # noqa: E402
from LLM import log as audit            # noqa: E402

TEXT_FIELDS = ("name", "nickname", "bed", "gender", "notes", "style")
NUM_FIELDS = ("age",)
SHOW = ("uid", "name", "nickname", "bed", "age", "kind")

PROJ = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJ / "LLM" / "data" / "backup"


def _uid_tables(conn):
    """所有带 `uid` 列的表（**自动发现**：以后新增表也不会漏删）。"""
    out = []
    for (name,) in conn.execute("select name from sqlite_master where type='table'"):
        cols = [d[1] for d in conn.execute("pragma table_info(%s)" % name)]
        if "uid" in cols:
            out.append(name)
    return out


def _uid_counts(uid):
    """这个 uid 在各表里有多少行（只列非零）。"""
    import sqlite3
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        out = {}
        for t in _uid_tables(conn):
            n = conn.execute("select count(*) from %s where uid=?" % t, (uid,)).fetchone()[0]
            if n:
                out[t] = n
        return out
    finally:
        conn.close()


def _uid_samples(uid):
    """磁盘上属于这个 uid 的样本（声纹 npz / 人脸样本目录）。"""
    out = []
    spk = PROJ / "LLM" / "data" / "speakers" / ("%s.npz" % uid)
    if spk.is_file():
        out.append(spk)
    faces = PROJ / "LLM" / "data" / "faces" / uid
    if faces.is_dir():
        out.append(faces)
    return out


def _backup_db():
    """破坏性操作前自动备份 SQLite 库（落在 LLM/data/backup/，已 gitignore）。

    文件名带**微秒**：早先用"到秒"的时间戳，连续删两个 uid 时第二份会覆盖第一份 ——
    那样"删除前的完整快照"就丢了（2026-09-19 实际踩到）。
    """
    import shutil
    import time as _t
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _t.strftime("%Y%m%d_%H%M%S") + "_%06d" % (_t.time_ns() // 1000 % 1000000)
    dst = BACKUP_DIR / ("brain-%s.db" % stamp)
    shutil.copy2(str(db.DB_PATH), str(dst))
    return dst


def delete_uid(uid, dry_run=False, yes=False):
    """**连根删掉**一个 uid：档案 + 全部关联数据 + 声纹/人脸样本。

    为什么"连根删"而不是只删 profiles：只删档案会留下**孤儿数据**（记忆页仍按 uid 列出
    那些记忆/表达/画像/历史），而且演示档案的补种逻辑（档案表为空才种）会跟残留数据对不上。
    所以要删就删干净：所有带 uid 列的表 + 磁盘样本。

    安全：**先备份 DB**；打印每张表要删多少行；**没有 `--yes` 只预览不执行**。
    建议：**后端停止时执行**（避免它同时读写同一个库）。
    """
    counts = _uid_counts(uid)
    samples = _uid_samples(uid)
    prof = db.get_profile(uid)
    if not counts and not samples and not prof:
        print("uid=%s 没有任何数据，无需删除。" % uid)
        return 0
    print("即将删除 uid=%s 的全部数据（**不可撤销**）：" % uid)
    if prof:
        print("   档案：%s（%s）" % (prof.get("name") or "（无名）", prof.get("uid")))
    for t, n in sorted(counts.items()):
        print("   %-20s %4d 行" % (t, n))
    for p in samples:
        extra = ""
        if p.is_dir():
            extra = "（%d 个文件）" % len(list(p.iterdir()))
        print("   %-20s %s%s" % ("样本", p.relative_to(PROJ), extra))
    if dry_run or not yes:
        print("\n（预览：什么都没删。确认执行请加 --yes）")
        return 0

    import shutil
    import sqlite3
    bak = _backup_db()
    print("\n已备份数据库 → %s" % bak.relative_to(PROJ))
    conn = sqlite3.connect(str(db.DB_PATH))
    removed = {}
    try:
        for t in _uid_tables(conn):
            cur = conn.execute("delete from %s where uid=?" % t, (uid,))
            if cur.rowcount:
                removed[t] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    for p in samples:                                    # 磁盘样本
        try:
            shutil.rmtree(str(p)) if p.is_dir() else p.unlink()
            removed["文件:" + p.name] = 1
        except Exception as e:                           # noqa: BLE001
            print("   ⚠️ 删不掉 %s：%r" % (p, e))
    # 如果当前"说话的人"正是他 → 清空主体，避免指向一个不存在的 uid
    try:
        from LLM import session
        if session.get_principal("kiosk")["uid"] == uid:
            session.set_subject("", locked=False, source="manual")
            print("   （当前会话主体就是他 → 已清空）")
    except Exception:                                    # noqa: BLE001
        pass
    audit.log("memory_change", action="profile_delete", uid=uid,
              removed=removed, backup=bak.name, by="operator")
    print("✅ 已删除 %d 张表的数据：%s"
          % (len([k for k in removed if not k.startswith("文件:")]),
             {k: v for k, v in removed.items()}))
    print("   恢复方式：把 %s 覆盖回 %s（先停后端）" % (bak.name, db.DB_PATH.name))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python scripts/update_elder.py",
        description="改老人档案字段（安全读改写；改姓名不会清空其他资料）")
    p.add_argument("uid", nargs="?", help="要改的 uid；不给则等同于 --list")
    for f in TEXT_FIELDS:
        p.add_argument("--" + f, default=None, help="新的%s" % f)
    p.add_argument("--age", type=int, default=None, help="新的年龄")
    p.add_argument("--dry-run", action="store_true", help="只打印会改什么/删什么，不写库")
    p.add_argument("--list", action="store_true", help="列出所有档案")
    p.add_argument("--delete", action="store_true",
                   help="删除这个 uid 的**全部**数据（档案/记忆/提醒/历史/表达/画像/样本），"
                        "需配合 --yes 才真删；不加 --yes 只预览")
    p.add_argument("--yes", action="store_true", help="确认执行 --delete")
    args = p.parse_args(argv)

    if args.delete:
        if not args.uid:
            print("用法：python scripts/update_elder.py <uid> --delete [--yes]")
            return 2
        return delete_uid(args.uid, dry_run=args.dry_run, yes=args.yes)

    if args.list or not args.uid:
        rows = db.list_profiles(kind="")
        if not rows:
            print("（还没有任何档案）")
            return 0
        print("  %-14s %-12s %-12s %-6s %-5s %s"
              % ("uid", "姓名", "称呼", "床位", "年龄", "kind"))
        for r in rows:
            print("  %-14s %-12s %-12s %-6s %-5s %s"
                  % (r.get("uid"), r.get("name") or "—", r.get("nickname") or "—",
                     r.get("bed") or "—", r.get("age") or "—", r.get("kind") or "—"))
        return 0

    cur = db.get_profile(args.uid)
    given = {f: getattr(args, f) for f in TEXT_FIELDS if getattr(args, f) is not None}
    if args.age is not None:
        given["age"] = args.age

    if not cur:
        # 档案不存在 → **建档**（新成员注册的第一步）。
        # 注意：新建时"upsert 全量覆盖"没有风险（本来就没有旧字段可丢），
        # 但 uid 一旦定下就别改名（它是主键，还被声像/人脸样本目录引用）。
        if not given:
            print("没有这个档案：%s，且你没有给任何字段（--name 等），无法建档。" % args.uid)
            print("建档示例：python scripts/update_elder.py %s --name 张桂芳 --nickname 张奶奶"
                  % args.uid)
            print("也可以用管理端「老人注册」页，或 POST /api/profiles {\"uid\":...,\"name\":...}")
            return 2
        print("档案 %s 不存在 → 将**新建**：" % args.uid)
        for k, v in given.items():
            print("   %-9s %r" % (k, v))
        print("   %-9s %r  （角色由 kind 推导，默认就是老人）" % ("kind", "elder"))
        if args.dry_run:
            print("（--dry-run：没有写库）")
            return 0
        prof = db.upsert_profile(
            args.uid,
            given.get("name", ""), given.get("nickname", ""), given.get("bed", ""),
            given.get("age", 0), {}, given.get("style", ""), {},
            given.get("notes", ""), gender=given.get("gender", ""), birthday="")
        audit.log("memory_change", action="profile_create", uid=args.uid,
                  fields=sorted(given), by="operator")
        print("✅ 已新建档案：%s"
              % json.dumps({k: prof.get(k) for k in SHOW}, ensure_ascii=False))
        print("   下一步挂人脸（对着镜头，自动起停服务）：")
        print("     python scripts/face_check.py --live --enroll %s --frames 5" % args.uid)
        return 0

    changes = {}
    for f in TEXT_FIELDS:
        v = getattr(args, f)
        if v is not None and v != (cur.get(f) or ""):
            changes[f] = v
    if args.age is not None and args.age != (cur.get("age") or 0):
        changes["age"] = args.age

    print("档案 %s 当前：%s"
          % (args.uid, json.dumps({k: cur.get(k) for k in ("name", "nickname", "bed", "age")},
                                  ensure_ascii=False)))
    if not changes:
        print("没有任何字段需要修改（用法见 --help）")
        return 0
    print("将修改：")
    for k, v in changes.items():
        print("   %-9s %r → %r" % (k, cur.get(k), v))
    if args.dry_run:
        print("（--dry-run：没有写库）")
        return 0

    # 读改写：只替换点名要改的字段，其余原样带回
    prof = db.upsert_profile(
        args.uid,
        changes.get("name", cur.get("name") or ""),
        changes.get("nickname", cur.get("nickname") or ""),
        changes.get("bed", cur.get("bed") or ""),
        changes.get("age", cur.get("age") or 0),
        cur.get("profile") or {},                       # 病史/用药：原样保留
        changes.get("style", cur.get("style") or ""),
        cur.get("preferences") or {},                   # 偏好（含"称呼"）：原样保留
        changes.get("notes", cur.get("notes") or ""),
        gender=changes.get("gender", cur.get("gender") or ""),
        birthday=cur.get("birthday") or "",
    )
    audit.log("memory_change", action="profile_update_field", uid=args.uid,
              fields=sorted(changes), by="operator")
    print("✅ 已更新：%s"
          % json.dumps({k: prof.get(k) for k in SHOW}, ensure_ascii=False))
    print("   人脸/声纹样本、记忆、提醒都挂在 uid 上 —— 这次改名不需要重拍、不需要重录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
