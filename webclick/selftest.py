"""自检：本地测试台上真跑一遍全部策略，用页面状态和截图双重验证。"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import socketserver
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "webclick", "tests", "site")
WORK = os.path.join(ROOT, "webclick", "tests")
OUT = os.path.join(ROOT, "webclick", "out")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass


def serve(directory: str) -> tuple[str, socketserver.TCPServer]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = functools.partial(_QuietHandler, directory=directory)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


def make_templates(url: str, log=print, headless: bool = False) -> dict:
    """从真实渲染结果里裁出模板图，确保和当前 DPI/字体一致。"""
    from .browser import open_page  # noqa: PLC0415

    assets = os.path.join(WORK, "assets")
    os.makedirs(assets, exist_ok=True)
    out = {}
    with open_page({"viewport": {"width": 1280, "height": 900}, "headless": headless},
                   log=log, headless=headless) as (page, _ctx):
        page.goto(url + "/index.html", wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        for sel, name in (("#cv", "canvas_start.png"), ("#img-btn", "img_arrow.png")):
            path = os.path.join(assets, name)
            page.locator(sel).screenshot(path=path)
            out[name] = path
            log(f"[模板] {sel} -> {path}")
    return out


def build_workflow(url: str) -> dict:
    rel_canvas = os.path.join(WORK, "assets", "canvas_start.png")
    rel_img = os.path.join(WORK, "assets", "img_arrow.png")
    return {
        "name": "selftest",
        "start_url": f"{url}/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 1, "settle_ms": 250},
        "steps": [
            {"do": "click", "name": "DOM: id 按钮", "find": [{"by": "css", "value": "#submit"}]},
            {"do": "fill", "name": "输入订单号", "value": "A-2026-10",
             "find": [{"by": "css", "value": "#order-id"}]},
            {"do": "assert", "name": "校验输入生效",
             "expect": {"js": "document.getElementById('order-id').value === 'A-2026-10'"}},
            {"do": "click", "name": "text: 链接式按钮", "find": [{"by": "text", "value": "下一步"}]},
            {"do": "click", "name": "template: 纯图片按钮",
             "find": [{"by": "template", "value": rel_img, "threshold": 0.85}]},
            {"do": "click", "name": "ocr: canvas 按钮",
             "find": [{"by": "ocr", "value": "开始采集", "min_score": 0.6}]},
            {"do": "click", "name": "iframe: 保存并关闭",
             "find": [{"by": "text", "value": "保存并关闭"}]},
            {"do": "assert", "name": "iframe 内按钮确实生效",
             "expect": {"js": "document.querySelector('iframe').contentDocument"
                              ".getElementById('inner-state').textContent.trim() === '已保存'"}},
            {"do": "wait", "name": "等延迟按钮出现", "timeout_ms": 6000,
             "find": [{"by": "css", "value": "#late"}]},
            {"do": "click", "name": "延迟按钮", "find": [{"by": "css", "value": "#late"}]},
            {"do": "click", "name": "首屏外 canvas（需滚屏 + OCR）",
             "find": [{"by": "ocr", "value": "继续学习", "min_score": 0.6}]},
            {"do": "assert", "name": "主页面点击序列正确",
             "expect": {"js": "window.__clicks.join('|') === 'submit|next-step|img-btn|canvas|late|far'"}},
            {"do": "assert", "name": "状态已完成",
             "expect": {"js": "document.getElementById('status').textContent === '已完成'"}},
        ],
    }


def check_recording(url: str, log=print) -> dict:
    """录制链路验证：真实点击 → 事件回传 → 生成 JSON → 回放同一份 JSON 必须成功。"""
    from . import spec as spec_mod  # noqa: PLC0415
    from .browser import open_page  # noqa: PLC0415
    from .engine import Engine  # noqa: PLC0415
    from .recorder import RECORD_JS, Recorder  # noqa: PLC0415

    out = os.path.join(WORK, "workflows", "_record_check.json")
    result = {"name": "录制链路可还原", "ok": False, "detail": ""}
    try:
        rec = Recorder(url + "/index.html", out, log=log, hotkey=None)
        with open_page({"viewport": {"width": 1280, "height": 900}, "headless": True}, log=log) as (page, _ctx):
            rec.page = page
            page.add_init_script(RECORD_JS)
            page.goto(url + "/index.html", wait_until="domcontentloaded")
            page.evaluate(RECORD_JS)
            for sel in ("#submit", "#next-step"):
                box = page.locator(sel).bounding_box()
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(400)
                rec.drain()
                rec.process_pending()
        with open(out, "r", encoding="utf-8") as fh:
            recorded = json.load(fh)
        steps = recorded.get("steps") or []
        sels = [f.get("value") for s in steps for f in (s.get("find") or []) if f.get("by") == "css"]
        if len(steps) != 2:
            result["detail"] = f"录制到 {len(steps)} 步（期望 2）"
            return result
        if not any("#submit" in str(x) for x in sels) or not any("#next-step" in str(x) for x in sels):
            result["detail"] = f"选择器不匹配: {sels}"
            return result
        # 回放：在原工作流末尾加断言，再用 Engine 真跑一次
        recorded["steps"].append({
            "do": "assert", "name": "回放后点击序列正确",
            "expect": {"js": "window.__clicks.join('|') === 'submit|next-step'"}})
        recorded["_base_dir"] = os.path.dirname(out)
        run_dir = spec_mod.new_run_dir(OUT, "record-replay")
        report = Engine(recorded, run_dir, log=log, headless=True).run()
        result["ok"] = bool(report["ok"])
        result["detail"] = f"{len(steps)} 步，回放 {'成功' if report['ok'] else '失败'} -> {run_dir}"
        if not report["ok"]:
            result["detail"] += f" :: {report.get('error')}"
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result


def run_selftest(headless: bool = False, log=print) -> dict:
    os.makedirs(OUT, exist_ok=True)
    url, httpd = serve(SITE)
    log(f"[自检] 静态服务器 {url} -> {SITE}")
    verdict = {"ok": False, "url": url, "checks": [], "report": None, "run_dir": None}
    try:
        make_templates(url, log=log, headless=headless)
        workflow = build_workflow(url)
        from . import spec as spec_mod  # noqa: PLC0415
        from .engine import Engine  # noqa: PLC0415

        spec_mod.validate(workflow)
        spec_path = os.path.join(WORK, "workflows", "selftest.json")
        spec_mod.save_spec(workflow, spec_path)
        run_dir = spec_mod.new_run_dir(OUT, "selftest")
        engine = Engine(workflow, run_dir, log=log, headless=headless)
        report = engine.run()
        verdict["report"] = report
        verdict["run_dir"] = run_dir

        strategies = {s.get("strategy", "") for s in report["steps"] if s.get("ok")}
        raw = [
            ("全部步骤成功", bool(report["ok"]), report.get("error")),
            ("DOM(css) 命中", any(s.startswith("css") for s in strategies), sorted(strategies)),
            ("text 命中", any(s.startswith("text") for s in strategies), sorted(strategies)),
            ("template 视觉命中", any(s.startswith("template") for s in strategies), sorted(strategies)),
            ("OCR 视觉命中", any(s.startswith("ocr") for s in strategies), sorted(strategies)),
            ("每一步都有截图", all(s.get("shot") for s in report["steps"]), None),
            ("报告已落盘", os.path.isfile(os.path.join(run_dir, "report.json")), run_dir),
        ]
        verdict["checks"] = [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in raw]
        rec_check = check_recording(url, log=log)
        verdict["checks"].append(rec_check)
        verdict["ok"] = all(c["ok"] for c in verdict["checks"])
    finally:
        httpd.shutdown()
    # 汇总
    lines = ["", "==== 自检结果 ===="]
    for c in verdict["checks"]:
        lines.append(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}" + (f" :: {c['detail']}" if c["detail"] else ""))
    lines.append(f"  结论: {'通过' if verdict['ok'] else '失败'}")
    log("\n".join(lines))
    return verdict


if __name__ == "__main__":
    run_selftest()
