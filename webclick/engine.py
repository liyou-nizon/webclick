"""执行工作流：定位 → 动作 → 取证（截图 + 报告）。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from typing import Any, Callable

from . import control, locator, spec as spec_mod, vision
from .browser import open_page

LogFn = Callable[[str], None]


class WorkflowError(RuntimeError):
    def __init__(self, message: str, step_index: int, report: dict):
        super().__init__(message)
        self.step_index = step_index
        self.report = report


def _now() -> str:
    return time.strftime("%H:%M:%S")


# 点击点是否被别的元素盖住；返回 "" 表示没被遮挡
OCCLUSION_JS = r"""
(el) => {
  if (!el || !el.getBoundingClientRect) return 'not-element';
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return 'zero-size';
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return 'offscreen';
  const top = document.elementFromPoint(x, y);
  if (!top) return 'nothing-at-point';
  if (top === el || el.contains(top) || top.contains(el)) return '';
  let tag = top.tagName.toLowerCase();
  if (top.id) tag += '#' + top.id;
  const cls = top.className && String(top.className).trim();
  if (cls) tag += '.' + cls.split(/\s+/).slice(0, 2).join('.');
  return tag;
}
"""

PAGE_SIG_JS = r"""
() => location.href + '|' + (document.body ? document.body.innerText.length : 0)
      + '|' + document.querySelectorAll('*').length
"""

# 找"确认弹窗"并标记其中的确认按钮：可见、浮层（fixed 或高 z-index）、含确认类文案、有可点按钮
DIALOG_JS = r"""
(extra) => {
  const BASE = ['确定', '继续学习', '继续观看', '继续', '知道了', '好的', '我已知晓',
                '是', '同意', '关闭'];
  const CONFIRM = BASE.concat(extra || []);
  const PREFER = ['确定', '继续学习', '继续观看', '继续', '知道了', '好的', '同意', '是', '确认'];
  const vis = (el) => {
    const st = getComputedStyle(el), r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden'
           && Number(st.opacity || 1) > 0.1 && r.width >= 160 && r.height >= 40;
  };
  // 按钮本身很小，不能用弹窗容器的尺寸门槛
  const visBtn = (el) => {
    const st = getComputedStyle(el), r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden'
           && Number(st.opacity || 1) > 0.1 && r.width >= 8 && r.height >= 8;
  };
  document.querySelectorAll('[data-wc-dialog-btn]').forEach(e => e.removeAttribute('data-wc-dialog-btn'));
  const found = [];
  for (const el of document.querySelectorAll('div,section,dialog,form')) {
    if (!vis(el)) continue;
    const st = getComputedStyle(el);
    const zi = parseInt(st.zIndex || '0', 10) || 0;
    if (!(st.position === 'fixed' || zi >= 100)) continue;
    const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 400) continue;
    if (!CONFIRM.some(w => txt.includes(w))) continue;
    const buttons = [...el.querySelectorAll('button,a,input,[role=button],span,div')].filter(b => {
      const bt = ((b.innerText || b.value || '') + '').trim();
      if (!bt || bt.length > 10) return false;
      if (!CONFIRM.includes(bt)) return false;   // 严格相等：避免把"取消 确定"这种容器当按钮
      if (b.children.length > 0) return false;   // 只认叶子节点
      return visBtn(b);
    });
    if (!buttons.length) continue;
    // 优先点"确定/继续"，否则点最后一个（弹窗按钮通常是 取消|确定）
    let btn = buttons.find(b => PREFER.some(w => ((b.innerText || b.value || '') + '').trim().includes(w)));
    if (!btn) btn = buttons[buttons.length - 1];
    btn.setAttribute('data-wc-dialog-btn', '1');
    found.push({text: txt.slice(0, 120),
                button: ((btn.innerText || btn.value || '') + '').trim().slice(0, 12),
                cls: (el.className || '').toString().slice(0, 50), zIndex: zi});
  }
  return found;
}
"""

# 通用"目录型列表"的下一个可点项：默认按常见的 posCatalog_select 结构（id=cur<chapterId>，
# 当前项带 posCatalog_active），选择器可用 step 里的 "selector" 覆盖。
NEXT_CHAPTER_JS = r"""
(sel) => {
  const m = location.href.match(/chapterId=(\d+)/);
  const cid = m ? m[1] : '';
  const items = [...document.querySelectorAll(sel)];
  let idx = items.findIndex(el => el.id === 'cur' + cid);
  if (idx < 0) {
    const active = document.querySelector(sel + '_active') || document.querySelector('.posCatalog_active');
    idx = items.indexOf(active);
  }
  if (idx < 0) return {ok: false, reason: 'list-not-found', cid, total: items.length};
  // 列表里可能混着分组标题（id 是纯数字，点它只会展开/收起），优先只认 id 以 cur 开头的真条目
  let nidx = -1;
  for (let j = idx + 1; j < items.length; j++) {
    if ((items[j].id || '').startsWith('cur')) { nidx = j; break; }
  }
  if (nidx < 0) { nidx = idx + 1 < items.length ? idx + 1 : -1; }
  if (nidx < 0) return {ok: false, reason: 'last-item', cid, total: items.length, idx};
  const next = items[nidx];
  return {ok: true, cid, idx, total: items.length, nextIdx: nidx,
          nextId: next.id || '',
          nextText: (next.innerText || '').replace(/\s+/g, ' ').slice(0, 60)};
}
"""


def dismiss_dialog(page, words: list[str] | None = None, log=print) -> dict | None:
    """找可见的确认弹窗并点掉它；words 可追加自定义按钮文案（如"确认""我知道了"）。"""
    for fr in page.frames:
        try:
            found = fr.evaluate(DIALOG_JS, words or []) or []
        except Exception:  # noqa: BLE001
            continue
        if not found:
            continue
        try:
            btn = fr.locator("[data-wc-dialog-btn]").first
            btn.click(timeout=3000)
            log(f"  关掉弹窗「{(found[0].get('text') or '')[:40]}」→ 点了「{found[0].get('button')}」")
            page.wait_for_timeout(300)
            return found[0]
        except Exception as exc:  # noqa: BLE001
            log(f"  弹窗找到了但点不动: {exc}")
            continue
    return None


def install_dialog_auto_accept(page, log=print) -> None:
    """让浏览器原生 confirm/alert（不是 DOM 弹窗）自动点"确定"。

    只在工作流里写 "auto_accept_dialogs": true 时安装，默认不装 ——
    否则任何确认框都会被吞掉。
    """

    def _handler(dialog):
        try:
            kind = dialog.type
            msg = (dialog.message or "").replace("\n", " ")[:80]
            dialog.accept()
            log(f"  原生对话框（{kind}）已自动确定：「{msg}」")
        except Exception as exc:  # noqa: BLE001
            log(f"  原生对话框处理失败: {exc}")

    try:
        page.on("dialog", _handler)
    except Exception as exc:  # noqa: BLE001
        log(f"  安装原生对话框处理失败: {exc}")


class Engine:
    def __init__(self, spec: dict[str, Any], out_dir: str, log: LogFn | None = None,
                 stop_event: threading.Event | None = None,
                 on_artifact: Callable[[str], None] | None = None,
                 headless: bool | None = None):
        spec_mod.validate(spec)
        spec_mod.resolve_assets(spec)
        self.spec = spec
        self.out_dir = out_dir
        self.stop_event = stop_event or threading.Event()
        self.on_artifact = on_artifact
        self.headless = headless
        self.steps_dir = os.path.join(out_dir, "steps")
        os.makedirs(self.steps_dir, exist_ok=True)
        self.report: dict[str, Any] = {
            "workflow": spec.get("name"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "start_url": spec.get("start_url"),
            "out_dir": out_dir,
            "steps": [],
            "ok": False,
            "error": None,
        }
        self._log_fn = log or (lambda m: print(m, flush=True))
        self.log_lines: list[str] = []

    def log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        self.log_lines.append(line)
        self._log_fn(line)

    # ---------- 取证 ----------
    def _shot(self, page, name: str, target=None, note: str = "", box: dict | None = None) -> str | None:
        try:
            png = page.screenshot(scale="css", type="png")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  截图失败: {exc}")
            return None
        path = os.path.join(self.steps_dir, name)
        boxes = []
        if box:
            boxes.append({**box, "label": note or "target"})
        elif target is not None and getattr(target, "box", None):
            boxes.append({**target.box, "label": note or target.strategy[:40]})
        try:
            if boxes:
                vision.annotate(png, boxes, path)
            else:
                with open(path, "wb") as fh:
                    fh.write(png)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  截图落盘失败: {exc}")
            return None
        if self.on_artifact and boxes:
            self.on_artifact(path)
        return path

    # ---------- 单步 ----------
    def _scroll_scan(self, page, finders, deadline: float):
        """目标不在当前视口时，向下/向上逐屏滚动搜索。"""
        defaults = self.spec.get("defaults") or {}
        if not defaults.get("scroll_search", True):
            return None, []
        max_screens = int(defaults.get("scroll_search_screens", 4))
        vp = page.viewport_size or {}
        vh = vp.get("height") or 800
        vw = vp.get("width") or 1280
        dv = int(vh * 0.8)
        delay = int(defaults.get("scroll_settle_ms", 250))
        start_y = page.evaluate("window.scrollY") or 0
        fails: list[str] = []
        self.log(f"  当前视口没找到，开始滚动搜索（最多 {max_screens} 屏）")
        # 把鼠标放到视口中央：wheel 会滚动光标下的容器，兼容"内层 div 滚动"的页面
        page.mouse.move(vw // 2, vh // 2)
        for direction in (1, -1):
            if direction == -1:
                page.evaluate(f"window.scrollTo(0, {start_y})")
                page.wait_for_timeout(delay)
            stuck = 0
            for i in range(max_screens):
                if time.time() >= deadline or self.stop_event.is_set():
                    page.evaluate(f"window.scrollTo(0, {start_y})")
                    return None, fails
                before = page.evaluate("window.scrollY")
                page.mouse.wheel(0, direction * dv)
                page.wait_for_timeout(delay)
                after = page.evaluate("window.scrollY")
                # 已经到顶/到底：再滚也不动，别空转
                if after == before:
                    stuck += 1
                    if stuck >= 2:
                        self.log(f"  已到页面{'底' if direction > 0 else '顶'}，停止滚动搜索")
                        break
                else:
                    stuck = 0
                t, fails = locator.locate(page, finders, log=None)
                if t is not None:
                    self.log(f"  滚动 {i + 1} 屏后找到目标（{t.strategy}）")
                    return t, fails
            page.evaluate(f"window.scrollTo(0, {start_y})")
        return None, fails

    def _resolve(self, page, step: dict, timeout_ms: int, allow_scroll: bool = True) -> locator.Target | None:
        finders = step.get("find") or []
        deadline = time.time() + timeout_ms / 1000
        last_fail: list[str] = []
        scanned = not allow_scroll
        while True:
            if self.stop_event.is_set():
                raise KeyboardInterrupt("用户中止")
            t, last_fail = locator.locate(page, finders, log=self.log)
            if t is not None:
                return t
            if not scanned:
                scanned = True
                t, last_fail = self._scroll_scan(page, finders, deadline)
                if t is not None:
                    return t
            if time.time() >= deadline:
                self.log(f"  定位失败，试过: {last_fail}")
                self._diagnose(page, finders)
                return None
            page.wait_for_timeout(250)

    def _diagnose(self, page, finders) -> None:
        """定位失败时把现场信息打出来，省得用户猜。"""
        try:
            self.log(f"  当前页面: {page.url[:160]}")
        except Exception:  # noqa: BLE001
            return
        for f in finders:
            by = f.get("by")
            if by == "css":
                try:
                    n = page.locator(f["value"]).count()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"    css {f['value'][:70]} -> 查询异常 {str(exc)[:60]}")
                    continue
                self.log(f"    css {f['value'][:70]} -> 命中 {n} 个"
                         + ("（选择器失效）" if n == 0 else ""))
            elif by == "text":
                try:
                    body = page.inner_text("body")[:4000]
                except Exception:  # noqa: BLE001
                    body = ""
                self.log(f"    文本 {f.get('value')!r} -> 页面上"
                         + ("有" if str(f.get("value")) in body else "没有"))
            elif by in {"template", "image"}:
                self.log(f"    模板 {os.path.basename(str(f.get('value')))} -> 未达到阈值 {f.get('threshold', 0.85)}")
            elif by == "ocr":
                self.log(f"    OCR {f.get('value')!r} -> 当前视口没识别到")

    def _occlusion(self, page, t) -> str:
        """点击点被别的元素盖住时返回那个元素，否则返回空串。"""
        try:
            if t.element is not None:
                return t.element.evaluate(OCCLUSION_JS) or ""
            if t.locator is not None:
                return t.locator.evaluate(OCCLUSION_JS) or ""
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _sig(self, page) -> str:
        try:
            return page.evaluate(PAGE_SIG_JS) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _dismiss_dialog(self, page, texts: list[str] | None = None) -> dict | None:
        return dismiss_dialog(page, words=texts, log=self.log)

    def _play_video(self, page, timeout_ms: int) -> dict:
        """在任意 frame 里找 <video>，点播放并验证 currentTime 真的在走。"""
        last = {}
        for fr in page.frames:
            try:
                if not fr.locator("video").count():
                    continue
            except Exception:  # noqa: BLE001
                continue
            try:
                st = fr.evaluate("() => { const v = document.querySelector('video');"
                                 " return v ? {paused: v.paused, t: v.currentTime} : null; }")
            except Exception:  # noqa: BLE001
                continue
            if not st:
                continue
            if not st["paused"] and st["t"] > 0.2:
                return {"frame": fr.url[:80], "playing": True, "t": round(st["t"], 2),
                        "clicked": "-"}
            for sel in ("button.vjs-big-play-button", "button[title='播放视频']", "video"):
                try:
                    loc = fr.locator(sel).first
                    if not loc.count():
                        continue
                    loc.click(timeout=min(timeout_ms, 5000))
                    self.log(f"  点了 {sel}")
                except Exception:  # noqa: BLE001
                    continue
                deadline = time.time() + 6
                while time.time() < deadline:
                    page.wait_for_timeout(300)
                    try:
                        st = fr.evaluate("() => { const v = document.querySelector('video');"
                                         " return v ? {paused: v.paused, t: v.currentTime} : null; }")
                    except Exception:  # noqa: BLE001
                        st = None
                    if st and not st["paused"] and st["t"] > 0.2:
                        return {"frame": fr.url[:80], "playing": True, "t": round(st["t"], 2),
                                "clicked": sel}
                    last = {"frame": fr.url[:80], "playing": False, "t": round((st or {}).get("t", 0), 2),
                            "clicked": sel}
        self.log("  注意：video 没播起来（currentTime 一直是 0）")
        return last or {"playing": False, "t": 0, "clicked": "-"}

    def _do_click(self, page, step: dict, timeout_ms: int, on_target=None,
                  allow_scroll: bool = True) -> dict:
        t = self._resolve(page, step, timeout_ms, allow_scroll=allow_scroll)
        if t is None:
            raise LookupError("找不到目标元素")
        if on_target:
            on_target(t)

        occluded = self._occlusion(page, t)
        if occluded in {"offscreen", "zero-size"}:
            self.log(f"  目标不可见（{occluded}），重新定位")
            t2 = self._resolve(page, step, min(timeout_ms, 4000), allow_scroll=False)
            if t2 is not None:
                t = t2
                on_target(t)
                occluded = self._occlusion(page, t)
        if occluded and occluded not in {"offscreen", "zero-size"}:
            self.log(f"  目标被 {occluded} 遮挡，按 Esc 关浮层后重试")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001
                pass
            t2 = self._resolve(page, step, min(timeout_ms, 4000), allow_scroll=False)
            if t2 is not None:
                t = t2
                on_target(t)
                occluded = self._occlusion(page, t)
                if not occluded:
                    self.log("  浮层已关闭，重新定位成功")

        before = self._sig(page)
        if t.locator is not None:
            t.locator.click(timeout=timeout_ms)
        elif t.element is not None:
            t.element.click(timeout=timeout_ms)
        else:
            c = t.center
            page.mouse.move(c[0], c[1])
            page.mouse.click(c[0], c[1])
        page.wait_for_timeout(200)
        after = self._sig(page)
        changed = bool(before) and before != after
        if not changed:
            self.log("  注意：点击后页面内容没有任何变化（可能没点中、被遮挡，或这按钮本来就只是滚动/计时）")
        return {"strategy": t.strategy, "box": t.box, "frame": t.frame,
                "occluded": occluded, "changed": changed,
                "detail": {k: v for k, v in t.detail.items() if k != "dsf"}}

    def _do_fill(self, page, step: dict, timeout_ms: int, on_target=None,
                 allow_scroll: bool = True) -> dict:
        t = self._resolve(page, step, timeout_ms, allow_scroll=allow_scroll)
        if t is None:
            raise LookupError("找不到输入框")
        if on_target:
            on_target(t)
        if t.locator is not None:
            t.locator.fill(str(step["value"]), timeout=timeout_ms)
        elif t.element is not None:
            t.element.click(timeout=timeout_ms)
            page.keyboard.type(str(step["value"]))
        else:
            c = t.center
            page.mouse.click(c[0], c[1])
            page.keyboard.type(str(step["value"]))
        return {"strategy": t.strategy, "box": t.box, "frame": t.frame, "value": step["value"]}

    def _check_expect(self, page, expect: dict) -> tuple[bool, str]:
        if not expect:
            return True, ""
        details = []
        ok = True
        if expect.get("url_contains"):
            cur = page.url
            hit = str(expect["url_contains"]) in cur
            ok = ok and hit
            details.append(f"url包含{expect['url_contains']!r}={hit}")
        if expect.get("text"):
            try:
                body = page.inner_text("body")
            except Exception:  # noqa: BLE001
                body = ""
            hit = str(expect["text"]) in body
            ok = ok and hit
            details.append(f"文本{expect['text']!r}={hit}")
        if expect.get("js"):
            try:
                val = page.evaluate(expect["js"])
            except Exception as exc:  # noqa: BLE001
                val = f"异常: {exc}"
            hit = bool(val)
            ok = ok and hit
            details.append(f"js={val!r}")
        return ok, "; ".join(details)

    def run_step(self, page, step: dict, idx: int, total: int) -> dict:
        defaults = self.spec.get("defaults") or {}
        timeout_ms = int(step.get("timeout_ms") or defaults.get("timeout_ms", 8000))
        settle = int(defaults.get("settle_ms", 350))
        retries = int(defaults.get("retries", 2))
        action = step["do"]
        label = step.get("name") or action
        self.log(f"{idx}/{total} {action} «{label}» 开始")
        t0 = time.time()
        result: dict[str, Any] = {}
        error = None
        target_shot: dict[str, str] = {}

        def on_target(t) -> None:
            if "path" not in target_shot:
                target_shot["path"] = self._shot(page, f"{idx:02d}-target.png", box=t.box, note=label) or ""

        for attempt in range(retries + 1):
            try:
                if action == "click":
                    result = self._do_click(page, step, timeout_ms, on_target=on_target,
                                            allow_scroll=(attempt == 0))
                elif action == "fill":
                    result = self._do_fill(page, step, timeout_ms, on_target=on_target,
                                           allow_scroll=(attempt == 0))
                elif action == "goto":
                    page.goto(step.get("value") or step.get("url"), wait_until="domcontentloaded",
                              timeout=timeout_ms)
                    result = {"url": page.url}
                elif action == "wait":
                    if step.get("find"):
                        t = self._resolve(page, step, timeout_ms)
                        if t is None:
                            raise LookupError("等待目标超时")
                        result = {"found": t.strategy}
                    else:
                        ms = int(step.get("value") or step.get("ms") or 500)
                        self._sleep(ms)
                        result = {"waited_ms": ms}
                elif action == "assert":
                    ok, detail = self._check_expect(page, step.get("expect") or {"text": step.get("value")})
                    if not ok:
                        raise AssertionError(f"断言失败: {detail}")
                    result = {"detail": detail}
                elif action == "press":
                    t = self._resolve(page, step, timeout_ms)
                    if t is None:
                        raise LookupError("找不到目标元素")
                    on_target(t)
                    keys = step.get("keys") or step.get("value") or "Enter"
                    if t.locator is not None:
                        t.locator.press(keys, timeout=timeout_ms)
                    else:
                        page.mouse.click(*t.center)
                        page.keyboard.press(keys)
                    result = {"keys": keys, "strategy": t.strategy}
                elif action == "scroll":
                    dy = int(step.get("value") or 600)
                    page.mouse.wheel(0, dy)
                    result = {"dy": dy}
                elif action == "next_chapter":
                    sel = step.get("selector") or ".posCatalog_select"
                    info = page.evaluate(NEXT_CHAPTER_JS, sel) or {}
                    if not info.get("ok"):
                        raise LookupError(f"找不到列表里的下一项（{info.get('reason')}）")
                    # 用 nth 下标点，别用 id 拼 CSS（有些条目 id 是纯数字，不是合法选择器）
                    cat_idx = int(info.get("nextIdx") or 0)
                    loc = page.locator(sel).nth(cat_idx)
                    loc.scroll_into_view_if_needed(timeout=3000)
                    loc.click(timeout=timeout_ms)
                    old_cid = info.get("cid")
                    deadline = time.time() + 12
                    changed = False
                    while time.time() < deadline:
                        m = re.search(r"chapterId=(\d+)", page.url)
                        if m and m.group(1) != old_cid:
                            changed = True
                            break
                        page.wait_for_timeout(300)
                    result = {"from": old_cid,
                              "to": (re.search(r"chapterId=(\d+)", page.url) or [None, ""])[1],
                              "chapter": info.get("nextText"), "changed": changed}
                    if not changed:
                        self.log(f"  注意：点了下一章但网址没变（{info.get('nextText')}）")
                elif action == "dismiss_dialog":
                    hit = self._dismiss_dialog(page, step.get("texts"))
                    result = {"found": bool(hit), "dialog": (hit or {}).get("text", ""),
                              "clicked": (hit or {}).get("button", "")}
                    if not hit:
                        self.log("  当前没有确认弹窗（正常，继续）")
                elif action == "play_video":
                    result = self._play_video(page, timeout_ms)
                elif action == "scroll_to":
                    y = int(step.get("value") or 0)
                    page.evaluate(f"window.scrollTo(0, {y})")
                    page.wait_for_timeout(150)
                    actual = page.evaluate("window.scrollY")
                    result = {"y": y, "actual": actual}
                    if abs(int(actual or 0) - y) > 4:
                        self.log(f"  注意：想滚到 {y}，实际停到 {actual}（页面可能不够长）")
                elif action == "screenshot":
                    result = {"path": self._shot(page, step.get("value") or f"shot-{idx}.png")}
                else:
                    raise ValueError(f"不支持的动作 {action}")
                error = None
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                error = exc
                if attempt < retries:
                    self.log(f"  第 {attempt + 1} 次失败（{type(exc).__name__}: {str(exc)[:120]}），重试")
                    page.wait_for_timeout(settle)

        elapsed = int((time.time() - t0) * 1000)
        if error is not None:
            shot = self._shot(page, f"fail-{idx:02d}.png", None, "失败现场")
            entry = {"index": idx, "name": label, "do": action, "ok": False, "elapsed_ms": elapsed,
                     "error": f"{type(error).__name__}: {str(error)[:300]}", "shot": shot, **result}
            if step.get("optional"):
                entry["ok"] = True
                entry["skipped"] = True
                self.log(f"{idx}/{total} SKIP «{label}»（可选步骤失败，继续）: {entry['error'][:120]}")
                self.report["steps"].append(entry)
                page.wait_for_timeout(settle)
                return entry
            self.log(f"{idx}/{total} FAIL «{label}» {entry['error']} ({elapsed}ms)")
            self.report["steps"].append(entry)
            raise WorkflowError(f"第 {idx} 步失败: {label} :: {entry['error']}", idx, self.report)

        shot = self._shot(page, f"{idx:02d}-after.png", target=None, note=label)
        if step.get("expect"):
            ok, detail = self._check_expect(page, step["expect"])
            result["expect_ok"] = ok
            result["expect_detail"] = detail
            if not ok:
                self.log(f"  期望未满足: {detail}（动作本身已执行）")
        entry = {"index": idx, "name": label, "do": action, "ok": True, "elapsed_ms": elapsed,
                 "shot": target_shot.get("path") or shot, "after_shot": shot, **result}
        self.report["steps"].append(entry)
        self.log(f"{idx}/{total} OK   «{label}» via {result.get('strategy') or action} ({elapsed}ms)")
        page.wait_for_timeout(settle)
        return entry

    def _sleep(self, ms: int) -> None:
        end = time.time() + ms / 1000
        while time.time() < end:
            if self.stop_event.is_set():
                raise KeyboardInterrupt("用户中止")
            time.sleep(0.05)

    # ---------- 全流程 ----------
    def run_on_page(self, page) -> dict:
        """在已经打开的页面上跑一遍所有步骤（不启动/不关闭浏览器）。"""
        steps = self.spec["steps"]
        total = len(steps)
        t0 = time.time()
        self.log(f"工作流 «{self.spec.get('name')}» 共 {total} 步，输出目录 {self.out_dir}")
        try:
            for i, step in enumerate(steps, 1):
                if self.stop_event.is_set():
                    raise KeyboardInterrupt("用户中止")
                if control.stop_requested():
                    control.clear_stop()
                    raise KeyboardInterrupt("检测到 STOP 文件")
                self.run_step(page, step, i, total)
            self.report["ok"] = True
        except KeyboardInterrupt:
            self.report["error"] = "用户中止"
            self.log("已中止")
        except WorkflowError as exc:
            self.report["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            self.report["error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"异常: {self.report['error']}")
        finally:
            self.report["duration_ms"] = int((time.time() - t0) * 1000)
            self.report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write_report()
        return self.report

    def run(self) -> dict:
        t0 = time.time()
        self.report["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open_page(self.spec, headless=self.headless, log=self.log) as (page, _ctx):
                if self.spec.get("auto_accept_dialogs"):
                    install_dialog_auto_accept(page, log=self.log)
                start = self.spec.get("start_url")
                if start:
                    self.log(f"打开 {start}")
                    page.goto(start, wait_until="domcontentloaded",
                              timeout=int((self.spec.get("defaults") or {}).get("timeout_ms", 8000)))
                    self._shot(page, "00-start.png")
                return self.run_on_page(page)
        except KeyboardInterrupt:
            self.report["error"] = "用户中止"
            self.report["ok"] = False
            self.log("已中止")
        except Exception as exc:  # noqa: BLE001
            self.report["error"] = f"{type(exc).__name__}: {exc}"
            self.report["ok"] = False
            self.log(f"异常: {self.report['error']}")
        finally:
            self.report.setdefault("duration_ms", int((time.time() - t0) * 1000))
            if not os.path.isfile(os.path.join(self.out_dir, "report.json")):
                self._write_report()
        return self.report

    def _write_report(self) -> None:
        with open(os.path.join(self.out_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(self.report, fh, ensure_ascii=False, indent=2)
        lines = [
            f"工作流: {self.report['workflow']}",
            f"结果: {'成功' if self.report['ok'] else '失败'}",
            f"错误: {self.report.get('error')}",
            f"耗时: {self.report.get('duration_ms')}ms",
            "",
        ]
        for s in self.report["steps"]:
            lines.append(
                f"{s['index']:>2}. [{('OK ' if s['ok'] else 'FAIL')}] {s['do']:<10} {s.get('name','')} "
                f"via {s.get('strategy','-')} {s.get('elapsed_ms')}ms {s.get('error','')}"
            )
        with open(os.path.join(self.out_dir, "summary.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.log(f"报告: {os.path.join(self.out_dir, 'report.json')}")


def run_file(path: str, out_dir: str, log: LogFn | None = None, headless: bool | None = None) -> dict:
    spec = spec_mod.load_spec(path)
    spec["_base_dir"] = os.path.dirname(os.path.abspath(path))
    return Engine(spec, out_dir, log=log, headless=headless).run()


class Session:
    """常驻模式：浏览器开一次、页面留在原地，每隔一段时间在同一页面里重跑一遍步骤。

    跟 Engine.run() 的区别：不退出浏览器、不重新加载页面（除非 reload_each）。
    每轮仍然产出独立的 out/<名>-<时间>/ 报告和截图。
    """

    def __init__(self, spec: dict[str, Any], out_base: str, log: LogFn | None = None,
                 stop_event: threading.Event | None = None, headless: bool | None = None,
                 on_artifact: Callable[[str], None] | None = None,
                 reload_each: bool = False, restart_each: bool = False, reset_each: bool = True,
                 goto_start_each: bool = False, reload_at_start: bool = False,
                 idle_seconds: float = 0, idle_mode: str = "page", idle_check_s: float = 2.0,
                 idle_threshold: float = 3.0, idle_ratio: float = 0.01,
                 idle_verbose: bool = False, idle_ignore_video: bool = False,
                 min_dwell_s: int = 0, auto_dismiss: bool = True,
                 max_wait_s: int = 0,
                 on_round: Callable[[Any, dict], None] | None = None,
                 on_start: Callable[[Any], None] | None = None):
        spec_mod.validate(spec)
        spec_mod.resolve_assets(spec)
        self.spec = spec
        self.out_base = out_base
        self.stop_event = stop_event or threading.Event()
        self.headless = headless
        self.on_artifact = on_artifact
        self.reload_each = reload_each
        self.restart_each = restart_each
        self.goto_start_each = goto_start_each
        self.reload_at_start = reload_at_start
        self.idle_seconds = float(idle_seconds or 0)
        self.idle_mode = idle_mode or "page"
        self.idle_check_s = float(idle_check_s or 2.0)
        self.idle_threshold = float(idle_threshold if idle_threshold is not None else 3.0)
        self.idle_ratio = float(idle_ratio if idle_ratio is not None else 0.01)
        self.idle_verbose = bool(idle_verbose)
        self.idle_ignore_video = bool(idle_ignore_video)
        self.min_dwell_s = int(min_dwell_s or 0)
        self.auto_dismiss = bool(auto_dismiss)
        self.max_wait_s = int(max_wait_s or 0)
        self.reset_each = reset_each
        self.on_round = on_round
        self.on_start = on_start
        self._first_url = None
        self.log = log or (lambda m: print(m, flush=True))
        self.page = None
        self.rounds: list[dict] = []

    def _wait(self, seconds: float) -> bool:
        """返回 False 表示被中止。"""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            if control.stop_requested():
                self.log("检测到 STOP 文件，结束常驻循环")
                control.clear_stop()
                return False
            time.sleep(min(1.0, max(0.0, end - time.time())))
        return not self.stop_event.is_set()

    # ---------- 静止触发 ----------
    def _frame(self, page):
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        from PIL import Image, ImageGrab  # noqa: PLC0415

        if self.idle_mode == "desktop":
            img = ImageGrab.grab()
        else:
            kwargs = dict(scale="css", type="png")
            if self.idle_ignore_video:
                try:
                    if page.locator("video").count():
                        kwargs["mask"] = [page.locator("video")]
                        kwargs["mask_color"] = "#000000"
                except Exception:  # noqa: BLE001
                    pass
            png = page.screenshot(**kwargs)
            img = Image.open(io.BytesIO(png))
        small = img.convert("L").resize((64, 36))
        return np.asarray(small, dtype=np.int16)

    @staticmethod
    def _best_diff(cur, prev, max_shift: int = 2):
        """在 ±max_shift 像素内对齐后再比，避免 1px 抖动/滚动条导致的整屏误判。"""
        import numpy as np  # noqa: PLC0415

        best_mean = float("inf")
        best_ratio = 0.0
        for dy in range(-max_shift, max_shift + 1):
            for dx in range(-max_shift, max_shift + 1):
                shifted = np.roll(np.roll(cur, dy, 0), dx, 1)
                d = np.abs(shifted - prev)
                m = float(d.mean())
                if m < best_mean:
                    best_mean = m
                    best_ratio = float((d >= 12).mean())
        return best_mean, best_ratio

    def _wait_until_idle(self, page, idle_seconds: float) -> bool:
        """一直采样页面/桌面，连续 idle_seconds 秒没有明显变化就返回 True。"""
        where = "桌面" if self.idle_mode == "desktop" else "页面"
        self.log(f"等待{where}静止：连续 {idle_seconds:g}s 没变化才进下一轮"
                 f"（每 {self.idle_check_s:g}s 采样一次，阈值 mean>{self.idle_threshold:g} 或 变化像素>{self.idle_ratio:.1%}）")
        prev = None
        stable_since = None
        started = time.time()
        i = 0
        last_report = 0.0
        while True:
            if self.stop_event.is_set():
                return False
            if control.stop_requested():
                self.log("检测到 STOP 文件，结束常驻循环")
                control.clear_stop()
                return False
            try:
                cur = self._frame(page)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  采样失败（当作有变化）: {exc}")
                stable_since = None
                time.sleep(self.idle_check_s)
                continue
            i += 1
            if prev is None:
                stable_since = time.time()
            else:
                mean_diff, moved_ratio = self._best_diff(cur, prev)
                changed = mean_diff > self.idle_threshold or moved_ratio > self.idle_ratio
                if changed:
                    stable_since = None
                elif stable_since is None:
                    stable_since = time.time()
                    self.log(f"  {where}已静止，开始计时")
                if self.idle_verbose or time.time() - last_report >= 15:
                    last_report = time.time()
                    verdict = "有变化（计时清零）" if changed else "静止"
                    self.log(f"  采样#{i}: 差异 mean={mean_diff:.2f} 变化像素={moved_ratio:.2%} → {verdict}")
            prev = cur
            if (stable_since is not None and time.time() - stable_since >= idle_seconds
                    and (not self.min_dwell_s or time.time() - started >= self.min_dwell_s)):
                self.log(f"  {where}连续 {time.time() - stable_since:.0f}s 没变化 → 执行下一轮")
                return True
            if self.max_wait_s and time.time() - started >= self.max_wait_s:
                self.log(f"  等待静止超过 {self.max_wait_s}s，不等了，直接执行下一轮")
                return True
            time.sleep(self.idle_check_s)

    def run(self, interval_s: float = 900, rounds: int = 0) -> list[dict]:
        name = self.spec.get("name") or "session"
        total_txt = f"{rounds} 轮" if rounds else "不限轮"
        if self.idle_seconds > 0:
            where = "桌面" if self.idle_mode == "desktop" else "页面"
            self.log(f"常驻模式启动：{where}静止 {self.idle_seconds:g}s 就重跑一遍，{total_txt}，"
                     f"浏览器保持打开（Ctrl+C / 中止 结束）")
        else:
            self.log(f"常驻模式启动：每 {interval_s:g}s 在同一页面重跑一遍，{total_txt}，"
                     f"浏览器保持打开（Ctrl+C / 中止 结束）")
        try:
            with open_page(self.spec, headless=self.headless, log=self.log) as (page, _ctx):
                self.page = page
                if self.spec.get("auto_accept_dialogs"):
                    install_dialog_auto_accept(page, log=self.log)
                self._loop(page, name, interval_s, rounds)
        except KeyboardInterrupt:
            self.log("常驻模式已中止")
        except Exception as exc:  # noqa: BLE001
            self.log(f"常驻模式异常: {type(exc).__name__}: {exc}")
        return self.rounds

    def _loop(self, page, name: str, interval_s: float, rounds: int) -> None:
        start = self.spec.get("start_url")
        if start:
            self.log(f"打开 {start}（只打开这一次）")
            page.goto(start, wait_until="domcontentloaded",
                      timeout=int((self.spec.get("defaults") or {}).get("timeout_ms", 8000)))
            page.wait_for_timeout(2000)
        if self.on_start and not page.is_closed():
            try:
                self.on_start(page)
            except Exception as exc:  # noqa: BLE001
                self.log(f"  on_start 回调失败: {exc}")
        n = 0
        while True:
            if self.stop_event.is_set():
                break
            n += 1
            run_dir = spec_mod.new_run_dir(self.out_base, name)
            self.log(f"===== 第 {n} 轮（页面保持不动）=====")
            if n > 1 and self.goto_start_each and start and not page.is_closed():
                # 只在"下一轮开始"时回入口，本轮结束后的页面状态原样保留
                try:
                    self.log("  本轮从起始网址开始（上一轮的页面状态已保留到最后）")
                    page.goto(start, wait_until="domcontentloaded",
                              timeout=int((self.spec.get("defaults") or {}).get("timeout_ms", 8000)))
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  回到起始网址失败: {exc}")
            if n > 1 and self.reload_at_start and not page.is_closed():
                # 本轮开始时刷新"当前所在网址"，不动 start_url
                try:
                    cur = page.url
                    self.log(f"  本轮开始：刷新当前页（{cur[:90]}）")
                    page.reload(wait_until="domcontentloaded",
                                timeout=int((self.spec.get("defaults") or {}).get("timeout_ms", 8000)))
                    self.log(f"  刷新完成，仍在（{page.url[:90]}）")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  刷新当前页失败: {exc}")
            if n > 1 and self.reset_each and not page.is_closed() and not self.reload_at_start:
                # 上一轮留下的浮层/滚动位置会让这一轮点错地方，先复位
                try:
                    if self.auto_dismiss:
                        dismiss_dialog(page, log=self.log)
                    page.keyboard.press("Escape")
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(200)
                    self.log("  已复位：关确认弹窗 + Esc 关浮层 + 回到页面顶部")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  复位失败: {exc}")
            try:
                cur = page.url
                if self._first_url is None:
                    self._first_url = cur
                elif cur != self._first_url:
                    self.log(f"  注意：当前页面已不是起始页（{cur[:90]}）")
            except Exception:  # noqa: BLE001
                pass
            engine = Engine(self.spec, run_dir, log=self.log, stop_event=self.stop_event,
                            on_artifact=self.on_artifact, headless=self.headless)
            report = engine.run_on_page(page)
            self.rounds.append(report)
            if self.on_round and not page.is_closed():
                try:
                    self.on_round(page, report)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  回合回调失败: {exc}")
            if rounds and n >= rounds:
                self.log(f"已跑满 {rounds} 轮")
                break
            if page.is_closed():
                self.log("页面已被关闭，停止")
                break
            if self.restart_each and start:
                self.log(f"回到起始页 {start[:80]}")
                page.goto(start, wait_until="domcontentloaded")
            elif self.reload_each:
                try:
                    self.log(f"重新加载当前页（不跳回起始页）: {page.url[:80]}")
                    page.reload(wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  reload 失败: {exc}")
            if self.idle_seconds > 0:
                if not self._wait_until_idle(page, self.idle_seconds):
                    break
            elif not self._wait(interval_s):
                break
