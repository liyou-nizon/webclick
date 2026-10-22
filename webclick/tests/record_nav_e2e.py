"""录制自检：点击会跳页的按钮时，最后一击不能丢；跳页后不要截错误的模板图。"""
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.recorder import Recorder  # noqa: E402
from webclick.selftest import serve  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")
OUT = os.path.join(ROOT, "webclick", "out")


def main() -> int:
    url, httpd = serve(SITE)
    out = os.path.join(OUT, "_rec_nav.json")
    for p in (out,):
        if os.path.isfile(p):
            os.remove(p)
    logs: list[str] = []
    checks = []
    try:
        stop_event = threading.Event()

        def on_ready(page):
            # 800ms 后点一个会跳转的链接，模拟"点一下就跳走"的按钮
            page.evaluate("setTimeout(() => document.getElementById('nav-link').click(), 800)")

        threading.Timer(6.0, stop_event.set).start()
        rec = Recorder(url + "/index.html", out, log=logs.append, headless=True,
                       stop_event=stop_event, hotkey=None, on_ready=on_ready)
        rec.run()

        with open(out, encoding="utf-8") as fh:
            spec = json.load(fh)
        steps = spec.get("steps") or []
        finders = steps[0].get("find") if steps else []
        css_vals = [f.get("value") for f in finders if f.get("by") == "css"]
        tpl = [f.get("value") for f in finders if f.get("by") == "template"]

        checks = [
            ("跳页那一下被录到", len(steps) == 1, f"{len(steps)} 步"),
            ("选择器是 #nav-link", any("nav-link" in str(v) for v in css_vals), str(css_vals)),
            ("跳页后不生成错误模板", not tpl, str(tpl)),
            ("日志提示了跳页", any("页面已跳转" in l or "跳过模板" in l for l in logs),
             [l for l in logs if "跳转" in l][:1]),
        ]
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

