"""常驻模式自检：浏览器只开一次、页面不重载、每轮在同一页面上重跑步骤。"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.engine import Session  # noqa: E402
from webclick.selftest import serve  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")
OUT = os.path.join(ROOT, "webclick", "out")


def build_workflow(url: str) -> dict:
    return {
        "name": "keeptest",
        "start_url": url + "/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 1, "settle_ms": 200},
        "steps": [
            {"do": "assert", "name": "轮次开始时在页面顶部（上一轮滚过）",
             "expect": {"js": "window.scrollY === 0"}},
            {"do": "click", "name": "下单", "find": [{"by": "css", "value": "#submit"}]},
            {"do": "scroll", "name": "往下滚，把页面留在非顶部", "value": 600},
            {"do": "assert", "name": "确实滚下来了",
             "expect": {"js": "window.scrollY > 0 && window.__clicks.length >= 1"}},
        ],
    }


def main() -> int:
    url, httpd = serve(SITE)
    logs: list[str] = []
    seen: list[str] = []
    checks: list[tuple[str, bool, str]] = []
    try:
        wf = build_workflow(url)
        session = Session(wf, OUT, log=logs.append, headless=True, reload_each=False,
                          on_round=lambda page, rep: seen.append(page.evaluate("window.__clicks.join('|')")))
        t0 = time.time()
        reports = session.run(interval_s=2, rounds=2)
        elapsed = time.time() - t0

        launches = sum(1 for line in logs if "浏览器已启动" in line)
        checks = [
            ("跑完 2 轮", len(reports) == 2, f"{len(reports)} 轮"),
            ("2 轮都成功", all(r.get("ok") for r in reports), str([r.get("ok") for r in reports])),
            ("浏览器只启动 1 次", launches == 1, f"启动 {launches} 次"),
            ("页面没有重新加载（点击记录累计）",
             seen == ["submit", "submit|submit"], str(seen)),
            ("第 2 轮开始自动复位（滚动回顶部）",
             any("已复位" in l for l in logs) and all(r.get("ok") for r in reports),
             [l for l in logs if "已复位" in l][:1]),
            ("间隔 2s 生效", elapsed >= 2.0, f"{elapsed:.1f}s"),
            ("每轮独立报告", len({r.get("out_dir") for r in reports}) == 2, ""),
        ]
    finally:
        httpd.shutdown()

    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
