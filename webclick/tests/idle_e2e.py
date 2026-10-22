"""静止触发自检：
1) 静止页面：连续 N 秒没变化 → 立刻进下一轮
2) 一直在动的页面：不会因为"某一帧没变"就误触发，只能等 max_wait 兜底
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from webclick.engine import Session  # noqa: E402
from webclick.selftest import serve, OUT  # noqa: E402

SITE = os.path.join(ROOT, "webclick", "tests", "site")


def workflow(url, page_name="index.html"):
    return {
        "name": f"idle-{page_name.replace('.html', '')}",
        "start_url": f"{url}/{page_name}",
        "viewport": {"width": 1280, "height": 900},
        "defaults": {"timeout_ms": 8000, "retries": 0, "settle_ms": 100},
        "steps": [{"do": "assert", "name": "空转一轮", "expect": {"js": "true"}}],
    }


def run_case(url, page_name, idle_seconds, max_wait, rounds=2, threshold=3.0, ratio=0.01):
    marks = []
    logs = []
    session = Session(workflow(url, page_name), OUT, log=logs.append, headless=True,
                      idle_seconds=idle_seconds, idle_check_s=1.0,
                      idle_threshold=threshold, idle_ratio=ratio,
                      max_wait_s=max_wait, on_round=lambda page, rep: marks.append((time.time(), rep)))
    t0 = time.time()
    reports = session.run(interval_s=900, rounds=rounds)
    gaps = [b[0] - a[0] for a, b in zip(marks, marks[1:])]
    return reports, gaps, time.time() - t0, logs


def main() -> int:
    url, httpd = serve(SITE)
    checks = []
    try:
        reps, gaps, total, logs = run_case(url, "static.html", idle_seconds=3, max_wait=0)
        gap = gaps[0] if gaps else -1
        checks.append(("静止页面：约 3s 后就进下一轮",
                       len(reps) == 2 and 2.5 <= gap <= 6.0,
                       f"两轮间隔 {gap:.1f}s"))
        checks.append(("日志说明是静止触发",
                       any("连续" in l and "没变化" in l for l in logs),
                       [l for l in logs if "没变化" in l][:1]))

        # 大范围动画（数字 + 进度条）：严格灵敏度下必须判定为"在动"
        reps2, gaps2, total2, logs2 = run_case(url, "anim.html", idle_seconds=3, max_wait=8,
                                               threshold=1.5, ratio=0.002)
        gap2 = gaps2[0] if gaps2 else -1
        checks.append(("一直在动的页面：严格灵敏度下不误触发",
                       len(reps2) == 2 and gap2 >= 7.0,
                       f"两轮间隔 {gap2:.1f}s（若误触发会 ~3s）"))
        checks.append(("兜底日志出现",
                       any("不等了" in l for l in logs2),
                       [l for l in logs2 if "不等了" in l][:1]))
    finally:
        httpd.shutdown()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
