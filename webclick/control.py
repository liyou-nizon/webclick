"""统一的"停止"开关：界面「中止」、Ctrl+C、以及 webclick\\STOP 文件都能触发。

为什么需要 STOP 文件：录制/常驻模式下如果命令行窗口没焦点（焦点在浏览器里），
Ctrl+C 根本到不了 python；这时候在别处创建这个文件，循环下一拍就会退出并保存。
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_FILE = os.path.join(ROOT, "webclick", "STOP")


def stop_requested() -> bool:
    return os.path.exists(STOP_FILE)


def request_stop() -> str:
    os.makedirs(os.path.dirname(STOP_FILE), exist_ok=True)
    with open(STOP_FILE, "w", encoding="ascii") as fh:
        fh.write("stop\n")
    return STOP_FILE


def clear_stop() -> bool:
    try:
        os.remove(STOP_FILE)
        return True
    except FileNotFoundError:
        return False
