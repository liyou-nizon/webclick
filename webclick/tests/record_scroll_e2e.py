"""录制/回放自检：滚动位置、换网址都能录；reload 只刷当前页，不跳回起始页。"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.engine import Engine, Session  # noqa: E402
from webclick.recorder import Recorder  # noqa: E402
from webclick.selftest import serve, OUT  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")


def record_scroll_and_nav(url):
    """录一段：滚动到 500 → 手动换到 inner.html，检查生成 scroll_to / goto。"""
    out = os.path.join(OUT, "_rec_scroll.json")
    if os.path.isfile(out):
        os.remove(out)
    logs = []
    stop_event = threading.Event()

    def on_ready(page):
        page.evaluate("setTimeout(() => window.scrollTo(0, 500), 600)")
        page.evaluate("setTimeout(() => { location.href = 'inner.html'; }, 2000)")

    threading.Timer(6.0, stop_event.set).start()
    rec = Recorder(url + "/index.html", out, log=logs.append, headless=True,
                   stop_event=stop_event, hotkey=None, on_ready=on_ready)
    rec.run()
    with open(out, encoding="utf-8") as fh:
        return json.load(fh), logs


def replay_scroll(url, spec):
    """回放 scroll_to 步骤，确认真的滚到了那个位置。"""
    spec = dict(spec)
    spec["steps"] = [s for s in spec["steps"] if s["do"] == "scroll_to"][:1]
    spec["steps"].append({"do": "assert", "name": "滚到了",
                          "expect": {"js": "window.scrollY >= 480"}})
    spec["start_url"] = url + "/index.html"
    spec["_base_dir"] = os.path.dirname(os.path.abspath(__file__))
    run_dir = os.path.join(OUT, "scroll-replay")
    return Engine(spec, run_dir, log=lambda m: None, headless=True).run()


def reload_keeps_url(url):
    """reload_each=True：第 2 轮不应回到 start_url。"""
    wf = {
        "name": "reloadtest",
        "start_url": url + "/index.html",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 0, "settle_ms": 150},
        "steps": [
            {"do": "goto", "name": "切到 inner", "value": url + "/inner.html"},
            {"do": "assert", "name": "当前是 inner",
             "expect": {"js": "location.pathname.endsWith('inner.html')"}},
        ],
    }
    urls = []
    session = Session(wf, OUT, log=lambda m: None, headless=True, reload_each=True,
                      on_round=lambda page, rep: urls.append(page.evaluate("location.pathname")))
    reports = session.run(interval_s=1, rounds=2)
    return reports, urls


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        spec, logs = record_scroll_and_nav(url)
        steps = spec.get("steps") or []
        kinds = [s["do"] for s in steps]
        scrolls = [s for s in steps if s["do"] == "scroll_to"]
        gotos = [s for s in steps if s["do"] == "goto"]
        checks.append(("录到滚动位置", bool(scrolls) and abs(int(scrolls[0]["value"]) - 500) <= 40,
                       f"{[(s['do'], s.get('value')) for s in steps]}"))
        checks.append(("录到换网址", bool(gotos) and "inner.html" in str(gotos[0]["value"]),
                       str([s.get("value") for s in gotos])))

        rep = replay_scroll(url, spec)
        checks.append(("scroll_to 能精确回放", bool(rep["ok"]), rep.get("error") or "ok"))

        reports, urls = reload_keeps_url(url)
        checks.append(("reload 后仍在当前页，不回起始页",
                       bool(urls) and all(u.endswith("inner.html") for u in urls),
                       str(urls)))
        checks.append(("2 轮都成功", all(r.get("ok") for r in reports),
                       str([r.get("ok") for r in reports])))
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
