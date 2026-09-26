# -*- coding: utf-8 -*-
r"""权限层体检：一眼看清「集体层 / 老人层 / 管理层」现在各自能做什么。

用法（项目根目录，Windows 用 .venv 里的解释器）：
    .venv\Scripts\python.exe scripts\show_permissions.py
    .venv\Scripts\python.exe scripts\show_permissions.py --url http://127.0.0.1:8000
    .venv\Scripts\python.exe scripts\show_permissions.py --audit 20
    .venv\Scripts\python.exe scripts\show_permissions.py --json      # 机器可读
    （终端中文乱码时先 `chcp 65001`，或直接用 --json）

**只读**：不写库、不建会话、不发车；`--url` 只发 GET。

事实来源（全部复用现成实现，本脚本不复制判定逻辑）：
    三层策略    LLM/agent/policy.py::POLICY_DEFAULTS / role_policy()
    uid→角色    LLM/agent/session.py::derive_role()（权威依据 profiles.kind，不看 uid 前缀）
    生效工具    LLM/agent/tools.py::effective_tools() 同口径：白名单 ∩ 工具 roles ∩ 开关
    口令门      LLM/store/db.py::get_admin_auth() / verify_admin_password()
    免鉴权路由  扫 LLM/server.py 的路由装饰器（**启发式**，仅供核对，不是安全判定）
规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PERM_EVENTS = {
    "policy_deny", "session_login", "session_login_fail", "session_logout",
    "session_expired", "admin_password_changed", "admin_password_restored",
    "admin_auth_changed", "voice_spk", "session_tick_error",
}


# ---------------------------------------------------------------- 各段采集
def policy_matrix() -> dict:
    """三层策略矩阵（含提示词文件是否存在）。"""
    from LLM.agent import policy as pol
    out = {}
    for role, raw in pol.POLICY_DEFAULTS.items():
        p = pol.role_policy(role)                       # 走公开入口，保证与业务同源
        f = Path(p["prompt_file"])
        out[role] = {
            "prompt_file": str(f),
            "prompt_exists": f.is_file(),
            "allowed_tools": p["allowed_tools"],        # None = 不按角色裁剪
            "data_scope": p["data_scope"],
            "ward_context": p["ward_context"],
        }
    return out


def tool_registry(url: str | None) -> dict:
    """工具清单：优先问活着的后端（才看得见 MCP 工具），否则退回本地注册表。

    返回 {name: {"server", "enabled", "roles"}}；`roles=None` = 不限角色（本地工具口径），
    MCP 工具按 conf.MCP_SERVERS 声明解析（未声明 → {"admin"}，与 tools._mcp_roles 一致）。
    """
    from LLM import conf
    servers = getattr(conf, "MCP_SERVERS", {}) or {}

    def mcp_roles(server: str):
        declared = (servers.get(server) or {}).get("roles")
        return {"admin"} if declared is None else set(declared)

    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url.rstrip("/") + "/api/tools", timeout=5) as r:
                data = json.load(r)
            out = {}
            for t in data.get("tools", []):
                name = t["function"]["name"]
                server = t.get("server", "")
                out[name] = {
                    "server": server or "local",
                    "enabled": bool(t.get("enabled", True)),
                    "roles": None if not server else mcp_roles(server),
                }
            return {"source": f"live {url}", "tools": out}
        except Exception as e:                          # noqa: BLE001  后端不在线就降级
            fallback = tool_registry(None)
            fallback["note"] = f"活体后端不可用（{e}），已退回本地注册表"
            return fallback

    try:
        from LLM.agent import tools as tool_mod
        out = {}
        for name, reg in tool_mod._TOOL_REGISTRY.items():
            out[name] = {"server": "local", "enabled": bool(reg["enabled"]),
                         "roles": None if reg.get("roles") is None else set(reg["roles"])}
        return {"source": "本地注册表（不含 MCP 工具，MCP 需后端在线）", "tools": out,
                "mcp_servers": {k: sorted(mcp_roles(k)) for k in servers}}
    except Exception as e:                              # noqa: BLE001
        return {"source": f"加载失败：{e}", "tools": {}, "mcp_servers": {}}


def effective_by_role(matrix: dict, reg: dict, settings: dict) -> dict:
    """每层「现在真正能用」的工具（与 tools.effective_tools 同口径）。"""
    out = {}
    for role, pol in matrix.items():
        allow = pol["allowed_tools"]
        usable, blocked = [], []
        for name, t in sorted(reg["tools"].items()):
            key = f"{name}_enabled" if t["server"] == "local" else "mcp_enabled"
            on = bool(settings.get(key, True))
            in_allow = allow is None or name in allow
            role_ok = t["roles"] is None or role in t["roles"]
            if on and in_allow and role_ok:
                usable.append(name)
            else:
                why = []
                if not on:
                    why.append(f"开关 {key}=off")
                if not in_allow:
                    why.append("不在角色白名单")
                if not role_ok:
                    why.append(f"工具自身 roles={sorted(t['roles'])}")
                blocked.append(f"{name}（{' + '.join(why)}）")
        out[role] = {"usable": usable, "blocked": blocked}
    return out


def role_map() -> list[dict]:
    """每个档案 uid → 角色（R1 唯一入口），以及 fail-closed 的兜底说明。"""
    from LLM.agent import session
    from LLM.store import db
    rows = []
    for p in db.list_profiles():
        uid = p["uid"]
        rows.append({
            "uid": uid,
            "kind_in_db": p.get("kind") or "",
            "role": session.derive_role(uid),
            "name": p.get("name", ""),
            "ward_id": p.get("ward_id", ""),
            "ward_map": p.get("ward_map", ""),
            "ward_zone": p.get("ward_zone", ""),
        })
    rows.sort(key=lambda r: (r["role"], r["uid"]))
    return rows


def admin_gate() -> dict:
    """口令门状态（不打印任何口令/哈希值）。"""
    from LLM import conf
    from LLM.store import db
    auth = db.get_admin_auth()
    factory = getattr(conf, "FACTORY_PASSWORD", "") or ""
    return {
        "auth_required": auth["required"],
        "password_set": bool(auth["hash"] and auth["salt"]),
        "half_broken": bool(auth["hash"]) != bool(auth["salt"]),
        "session_ttl_s": db.get_settings().get("admin_session_ttl_s"),
        "env_factory_password_set": bool(factory),
        "env_factory_password_still_valid": bool(factory) and db.verify_admin_password(factory),
    }


def live_sessions(url: str) -> dict:
    """活体会话（内存态只有后端进程自己知道，故走 HTTP）。"""
    import urllib.request
    out = {}
    for slot in ("kiosk", "admin"):
        req = urllib.request.Request(url.rstrip("/") + "/api/session/user",
                                     headers={"X-Surface": slot})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                out[slot] = json.load(r)
        except Exception as e:                          # noqa: BLE001
            out[slot] = {"ok": False, "error": str(e)}
    return out


def route_scan() -> list[dict]:
    """启发式扫 server.py：哪些路由**没有**角色判定（`_surface` / `get_principal`）。

    判定口径：只取该 handler **自己的缩进块**，并额外认「被守卫的模块级辅助函数」
    （如 `server._notice_admin` 里判 admin，路由只是调它一个名字）——否则
    `DELETE /api/notifications/{nid}` 会被误报成免鉴权。
    「判角色」= 取 principal **且**做了角色比较（`["role"] !=/== ...`）；
    只调 `get_principal()` 看 uid（如 `_session_user_payload`）不算闸门。
    只作核对用：命中 ≠ 漏洞（通知/Plan 是规格明写的免鉴权面），但能一眼看出边界。
    """
    lines = (ROOT / "LLM" / "server.py").read_text(encoding="utf-8").splitlines()
    deco = re.compile(r'^@app\.(get|post|delete|put)\("([^"]+)"')
    role_cmp = re.compile(r'\["role"\]\s*(==|!=)|role"\]\s*(==|!=)')

    # 1) 模块级辅助函数里「取 principal + 判角色」的 → 视为守卫函数
    guarded_helpers, cur = set(), None
    for line in lines:
        if line[:1] not in (" ", "\t", "") and line.strip():
            if line.startswith(("def ", "async def ")):
                cur = line.split("(")[0].replace("async def ", "").replace("def ", "")
            else:
                cur = None
        elif cur and "get_principal" in line and role_cmp.search(line):
            guarded_helpers.add(cur)

    def body_of(start: int) -> str:
        """从装饰器行往后，取第一个 def 起、连续的缩进块（顶格行即结束）。"""
        i = start
        while i < len(lines) and not lines[i].startswith(("async def ", "def ")):
            i += 1
        chunk, j = [], i
        while j < len(lines):
            if chunk and lines[j].strip() and lines[j][:1] not in (" ", "\t"):
                break
            chunk.append(lines[j])
            j += 1
        return "\n".join(chunk)

    out, seen = [], set()
    for i, line in enumerate(lines):
        m = deco.match(line)
        if not m:
            continue
        path, method = m.group(2), m.group(1).upper()
        if (method, path) in seen:          # 悬空/重复装饰器只算一次
            continue
        seen.add((method, path))
        body = body_of(i)
        head = body.split("\n")[0]
        out.append({
            "method": method,
            "path": path,
            "handler": head.split("(")[0].replace("async def ", "").replace("def ", "") or "(?)",
            "uses_surface": "_surface(" in body,
            "checks_principal": (bool(role_cmp.search(body)) and "get_principal" in body)
                                or any(h in body for h in guarded_helpers),
            "checks_page_pin": "_verify_nurse_pin" in body,
        })
    return out


def recent_audit(n: int) -> list[dict]:
    """审计里最近 N 条权限相关事件（audit.jsonl 可能混有测试写入，看 ts 分辨）。"""
    path = ROOT / "LLM" / "data" / "audit.jsonl"
    if not path.is_file():
        return []
    hits = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") in PERM_EVENTS:
            hits.append(row)
    return hits[-n:]


# ---------------------------------------------------------------- 输出
def render(data: dict) -> None:
    def title(s):
        print("\n" + "=" * 72 + "\n" + s + "\n" + "=" * 72)

    title("[1] 三层策略矩阵（LLM/agent/policy.py）")
    for role, p in data["policy"].items():
        allow = p["allowed_tools"]
        allow = "全部（只受全局 per-tool 开关约束）" if allow is None else ", ".join(allow)
        mark = "" if p["prompt_exists"] else "   ⚠️ 提示词文件缺失（降级为空串，不阻断对话）"
        print(f"\n  ● {role}   data_scope={p['data_scope']}  ward_context={p['ward_context']}")
        print(f"      提示词：{Path(p['prompt_file']).name}{mark}")
        print(f"      白名单：{allow}")

    title("[2] 每层「现在真正能用」的工具（白名单 ∩ 工具 roles ∩ 开关）")
    print(f"  工具清单来源：{data['registry']['source']}")
    if not data["registry"]["source"].startswith("live"):
        print("  ⚠️ 离线模式只见本地工具（车控/通知/视觉/联网都是 MCP 工具）："
              "加 `--url http://127.0.0.1:8000` 才看得到完整的生效面。")
    for role, e in data["effective"].items():
        print(f"\n  ● {role} → {', '.join(e['usable']) or '（一个都没有）'}")
        for b in e["blocked"]:
            print(f"      ✗ {b}")

    title("[3] uid → 角色（R1：只认 profiles.kind，不看前缀）")
    if not data["roles"]:
        print("  （profiles 表为空：任何 uid 都会 fail-closed 落到集体层）")
    for r in data["roles"]:
        print(f"  {r['uid']:<18} kind={r['kind_in_db'] or '(空→按 elder 处理)':<8}"
              f"→ {r['role']:<6} 病房={r['ward_id'] or '-'} 图/区={r['ward_map'] or '-'}/"
              f"{r['ward_zone'] or '-'}")
    print("\n  未列出/未知 uid（含空串、幽灵 uid）→ 一律 ward（R2 fail-closed）；"
          "admin 不写进 profiles，只能由口令登录产生。")

    g = data["admin_gate"]
    title("[4] 管理员口令门（拿 admin 的唯一通道 = POST /api/session/login）")
    print(f"  口令门开关 admin_auth_required = {g['auth_required']}"
          f"{'（关闭=任何端都能免口令进管理层！）' if not g['auth_required'] else ''}")
    print(f"  库内是否已设口令 = {g['password_set']}   半写坏（只有哈希没盐）= {g['half_broken']}")
    print(f"  管理会话 TTL = {g['session_ttl_s']} 秒（到期自动降权回 ward，tick 每秒检查）")
    print(f"  .env PASSWORD 已配置 = {g['env_factory_password_set']}"
          f"   当前仍是有效口令 = {g['env_factory_password_still_valid']}")

    if data.get("sessions"):
        title("[5] 活体会话（后端内存态，HTTP 只读）")
        for slot, s in data["sessions"].items():
            print(f"  [{slot}] {json.dumps(s, ensure_ascii=False)}")
    else:
        print("\n（未加 --url：会话是后端进程内存态，离线看不了，故本段跳过）")

    title("[6] 路由面：谁会被判角色，谁不会被判（启发式扫描 server.py）")
    gated = [r for r in data["routes"] if r["checks_principal"]]
    surface_only = [r for r in data["routes"] if not r["checks_principal"] and r["uses_surface"]]
    pin_only = [r for r in data["routes"] if not r["checks_principal"] and r["checks_page_pin"]]
    naked = [r for r in data["routes"]
             if not r["checks_principal"] and not r["uses_surface"] and not r["checks_page_pin"]]

    print(f"\n  ● 判角色（session.get_principal）{len(gated)} 条：")
    print("      " + ", ".join(f"{r['method']} {r['path']}" for r in gated))
    print(f"\n  ● 只认 X-Surface 端槽位、不判角色 {len(surface_only)} 条：")
    print("      " + ", ".join(f"{r['method']} {r['path']}" for r in surface_only))
    print(f"\n  ● 只校验护士台页面口令 {len(pin_only)} 条：")
    print("      " + ", ".join(f"{r['method']} {r['path']}" for r in pin_only))
    print(f"\n  ● 完全不做任何会话/角色判定 {len(naked)} 条（按路径前缀聚合）：")
    groups: dict[str, dict] = {}
    for r in naked:
        parts = [p for p in r["path"].split("/") if p and not p.startswith("{")]
        key = "/" + "/".join(parts[:2]) if parts else r["path"]
        g = groups.setdefault(key, {"n": 0, "writes": 0, "sample": r["path"]})
        g["n"] += 1
        if r["method"] != "GET":
            g["writes"] += 1
    for key in sorted(groups):
        g = groups[key]
        print(f"      {key:<28} 共 {g['n']} 条（写 {g['writes']} 条）例：{g['sample']}")
    print("\n  说明：这些免鉴权面**大多不是回归**——按设计，通知投递/读取、Plan API、语音/人脸、"
          "记忆与提醒等模块接口本就只依赖 LAN 边界，判角色只发生在对话与管理员写操作上；"
          "`/api/plans*` 与 `/api/notifications` 的免鉴权是规格明写的边界"
          "（docs/参考资料/plan表设计-初版.md、2026-09-18 护士台设计）；"
          "`DELETE /api/notifications/{nid}` 仍要管理员。"
          "**派生的风险**：任何能访问 8000 端口的人都能创建/调级/取消 Plan，"
          "护士台 PIN 只护页面、不护 API。")

    if data["audit"]:
        title("[7] 最近权限相关审计（含测试写入，看 ts 分辨）")
        for row in data["audit"]:
            extra = {k: v for k, v in row.items() if k not in ("ts", "event")}
            print(f"  {row.get('ts')}  {row.get('event'):<24} {json.dumps(extra, ensure_ascii=False)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="查看各权限层现在到底有什么权限（只读）")
    ap.add_argument("--url", default="", help="活体后端地址，如 http://127.0.0.1:8000")
    ap.add_argument("--audit", type=int, default=10, help="附带最近 N 条权限审计（0=不看）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（终端乱码时用）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

    from LLM.store import db
    settings = db.get_settings()
    matrix = policy_matrix()
    reg = tool_registry(args.url or None)
    data = {
        "db_path": str(db.DB_PATH),
        "settings_relevant": {k: settings.get(k) for k in (
            "admin_auth_required", "admin_session_ttl_s", "mcp_enabled",
            "ward_autoswitch_enabled", "ward_map_source", "current_map")},
        "policy": matrix,
        "registry": {"source": reg["source"], "tools": sorted(reg["tools"])},
        "effective": effective_by_role(matrix, reg, settings),
        "roles": role_map(),
        "admin_gate": admin_gate(),
        "routes": route_scan(),
        "audit": recent_audit(args.audit) if args.audit > 0 else [],
    }
    if args.url:
        data["sessions"] = live_sessions(args.url)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
