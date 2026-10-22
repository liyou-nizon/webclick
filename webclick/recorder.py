"""录制：在真实浏览器里手动点一遍，自动生成工作流 JSON（含模板图片）。

事件用 sessionStorage 缓冲，Python 端定时抽取（不是 binding 回调）：
点了会跳页的按钮时，binding 常常还没送达、页面就换了，最后一击会丢；
sessionStorage 在同源跳转后仍在，能把这一击补回来。
"""
from __future__ import annotations

import json
import os
import queue
import re
import time

from .browser import open_page
from .hotkey import Hotkey
from . import control
from . import spec as spec_mod

RECORD_JS = r"""
(() => {
  if (window.__wcInstalled) return;
  window.__wcInstalled = true;

  function cssEsc(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_\u4e00-\u9fa5-]/g, m => '\\' + m);
  }
  function unique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function pathOf(el) {
    if (el.id) { const s = '#' + cssEsc(el.id); if (unique(s)) return s; }
    for (const attr of ['data-testid', 'data-test', 'name', 'aria-label', 'alt', 'title']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) {
        const s = `${el.tagName.toLowerCase()}[${attr}="${String(v).replace(/"/g, '\\"')}"]`;
        if (unique(s)) return s;
      }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (unique(parts.join(' > '))) return parts.join(' > ');
      node = parent;
    }
    return parts.join(' > ');
  }
  // 点到子元素（span/svg）时，尽量记最近的可点祖先，回放更稳
  function pick(el) {
    if (!el || el.nodeType !== 1) return el;
    try {
      const a = el.closest && el.closest('a,button,[role=button],[onclick],input,select,textarea,summary');
      if (a) return a;
    } catch (e) {}
    return el;
  }
  function info(el, kind, value, raw) {
    const r = el.getBoundingClientRect();
    return {
      kind,
      value: value || '',
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute && el.getAttribute('type')) || '',
      text: ((el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || '')
              .replace(/\s+/g, ' ').trim()).slice(0, 40),
      aria: (el.getAttribute && el.getAttribute('aria-label')) || '',
      role: (el.getAttribute && el.getAttribute('role')) || '',
      id: el.id || '',
      selector: pathOf(el),
      box: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      frame: (window === window.top) ? 'main' : (location.href || 'frame'),
      url: location.href,
      raw_tag: raw ? (raw.tagName || '').toLowerCase() : '',
      ts: Date.now()
    };
  }
  function enqueue(ev) {
    try {
      const k = '__wcQueue';
      const arr = JSON.parse(sessionStorage.getItem(k) || '[]');
      arr.push(ev);
      sessionStorage.setItem(k, JSON.stringify(arr.slice(-200)));
    } catch (e) {}
  }
  document.addEventListener('click', e => {
    const raw = e.target;
    if (!raw || raw.nodeType !== 1) return;
    enqueue(info(pick(raw), 'click', '', raw));
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT') return;
    enqueue(info(pick(el), 'fill', el.value, el));
  }, true);

  // 滚动：停下来 400ms 才记一条，记的是绝对位置（可精确回放）
  let scrollTimer = null;
  window.addEventListener('scroll', () => {
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      enqueue({ kind: 'scroll', y: Math.round(window.scrollY), url: location.href, ts: Date.now() });
    }, 400);
  }, true);
})();
"""

DRAIN_JS = r"""
() => {
  const k = '__wcQueue';
  let arr = [];
  try {
    arr = JSON.parse(sessionStorage.getItem(k) || '[]');
    sessionStorage.setItem(k, '[]');
  } catch (e) { arr = []; }
  return arr || [];
}
"""


def _slug(text: str, fallback: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", text or "").strip("_")
    return (t or fallback)[:24]


class Recorder:
    def __init__(self, url: str, out_path: str, assets_dir: str | None = None, log=print,
                 seconds: int | None = None, headless: bool = False, stop_event=None,
                 hotkey: str | None = "F9", on_ready=None):
        self.url = url
        self.out_path = out_path
        self.assets_dir = assets_dir or os.path.join(os.path.dirname(os.path.abspath(out_path)), "assets")
        self.log = log
        self.seconds = seconds
        self.headless = headless
        self.stop_event = stop_event
        self.hotkey_spec = hotkey
        self.hotkey = Hotkey(hotkey, log=log) if hotkey else None
        self.on_ready = on_ready
        self.spec = spec_mod.new_spec(url, name=_slug(os.path.basename(out_path).rsplit(".", 1)[0], "workflow"))
        self.count = 0
        self.templates = 0
        self.page = None
        self.last_url = ""
        self._last_click_ts = 0.0
        self._last_scroll_y = None
        self.events: "queue.Queue[dict]" = queue.Queue()

    # ---------- 事件采集 ----------
    def drain(self) -> int:
        """从页面 sessionStorage 取出缓冲的事件（跳页也不会丢）。"""
        if self.page is None or self.page.is_closed():
            return 0
        got = 0
        for _ in range(3):
            try:
                items = self.page.evaluate(DRAIN_JS) or []
            except Exception:  # noqa: BLE001  正在跳页，稍后再取
                time.sleep(0.12)
                continue
            for ev in items:
                self.events.put(ev)
                got += 1
            if got or not items:
                break
        return got

    def _record_nav(self) -> None:
        """地址栏/pushState 引起的换页也记一步 goto（点击引起的换页不算，点击本身已经记了）。"""
        if self.page is None or self.page.is_closed():
            return
        try:
            url = self.page.url
        except Exception:  # noqa: BLE001
            return
        if not url or url.startswith("about:") or url == self.last_url:
            return
        self.last_url = url
        if time.time() - self._last_click_ts < 1.5:
            return
        self.count += 1
        step = {"do": "goto", "name": f"打开 {url[:40]}", "value": url}
        self.spec["steps"].append(step)
        spec_mod.save_spec(self.spec, self.out_path)
        self.log(f"[录制] 第 {self.count} 步 goto «地址变了» {url[:100]}")

    def process_pending(self) -> int:
        """在主循环里处理事件（此时可以安全截图）。"""
        n = 0
        while True:
            try:
                ev = self.events.get_nowait()
            except Exception:  # noqa: BLE001
                break
            n += 1
            try:
                self._handle(ev)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[录制] 事件处理失败: {exc}")
        return n

    def _handle(self, ev: dict) -> None:
        kind = ev.get("kind")
        text = ev.get("text") or ""
        selector = ev.get("selector") or ""
        self.count += 1
        n = self.count
        if kind == "click":
            self._last_click_ts = time.time()

        finders: list[dict] = []
        if ev.get("role") and text:
            finders.append({"by": "role", "role": ev["role"], "value": text})
        if ev.get("aria"):
            finders.append({"by": "label", "value": ev["aria"]})
        if kind == "fill" and ev.get("tag") in {"input", "textarea"}:
            finders.insert(0, {"by": "css", "value": selector})
        elif text and len(text) <= 20:
            finders.append({"by": "text", "value": text})
        if selector:
            finders.append({"by": "css", "value": selector})
        finders = [f for f in finders if f.get("value") or f.get("by") == "css"]

        tpl = self._save_template(ev, n)
        if tpl:
            finders.append({"by": "template", "value": tpl, "threshold": 0.88})

        if kind == "click":
            step = {"do": "click", "name": text[:20] or f"点击{n}", "find": finders}
        elif kind == "fill":
            step = {"do": "fill", "name": text[:20] or f"填写{n}", "find": finders,
                    "value": ev.get("value") or ""}
        elif kind == "scroll":
            y = int(ev.get("y") or 0)
            if self._last_scroll_y is not None and abs(y - self._last_scroll_y) < 20:
                return  # 和上一条几乎一样，跳过
            self._last_scroll_y = y
            step = {"do": "scroll_to", "name": f"滚动到 {y}", "value": y}
        else:
            return

        self.spec["steps"].append(step)
        spec_mod.save_spec(self.spec, self.out_path)
        url = ev.get("url") or ""
        moved = ""
        if url and self.last_url and url != self.last_url:
            moved = f"（页面已跳转到 {url[:80]}）"
        self.last_url = url or self.last_url
        self.log(f"[录制] 第 {n} 步 {step['do']} «{step['name']}» 选择器 {selector}"
                 + (f" 模板 {tpl}" if tpl else "") + moved)
        if ev.get("raw_tag") in {"div", "span", "svg", "path"} and not text:
            self.log(f"[录制] 提示：第 {n} 步点中的是 <{ev['raw_tag']}> 且没有文字，"
                     "回放时容易失败；如果这不是你要点的东西，可以直接编辑 JSON 删掉这一步")

    def _save_template(self, ev: dict, n: int) -> str | None:
        box = ev.get("box") or {}
        if box.get("w", 0) < 8 or box.get("h", 0) < 8 or ev.get("frame", "main") != "main":
            return None
        page = self.page
        if page is None or page.is_closed():
            return None
        # 已经跳到别的页面了：此刻截图截到的是新页面，模板会错，跳过
        cur = ""
        try:
            cur = page.url or ""
        except Exception:  # noqa: BLE001
            return None
        if ev.get("url") and cur and cur != ev["url"]:
            self.log(f"[录制] 第 {n} 步点击后页面已跳转，跳过模板截图（改用文本/选择器定位）")
            return None
        try:
            png = page.screenshot(scale="css", type="png")
            from PIL import Image  # noqa: PLC0415
            import io  # noqa: PLC0415

            img = Image.open(io.BytesIO(png))
            crop = img.crop((box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]))
            os.makedirs(self.assets_dir, exist_ok=True)
            name = f"{n:02d}-{_slug(ev.get('text') or ev.get('tag'), 'el')}.png"
            rel = os.path.join(os.path.basename(self.assets_dir), name)
            crop.save(os.path.join(self.assets_dir, name))
            self.templates += 1
            return rel
        except Exception as exc:  # noqa: BLE001
            self.log(f"[录制] 模板截图失败: {exc}")
            return None

    # ---------- 主流程 ----------
    def run(self) -> dict:
        spec_mod.save_spec(self.spec, self.out_path)
        control.clear_stop()
        self.log(f"[录制] 目标 {self.url}")
        self.log("[录制] 在浏览器里正常操作，点一下记一步。结束方式（任选其一）：")
        if self.hotkey is not None:
            if self.hotkey.ok:
                self.log(f"[录制]   0) 按全局热键 {self.hotkey.describe()} —— 焦点在浏览器里也有效（推荐）")
            else:
                self.log(f"[录制]   0) 全局热键不可用: {self.hotkey.describe()}")
        self.log("[录制]   1) 界面点「中止」；或回到命令行窗口按 Ctrl+C")
        self.log(f"[录制]   2) 新建文件 {control.STOP_FILE}（焦点在浏览器里、Ctrl+C 送不到控制台时用）")
        self.log("[录制]   3) 关掉浏览器窗口；或启动时带 --seconds N 定时结束")
        self.page = None
        try:
            with open_page(self.spec, headless=self.headless, log=self.log) as (page, _ctx):
                self.page = page
                page.add_init_script(RECORD_JS)
                page.goto(self.url, wait_until="domcontentloaded")
                page.evaluate(RECORD_JS)
                self.last_url = page.url
                if self.on_ready:
                    try:
                        self.on_ready(page)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"[录制] on_ready 失败: {exc}")
                deadline = time.time() + self.seconds if self.seconds else None
                while True:
                    if deadline and time.time() > deadline:
                        break
                    if self.stop_event is not None and self.stop_event.is_set():
                        self.log("[录制] 收到中止信号")
                        break
                    if self.hotkey is not None and self.hotkey.poll():
                        self.log(f"[录制] 热键 {self.hotkey.describe()} 触发，结束录制并保存")
                        break
                    if control.stop_requested():
                        self.log("[录制] 检测到 STOP 文件，结束录制")
                        control.clear_stop()
                        break
                    if page.is_closed():
                        self.log("[录制] 浏览器已关闭")
                        break
                    self.drain()
                    self.process_pending()
                    self._record_nav()
                    page.wait_for_timeout(250)
                # 收尾：把还在路上的事件再捞两次（跳页时最长约 1s）
                for _ in range(3):
                    self.drain()
                    self.process_pending()
                    self._record_nav()
                    time.sleep(0.25)
        except KeyboardInterrupt:
            self.log("[录制] 手动结束")
        finally:
            if self.hotkey is not None:
                self.hotkey.close()
            spec_mod.save_spec(self.spec, self.out_path)
            self.log(f"[录制] 已保存 {self.out_path}（{len(self.spec['steps'])} 步，{self.templates} 张模板）")
        return self.spec

