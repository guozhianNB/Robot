# -*- coding: utf-8 -*-
r"""人脸识别自测工具 —— 一条命令看清"能不能检测、能不能认出、能不能切换"。

三种用法（**都能你自己跑，不需要我在场**）：

    1) 只看状态（不碰摄像头）：
           python scripts/face_check.py

    2) 用图片测（不碰摄像头，最快）：
           python scripts/face_check.py --photo 照片.jpg
           python scripts/face_check.py --photo vision/testdata/portrait_lena.jpg

    3) 摄像头实测（"能不能认出我"）：
           python scripts/face_check.py --live
       顺手注册/补拍（会先倒计时提醒你坐好）：
           python scripts/face_check.py --live --enroll 000 --frames 3

输出每一列怎么读
----------------
    检测分    YOLO "框得多准"。门槛 FACE_STABLE_CONF（默认 0.45），弱光/偏远会掉到 0.4 附近
    认出      库里最像的那个人；显示 "—" 表示还没到进库阈值（FACE_MATCH_THRESHOLD，默认 0.45）
    本帧像度  这一帧与库中最像那个人的**余弦相似度**。同一个人：同条件约 0.9+，跨条件可能只有 0.5
    连续均分  当前"同一身份连续段"的平均相似度。切换门槛 FACE_IDENTITY_CONF（默认 0.55）
    frames    "连续几帧看到的是同一组人脸"。要攒够 FACE_STABLE_FRAMES（默认 5）
    stable    人脸稳定出现；switchable 可以据此切换主体（还需"只有一个人 + 身份也稳"）
    reason    没过门槛的原因，见下表

reason 对照表
-------------
    no_face              画面里没检到人脸（人不在/太暗/太远）
    not_enough_frames    看到了，但连续帧数还没攒够（继续对着镜头）
    low_score            检测框置信度不够（改善光照/坐近一点）
    identity_low_score   认出来了，但"认得多像"不够 → **去补拍当前条件下的样本**
    identity_unstable    身份在抖（刚认出来/中间断过，或人在晃）
    multiple_faces       画面里不止一个人（有歧义，系统不猜）
    no_identity          库里没这个人，或不像到阈值以下
    frames_reset         轨迹集合刚变过（画面里多了人/短暂丢失后重连），一致帧数重新数
    ok                   全部达标
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PY = sys.executable
DEFAULT_UID = "000"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def name_map():
    """uid → 给人看的显示名（优先"称呼" nickname，其次姓名 name，都没有才回落 uid）。

    与前端一致（admin 端 `elderLabel()` 就是这个优先级）。测试时看到 "fuze（000）"
    比只看到 "000" 直观得多 —— 而 uid 仍然带上，排查时才对得上库里的目录名。
    """
    try:
        from LLM import db
        out = {}
        for p in db.list_profiles(kind=""):
            uid = p.get("uid") or ""
            if uid:
                out[uid] = p.get("nickname") or p.get("name") or uid
        return out
    except Exception:                                    # noqa: BLE001
        return {}                                        # 名字只是锦上添花，取不到不影响测试


def label(uid, names=None):
    """uid → "fuze（000）"；空/未知 → "未知"。"""
    if not uid:
        return "未知"
    names = name_map() if names is None else names
    nm = names.get(uid)
    return "%s（%s）" % (nm, uid) if nm and nm != uid else str(uid)


# --------------------------------------------------------------------------
# 模式 1：状态（不碰摄像头）
# --------------------------------------------------------------------------
def show_status():
    from LLM import conf, face_api, face_lib
    st = face_api.status()
    names = name_map()
    print("=== 人脸模块状态 ===")
    print("  检测器：%s  %s  (imgsz=%s conf=%s)"
          % ("可用" if st["detector"]["available"] else "不可用",
             st["detector"].get("model") or "-", st["detector"]["imgsz"],
             st["detector"]["conf"]))
    print("  识别器：%s  %s  (维度=%s)"
          % ("可用" if st["embedder"]["available"] else "不可用",
             st["embedder"].get("model") or "-", st["embedder"]["dim"]))
    print("  摄像头：%s" % ("可达（%s:%s）" % (st["camera"]["host"], st["camera"]["port"])
                          if st["camera"]["reachable"] else
                          "不可达 —— %s" % st["camera"]["reason"]))
    t = st["thresholds"]
    print("  门槛：连续 %s 帧 / 检测分 >= %s / 身份分 >= %s / 进库阈值 >= %s"
          % (t["stable_frames"], t["stable_conf"], t["identity_conf"],
             t["match_threshold"]))
    lib = st["library"]
    per = lib.get("per_uid") or {}
    if per:
        detail = "、".join("%s %d 张" % (label(u, names), n) for u, n in per.items())
        print("  已记住的人：%d 位 / %d 张样本 —— %s"
              % (lib.get("uids"), lib.get("samples"), detail))
    else:
        print("  已记住的人：**空**（先注册：--live --enroll 000，"
              "或 python -m LLM.face_api enroll-photo 000 照片.jpg）")
    if lib.get("under_sampled"):
        print("           ⚠️ 样本偏少（建议 >=%s 张，跨光照更容易认不出）：%s"
              % (lib.get("min_samples"),
                 "、".join(label(u, names) for u in lib["under_sampled"])))
    print("  说明：%s" % st.get("note"))
    print("\n提示：想让摄像头可用，先起摄像头服务：")
    print("  .\\.venv\\Scripts\\python.exe -m vision.camera_server --source webcam --fps 15")
    return 0


# --------------------------------------------------------------------------
# 模式 2：图片识别（不碰摄像头，离线）
# --------------------------------------------------------------------------
def check_photo(path):
    import cv2
    from LLM import face_api
    img = cv2.imread(str(path))
    if img is None:
        print("读不到图片：%s" % path)
        return 2
    names = name_map()
    res = face_api.identify_photo(img)
    print("=== 图片识别：%s ===" % path)
    if not res.get("ok"):
        print("  %s（%s）" % (res.get("error"), res.get("status")))
        return 1
    print("  检出 %d 张脸，返回 %d 张：" % (res.get("detected"), res["count"]))
    for i, f in enumerate(res["faces"], 1):
        print("    #%d 检测分=%s  认出=%s  相似度=%s  与第二名差距=%s  (%s)"
              % (i, f["score"], label(f["identity"], names),
                 f["identity_score"], f["identity_margin"], f["identity_reason"]))
    known = [f for f in res["faces"] if f["identity"]]
    if known:
        print("\n结论：认出 %s（相似度 %s）"
              % (label(known[0]["identity"], names), known[0]["identity_score"]))
    else:
        print("\n结论：都不在库里（相似度 %s < 进库阈值 %s）——"
              "想让它认得，先用这张照片注册："
              "python -m LLM.face_api enroll-photo 000 这张图.jpg"
              % (res["faces"][0]["identity_score"],
                 res["library"].get("threshold")))
    return 0


# --------------------------------------------------------------------------
# 模式 3：摄像头实测（自动起/停摄像头服务与后端）
# --------------------------------------------------------------------------
def _wait_port(port, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("端口 %d 未就绪（服务没起来？）" % port)


_IDENTITY_HINT = {
    "below_threshold": "相似度没到进库阈值 —— 库里可能没这个人，"
                       "或光照/距离/角度与注册时差太远（→ 补拍或改善拍摄条件）",
    "empty_library": "样本库是空的 —— 先注册：--live --enroll <uid> --frames 5",
    "ambiguous": "库里有两个人分数咬得很近（差 < 0.03）—— 系统宁可不认",
}


def report_conclusion(j, v, names, st, hits, rounds, scores, seen_names, uid):
    """打印实测结论（抽成函数是为了能脱离摄像头验证：给合成的探针结果也能跑）。

    `verdict.reason` 是**整体结论**（`no_identity` = 检到脸但说不清是谁）；
    脸自己的 `identity_reason` 才说识别那一关卡在哪一步 —— 两者不是一个粒度，别混。
    """
    who_txt = "、".join("%s %d 帧" % (k, n) for k, n in seen_names.items()) or "（没认出谁）"
    print("  认出的人：%s  合计 %d/%d 帧；相似度 %s"
          % (who_txt, hits, rounds,
             ("%.3f ~ %.3f" % (min(scores), max(scores))) if scores else "无"))
    print("  stable=%s switchable=%s identity=%s 连续=%s/%s reason=%s"
          % (v["stable"], v["switchable"], label(v["identity"], names),
             v["identity_frames"], v["required"], v["reason"]))
    if hits and v["switchable"]:
        print("  ✅ 正常：认出 %s，且身份稳定到可用于切换主体"
              % label(v["identity"], names))
    elif hits:
        print("  🟡 能认出 %s，但还没到可切换 → 按 reason 处理："
              "identity_low_score=去补拍；not_enough_frames=多对一会儿镜头；"
              "low_score=改善光照/坐近；multiple_faces=画面里只能有一个人"
              % label(v["identity"], names))
    else:
        print("  ❌ 没认出来：若库是空的先注册（--enroll %s）" % uid)

    f0 = j["faces"][0] if j.get("faces") else None
    fr = f0.get("identity_reason") if f0 else None
    if not f0 or v["switchable"]:
        return
    if fr == "ok":
        print("  识别细节：这一帧**已经认出 %s**（相似度 %s），"
              "只是连续帧/切换条件还没满足 → reason=%s"
              % (label(v.get("identity") or f0.get("identity"), names),
                 f0.get("identity_score"), v["reason"]))
        return
    if not fr:
        return
    print("  识别细节（本次最后一次探测）：identity_reason=%s —— %s"
          % (fr, _IDENTITY_HINT.get(fr, "见 vision/人脸识别操作手册.md §5 常见问题")))
    print("    本次相似度=%s  进库阈值=%s  与第二名差距=%s"
          % (f0.get("identity_score"), st["thresholds"].get("match_threshold"),
             f0.get("identity_margin")))


def check_live(rounds, uid, enroll_frames, api_port, cam_port, force=False):
    # 红线提醒先做（不启服务、不碰摄像头，失败得越快越好）：
    # uid 必须先在档案里存在 —— 没有档案的 uid 会被当作「未知角色」，语音/记忆/提醒全不对。
    # 不自动建档：打错一个 uid 就会多出一条垃圾档案 + 垃圾人脸样本，让用户显式建。
    if enroll_frames and not force and uid not in name_map():
        print("⚠️ uid=%s 还没有档案 —— **先建档，再挂脸**：" % uid)
        print("   .\\.venv\\Scripts\\python.exe scripts\\update_elder.py %s "
              "--name 姓名 --nickname 称呼" % uid)
        print("   建完再回来跑本命令挂人脸。确实只想挂人脸、不动档案？加 --force。")
        return 2

    env = {**os.environ, "PYTHONUTF8": "1", "VISION_HOST": "127.0.0.1",
           "VISION_PORT": str(cam_port)}

    def req(method, path, body=None, timeout=120):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (api_port, path), data=data,
                                   headers={"Content-Type": "application/json"},
                                   method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:                            # noqa: BLE001
                return e.code, {"ok": False, "error": "HTTP %s" % e.code}

    procs = []
    try:
        print("=== 启动摄像头服务与后端（端口 %d / %d）===" % (cam_port, api_port))
        procs.append(subprocess.Popen(
            [PY, "-m", "vision.camera_server", "--source", "webcam",
             "--port", str(cam_port), "--fps", "15",
             "--channels", "1280x720,640x480"],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
        _wait_port(cam_port)
        procs.append(subprocess.Popen(
            [PY, "-m", "uvicorn", "LLM.server:app", "--host", "127.0.0.1",
             "--port", str(api_port), "--log-level", "warning"],
            cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env))
        _wait_port(api_port)

        code, st = req("GET", "/api/face/status")
        if not st.get("detector", {}).get("available"):
            print("  检测器不可用：%s（先跑 python -m vision.face --download）"
                  % st.get("reason"))
            return 1
        if not st.get("embedder", {}).get("available"):
            print("  识别器不可用：%s（先跑 python -m vision.faceid --download）"
                  % st["embedder"].get("reason"))
            return 1
        names = name_map()
        lib = st["library"].get("per_uid") or {}
        lib_txt = "、".join("%s %d 张" % (label(u, names), n) for u, n in lib.items())
        print("  检测器=%s 识别器=%s\n  已记住的人=%s"
              % (st["detector"].get("model"), st["embedder"].get("model"),
                 lib_txt or "**空**（先注册：--enroll 000）"))

        if enroll_frames:
            print("\n=== 先注册/补拍 %d 帧（uid=%s）===" % (enroll_frames, uid))
            for i in range(6, 0, -1):
                print("    %d…（请正对镜头）" % i)
                time.sleep(1)
            code, j = req("POST", "/api/face/enroll",
                          {"uid": uid, "frames": enroll_frames})
            print("  ok=%s added=%s 现有样本=%s %s"
                  % (j.get("ok"), j.get("added"), j.get("samples_total"),
                     j.get("error") or ""))
            if not j.get("ok"):
                return 1

        print("\n=== 连续探测 %d 次（请正对镜头）===" % rounds)
        print("  %-3s %-7s %-16s %-9s %-9s %-6s %-7s %-11s %-20s %s"
              % ("#", "检测分", "认出的是谁", "本帧像度", "连续均分", "frames", "stable",
                 "switchable", "reason", "耗时ms"))
        hits, scores, first_ok, last, seen_names = 0, [], None, None, {}
        for i in range(1, rounds + 1):
            code, j = req("POST", "/api/face/probe")
            if code != 200:
                print("  第 %d 次失败 %s：%s" % (i, code, j.get("error")))
                break
            v, f0 = j["verdict"], (j["faces"][0] if j["faces"] else {})
            last = (j, v)
            who = label(f0.get("identity"), names)
            if f0.get("identity"):
                hits += 1
                seen_names[who] = seen_names.get(who, 0) + 1
            if f0.get("identity_score") is not None:
                scores.append(float(f0["identity_score"]))
            if v["switchable"] and first_ok is None:
                first_ok = i
            print("  %-3d %-7s %-16s %-9s %-9s %-6d %-7s %-11s %-20s %.0f"
                  % (i, f0.get("score"), who,
                     f0.get("identity_score"), v.get("identity_score"), v["frames"],
                     v["stable"], v["switchable"], v["reason"], j["total_ms"]))
            time.sleep(0.2)

        print("\n=== 结论 ===")
        if not last:
            print("  没有拿到结果（服务没起来？）")
            return 1
        j, v = last
        report_conclusion(j, v, names, st, hits, rounds, scores, seen_names, uid)
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=15)
            except Exception:                            # noqa: BLE001
                p.kill()
        print("\n（摄像头服务与后端已停止）")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python scripts/face_check.py",
        description="人脸识别自测：状态 / 图片 / 摄像头实测（默认只打印状态）")
    p.add_argument("--photo", metavar="图片", help="用一张图片识别（不需要摄像头）")
    p.add_argument("--live", action="store_true", help="摄像头实测（自动起停服务）")
    p.add_argument("--rounds", type=int, default=8, help="摄像头实测的探测次数（默认 8）")
    p.add_argument("--enroll", metavar="UID", default=None,
                   help="实测前先注册/补拍这个 uid（配合 --frames）")
    p.add_argument("--frames", type=int, default=3, help="补拍帧数（默认 3）")
    p.add_argument("--force", action="store_true",
                   help="uid 没有档案时也照样挂脸（不推荐：该 uid 会被当成未知角色）")
    args = p.parse_args(argv)

    if args.photo:
        return check_photo(args.photo)
    if args.live:
        return check_live(args.rounds, args.enroll or DEFAULT_UID,
                          args.frames if args.enroll else 0,
                          free_port(), free_port(), force=args.force)
    return show_status()


if __name__ == "__main__":
    sys.exit(main())
