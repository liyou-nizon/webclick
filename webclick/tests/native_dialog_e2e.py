"""原生 confirm/alert 自检：
- 不开 auto_accept_dialogs：Playwright 默认 dismiss（页面记录 native-dismissed）
- 开 auto_accept_dialogs：自动点"确定"（页面记录 native-accepted）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.engine import Engine  # noqa: E402
from webclick.selftest import serve, OUT  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")


def workflow(url, auto_accept: bool):
    spec = {
        "name": f"native-{'on' if auto_accept else 'off'}",
        "start_url": url + "/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 0, "settle_ms": 200},
        "steps": [
            {"do": "click", "name": "触发原生 confirm",
             "find": [{"by": "css", "value": "#native-btn"}]},
            {"do": "wait", "name": "等页面处理完", "value": 800},
            {"do": "assert", "name": "确认结果符合预期",
             "expect": {"js": "window.__clicks.includes('%s')"
                              % ("native-accepted" if auto_accept else "native-dismissed")}},
        ],
    }
    if auto_accept:
        spec["auto_accept_dialogs"] = True
    return spec


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        for flag in (False, True):
            run_dir = os.path.join(OUT, f"native-{'on' if flag else 'off'}")
            report = Engine(workflow(url, flag), run_dir,
                            log=lambda m: print(m, flush=True), headless=True).run()
            label = "开了 auto_accept_dialogs" if flag else "没开（默认 dismiss）"
            checks.append((f"{label}：结果正确", bool(report["ok"]), report.get("error") or "ok"))
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
