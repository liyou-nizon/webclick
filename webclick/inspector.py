"""DOM 候选元素枚举 + 稳定选择器生成（注入页面执行）。"""
from __future__ import annotations

import json

CANDIDATE_JS = r"""
() => {
  const SEL = [
    'a', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]', '[role=option]',
    '[onclick]', '[contenteditable=true]', 'canvas', 'img[onclick]', '[data-testid]'
  ].join(',');

  function cssEsc(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_\u4e00-\u9fa5-]/g, m => '\\' + m);
  }
  function unique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function pathOf(el) {
    if (el.id) {
      const s = '#' + cssEsc(el.id);
      if (unique(s)) return s;
    }
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
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) return false;
    return true;
  }
  function label(el) {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || '')
      .replace(/\s+/g, ' ').trim();
    return t.slice(0, 60);
  }

  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (!visible(el)) continue;
    // 过滤掉被祖先完全覆盖的重复候选（保留最小的可点单元）
    out.push({
      index: out.length + 1,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      text: label(el),
      role: el.getAttribute('role') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      testid: el.getAttribute('data-testid') || '',
      selector: pathOf(el),
      cursor: getComputedStyle(el).cursor,
      box: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
    });
  }
  return out;
}
"""


def enumerate_candidates(page) -> list[dict]:
    """枚举所有页面的可交互候选（含 iframe）。"""
    results = []
    for frame in page.frames:
        try:
            items = frame.evaluate(CANDIDATE_JS)
        except Exception as exc:  # noqa: BLE001
            results.append({"frame": frame.url, "error": str(exc)})
            continue
        for it in items or []:
            it["frame"] = "main" if frame == page.main_frame else frame.url
            results.append(it)
    return results


def format_table(items: list[dict], limit: int = 60) -> str:
    rows = []
    for it in items[:limit]:
        if "error" in it:
            rows.append(f"  [!] frame 读取失败: {it['frame']} :: {it['error']}")
            continue
        b = it["box"]
        rows.append(
            f"  [{it['index']:>3}] <{it['tag']}> text={it['text']!r} "
            f"sel={it['selector']} @({b['x']},{b['y']},{b['w']}x{b['h']}) frame={it['frame']}"
        )
    return "\n".join(rows)


def dump_json(items: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
