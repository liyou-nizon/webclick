"""命令行入口：python -m webclick <命令>。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, control, inspector, spec as spec_mod, vision
from .browser import DEFAULT_PROFILE, open_page
from .engine import Engine, run_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "webclick", "out")
WORKFLOWS = os.path.join(ROOT, "webclick", "workflows")


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------- serve ----------------
def cmd_serve(args) -> int:
    import http.server  # noqa: PLC0415
    import functools  # noqa: PLC0415
    import socketserver  # noqa: PLC0415

    directory = args.dir or os.path.join(ROOT, "webclick", "tests", "site")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler) as httpd:
        _log(f"测试台: http://127.0.0.1:{args.port}/index.html  (Ctrl+C 结束)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            _log("已停止")
    return 0


# ---------------- doctor ----------------
def cmd_doctor(_args) -> int:
    ok = True
    print(f"webclick {__version__}")
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    py_files = [os.path.join(pkg_dir, f) for f in os.listdir(pkg_dir) if f.endswith(".py")]
    newest = max(py_files, key=os.path.getmtime)
    print(f"代码最后修改: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(newest)))}"
          f"  ({os.path.basename(newest)})")
    print(f"python   {sys.version.split()[0]}  ({sys.executable})")
    try:
        import playwright  # noqa: F401, PLC0415

        from playwright._impl._driver import compute_driver_executable  # noqa: PLC0415

        print(f"playwright 已安装: {compute_driver_executable()[0]}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"playwright 缺失: {exc}")
    try:
        import cv2  # noqa: PLC0415

        print(f"opencv 已安装: {cv2.__version__}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"opencv 缺失: {exc}")
    good, why = vision.ocr_available()
    print(f"OCR: {'可用' if good else '不可用'} {'' if good else why}")
    ok = ok and good
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(p):
            print(f"浏览器: {p}")
    print(f"profile: {DEFAULT_PROFILE}")
    return 0 if ok else 1


# ---------------- inspect ----------------
def cmd_inspect(args) -> int:
    spec = {"viewport": {"width": args.width, "height": args.height}, "headless": args.headless}
    os.makedirs(OUT, exist_ok=True)
    with open_page(spec, headless=args.headless, log=_log, url=args.url) as (page, _ctx):
        page.wait_for_timeout(args.wait * 1000)
        items = inspector.enumerate_candidates(page)
        real = [i for i in items if "error" not in i]
        _log(f"候选元素 {len(real)} 个（含 iframe）")
        _log(inspector.format_table(items))
        out_json = args.out or os.path.join(OUT, "candidates.json")
        inspector.dump_json(items, out_json)
        _log(f"候选已保存: {out_json}")
        if not args.no_shot:
            shot = page.screenshot(scale="css", type="png")
            boxes = [{"x": i["box"]["x"], "y": i["box"]["y"], "w": i["box"]["w"], "h": i["box"]["h"],
                      "label": f"#{i['index']} {i['text'][:12]}"} for i in real]
            p = os.path.join(OUT, "candidates.png")
            try:
                vision.annotate(shot, boxes, p)
                _log(f"标注截图: {p}")
            except Exception as exc:  # noqa: BLE001
                _log(f"标注截图失败: {exc}")
        if args.keep_open:
            _log("浏览器保持打开，Ctrl+C 退出")
            try:
                while not page.is_closed():
                    page.wait_for_timeout(500)
            except KeyboardInterrupt:
                pass
    return 0


# ---------------- record ----------------
def cmd_record(args) -> int:
    import signal  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from .recorder import Recorder  # noqa: PLC0415

    out = args.out or os.path.join(WORKFLOWS, "recorded.json")
    stop_event = threading.Event()

    def on_sigint(_signum, _frame):
        if stop_event.is_set():
            _log("[录制] 再按一次 Ctrl+C，强制退出（已录到的步骤都已落盘）")
            raise KeyboardInterrupt
        stop_event.set()
        _log("[录制] 收到 Ctrl+C，正在收尾并保存…（焦点必须在命令行窗口；再按一次强制退出）")

    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        rec = Recorder(args.url, out, log=_log, seconds=args.seconds,
                       headless=args.headless, stop_event=stop_event,
                       hotkey=None if args.no_hotkey else args.hotkey)
        rec.run()
    finally:
        signal.signal(signal.SIGINT, old)
    print(json.dumps(rec.spec, ensure_ascii=False, indent=2)[:4000])
    return 0


# ---------------- stop ----------------
def cmd_stop(args) -> int:
    from . import control  # noqa: PLC0415

    if args.clear:
        removed = control.clear_stop()
        _log(f"STOP 文件{'已清除' if removed else '本来就不存在'}: {control.STOP_FILE}")
        return 0
    path = control.request_stop()
    _log(f"已写入停止标记: {path}")
    _log("正在跑的录制/常驻循环会在下一拍（≤1s）退出并保存；用 `python -m webclick stop --clear` 清除")
    return 0


# ---------------- run ----------------
def _append_log(path: str | None, text: str) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _run_once(args, quiet: bool, logf) -> dict:
    name = os.path.splitext(os.path.basename(args.workflow))[0]
    out = spec_mod.new_run_dir(OUT, name) if not args.out else args.out
    os.makedirs(out, exist_ok=True)

    def log(msg: str) -> None:
        if quiet:
            _append_log(logf, msg)
        else:
            _log(msg)

    if quiet:
        _append_log(logf, f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} 开始 =====")
    spec = spec_mod.load_spec(args.workflow)
    spec["_base_dir"] = os.path.dirname(os.path.abspath(args.workflow))
    if getattr(args, "only", None):
        k = int(args.only)
        if k < 1 or k > len(spec["steps"]):
            _log(f"--only {k} 超出范围（共 {len(spec['steps'])} 步）")
            return {"workflow": name, "ok": False, "error": "step out of range", "duration_ms": 0}
        spec["steps"] = [spec["steps"][k - 1]]
        log(f"[调试] 只跑第 {k} 步")
    elif getattr(args, "from_step", None):
        k = int(args.from_step)
        spec["steps"] = spec["steps"][k - 1:]
        log(f"[调试] 从第 {k} 步开始，共 {len(spec['steps'])} 步")
    from .engine import Engine  # noqa: PLC0415

    report = Engine(spec, out, log=log, headless=args.headless).run()
    line = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {name} "
            f"{'OK' if report['ok'] else 'FAIL'} {report.get('duration_ms')}ms {report.get('error') or ''}")
    _append_log(logf, line)
    if not quiet:
        print(line, flush=True)
    return report


def cmd_run(args) -> int:
    interval = int(args.every or 0)
    logf = args.log_file
    if interval <= 0:
        report = _run_once(args, quiet=bool(args.quiet), logf=logf)
        print(json.dumps({k: report[k] for k in ("workflow", "ok", "error", "duration_ms")},
                         ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    total = int(args.max_runs or 0)
    control.clear_stop()
    _log(f"循环模式：每 {interval}s 跑一次" + (f"，最多 {total} 次" if total else "，Ctrl+C 停止"))
    i = 0
    last_ok = False
    try:
        while True:
            if control.stop_requested():
                _log("检测到 STOP 文件，循环结束")
                control.clear_stop()
                break
            i += 1
            started = time.time()
            _log(f"===== 第 {i} 轮 =====")
            report = _run_once(args, quiet=False, logf=logf)
            last_ok = bool(report["ok"])
            if args.until_success and last_ok:
                _log("已成功，按 --until-success 结束循环")
                break
            if total and i >= total:
                _log(f"已跑满 {total} 轮")
                break
            wait = interval - (time.time() - started)
            if wait > 0:
                _log(f"等待 {wait:.0f}s 后开始第 {i + 1} 轮")
                end = time.time() + wait
                while time.time() < end:
                    time.sleep(min(1.0, max(0.0, end - time.time())))
    except KeyboardInterrupt:
        _log("循环已中止")
    return 0 if last_ok else 1


# ---------------- keep（常驻：不关浏览器，定时重跑） ----------------
def cmd_keep(args) -> int:
    from .engine import Session  # noqa: PLC0415

    spec = spec_mod.load_spec(args.workflow)
    spec["_base_dir"] = os.path.dirname(os.path.abspath(args.workflow))
    session = Session(spec, OUT, log=_log, headless=args.headless,
                      reload_each=args.reload_each, restart_each=args.restart_each,
                      goto_start_each=args.from_start_each,
                      reload_at_start=args.reload_at_start,
                      idle_seconds=args.idle_trigger, idle_mode=args.idle_mode,
                      idle_check_s=args.idle_check, idle_threshold=args.idle_threshold,
                      idle_ratio=args.idle_ratio, idle_verbose=args.idle_verbose,
                      idle_ignore_video=args.ignore_video,
                      max_wait_s=args.max_wait,
                      reset_each=not args.no_reset)
    reports = session.run(interval_s=float(args.every), rounds=int(args.rounds or 0))
    ok = sum(1 for r in reports if r.get("ok"))
    print(json.dumps({"rounds": len(reports), "ok": ok,
                      "failed": len(reports) - ok}, ensure_ascii=False, indent=2))
    return 0 if reports and ok == len(reports) else 1


# ---------------- schedule（Windows 计划任务） ----------------
def _task_name(name: str) -> str:
    return f"webclick-{name}"


def cmd_schedule(args) -> int:
    import subprocess  # noqa: PLC0415

    action = args.action
    wf = os.path.abspath(args.workflow) if args.workflow else None
    if wf and not os.path.isfile(wf):
        _log(f"工作流不存在: {wf}")
        return 2
    name = args.name or (os.path.splitext(os.path.basename(wf))[0] if wf else "job")
    tn = _task_name(name)

    if action == "install":
        every = int(args.every_minutes or 15)
        sched_dir = os.path.join(ROOT, "webclick", "schedules")
        os.makedirs(sched_dir, exist_ok=True)
        logf = os.path.join(OUT, f"loop-{name}.log")
        launcher = os.path.join(sched_dir, f"{name}.cmd")
        py = sys.executable
        with open(launcher, "w", encoding="ascii") as fh:
            fh.write("@echo off\r\n")
            fh.write(f'cd /d "{ROOT}"\r\n')
            fh.write(f'"{py}" -m webclick run "{wf}" --headless --quiet --log-file "{logf}"\r\n')
        tr = f'"{launcher}"'
        r = subprocess.run(["schtasks", "/Create", "/TN", tn, "/TR", tr, "/SC", "MINUTE",
                            "/MO", str(every), "/F"], capture_output=True, text=True)
        _log(r.stdout.strip() or r.stderr.strip())
        if r.returncode == 0:
            ps = (
                f"$t = Get-ScheduledTask -TaskName '{tn}'; "
                "$t.Settings.DisallowStartIfOnBatteries = $false; "
                "$t.Settings.StopIfGoingOnBatteries = $false; "
                "$t.Settings.WakeToRun = $true; "
                "Set-ScheduledTask -TaskName "
                f"'{tn}' -Settings $t.Settings | Out-Null; 'settings patched'"
            )
            p2 = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                capture_output=True, text=True)
            _log((p2.stdout or p2.stderr).strip())
            _log(f"已注册计划任务 {tn}：每 {every} 分钟跑一次")
            _log(f"启动脚本: {launcher}")
            _log(f"日志: {logf}")
            _log(f"立即试跑一次: schtasks /Run /TN \"{tn}\"")
        return 0 if r.returncode == 0 else 1

    if action == "list":
        r = subprocess.run(["schtasks", "/Query", "/TN", tn, "/FO", "LIST", "/V"],
                           capture_output=True, text=True)
        out = r.stdout.strip() or r.stderr.strip()
        _log(out)
        return 0 if r.returncode == 0 else 1

    if action == "run":
        r = subprocess.run(["schtasks", "/Run", "/TN", tn], capture_output=True, text=True)
        _log((r.stdout or r.stderr).strip())
        return 0 if r.returncode == 0 else 1

    if action == "delete":
        r = subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], capture_output=True, text=True)
        _log((r.stdout or r.stderr).strip())
        return 0 if r.returncode == 0 else 1

    _log(f"未知操作: {action}")
    return 2


# ---------------- selftest ----------------
def cmd_selftest(args) -> int:
    from .selftest import run_selftest  # noqa: PLC0415

    verdict = run_selftest(headless=args.headless)
    if verdict["run_dir"]:
        print(f"输出目录: {verdict['run_dir']}")
    return 0 if verdict["ok"] else 1


# ---------------- gui ----------------
def cmd_gui(args) -> int:
    from .gui import main as gui_main  # noqa: PLC0415

    return gui_main(smoke=getattr(args, "smoke", False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webclick", description="浏览器按钮识别 + 点击自动化")
    p.add_argument("--version", action="version", version=f"webclick {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="检查依赖与环境")
    d.set_defaults(func=cmd_doctor)

    sv = sub.add_parser("serve", help="启动本地按钮测试台")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--dir", default=None)
    sv.set_defaults(func=cmd_serve)

    i = sub.add_parser("inspect", help="列出页面上的可交互元素并生成选择器")
    i.add_argument("url")
    i.add_argument("--out", default=None)
    i.add_argument("--wait", type=float, default=1.5, help="打开后等待秒数")
    i.add_argument("--width", type=int, default=1280)
    i.add_argument("--height", type=int, default=900)
    i.add_argument("--no-shot", action="store_true")
    i.add_argument("--keep-open", action="store_true")
    i.add_argument("--headless", action="store_true")
    i.set_defaults(func=cmd_inspect)

    r = sub.add_parser("record", help="边操作边录制工作流")
    r.add_argument("url")
    r.add_argument("--out", default=None)
    r.add_argument("--seconds", type=int, default=None)
    r.add_argument("--hotkey", default="F9", help="停止并保存的全局热键，默认 F9，例如 ctrl+alt+q")
    r.add_argument("--no-hotkey", action="store_true", help="不注册全局热键")
    r.add_argument("--headless", action="store_true")
    r.set_defaults(func=cmd_record)

    sp = sub.add_parser("stop", help="给正在跑的录制/常驻循环发停止信号（STOP 文件）")
    sp.add_argument("--clear", action="store_true", help="清除停止标记")
    sp.set_defaults(func=cmd_stop)

    run = sub.add_parser("run", help="执行工作流 JSON")
    run.add_argument("workflow")
    run.add_argument("--out", default=None)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--every", type=int, default=0, help="循环间隔秒数，例如 900 = 15 分钟")
    run.add_argument("--max-runs", type=int, default=0, help="最多跑几轮，0 = 不限")
    run.add_argument("--until-success", action="store_true", help="成功后停止")
    run.add_argument("--log-file", default=None, help="把每轮结果追加写入这个文件")
    run.add_argument("--quiet", action="store_true", help="不输出步骤日志（只写 --log-file）")
    run.add_argument("--only", type=int, default=None, help="只跑第 N 步（调试某一步用）")
    run.add_argument("--from-step", type=int, default=None, help="从第 N 步开始跑")
    run.set_defaults(func=cmd_run)

    sc = sub.add_parser("schedule", help="用 Windows 计划任务定时跑（不占进程）")
    sc.add_argument("action", choices=["install", "list", "run", "delete"])
    sc.add_argument("--workflow", default=None)
    sc.add_argument("--name", default=None)
    sc.add_argument("--every-minutes", type=int, default=15)
    sc.set_defaults(func=cmd_schedule)

    kp = sub.add_parser("keep", help="常驻：浏览器不关、页面不动，定时重跑步骤")
    kp.add_argument("workflow")
    kp.add_argument("--every", type=int, default=900, help="间隔秒数，默认 900 = 15 分钟")
    kp.add_argument("--rounds", type=int, default=0, help="最多跑几轮，0 = 不限")
    kp.add_argument("--reload-each", action="store_true",
                    help="每轮结束 reload 当前页（不会跳回起始网址）")
    kp.add_argument("--restart-each", action="store_true",
                    help="每轮结束回到 start_url（旧的 --reload-each 行为）")
    kp.add_argument("--from-start-each", action="store_true",
                    help="每轮【开始】时回到 start_url（不打断上一轮结束后的页面状态）")
    kp.add_argument("--reload-at-start", action="store_true",
                    help="每轮【开始】时刷新当前所在网址（不跳回 start_url）")
    kp.add_argument("--idle-trigger", type=int, default=0,
                    help="改成静止触发：页面（或桌面）连续 N 秒没变化就执行下一轮")
    kp.add_argument("--idle-mode", choices=["page", "desktop"], default="page",
                    help="观察对象：page=只盯网页内容，desktop=盯整个桌面（默认 page）")
    kp.add_argument("--idle-check", type=float, default=2.0, help="采样间隔秒数，默认 2")
    kp.add_argument("--idle-threshold", type=float, default=3.0,
                    help="平均像素差阈值，越大越不敏感，默认 3.0")
    kp.add_argument("--idle-ratio", type=float, default=0.01,
                    help="判定有变化所需的变化像素占比，默认 0.01（1%%）")
    kp.add_argument("--idle-verbose", action="store_true",
                    help="每次采样都打印差异数值，用来排查为什么不触发")
    kp.add_argument("--ignore-video", action="store_true",
                    help="比对时把 <video> 区域涂黑（视频在播也不影响判定）")
    kp.add_argument("--max-wait", type=int, default=0,
                    help="静止触发时最长等多久（秒），0=一直等")
    kp.add_argument("--no-reset", action="store_true", help="每轮开始不按 Esc / 不回到页面顶部")
    kp.add_argument("--headless", action="store_true")
    kp.set_defaults(func=cmd_keep)

    st = sub.add_parser("selftest", help="本地测试台自检")
    st.add_argument("--headless", action="store_true")
    st.set_defaults(func=cmd_selftest)

    g = sub.add_parser("gui", help="打开图形界面")
    g.add_argument("--smoke", action="store_true", help="只构建界面并自检，不进入主循环")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台常是 GBK，日志里的「»」等字符会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
