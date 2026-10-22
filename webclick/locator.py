"""定位策略：DOM 语义优先，视觉兜底。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

DOM_STRATEGIES = {"css", "xpath", "text", "role", "label", "placeholder", "testid", "alt", "title", "js"}
VISUAL_STRATEGIES = {"template", "image", "ocr"}
ALL_STRATEGIES = DOM_STRATEGIES | VISUAL_STRATEGIES | {"point"}


@dataclass
class Target:
    strategy: str
    locator: Any = None
    element: Any = None
    box: dict | None = None
    frame: str = "main"
    detail: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.box:
            return None
        return int(self.box["x"] + self.box["w"] / 2), int(self.box["y"] + self.box["h"] / 2)

    def describe(self) -> str:
        b = self.box
        pos = f"@({b['x']},{b['y']},{b['w']}x{b['h']})" if b else "@未知位置"
        return f"{self.strategy} {pos} frame={self.frame}"


def _locator_from(frame, f: dict):
    by = f["by"]
    value = f.get("value")
    exact = bool(f.get("exact", False))
    if by == "css":
        return frame.locator(value)
    if by == "xpath":
        return frame.locator("xpath=" + value)
    if by == "text":
        return frame.get_by_text(value, exact=exact)
    if by == "role":
        return frame.get_by_role(f.get("role", "button"), name=f.get("name") or value, exact=exact)
    if by == "label":
        return frame.get_by_label(value, exact=exact)
    if by == "placeholder":
        return frame.get_by_placeholder(value, exact=exact)
    if by == "testid":
        return frame.get_by_test_id(value)
    if by == "alt":
        return frame.get_by_alt_text(value, exact=exact)
    if by == "title":
        return frame.get_by_title(value, exact=exact)
    if by == "js":
        sel = frame.evaluate(value)
        if not sel or not isinstance(sel, str):
            return None
        return frame.locator(sel).first
    raise ValueError(f"未知定位方式: {by}")


def _frames_for(page, f: dict):
    want = f.get("frame")
    frames = list(page.frames)
    if not want:
        # 主帧优先
        frames.sort(key=lambda fr: 0 if fr == page.main_frame else 1)
        return frames
    return [fr for fr in frames if want in (fr.url or "") or (want == "main" and fr == page.main_frame)]


def _frame_label(page, frame) -> str:
    return "main" if frame == page.main_frame else (frame.name or frame.url)


def find_dom(page, f: dict, log=None) -> Target | None:
    for frame in _frames_for(page, f):
        try:
            loc = _locator_from(frame, f)
            if loc is None:
                continue
            if loc.count() == 0:
                continue
            first = loc.first
            try:
                first.scroll_into_view_if_needed(timeout=1500)
            except Exception:  # noqa: BLE001
                pass
            visible = first.is_visible()
            if not visible:
                continue
            box = None
            try:
                box = first.bounding_box()
            except Exception:  # noqa: BLE001
                box = None
            if box:
                box = {"x": round(box["x"]), "y": round(box["y"]),
                       "w": round(box["width"]), "h": round(box["height"])}
            return Target(strategy=_label(f), locator=first, box=box, frame=_frame_label(page, frame))
        except Exception as exc:  # noqa: BLE001
            msg = _type_err(exc)
            if log and "detached" not in msg.lower():
                log(f"    定位异常 [{_label(f)}] frame={_frame_label(page, frame)}: {msg}")
            continue
    return None


def _type_err(exc: Exception) -> str:
    txt = str(exc).strip().replace("\n", " ")
    return txt[:180]


def _label(f: dict) -> str:
    by = f["by"]
    extra = f" role={f.get('role')}" if f.get("role") else ""
    return f"{by}:{f.get('value') or f.get('name') or ''}{extra}"


def find_visual(page, f: dict, log=None) -> Target | None:
    from . import vision  # noqa: PLC0415

    by = f["by"]
    if by == "point":
        box = {"x": int(f["x"]) - 2, "y": int(f["y"]) - 2, "w": 4, "h": 4}
        return Target(strategy=f"point:({f['x']},{f['y']})", box=box, frame="main")

    if by in {"template", "image"}:
        threshold = float(f.get("threshold", 0.85))
        png = page.screenshot(scale="css", type="png")
        hit = vision.match_template(png, f["value"], threshold=threshold)
        if not hit:
            if log:
                log(f"    模板未命中（阈值 {threshold}）: {f['value']}")
            return None
        box = {"x": hit["x"], "y": hit["y"], "w": hit["w"], "h": hit["h"]}
        t = Target(strategy=f"template:{hit['score']:.3f}@{hit['scale']}", box=box, frame="main",
                   detail={"score": hit["score"], "scale": hit["scale"], "image": f["value"]})
        _upgrade_to_element(page, t)
        return t

    if by == "ocr":
        dsf = float(page.evaluate("window.devicePixelRatio") or 1)
        png_dev = page.screenshot(scale="device", type="png")
        img = vision.png_to_bgr(png_dev)
        hit = vision.find_text(img, str(f["value"]), min_score=float(f.get("min_score", 0.6)))
        if not hit:
            if log:
                log(f"    OCR 未命中: {f['value']!r}")
            return None
        b = hit["box"]
        box = {"x": round(b["x"] / dsf), "y": round(b["y"] / dsf),
               "w": round(b["w"] / dsf), "h": round(b["h"] / dsf)}
        t = Target(strategy=f"ocr:{hit['text']}", box=box, frame="main",
                   detail={"text": hit["text"], "sim": hit["sim"], "dsf": dsf})
        _upgrade_to_element(page, t)
        return t

    raise ValueError(f"未知视觉定位方式: {by}")


def _upgrade_to_element(page, t: Target) -> None:
    """视觉命中后，试着拿真实 DOM 元素，点击语义更可靠。"""
    c = t.center
    if not c:
        return
    try:
        handle = page.evaluate_handle(
            "([x, y]) => { const e = document.elementFromPoint(x, y); return e || null; }", [c[0], c[1]])
        el = handle.as_element()
        if el is not None:
            t.element = el
    except Exception:  # noqa: BLE001
        pass


def locate(page, finders: list[dict], log=None) -> tuple[Target | None, list[str]]:
    """按顺序尝试所有 finder，返回第一个命中的目标与失败原因列表。"""
    failures: list[str] = []
    for f in finders or []:
        by = f.get("by")
        if by in VISUAL_STRATEGIES or by == "point":
            t = find_visual(page, f, log=log)
        elif by in DOM_STRATEGIES:
            t = find_dom(page, f, log=log)
        else:
            failures.append(f"未知定位方式 {by!r}")
            continue
        if t is not None:
            return t, failures
        failures.append(_label(f))
    return None, failures


def wait_for_any(page, finders: list[dict], timeout_ms: int, log=None) -> Target | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        t, _ = locate(page, finders, log=None)
        if t:
            return t
        page.wait_for_timeout(250)
    if log:
        log(f"    等待 {timeout_ms}ms 仍未找到目标")
    return None
