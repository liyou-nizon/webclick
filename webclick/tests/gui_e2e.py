"""GUI 端到端自检：构造界面 → 点「运行工作流」→ 检查日志/预览图；再测循环模式。"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick import spec as spec_mod  # noqa: E402
from webclick.gui import App  # noqa: E402
from webclick.selftest import serve  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")


def pump(app, timeout: float, done_when=None) -> None:
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        time.sleep(0.05)
        if done_when and done_when():
            return
        if app.status.cget("text") in {"完成", "异常结束"}:
            return


def make_workflow(url: str) -> str:
    """可重复跑的流程：两次点击 + 幂等断言（循环时状态会累计）。"""
    wf = {
        "name": "gui-e2e",
        "start_url": url + "/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 1, "settle_ms": 200},
        "steps": [
            {"do": "click", "name": "下单", "find": [{"by": "css", "value": "#submit"}]},
            {"do": "click", "name": "下一步", "find": [{"by": "text", "value": "下一步"}]},
            {"do": "assert", "name": "点击次数为偶数",
             "expect": {"js": "window.__clicks.length >= 2 && window.__clicks.length % 2 === 0"}},
        ],
    }
    path = os.path.join(ROOT, "webclick", "out", "_gui_e2e.json")
    spec_mod.save_spec(wf, path)
    return path


def run_once(wf_path: str) -> tuple[bool, str]:
    app = App()
    app.update()
    app.wf_var.set(wf_path)
    app.on_run()
    pump(app, 120)
    for _ in range(30):
        app.update()
        time.sleep(0.05)
    log = app.log.get("1.0", "end")
    ok = ("结果: 成功" in log and app.status.cget("text") == "完成"
          and app.last_artifact is not None and os.path.isfile(app.last_artifact or ""))
    detail = f"状态={app.status.cget('text')} 预览图={app.last_artifact is not None}"
    app.destroy()
    return ok, detail


def run_loop(wf_path: str) -> tuple[bool, str]:
    app = App()
    app.update()
    app.wf_var.set(wf_path)
    app.loop_var.set(True)
    app.interval_var.set("0.1")   # 6 秒间隔
    app.rounds_var.set("2")
    app.on_run()
    pump(app, 180)
    for _ in range(40):
        app.update()
        time.sleep(0.05)
    log = app.log.get("1.0", "end")
    ok = ("常驻模式启动" in log and "第 2 轮" in log and "已跑满 2 轮" in log
          and "常驻结束：共 2 轮，成功 2" in log and app.status.cget("text") == "完成")
    detail = f"状态={app.status.cget('text')} 常驻={'常驻结束：共 2 轮，成功 2' in log}"
    app.destroy()
    return ok, detail


def run_record_stop(url: str) -> tuple[bool, str]:
    """界面录制 → 点「中止」→ 必须停下来并保存。"""
    out = os.path.join(ROOT, "webclick", "out", "_gui_record.json")
    if os.path.isfile(out):
        os.remove(out)
    app = App()
    app.update()
    app.url_var.set(url + "/index.html")
    app.wf_var.set(out)
    app.headless_var.set(True)
    app.on_record()
    end = time.time() + 15
    while time.time() < end:
        app.update()
        time.sleep(0.05)
        if "浏览器已启动" in app.log.get("1.0", "end"):
            break
    for _ in range(20):
        app.update()
        time.sleep(0.05)
    app.on_stop()
    pump(app, 30)
    for _ in range(30):
        app.update()
        time.sleep(0.05)
    log = app.log.get("1.0", "end")
    ok = (os.path.isfile(out) and "已保存" in log and app.status.cget("text") == "完成")
    detail = f"状态={app.status.cget('text')} 文件={os.path.isfile(out)}"
    app.destroy()
    return ok, detail


def main() -> int:
    url, httpd = serve(SITE)
    rc = 0
    try:
        wf_path = make_workflow(url)
        for name, fn in (("单次运行", run_once), ("常驻循环 2 轮", run_loop),
                         ("录制后中止", run_record_stop)):
            if name == "录制后中止":
                ok, detail = fn(url)
            else:
                ok, detail = fn(wf_path)
            print(f"[{'PASS' if ok else 'FAIL'}] GUI {name}: {detail}")
            rc = rc or (0 if ok else 1)
    finally:
        httpd.shutdown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
