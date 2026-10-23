"""确认弹窗自检：弹窗出现 → dismiss_dialog 点掉"确定" → 页面状态正确。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.engine import Engine  # noqa: E402
from webclick.selftest import serve, OUT  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")


def workflow(url):
    return {
        "name": "dialog-demo",
        "start_url": url + "/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 0, "settle_ms": 200},
        "steps": [
            {"do": "click", "name": "打开确认弹窗",
             "find": [{"by": "css", "value": "#dialog-btn"}]},
            {"do": "assert", "name": "弹窗确实开着",
             "expect": {"js": "window.__dialogOpen()"}},
            {"do": "dismiss_dialog", "name": "点掉确定"},
            {"do": "assert", "name": "弹窗已关且点的是确定",
             "expect": {"js": "!window.__dialogOpen() && window.__clicks.includes('confirm-ok')"}},
            {"do": "dismiss_dialog", "name": "没弹窗时也不报错"},
            {"do": "assert", "name": "没弹窗时不产生副作用",
             "expect": {"js": "!window.__dialogOpen()"}},
        ],
    }


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        run_dir = os.path.join(OUT, "dialog-e2e")
        report = Engine(workflow(url), run_dir, log=lambda m: print(m, flush=True),
                        headless=True).run()
        by_name = {s.get("name"): s for s in report["steps"]}
        dismiss = by_name.get("点掉确定", {})
        empty = by_name.get("没弹窗时也不报错", {})
        checks = [
            ("整体成功", bool(report["ok"]), report.get("error") or "ok"),
            ("点掉的是「确定」", dismiss.get("clicked") == "确定",
             f"clicked={dismiss.get('clicked')!r}"),
            ("没有弹窗时返回 found=False", empty.get("found") is False,
             f"found={empty.get('found')}"),
            ("断言全过", all(s.get("ok") for s in report["steps"] if s["do"] == "assert"), ""),
        ]
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
