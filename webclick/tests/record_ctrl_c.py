"""验证录制能被三种方式停下并保存：SIGINT（Ctrl+C）、STOP 文件、stop_event（界面中止）。"""
import json
import os
import signal
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick import control  # noqa: E402
from webclick.recorder import Recorder  # noqa: E402
from webclick.selftest import serve  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")
OUT = os.path.join(ROOT, "webclick", "out")


def fake_click(rec, text="#submit"):
    rec.events.put({"kind": "click", "tag": "button", "text": "确认下单",
                    "selector": text, "box": {"x": 41, "y": 112, "w": 98, "h": 42},
                    "frame": "main", "role": "", "aria": "", "id": text.lstrip("#"), "type": ""})


def saved_steps(path):
    with open(path, encoding="utf-8") as fh:
        return len(json.load(fh).get("steps") or [])


def scenario_sigint(url):
    out = os.path.join(OUT, "_rec_ctrlc.json")
    rec = Recorder(url + "/index.html", out, log=lambda m: None, headless=True)
    fake_click(rec)
    threading.Timer(5.0, lambda: signal.raise_signal(signal.SIGINT)).start()
    t0 = time.time()
    rec.run()
    return time.time() - t0, saved_steps(out), out


def scenario_stop_file(url):
    out = os.path.join(OUT, "_rec_stopfile.json")
    rec = Recorder(url + "/index.html", out, log=lambda m: None, headless=True)
    fake_click(rec)

    def touch():
        time.sleep(5.0)
        control.request_stop()

    threading.Thread(target=touch, daemon=True).start()
    t0 = time.time()
    rec.run()
    return time.time() - t0, saved_steps(out), out


def scenario_stop_event(url):
    out = os.path.join(OUT, "_rec_stopevent.json")
    ev = threading.Event()
    rec = Recorder(url + "/index.html", out, log=lambda m: None, headless=True, stop_event=ev)
    fake_click(rec)
    threading.Timer(5.0, ev.set).start()
    t0 = time.time()
    rec.run()
    return time.time() - t0, saved_steps(out), out


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        for name, fn in (("SIGINT/Ctrl+C", scenario_sigint),
                         ("STOP 文件", scenario_stop_file),
                         ("界面中止(stop_event)", scenario_stop_event)):
            elapsed, steps, out = fn(url)
            ok = elapsed < 9 and steps >= 1
            checks.append((name, ok, f"{elapsed:.1f}s 退出，保存 {steps} 步 -> {os.path.basename(out)}"))
    finally:
        httpd.shutdown()
        control.clear_stop()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
