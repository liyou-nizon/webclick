"""热键自检：真的向系统发一次按键，看录制能否被热键停下并保存。"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.hotkey import Hotkey, send_key  # noqa: E402
from webclick.recorder import Recorder  # noqa: E402
from webclick.selftest import serve  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")
OUT = os.path.join(ROOT, "webclick", "out")
VK = {"F9": 0x78, "F10": 0x79, "CTRL": 0x11, "ALT": 0x12}


def press(vk, mods=()):
    for m in mods:
        send_key(VK[m] if m in VK else m)
    time.sleep(0.05)
    send_key(vk)
    time.sleep(0.08)
    send_key(vk, up=True)
    for m in reversed(mods):
        send_key(VK[m] if m in VK else m, up=True)


def saved_steps(path):
    with open(path, encoding="utf-8") as fh:
        return len(json.load(fh).get("steps") or [])


def fake_click(rec):
    rec.events.put({"kind": "click", "tag": "button", "text": "确认下单", "selector": "#submit",
                    "box": {"x": 41, "y": 112, "w": 98, "h": 42}, "frame": "main",
                    "role": "", "aria": "", "id": "submit", "type": ""})


def scenario(url, hotkey, sequence):
    """sequence: [(delay_s, vk, mods), ...] 按时间投放按键；返回退出的总耗时与步数。"""
    out = os.path.join(OUT, "_rec_hotkey.json")
    logs = []
    rec = Recorder(url + "/index.html", out, log=logs.append, headless=True, hotkey=hotkey)
    fake_click(rec)
    t0 = time.time()

    def inject():
        for delay, vk, mods in sequence:
            time.sleep(delay)
            press(vk, mods)

    threading.Thread(target=inject, daemon=True).start()
    rec.run()
    return time.time() - t0, saved_steps(out), logs


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        # 1) 默认 F9 能停
        print("[1/3] F9 ...", flush=True)
        el, steps, logs = scenario(url, "F9", [(4.0, VK["F9"], ())])
        print(f"      退出用时 {el:.1f}s，{steps} 步", flush=True)
        checks.append(("F9 停止录制", el < 7 and steps >= 1,
                       f"{el:.1f}s 退出，保存 {steps} 步"))

        # 2) 只按 F10 不应触发 ctrl+alt+F10
        print("[2/3] ctrl+alt+F10 ...", flush=True)
        el, steps, logs = scenario(url, "ctrl+alt+F10", [(3.0, VK["F10"], ()), (4.0, VK["F10"], ("CTRL", "ALT"))])
        print(f"      退出用时 {el:.1f}s，{steps} 步", flush=True)
        checks.append(("别的键不误触 / 组合键生效", el < 10 and steps >= 1,
                       f"{el:.1f}s 退出（前 3s 单按 F10 没停），保存 {steps} 步"))

        # 3) 解析器
        print("[3/3] 解析 ...", flush=True)
        parsed = [Hotkey(s).ok for s in ("F9", "ctrl+shift+q", "alt+F2", "bogus", "ctrl+")]
        checks.append(("热键解析", parsed == [True, True, True, False, False], str(parsed)))
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
