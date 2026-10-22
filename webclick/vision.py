"""视觉兜底：模板匹配 + OCR。

输入都是 viewport 截图字节，输出 CSS 像素坐标（调用方负责缩放换算）。
模型加载是懒加载：不用视觉策略时不会付出启动代价。
"""
from __future__ import annotations

import difflib
import os
import threading

import numpy as np

_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()
_OCR_ERROR: str | None = None


def _cv2():
    import cv2  # noqa: PLC0415

    return cv2


def png_to_bgr(data: bytes):
    cv2 = _cv2()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("截图解码失败")
    return img


def match_template(haystack_png: bytes, template_path: str, threshold: float = 0.85,
                   scales: tuple[float, ...] = (0.7, 0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.35, 1.5)):
    """在截图里找模板，返回 {x, y, w, h, score, scale} 或 None。"""
    cv2 = _cv2()
    if not os.path.isfile(template_path):
        raise FileNotFoundError(f"模板图片不存在: {template_path}")
    tpl = cv2.imdecode(np.frombuffer(open(template_path, "rb").read(), np.uint8), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise ValueError(f"模板图片无法解码: {template_path}")
    hay = cv2.cvtColor(png_to_bgr(haystack_png), cv2.COLOR_BGR2GRAY)
    best = None
    for s in scales:
        w = max(6, int(round(tpl.shape[1] * s)))
        h = max(6, int(round(tpl.shape[0] * s)))
        if w >= hay.shape[1] or h >= hay.shape[0]:
            continue
        resized = cv2.resize(tpl, (w, h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(hay, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if best is None or max_val > best["score"]:
            best = {"x": int(max_loc[0]), "y": int(max_loc[1]), "w": w, "h": h,
                    "score": float(max_val), "scale": s}
    if best is None:
        return None
    if best["score"] < threshold:
        return None
    return best


def _norm(text: str) -> str:
    return "".join(str(text).split()).replace("：", ":").replace("（", "(").replace("）", ")").lower()


def _similar(query: str, target: str) -> float:
    """包含关系给高分，其余用模糊比。"""
    q, t = _norm(query), _norm(target)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if q in t:
        return 0.95 - 0.02 * min(len(t) - len(q), 5)
    if t in q:
        return 0.80
    return difflib.SequenceMatcher(None, q, t).ratio()


class OcrUnavailable(RuntimeError):
    pass


def _get_ocr():
    global _OCR_ENGINE, _OCR_ERROR
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415

            _OCR_ENGINE = RapidOCR()
            _OCR_ERROR = None
        except Exception as exc:  # noqa: BLE001
            _OCR_ERROR = f"OCR 引擎不可用: {exc}"
            raise OcrUnavailable(_OCR_ERROR) from exc
    return _OCR_ENGINE


def ocr_available() -> tuple[bool, str]:
    try:
        _get_ocr()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def ocr_texts(img_bgr) -> list[dict]:
    """识别整张图，返回 [{text, score, box:{x,y,w,h}}...]（图片像素坐标）。"""
    engine = _get_ocr()
    result, _elapse = engine(img_bgr)
    out = []
    for item in result or []:
        box, text, score = item[0], item[1], float(item[2])
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({
            "text": str(text),
            "score": score,
            "box": {"x": int(min(xs)), "y": int(min(ys)),
                    "w": int(max(xs) - min(xs)), "h": int(max(ys) - min(ys))},
        })
    return out


def find_text(img_bgr, query: str, min_score: float = 0.6):
    """在 OCR 结果里找和 query 最像的一行，返回 {text, score, box, sim} 或 None。"""
    best = None
    for item in ocr_texts(img_bgr):
        sim = _similar(query, item["text"])
        if best is None or sim > best["sim"]:
            best = {**item, "sim": sim}
    if best is None or best["sim"] < min_score:
        return None
    return best


def annotate(png_bytes: bytes, boxes: list[dict], out_path: str) -> str:
    """在截图上画红框 + 标签，存盘，用于人工核对。"""
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    import io as _io

    img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = None
    for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if os.path.isfile(cand):
            try:
                font = ImageFont.truetype(cand, 14)
                break
            except Exception:  # noqa: BLE001
                continue
    for b in boxes:
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        draw.rectangle([x, y, x + w, y + h], outline=(255, 40, 40), width=3)
        label = b.get("label") or ""
        if label:
            if font is not None:
                l, t, r, bo = draw.textbbox((0, 0), label, font=font)
                tw, th = r - l + 8, bo - t + 6
            else:
                tw, th = max(60, int(len(label) * 7.5)), 18
            ty = y - th if y - th >= 0 else y + h
            draw.rectangle([x, ty, x + tw, ty + th], fill=(255, 40, 40))
            draw.text((x + 4, ty + 2), label, fill=(255, 255, 255), font=font)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path)
    return out_path
