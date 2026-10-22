"""Tkinter 图形界面：填网址 → 录制 → 运行 → 看日志和截图。"""
from __future__ import annotations

import os
import queue
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import inspector, spec as spec_mod
from .browser import open_page
from .engine import Engine
from .recorder import Recorder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "webclick", "out")
WORKFLOWS = os.path.join(ROOT, "webclick", "workflows")
PREVIEW_MAX = (620, 420)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("webclick · 浏览器按钮识别与自动化")
        self.geometry("1280x820")
        self.minsize(1120, 700)
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_artifact: str | None = None
        self._img_ref = None
        self._build()
        # 按实际需要的尺寸自适应，防止高分屏（125%/150% 缩放）把控件挤出窗口
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(max(1280, self.winfo_reqwidth() + 24), sw - 40)
        h = min(max(820, self.winfo_reqheight() + 24), sh - 80)
        self.geometry(f"{w}x{h}")
        self.minsize(min(1120, w), min(700, h))
        self.after(120, self._pump)

    # ---------------- UI ----------------
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="网址").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value="https://")
        ttk.Entry(top, textvariable=self.url_var).grid(row=0, column=1, columnspan=2, sticky="we", padx=4)

        ttk.Label(top, text="工作流").grid(row=1, column=0, sticky="w")
        self.wf_var = tk.StringVar(value=os.path.join(WORKFLOWS, "recorded.json"))
        ttk.Entry(top, textvariable=self.wf_var).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(top, text="浏览…", command=self.on_browse, width=8).grid(row=1, column=2, sticky="w")

        # ---- 操作按钮行 ----
        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.btn_record = ttk.Button(bar, text="录制并打开浏览器", command=self.on_record)
        self.btn_record.pack(side="left")
        self.btn_inspect = ttk.Button(bar, text="识别页面按钮", command=self.on_inspect)
        self.btn_inspect.pack(side="left", padx=6)
        self.btn_run = ttk.Button(bar, text="运行工作流", command=self.on_run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="中止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="输出目录", command=self.on_open_out).pack(side="left")

        self.status = ttk.Label(bar, text="就绪", foreground="#2b6cb0")
        self.status.pack(side="right", padx=6)
        self.hotkey_var = tk.StringVar(value="F9")
        ttk.Entry(bar, textvariable=self.hotkey_var, width=6).pack(side="right", padx=(2, 10))
        ttk.Label(bar, text="录制热键").pack(side="right")
        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="无头模式", variable=self.headless_var).pack(side="right", padx=10)

        # ---- 常驻循环 ----
        loopbox = ttk.LabelFrame(self, text="常驻循环（浏览器不关）")
        loopbox.pack(fill="x", **pad)
        line1 = ttk.Frame(loopbox)
        line1.pack(fill="x", padx=6, pady=(4, 2))
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(line1, text="启用", variable=self.loop_var).pack(side="left")
        ttk.Label(line1, text="每").pack(side="left", padx=(10, 2))
        self.interval_var = tk.StringVar(value="15")
        ttk.Spinbox(line1, from_=0.1, to=1440, increment=1, width=6,
                    textvariable=self.interval_var).pack(side="left")
        ttk.Label(line1, text="分钟跑一次，共").pack(side="left")
        self.rounds_var = tk.StringVar(value="0")
        ttk.Spinbox(line1, from_=0, to=9999, increment=1, width=5,
                    textvariable=self.rounds_var).pack(side="left")
        ttk.Label(line1, text="轮（0 = 不限）").pack(side="left")

        line_idle = ttk.Frame(loopbox)
        line_idle.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Label(line_idle, text="触发方式：").pack(side="left")
        self.trigger_mode = tk.StringVar(value="interval")
        ttk.Radiobutton(line_idle, text="固定间隔", value="interval",
                        variable=self.trigger_mode).pack(side="left")
        ttk.Radiobutton(line_idle, text="连续", value="idle",
                        variable=self.trigger_mode).pack(side="left", padx=(10, 2))
        self.idle_var = tk.StringVar(value="60")
        ttk.Spinbox(line_idle, from_=5, to=3600, increment=5, width=6,
                    textvariable=self.idle_var).pack(side="left")
        ttk.Label(line_idle, text="秒没变化就跑下一轮").pack(side="left")
        self.idle_desktop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(line_idle, text="盯整个桌面（否则只看网页）",
                        variable=self.idle_desktop_var).pack(side="left", padx=8)

        line_idle2 = ttk.Frame(loopbox)
        line_idle2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(line_idle2, text="灵敏度：").pack(side="left")
        self.sens_var = tk.StringVar(value="适中")
        ttk.Combobox(line_idle2, textvariable=self.sens_var, width=6, state="readonly",
                     values=["严格", "适中", "宽松"]).pack(side="left")
        self.ignore_video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(line_idle2, text="忽略视频画面（视频在播也算静止）",
                        variable=self.ignore_video_var).pack(side="left", padx=10)
        self.idle_verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(line_idle2, text="打印采样数值（排查用）",
                        variable=self.idle_verbose_var).pack(side="left")

        line2 = ttk.Frame(loopbox)
        line2.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(line2, text="每轮怎么重置：").grid(row=0, column=0, sticky="nw")
        self.reset_mode = tk.StringVar(value="none")
        modes = [
            ("none", "不重置（接着上一轮）"),
            ("reload_start", "轮开始：刷新当前网址 ★"),
            ("goto_start", "轮开始：回起始网址"),
            ("reload_end", "轮结束：刷新当前页"),
            ("restart_end", "轮结束：回起始网址"),
        ]
        for i, (val, text) in enumerate(modes):
            ttk.Radiobutton(line2, text=text, value=val,
                            variable=self.reset_mode).grid(row=i % 3, column=1 + i // 3, sticky="w", padx=(0, 18))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, **pad)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(left, text="日志").pack(anchor="w")
        self.log = tk.Text(left, wrap="word", height=18, font=("Consolas", 9))
        sb = ttk.Scrollbar(left, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, text="最近一次点击的截图（红框 = 识别目标）").pack(anchor="w")
        self.canvas = tk.Canvas(right, bg="#f2f4f8", width=520, height=380, highlightthickness=1,
                                highlightbackground="#cbd5e0")
        self.canvas.pack(fill="both", expand=True)

    # ---------------- helpers ----------------
    def logline(self, msg: str) -> None:
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _pump(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.logline(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "artifact":
                    self.show_image(payload)
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "done":
                    self.status.configure(text=payload)
                    self._set_busy(False)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._after_id = self.after(120, self._pump)

    def destroy(self):
        try:
            if getattr(self, "_after_id", None):
                self.after_cancel(self._after_id)
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for b in (self.btn_record, self.btn_inspect, self.btn_run):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        if busy:
            self.stop_event.clear()

    def _start(self, target, *args) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("忙", "已有任务在跑")
            return
        self._set_busy(True)
        self.queue.put(("status", "运行中…"))

        def wrap():
            try:
                target(*args)
            except Exception:  # noqa: BLE001
                tb = traceback.format_exc()
                self.queue.put(("log", tb))
                self.queue.put(("done", "异常结束"))
            else:
                self.queue.put(("done", "完成"))

        self.worker = threading.Thread(target=wrap, daemon=True)
        self.worker.start()

    def logf(self, msg: str) -> None:
        self.queue.put(("log", msg))

    def put_log(self, msg: str) -> None:
        self.queue.put(("log", msg))

    def show_image(self, path: str) -> None:
        try:
            self.last_artifact = path
            img = Image.open(path).convert("RGB")
            img.thumbnail(PREVIEW_MAX)
            self._img_ref = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            w = self.canvas.winfo_width() or PREVIEW_MAX[0]
            h = self.canvas.winfo_height() or PREVIEW_MAX[1]
            self.canvas.create_image(w // 2, h // 2, image=self._img_ref)
        except Exception as exc:  # noqa: BLE001
            self.logf(f"[预览] 失败: {exc}")

    # ---------------- actions ----------------
    def on_browse(self):
        path = filedialog.askopenfilename(initialdir=WORKFLOWS, filetypes=[("JSON", "*.json")])
        if path:
            self.wf_var.set(path)

    def on_open_out(self):
        os.makedirs(OUT, exist_ok=True)
        os.startfile(OUT)  # noqa: S606

    def on_record(self):
        url = self.url_var.get().strip()
        if not url.startswith("http"):
            messagebox.showwarning("网址无效", "请先填写完整网址（http/https）")
            return
        out = self.wf_var.get().strip() or os.path.join(WORKFLOWS, "recorded.json")
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        self.wf_var.set(out)
        hotkey = self.hotkey_var.get().strip() or "F9"
        self.logline(f"[录制] 目标 {url}；浏览器里操作，按热键 {hotkey}（或点「中止」）结束")
        headless = bool(self.headless_var.get())  # Tk 变量只能在主线程读

        def job():
            rec = Recorder(url, out, log=self.logf, headless=headless,
                           stop_event=self.stop_event, hotkey=hotkey)
            rec.run()
            self.queue.put(("log", f"[录制] 工作流已保存到 {out}"))

        self._start(job)

    def on_inspect(self):
        url = self.url_var.get().strip()
        if not url.startswith("http"):
            messagebox.showwarning("网址无效", "请先填写完整网址（http/https）")
            return
        headless = bool(self.headless_var.get())

        def job():
            os.makedirs(OUT, exist_ok=True)
            with open_page({"viewport": {"width": 1280, "height": 900}},
                           headless=headless, log=self.logf, url=url) as (page, _ctx):
                page.wait_for_timeout(1200)
                items = inspector.enumerate_candidates(page)
                self.queue.put(("log", f"候选元素 {len([i for i in items if 'error' not in i])} 个"))
                self.queue.put(("log", inspector.format_table(items)))
                inspector.dump_json(items, os.path.join(OUT, "candidates.json"))
                shot = page.screenshot(scale="css", type="png")
                from . import vision  # noqa: PLC0415

                boxes = [{"x": i["box"]["x"], "y": i["box"]["y"], "w": i["box"]["w"], "h": i["box"]["h"],
                          "label": f"#{i['index']} {i['text'][:10]}"} for i in items if "error" not in i]
                p = vision.annotate(shot, boxes, os.path.join(OUT, "candidates.png"))
                self.queue.put(("artifact", p))

        self._start(job)

    def on_run(self):
        path = self.wf_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showwarning("找不到工作流", f"文件不存在:\n{path}")
            return
        self.logline("=" * 60)
        # Tk 变量只能在主线程读，先全部取出来
        headless = bool(self.headless_var.get())
        loop = bool(self.loop_var.get())
        try:
            interval_min = float(self.interval_var.get() or 15)
        except ValueError:
            interval_min = 15.0
        try:
            rounds = int(float(self.rounds_var.get() or 0))
        except ValueError:
            rounds = 0
        mode = self.reset_mode.get()
        reload_each = mode == "reload_end"
        restart_each = mode == "restart_end"
        goto_start_each = mode == "goto_start"
        reload_at_start = mode == "reload_start"
        idle_mode = "idle" if self.trigger_mode.get() == "idle" else "interval"
        try:
            idle_seconds = float(self.idle_var.get() or 60)
        except ValueError:
            idle_seconds = 60.0
        idle_on_desktop = bool(self.idle_desktop_var.get())
        sens_map = {"严格": (1.5, 0.002), "适中": (3.0, 0.01), "宽松": (6.0, 0.03)}
        idle_threshold, idle_ratio = sens_map.get(self.sens_var.get(), (3.0, 0.01))
        idle_verbose = bool(self.idle_verbose_var.get())
        idle_ignore_video = bool(self.ignore_video_var.get())
        interval_s = max(5.0, interval_min * 60.0)

        def job():
            spec = spec_mod.load_spec(path)
            spec["_base_dir"] = os.path.dirname(os.path.abspath(path))
            if loop:
                # 常驻：浏览器只开一次，页面留在原地，每轮只重跑步骤
                from .engine import Session  # noqa: PLC0415

                session = Session(spec, OUT, log=self.logf, stop_event=self.stop_event,
                                  on_artifact=lambda p: self.queue.put(("artifact", p)),
                                  headless=headless, reload_each=reload_each,
                                  restart_each=restart_each, goto_start_each=goto_start_each,
                                  reload_at_start=reload_at_start,
                                  idle_seconds=idle_seconds if idle_mode == "idle" else 0,
                                  idle_mode="desktop" if idle_on_desktop else "page",
                                  idle_check_s=2.0, idle_threshold=idle_threshold,
                                  idle_ratio=idle_ratio, idle_verbose=idle_verbose,
                                  idle_ignore_video=idle_ignore_video, max_wait_s=0)
                reports = session.run(interval_s=interval_s, rounds=rounds)
                ok = sum(1 for r in reports if r.get("ok"))
                self.put_log(f"常驻结束：共 {len(reports)} 轮，成功 {ok}，失败 {len(reports) - ok}")
                return
            run_dir = spec_mod.new_run_dir(OUT, spec.get("name") or "run")
            engine = Engine(spec, run_dir, log=self.logf, stop_event=self.stop_event,
                            on_artifact=lambda p: self.queue.put(("artifact", p)),
                            headless=headless)
            last = engine.run()
            self.put_log(f"结果: {'成功' if last['ok'] else '失败'} "
                         f"{last.get('error') or ''}  输出: {run_dir}")

        self._start(job)

    def on_stop(self):
        self.stop_event.set()
        self.status.configure(text="正在中止…")


def main(smoke: bool = False) -> int:
    os.makedirs(WORKFLOWS, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    app = App()
    if smoke:
        app.update_idletasks()
        app.update()
        widgets = len(app.winfo_children())
        print(f"GUI 自检: 窗口 {app.winfo_width()}x{app.winfo_height()}，顶层控件 {widgets} 个")
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
