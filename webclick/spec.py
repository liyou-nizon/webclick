"""工作流 JSON 的读写与校验。"""
from __future__ import annotations

import json
import os
import time
from typing import Any

STEP_KEYS = {"do", "name", "find", "value", "timeout_ms", "expect", "optional", "keys", "js",
             "selector", "texts"}
SUPPORTED_ACTIONS = {"click", "fill", "goto", "wait", "assert", "screenshot", "press",
                     "scroll", "scroll_to", "next_chapter", "play_video", "dismiss_dialog"}

DEFAULT_SPEC: dict[str, Any] = {
    "name": "workflow",
    "start_url": "",
    "viewport": {"width": 1280, "height": 800},
    "headless": False,
    "defaults": {"timeout_ms": 8000, "retries": 1, "settle_ms": 350,
                 "scroll_search": True, "scroll_search_screens": 4, "scroll_settle_ms": 250},
    "steps": [],
}


class SpecError(ValueError):
    pass


def load_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    spec = json.loads(json.dumps(DEFAULT_SPEC))
    for k, v in raw.items():
        if k == "defaults":
            spec["defaults"].update(v or {})
        else:
            spec[k] = v
    validate(spec)
    return spec


def validate(spec: dict[str, Any]) -> None:
    if not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise SpecError("steps 不能为空")
    for i, step in enumerate(spec["steps"], 1):
        if not isinstance(step, dict):
            raise SpecError(f"第 {i} 步不是对象")
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise SpecError(f"第 {i} 步含未知字段: {sorted(unknown)}")
        action = step.get("do")
        if action not in SUPPORTED_ACTIONS:
            raise SpecError(f"第 {i} 步 do={action!r} 不支持，可选 {sorted(SUPPORTED_ACTIONS)}")
        if action in {"click", "fill", "press"} and not step.get("find"):
            raise SpecError(f"第 {i} 步（{action}）缺少 find")
        if action == "assert" and not (step.get("expect") or step.get("value") or step.get("find")):
            raise SpecError(f"第 {i} 步（assert）需要 expect 或 value")
        if action == "fill" and "value" not in step:
            raise SpecError(f"第 {i} 步（fill）缺少 value")
        for f in step.get("find") or []:
            if not isinstance(f, dict) or "by" not in f:
                raise SpecError(f"第 {i} 步 finder 必须是 {{'by': ...}} 对象")
            by = f["by"]
            if by in {"template", "image", "ocr"}:
                if not f.get("value"):
                    raise SpecError(f"第 {i} 步 finder by={by} 缺少 value（图片路径或文字）")
            elif by == "point":
                if "x" not in f or "y" not in f:
                    raise SpecError(f"第 {i} 步 finder by=point 需要 x/y")
            elif not f.get("value"):
                raise SpecError(f"第 {i} 步 finder by={by} 缺少 value")


def resolve_assets(spec: dict[str, Any], base_dir: str | None = None) -> None:
    """把模板图片的相对路径按工作流文件所在目录解析成绝对路径。"""
    base = base_dir or spec.get("_base_dir") or os.getcwd()
    for step in spec.get("steps") or []:
        for f in step.get("find") or []:
            if f.get("by") in {"template", "image"} and f.get("value"):
                if not os.path.isabs(f["value"]):
                    f["value"] = os.path.normpath(os.path.join(base, f["value"]))


def new_spec(start_url: str, name: str = "workflow") -> dict[str, Any]:
    spec = json.loads(json.dumps(DEFAULT_SPEC))
    spec["name"] = name
    spec["start_url"] = start_url
    return spec


def save_spec(spec: dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def new_run_dir(base: str, name: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(base, f"{name}-{stamp}")
    os.makedirs(d, exist_ok=True)
    return d
