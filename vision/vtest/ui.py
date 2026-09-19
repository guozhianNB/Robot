# -*- coding: utf-8 -*-
r"""三个 Tkinter 窗口（一级/二级/三级）—— `vision_test_start.py` 的界面层。

窗口层级"
--------
    一级小窗  主窗口：三个按钮 + 库里现状 + 最近一次操作
    二级小窗  Toplevel：人脸录入 / 人脸检测 / 删除数据
    三级小窗  Toplevel：录入老人信息表单（由"人脸录入"的二级窗打开）

并发模型
--------
Tkinter 控件只允许主线程碰，而抓帧+推理很慢，所以：

    工作线程（pipeline.FrameWorker）  抓帧 → 检测/识别 → 发布"最新快照"
    主线程（root.after 每 33ms）      取快照 → 画到 Canvas → 刷新文字

主线程只做"缩放 + PNG 编码 + 画框"，都是毫秒级，所以窗口不会卡。

人脸框与姓名为什么画在 Canvas 上、而不是用 cv2 画在图里
------------------------------------------------------
OpenCV 的 Hershey 字体**画不了中文**（"张桂芳"会变成一串 ?）。Tk 的 Canvas 文字用系统
字体，中文、生僻字都没问题，字还更清楚。所以预览用 Canvas：底图是 PhotoImage，
人脸框/姓名是 Canvas 上的图形项，按画面→控件的缩放比换算坐标。
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import people, pipeline, service

try:
    import cv2
except ImportError:                                      # noqa: BLE001
    cv2 = None

BG = "#f5f6f8"
FG = "#1b1b1b"
OK_COLOR = "#0a7d28"
WARN_COLOR = "#b26a00"
ERR_COLOR = "#b00020"
MUTED = "#555555"

PREVIEW_W, PREVIEW_H = 640, 360


def _family():
    """中文字体：Windows 用微软雅黑，Linux 用 Noto（找不到时 Tk 自己回落）。"""
    return "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"


def _tail(text, limit=700):
    """长输出只留尾巴（错误信息通常在最后几行）。"""
    s = str(text or "").strip()
    return s if len(s) <= limit else "…（前略）\n" + s[-limit:]


class App:
    """整套界面。构造完调 `root.mainloop()`（由入口负责）。"""

    def __init__(self, root, opts):
        self.root = root
        self.opts = opts
        self.fam = _family()
        self.font_btn = (self.fam, 12)
        self.font_h = (self.fam, 13, "bold")
        self.font_name = (self.fam, 11, "bold")
        self.font_small = (self.fam, 9)
        self.font_tiny = (self.fam, 8)
        self.pw = int(getattr(opts, "preview_width", PREVIEW_W))
        self.ph = int(getattr(opts, "preview_height", PREVIEW_H))

        self._photo = None
        self._render_job = None
        self._tick_job = None
        self._after_job = None
        self._exit_requested = False
        self._shutdown_done = False
        self._busy = False
        self._rows = []
        self._names = {}
        self._threshold = None
        self._last_action = "—"

        self.win2 = None
        self.win3 = None
        self.worker = None
        self.cam = None
        self.page_kind = None
        self.canvas = None
        self._img_item = None
        self.lv2_status = None
        self._start_btn = None
        self._form_widgets = []
        self._form_back = None
        self._form_status_lbl = None
        self._del_tree = None
        self._del_hits = []
        self._del_query = None
        self._del_status = None
        self._del_out = None

        self.root.title("vision_test_start —— 人脸测试台（一级小窗）")
        self.root.configure(bg=BG)
        self._ensure_vision_env()                        # 必须最早：conf 是 import 时读环境
        self._reload_people()
        self._build_level1()
        self._install_signals()
        self._tick_job = self.root.after(200, self._tick)

    # ==================================================================
    # 一级小窗
    # ==================================================================
    def _build_level1(self):
        f = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        f.pack(fill="both", expand=True)

        tk.Label(f, text="人脸测试台", font=(self.fam, 17, "bold"), bg=BG,
                 fg=FG).pack(anchor="w")
        self.lv1_model = tk.Label(f, text="", font=self.font_tiny, bg=BG, fg=MUTED,
                                  justify="left")
        self.lv1_model.pack(anchor="w", pady=(6, 0))
        self.lv1_lib = tk.Label(f, text="", font=self.font_tiny, bg=BG, fg=MUTED,
                                justify="left")
        self.lv1_lib.pack(anchor="w")

        row = tk.Frame(f, bg=BG)
        row.pack(pady=14)
        self.btn_enroll = tk.Button(row, text="① 人脸录入", font=self.font_btn,
                                    width=13, height=2, command=self.open_enroll_window)
        self.btn_enroll.pack(side="left")
        self.btn_detect = tk.Button(row, text="② 人脸检测", font=self.font_btn,
                                    width=13, height=2, command=self.open_detect_window)
        self.btn_detect.pack(side="left", padx=8)
        self.btn_delete = tk.Button(row, text="③ 删除数据", font=self.font_btn,
                                    width=13, height=2, command=self.open_delete_window)
        self.btn_delete.pack(side="left")
        self._root_buttons = [self.btn_enroll, self.btn_detect, self.btn_delete]

        tk.Label(f, text="本窗口包装的三条命令（与你手工敲的完全同一条路径）：",
                 font=self.font_tiny, bg=BG, fg=MUTED).pack(anchor="w")
        tk.Label(f, justify="left", font=self.font_tiny, bg=BG, fg=MUTED,
                 text=("  人脸录入  python scripts\\update_elder.py <uid> --name 姓名 "
                       "--nickname 称呼 --bed 床位 --age 年龄\n"
                       "  人脸检测  python scripts\\face_check.py --live\n"
                       "  删除数据  python scripts\\update_elder.py <uid> --delete --yes")
                 ).pack(anchor="w")

        self.lv1_action = tk.Label(f, text="", font=self.font_small, bg=BG, fg=MUTED,
                                   justify="left", wraplength=520)
        self.lv1_action.pack(anchor="w", pady=(10, 0))
        tk.Label(f, text="退出：按 Ctrl+C，或直接关掉本窗口。", font=self.font_small,
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(6, 0))
        self._refresh_level1()

    def _refresh_level1(self):
        """刷新模型可用性与库现状（每关掉一个二级小窗都会刷新）。"""
        st = people.model_status()
        det = st.get("detector") or {}
        emb = st.get("embedder") or {}
        self.lv1_model.config(text=(
            "检测模型：%s%s      识别模型：%s%s"
            % ("✅ " + (det.get("model") or "") if det.get("available")
               else "❌ " + str(det.get("reason") or "不可用"),
               "",
               "✅ " + (emb.get("model") or "") if emb.get("available")
               else "❌ " + str(emb.get("reason") or "不可用"),
               "")))
        lines = people.summary_lines(self._rows)
        self.lv1_lib.config(text="\n".join(lines))
        self.lv1_action.config(text="最近一次操作：%s" % self._last_action)

    def _set_root_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        for b in self._root_buttons:
            try:
                b.config(state=state)
            except tk.TclError:
                pass

    def _ensure_vision_env(self):
        """保证取帧方（`LLM.conf`）与本 GUI 起的摄像头服务**是同一个地址**。

        顺序很要紧：`LLM/conf.py` 在 **import 时**就把 `VISION_HOST/VISION_PORT`
        固化下来了（默认 127.0.0.1:9540）。入口脚本本来就先定端口、再写环境变量；
        这里再兜一次，是为了"不走入口、直接构造 App"（自测脚本）也不会两者对不上：

        * 端口没给 → 现挑一个空闲端口；
        * 已经 import 过 `LLM.conf` → 直接改它的模块属性（改环境变量已经晚了）。
        """
        port = getattr(self.opts, "camera_port", None)
        if not port:
            port = service.free_port()
            self.opts.camera_port = port
        port = int(port)
        os.environ.setdefault("VISION_HOST", "127.0.0.1")
        os.environ["VISION_PORT"] = str(port)
        mod = sys.modules.get("LLM.conf")
        if mod is not None and getattr(mod, "VISION_PORT", None) != port:
            mod.VISION_PORT = port
            mod.VISION_HOST = os.environ["VISION_HOST"]

    def _reload_people(self):
        self._rows = people.profiles()
        self._names = people.name_map(self._rows)

    # ==================================================================
    # 定时器 / 退出
    # ==================================================================
    def _install_signals(self):
        import signal

        def handler(*_a):
            self._exit_requested = True

        try:
            signal.signal(signal.SIGINT, handler)
        except Exception:                                # noqa: BLE001
            pass                                          # 非主线程/不支持时忽略

    def _tick(self):
        """每 200ms 一次空转：让 Python 有机会处理 Ctrl+C（Tk 的 mainloop 是 C 循环）。"""
        self._tick_job = None
        if self._exit_requested:
            self.shutdown()
            return
        self._tick_job = self.root.after(200, self._tick)

    def _cancel(self, job):
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:                            # noqa: BLE001
                pass

    def shutdown(self):
        """退出：停线程、停摄像头服务、关窗（幂等）。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._cancel(self._render_job)
        self._cancel(self._tick_job)
        self._cancel(self._after_job)
        self._render_job = self._tick_job = self._after_job = None
        try:
            self.close_window2(silent=True)
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                                # noqa: BLE001
            pass
        print("[vision_test_start] 已退出（摄像头服务已停止）。", flush=True)

    # ==================================================================
    # 二级小窗的公共部分
    # ==================================================================
    def _new_window2(self, title):
        win = tk.Toplevel(self.root)
        win.title("%s —— 二级小窗" % title)
        win.configure(bg=BG)
        win.protocol("WM_DELETE_WINDOW", self.close_window2)
        win.transient(self.root)
        win.resizable(False, False)
        self.win2 = win
        self._set_root_buttons(False)
        return win

    def _place(self, win):
        """把二级小窗摆在主窗口右下一点，别完全盖住一级小窗。"""
        try:
            win.update_idletasks()
            x = self.root.winfo_rootx() + 40
            y = self.root.winfo_rooty() + 40
            win.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def close_window2(self, silent=False):
        """关掉二级（连带三级）、停工作线程与摄像头服务，回到一级小窗。"""
        if self.win2 is None and self.worker is None and self.cam is None:
            return
        self._cancel(self._render_job)
        self._cancel(self._after_job)
        self._render_job = self._after_job = None
        self.page_kind = None
        if self.worker is not None:
            self.worker.stop(3.0)
            self.worker = None
        if self.win3 is not None:
            try:
                self.win3.grab_release()
            except Exception:                            # noqa: BLE001
                pass
            try:
                self.win3.destroy()
            except Exception:                            # noqa: BLE001
                pass
            self.win3 = None
        if self.cam is not None:
            self.cam.stop()                              # 用完就关，不常开摄像头
            self.cam = None
        if self.win2 is not None:
            try:
                self.win2.destroy()
            except Exception:                            # noqa: BLE001
                pass
            self.win2 = None
        self.canvas = None
        self._img_item = None
        self.lv2_status = None
        self._start_btn = None
        self._form_widgets = []
        self._form_back = None
        self._form_status_lbl = None
        self._del_tree = None
        self._del_hits = []
        self._del_query = None
        self._del_status = None
        self._del_out = None
        self._busy = False
        if not silent:
            self._set_root_buttons(True)
            self._reload_people()
            self._refresh_level1()

    def _ensure_camera(self):
        """起摄像头服务（已起则复用）。返回 `(ok, 说明)`。"""
        if self.cam is not None and self.cam.running():
            return True, "ok"
        self.cam = service.CameraService(
            source=self.opts.source, port=int(self.opts.camera_port),
            fps=int(self.opts.camera_fps), device=self.opts.device,
            channels=self.opts.channels, wait=float(self.opts.camera_wait))
        return self.cam.start()

    # ==================================================================
    # ② / ① 摄像头窗口
    # ==================================================================
    def open_detect_window(self):
        self._open_camera_window("detect")

    def open_enroll_window(self):
        self._open_camera_window("enroll")

    def _open_camera_window(self, kind):
        if self.win2 is not None:
            return
        if cv2 is None:
            self._notify("缺少依赖", "图片缩放/编码需要 opencv-python：\n"
                                     ".venv\\Scripts\\pip.exe install opencv-python")
            return
        title = "人脸录入" if kind == "enroll" else "人脸检测"
        win = self._new_window2(title)
        self.page_kind = kind
        head = ("① 人脸录入 —— 先确认镜头前有脸，再点「开始录入」"
                if kind == "enroll" else
                "② 人脸检测 —— 认出的人名会画在头上（未知 = 不在库里）")
        tk.Label(win, text=head, font=self.font_h, bg=BG, fg=FG).pack(
            anchor="w", padx=12, pady=(10, 6))

        self.canvas = tk.Canvas(win, width=self.pw, height=self.ph, bg="#101010",
                                highlightthickness=0)
        self.canvas.pack(padx=12)
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        if kind == "enroll":
            self._start_btn = tk.Button(bar, text="开始录入", font=(self.fam, 13, "bold"),
                                        width=12, state="disabled",
                                        command=self._start_enroll)
            self._start_btn.pack(side="left")
        tk.Button(bar, text="返回一级小窗", font=self.font_btn,
                  command=self.close_window2).pack(side="left", padx=8)

        self.lv2_status = tk.Label(win, text="正在打开摄像头…", font=self.font_small,
                                   bg=BG, fg=MUTED, justify="left",
                                   wraplength=self.pw + 24)
        self.lv2_status.pack(anchor="w", padx=12, pady=(0, 10))
        self._place(win)

        ok, msg = self._ensure_camera()
        if not ok:
            self.lv2_status.config(
                text=("❌ 摄像头不可用，本窗口只能看提示。\n"
                      "可能原因：摄像头没插 / 被别的程序占用（后端、另一个测试工具）/ "
                      "设备号不对（试 --device 1）/ 板卡上要 --source mipi。\n"
                      "详情：%s" % _tail(msg)), fg=ERR_COLOR)
            return
        self._threshold = self._grab_threshold()
        from LLM import face_api
        face_api.reset()                                 # 换场景，重新数"连续帧"
        self.worker = pipeline.FrameWorker(
            self.cam.host, self.cam.port, channel=int(self.opts.channel),
            identify=(kind == "detect"))
        self.worker.start()
        self._render_job = self.root.after(60, self._render)

    def _grab_threshold(self):
        try:
            st = people.model_status()
            return (st.get("thresholds") or {}).get("match_threshold")
        except Exception:                                # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def _render(self):
        """主线程：把最新快照画出来（每 33ms 一次，约 30fps 上限）。

        注意这里**只读**快照：抓帧在工作线程（"预览流畅"），检测在另一个工作线程
        （"识别较慢"）—— 所以推理慢不会把画面拖成一顿一顿的。
        """
        self._render_job = None
        if self.page_kind is None or self.worker is None or self.canvas is None:
            return
        snap = self.worker.snapshot()
        bgr = snap.get("bgr")
        if bgr is not None:
            fit = self._fit(bgr)
            if fit is not None:
                dw, dh, ox, oy, _scale = fit
                try:
                    small = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".png", small)
                except Exception:                        # noqa: BLE001
                    ok, buf = False, None
                if ok:
                    try:
                        self._photo = tk.PhotoImage(data=buf.tobytes())
                        self.canvas.itemconfigure(self._img_item, image=self._photo)
                        self.canvas.coords(self._img_item, ox, oy)
                    except tk.TclError:
                        pass
            self._draw_faces(bgr, snap.get("faces") or [])
        self._update_lv2_status(snap)
        self._render_job = self.root.after(33, self._render)

    def _fit(self, bgr):
        """等比缩放到画布里并居中（返回 `(dw, dh, ox, oy, scale)`）。

        必须等比：直接把 640x480 的画面塞进 640x360 的控件会把脸压扁，
        而且画出来的框会与脸错位。等比之后多出来的地方留黑边。
        """
        try:
            h, w = bgr.shape[:2]
        except Exception:                                # noqa: BLE001
            return None
        if not w or not h:
            return None
        scale = min(self.pw / float(w), self.ph / float(h))
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        return dw, dh, (self.pw - dw) // 2, (self.ph - dh) // 2, scale

    def _draw_faces(self, bgr, faces):
        """在 Canvas 上画框与姓名（坐标按画面→控件缩放换算）。"""
        self.canvas.delete("face")
        fit = self._fit(bgr)
        if fit is None:
            return
        _dw, _dh, ox, oy, scale = fit
        for f in faces:
            try:
                x1, y1, x2, y2 = [float(v) for v in f["box"]]
            except Exception:                            # noqa: BLE001
                continue
            X1 = max(0, min(self.pw - 1, ox + x1 * scale))
            Y1 = max(0, min(self.ph - 1, oy + y1 * scale))
            X2 = max(0, min(self.pw - 1, ox + x2 * scale))
            Y2 = max(0, min(self.ph - 1, oy + y2 * scale))
            known = bool(f.get("identity"))
            color = OK_COLOR if known else WARN_COLOR
            self.canvas.create_rectangle(X1, Y1, X2, Y2, outline=color, width=2,
                                         tags="face")
            if self.page_kind == "detect":
                txt = people.label(f.get("identity"), self._names) if known else "未知"
                sc = f.get("identity_score")
                if sc is not None:
                    txt += "  %.2f" % float(sc)
            else:
                txt = "检测到人脸 %.2f" % float(f.get("score") or 0.0)
            if Y1 < 20:                                  # 贴顶 → 名字画到框下面
                self.canvas.create_text(X1 + 4, min(self.ph - 4, Y2 + 4),
                                        anchor="nw", text=txt, fill=color,
                                        font=self.font_name, tags="face")
            else:
                self.canvas.create_text(X1 + 4, Y1 - 4, anchor="sw", text=txt,
                                        fill=color, font=self.font_name, tags="face")

    def _update_lv2_status(self, snap):
        if self.lv2_status is None or self._busy:
            return
        faces = snap.get("faces") or []
        v = snap.get("verdict") or {}
        lines = []
        for key in ("error", "detect_error"):
            if snap.get(key):
                lines.append("⚠️ %s" % snap[key])
        if not faces:
            lines.append("还没检测到人脸 —— 请正对镜头、光线亮一点、坐近一点。")
        elif self.page_kind == "enroll":
            lines.append("✅ 检测到 %d 张脸（检测分 %s）—— 点「开始录入」开始采样。"
                         % (len(faces), "、".join("%.2f" % float(f.get("score") or 0.0)
                                                  for f in faces[:3])))
            if len(faces) > 1:
                lines.append("⚠️ 画面里不止一个人：录入只取**面积最大**的那张脸，"
                             "建议只留一个人。")
        else:
            known = [f for f in faces if f.get("identity")]
            f0 = known[0] if known else faces[0]
            if known:
                lines.append("认出：%s（相似度 %s，检测分 %s）"
                             % (people.label(f0.get("identity"), self._names),
                                f0.get("identity_score"), f0.get("score")))
            else:
                lines.append("认不出这是谁（identity_reason=%s，相似度 %s < 进库阈值 %s）"
                             % (f0.get("identity_reason") or "—",
                                f0.get("identity_score"), self._threshold))
            if v:
                lines.append("稳定出现=%s  可切换主体=%s  连续 %s/%s 帧  reason=%s"
                             % (v.get("stable"), v.get("switchable"),
                                v.get("identity_frames"), v.get("required"),
                                v.get("reason")))
                if v.get("switchable"):
                    lines.append("✅ 已满足切换条件：%s"
                                 % people.label(v.get("identity"), self._names))
            lines.append("等价命令：python scripts\\face_check.py --live"
                         "（同一套 probe 判定，本窗口把结果显示在画面上）")
        # 两个速率分开报：**预览**低 = 取帧/摄像头侧的问题；**推理**低 = 模型慢，正常
        lines.append("预览 %s fps   推理 %s 次/秒（检测 %s ms）   帧号 %s / 已推理 %s"
                     % (snap.get("fps") or 0, snap.get("analyze_fps") or 0,
                        snap.get("analyze_ms"), snap.get("n"), snap.get("analyzed")))
        self.lv2_status.config(text="\n".join(lines),
                               fg=OK_COLOR if faces and not snap.get("error")
                               else WARN_COLOR)
        if self._start_btn is not None:
            self._start_btn.config(state="normal" if faces else "disabled")

    # ==================================================================
    # ① 人脸录入
    # ==================================================================
    def _start_enroll(self):
        """「开始录入」：镜头前没人就退出，**不动数据库**。"""
        if self.worker is None:
            return
        faces = self.worker.snapshot().get("faces") or []
        if not faces:
            print("[人脸录入] 未检测到人脸 → 退出录入，档案与样本库都没有改动。",
                  flush=True)
            self._notify("未检测到人脸",
                         "镜头前没有检测到人脸 → 已退出录入。\n\n"
                         "数据库与样本库都没有任何改动。")
            self.close_window2()
            return
        self._open_enroll_form(people.next_uid(self._rows))

    def _open_enroll_form(self, uid):
        win = tk.Toplevel(self.win2 or self.root)
        win.title("录入老人信息 —— 三级小窗")
        win.configure(bg=BG)
        win.transient(self.win2 or self.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", self._close_win3)
        self.win3 = win

        tk.Label(win, text="录入老人信息", font=(self.fam, 15, "bold"), bg=BG,
                 fg=FG).grid(row=0, column=0, columnspan=2, sticky="w",
                             padx=14, pady=(12, 2))
        tk.Label(win, text="uid 由系统给（前一个 uid + 1），可改；姓名会显示在检测画面上。",
                 font=self.font_tiny, bg=BG, fg=MUTED).grid(
                     row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        vars_ = {"name": tk.StringVar(), "nickname": tk.StringVar(),
                 "bed": tk.StringVar(), "age": tk.StringVar(),
                 "uid": tk.StringVar(value=str(uid))}
        fields = [("姓名 *", "name"), ("称呼", "nickname"), ("床位", "bed"),
                  ("年龄", "age"), ("uid", "uid")]
        first = None
        for i, (lab, key) in enumerate(fields):
            tk.Label(win, text=lab, font=self.font_small, bg=BG, fg=FG).grid(
                row=2 + i, column=0, sticky="e", padx=(14, 6), pady=3)
            ent = tk.Entry(win, textvariable=vars_[key], font=(self.fam, 11), width=22)
            ent.grid(row=2 + i, column=1, sticky="w", pady=3)
            self._form_widgets.append(ent)
            if first is None:
                first = ent
        tk.Label(win, text="人脸样本：拍 %s 张（与库里已有的取平均，跨光照更稳）"
                 % self.opts.enroll_frames, font=self.font_tiny, bg=BG,
                 fg=MUTED).grid(row=7, column=0, columnspan=2, sticky="w",
                                padx=14, pady=(6, 0))

        self._form_status_lbl = tk.Label(win, text="", font=self.font_small, bg=BG,
                                         fg=ERR_COLOR, justify="left", wraplength=380)
        self._form_status_lbl.grid(row=8, column=0, columnspan=2, sticky="w",
                                   padx=14, pady=(8, 0))

        btn_row = tk.Frame(win, bg=BG)
        btn_row.grid(row=9, column=0, columnspan=2, sticky="w", padx=14, pady=12)
        ok_btn = tk.Button(btn_row, text="确认录入", font=self.font_btn, width=10,
                           command=lambda: self._submit_enroll(win, vars_))
        ok_btn.pack(side="left")
        cancel = tk.Button(btn_row, text="取消", font=self.font_btn, width=8,
                           command=self._close_win3)
        cancel.pack(side="left", padx=8)
        self._form_widgets += [ok_btn, cancel]
        self._form_back = tk.Button(btn_row, text="返回一级小窗", font=self.font_btn,
                                    width=14, command=self.close_window2)

        self._place(win)
        try:
            if first is not None:
                first.focus_set()
        except tk.TclError:
            pass

    def _close_win3(self):
        if self.win3 is None:
            return
        try:
            self.win3.grab_release()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.win3.destroy()
        except Exception:                                # noqa: BLE001
            pass
        self.win3 = None

    def _form_status(self, text, color=ERR_COLOR):
        if self._form_status_lbl is not None:
            try:
                self._form_status_lbl.config(text=text, fg=color)
            except tk.TclError:
                pass

    def _submit_enroll(self, win, vars_):
        if self._busy:
            return
        name = vars_["name"].get().strip()
        nickname = vars_["nickname"].get().strip()
        bed = vars_["bed"].get().strip()
        age = vars_["age"].get().strip()
        uid = vars_["uid"].get().strip()
        if not name:
            self._form_status("姓名必填 —— 它会显示在检测画面的人头上。")
            return
        if age and not age.isdigit():
            self._form_status("年龄请填数字（或留空）。")
            return
        ok, why = people.check_uid(uid)
        if not ok:
            self._form_status(why)
            return
        exists = bool(people.profile_of(uid))
        if exists and not self._ask(
                "uid 已存在",
                "uid=%s 已经有档案（%s）。\n\n继续的话：人脸样本会**追加**到这个人身上，"
                "档案字段会被更新。\n\n要继续吗？"
                % (uid, people.label(uid, self._names))):
            return

        self._busy = True
        self._form_status("正在采样人脸…（请正对镜头、保持不动）", WARN_COLOR)
        for w in self._form_widgets:
            try:
                w.config(state="disabled")
            except tk.TclError:
                pass
        if self.worker is not None:
            self.worker.set_enabled(False)               # 采样期间让 enroll 独占模型

        q = queue.Queue()
        args = (uid, name, nickname, bed, age, exists)

        def work():
            q.put(self._do_enroll(*args))

        threading.Thread(target=work, daemon=True, name="vtest-enroll").start()
        self._poll_enroll(win, q)

    def _do_enroll(self, uid, name, nickname, bed, age, exists):
        """工作线程：先采人脸样本 → 再写档案（失败则回滚刚采的样本）。"""
        from LLM import face_api
        out = {"ok": False, "stage": "人脸采样", "uid": uid, "name": name,
               "exists": exists}
        try:
            before = (face_api.library().get("per_uid") or {}).get(uid, 0)
        except Exception:                                # noqa: BLE001
            before = 0
        try:
            res = face_api.enroll(uid, frames=int(self.opts.enroll_frames))
        except Exception as e:                           # noqa: BLE001
            out["detail"] = "采样异常：%r" % (e,)
            return out
        if not res.get("ok"):
            out["detail"] = res.get("error") or res.get("status") or "采样失败"
            return out
        out["added"] = res.get("added")
        out["total"] = res.get("samples_total")

        out["stage"] = "写档案"
        args = people.create_cmd(uid, name, nickname, bed, age)
        code, text = people.run_update_elder(args)
        out["cmd"] = people.cmd_text(args)
        out["out"] = text
        if code != 0:
            out["detail"] = "update_elder.py 退出码 %s（档案没写成功）" % code
            if before == 0:
                try:
                    face_api.delete_person(uid)
                    out["rolled_back"] = True
                except Exception as e:                   # noqa: BLE001
                    out["rolled_back"] = "回滚失败：%r" % (e,)
            else:
                out["rolled_back"] = ("未回滚：该 uid 本来就有 %d 张样本，"
                                      "删了会连旧的样本一起没" % before)
            return out
        out["ok"] = True
        out["created"] = "已新建档案" in text
        return out

    def _poll_enroll(self, win, q):
        try:
            res = q.get_nowait()
        except queue.Empty:
            if not win.winfo_exists():
                return
            self.root.after(120, lambda: self._poll_enroll(win, q))
            return
        self._finish_enroll(win, res)

    def _finish_enroll(self, win, res):
        self._busy = False
        if not win.winfo_exists():
            return
        if res.get("ok"):
            uid = res["uid"]
            self._last_action = "人脸录入成功：%s（新增 %s 张样本，共 %s 张，%s）" % (
                people.label(uid, people.name_map()), res.get("added"),
                res.get("total"), "新建档案" if res.get("created") else "更新档案")
            print("[人脸录入] ✅ %s" % self._last_action, flush=True)
            for w in self._form_widgets:
                try:
                    w.pack_forget()
                    w.grid_forget()
                except tk.TclError:
                    pass
            self._form_status(
                "✅ 已录入\n\n"
                "  uid：%s\n  姓名：%s\n  人脸样本：新增 %s 张（共 %s 张）\n  档案：%s\n\n"
                "3 秒后自动返回一级小窗…"
                % (uid, people.label(uid, people.name_map()), res.get("added"),
                   res.get("total"), "已新建" if res.get("created") else "已更新"),
                OK_COLOR)
            try:
                self._form_status_lbl.config(font=(self.fam, 11, "bold"))
                self._form_back.pack(side="left", padx=8)
            except tk.TclError:
                pass
            self._after_job = self.root.after(3000, self.close_window2)
            return

        msg = "%s失败：%s" % (res.get("stage") or "录入", res.get("detail") or "未知原因")
        rb = res.get("rolled_back")
        if rb is True:
            msg += "\n（已回滚：刚采的人脸样本已删除，样本库没有留痕）"
        elif rb:
            msg += "\n（%s）" % rb
        if res.get("cmd"):
            msg += "\n\n命令：%s" % res["cmd"]
        if res.get("out"):
            msg += "\n输出：%s" % _tail(res["out"], 500)
        print("[人脸录入] ❌ %s" % msg.replace("\n", " / "), flush=True)
        self._form_status(msg)
        for w in self._form_widgets:
            try:
                w.config(state="normal")
            except tk.TclError:
                pass
        if self.worker is not None:
            self.worker.set_enabled(True)

    # ==================================================================
    # ③ 删除数据
    # ==================================================================
    def open_delete_window(self):
        if self.win2 is not None:
            return
        win = self._new_window2("删除数据")
        self.page_kind = "delete"
        tk.Label(win, text="③ 删除数据", font=self.font_h, bg=BG, fg=FG).pack(
            anchor="w", padx=12, pady=(10, 2))
        tk.Label(win, text="输入姓名（或称呼）→ 反查 uid → 删掉这个人的全部数据。\n"
                           "命令：python scripts\\update_elder.py <uid> --delete --yes",
                 font=self.font_tiny, bg=BG, fg=MUTED, justify="left").pack(
                     anchor="w", padx=12)

        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=12, pady=8)
        tk.Label(row, text="姓名：", font=self.font_btn, bg=BG, fg=FG).pack(side="left")
        self._del_query = tk.StringVar()
        ent = tk.Entry(row, textvariable=self._del_query, font=(self.fam, 12), width=18)
        ent.pack(side="left")
        ent.bind("<Return>", lambda _e: self._delete_find())
        tk.Button(row, text="查找", font=self.font_btn, width=6,
                  command=self._delete_find).pack(side="left", padx=6)

        cols = ("uid", "name", "nick", "bed", "age", "how")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=5)
        for c, t, wd in (("uid", "uid", 70), ("name", "姓名", 110), ("nick", "称呼", 100),
                         ("bed", "床位", 60), ("age", "年龄", 50), ("how", "命中方式", 80)):
            tree.heading(c, text=t)
            tree.column(c, width=wd, anchor="w")
        tree.pack(fill="x", padx=12)
        self._del_tree = tree

        self._del_status = tk.Label(win, text="先输入姓名再点「查找」。", font=self.font_small,
                                    bg=BG, fg=MUTED, justify="left", wraplength=520)
        self._del_status.pack(anchor="w", padx=12, pady=(6, 0))

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=12, pady=8)
        tk.Button(bar, text="删除选中的人", font=(self.fam, 12, "bold"), width=13,
                  fg=ERR_COLOR, command=self._delete_selected).pack(side="left")
        tk.Button(bar, text="返回一级小窗", font=self.font_btn, width=13,
                  command=self.close_window2).pack(side="left", padx=8)

        self._del_out = tk.Text(win, height=7, width=74, font=("Consolas", 9),
                                bg="#ffffff", fg=FG, wrap="word")
        self._del_out.pack(fill="both", padx=12, pady=(0, 12))
        self._place(win)
        try:
            ent.focus_set()
        except tk.TclError:
            pass

    def _delete_find(self):
        if self._del_tree is None:
            return
        q = (self._del_query.get() or "").strip()
        for iid in self._del_tree.get_children():
            self._del_tree.delete(iid)
        self._del_hits = people.find_people(q, self._rows)
        if not self._del_hits:
            print("[删除数据] 查「%s」→ 没找到。库里现有：%s"
                  % (q, people.all_names(self._rows)), flush=True)
            self._del_status.config(
                text="没找到「%s」。库里现有：%s" % (q, people.all_names(self._rows)),
                fg=WARN_COLOR)
            return
        print("[删除数据] 查「%s」→ 命中 %d 位：%s"
              % (q, len(self._del_hits),
                 "、".join("%s（%s，%s）" % (r.get("name") or "—", r.get("uid"),
                                            r.get("_how")) for r in self._del_hits)),
              flush=True)
        for i, r in enumerate(self._del_hits):
            iid = "row%d" % i
            self._del_tree.insert(
                "", "end", iid=iid,
                values=(r.get("uid"), r.get("name") or "—", r.get("nickname") or "—",
                        r.get("bed") or "—", r.get("age") or "—", r.get("_how") or ""))
        self._del_tree.selection_set("row0")
        self._del_status.config(
            text="找到 %d 位 —— 确认是本人后点「删除选中的人」（会先自动备份数据库，"
                 "但删除本身不可撤销）。" % len(self._del_hits), fg=OK_COLOR)

    def _delete_selected(self):
        if self._busy or self._del_tree is None:
            return
        sel = self._del_tree.selection()
        if not sel:
            self._del_status.config(text="先在列表里选中一行。", fg=WARN_COLOR)
            return
        idx = int(str(sel[0]).replace("row", "") or 0)
        if idx >= len(self._del_hits):
            return
        uid = str(self._del_hits[idx].get("uid") or "")
        nm = people.label(uid, self._names)
        if not self._ask("确认删除",
                         "将删除 %s 的**全部数据**：档案、人脸样本、声纹、记忆、提醒、"
                         "对话历史……\n\n脚本会先自动备份数据库，但删除本身不可撤销。\n\n"
                         "确认删除？" % nm):
            return
        self._busy = True
        self._del_status.config(text="正在删除 %s …" % nm, fg=WARN_COLOR)
        self._write_out("$ %s\n" % people.cmd_text(people.delete_cmd(uid)))
        q = queue.Queue()

        def work():
            try:
                code, out = people.run_update_elder(people.delete_cmd(uid))
            except Exception as e:                       # noqa: BLE001
                code, out = -1, "执行失败：%r" % (e,)
            q.put({"ok": code == 0, "uid": uid, "code": code, "out": out})

        threading.Thread(target=work, daemon=True, name="vtest-delete").start()
        self._poll_delete(q)

    def _poll_delete(self, q):
        try:
            res = q.get_nowait()
        except queue.Empty:
            self.root.after(120, lambda: self._poll_delete(q))
            return
        self._busy = False
        self._write_out(_tail(res.get("out"), 1200) + "\n")
        if res.get("ok"):
            self._last_action = "删除数据成功：uid=%s" % res.get("uid")
            print("[删除数据] ✅ 已删除 uid=%s（3 秒后返回一级小窗）"
                  % res.get("uid"), flush=True)
            self._del_status.config(
                text="✅ 已删除 uid=%s。3 秒后自动返回一级小窗…" % res.get("uid"),
                fg=OK_COLOR)
            self._after_job = self.root.after(3000, self.close_window2)
        else:
            print("[删除数据] ❌ 失败（退出码 %s）：%s"
                  % (res.get("code"), _tail(res.get("out"), 200).replace("\n", " / ")),
                  flush=True)
            self._del_status.config(
                text="❌ 删除失败（退出码 %s）—— 看下面的输出。" % res.get("code"),
                fg=ERR_COLOR)

    def _write_out(self, text):
        if self._del_out is None:
            return
        try:
            self._del_out.insert("end", text)
            self._del_out.see("end")
        except tk.TclError:
            pass

    # ==================================================================
    # 弹窗（非交互模式下改成打印，便于脚本化自测）
    # ==================================================================
    def _notify(self, title, msg):
        if getattr(self.opts, "auto", False):
            print("[弹窗:%s] %s" % (title, str(msg).replace("\n", " / ")), flush=True)
            return
        messagebox.showinfo(title, msg, parent=self.win3 or self.win2 or self.root)

    def _ask(self, title, msg):
        if getattr(self.opts, "auto", False):
            yes = bool(getattr(self.opts, "auto_yes", False))
            print("[确认:%s] %s → %s"
                  % (title, str(msg).replace("\n", " / "),
                     "是" if yes else "否（非交互模式默认）"), flush=True)
            return yes
        return bool(messagebox.askyesno(title, msg, parent=self.win2 or self.root))

    # ==================================================================
    # 自测入口（--open / --click）
    # ==================================================================
    def open_window(self, kind):
        if kind == "enroll":
            self.open_enroll_window()
        elif kind == "detect":
            self.open_detect_window()
        elif kind == "delete":
            self.open_delete_window()

    def schedule_click(self, action):
        """`--click`：等界面稳下来后自动点按钮（自测用）。

        支持逗号分隔的一串动作，按顺序间隔 1.5 秒执行 —— 例如
        `--click find,delete` 就是"先查姓名，再删选中的那行"。
        """

        def make(step):
            def fire():
                try:
                    if step == "start_enroll":
                        self._start_enroll()
                    elif step == "find":
                        if self._del_query is not None and self.opts.click_arg:
                            self._del_query.set(self.opts.click_arg)
                        self._delete_find()
                    elif step == "delete":
                        self._delete_selected()
                    else:
                        print("[--click] 不认识的动作：%s" % step, flush=True)
                except Exception as e:                   # noqa: BLE001
                    print("[--click %s] 出错：%r" % (step, e), flush=True)
            return fire

        delay = 2500
        for step in [s.strip() for s in str(action).split(",") if s.strip()]:
            self.root.after(delay, make(step))
            delay += 1500
