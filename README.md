# webclick

用真实浏览器（本机 Edge/Chrome）识别页面上的按钮并点击，支持**录制 → 回放 → 循环**。
按钮定位分三级降级：**DOM 语义 → 模板匹配 → OCR**，所以 canvas 画出来的按钮、纯图片按钮也能点。

A browser automation tool that finds and clicks buttons by DOM selectors, image templates, or OCR — with recording, replay, loops, and a "page went idle" trigger.

---

## 它解决什么问题

写死坐标的自动化一改版就废，纯视觉的又常常点偏。webclick 的做法是：

1. 先用真实 DOM 语义找（`#id`、`text`、`role`、`aria-label`…），稳；
2. 找不到再用**模板匹配**（OpenCV 多尺度 `matchTemplate`）；
3. 还不行就用 **OCR**（RapidOCR）读画面文字定位。

每一步都留下截图和 JSON 报告，点没点中、点到哪，事后能查。

## 特性

- **录制**：在弹出浏览器里点一遍，自动生成工作流 JSON（点击 / 输入 / 滚动位置 / 换网址都会记）
- **回放**：每步都产出 `NN-target.png`（动作前，红框标出识别目标）和 `NN-after.png`
- **循环**：固定间隔，或"页面连续 N 秒没变化"（静止触发）后再跑下一轮
- **遮挡/抖动防护**：点击前检查目标是否被浮层盖住；视觉比对先做 ±2px 对齐
- **失败可诊断**：定位失败会打印每个选择器命中几个、文本在不在页面上
- **全局热键**：录制中按 F9 立即停下并保存（不需要窗口焦点）
- **单实例锁**：同一个浏览器 profile 不会被两个进程同时占用

## 安装

### 方式 A：Windows 懒人包（不装 Python，解压即用）

到 [Releases](https://github.com/liyou-nizon/webclick/releases) 下载
`webclick-0.1.0-win64-portable.zip`（约 150 MB），解压后双击 `启动界面.cmd`。
包里自带 Python 3.12 + 全部依赖（含 OCR 模型），只需要系统自带 Edge。

### 方式 B：从源码装

需要 Python 3.10+ 和本机已安装的 Edge 或 Chrome（不下载 Chromium）。

```bash
git clone https://github.com/liyou-nizon/webclick.git
cd webclick
python -m pip install -r requirements.txt
python -m webclick doctor        # 检查 playwright / opencv / OCR / 浏览器
```

只想用 DOM 定位、不想装 400MB 视觉依赖：

```bash
python -m pip install -e ".[dom]"
```

> 平台说明：**热键（F9）和 Tkinter 界面目前只在 Windows 上验证过**，
> 其它平台 `hotkey.py` 会降级为"无全局热键"，其余命令行功能可用。

> 依赖较大（opencv + onnxruntime + OCR 模型，约 400MB）。只想用 DOM 定位的话，
> 可以不装 `opencv-python-headless` 和 `rapidocr-onnxruntime`，其余功能照常。

## 快速开始

```bash
# 1. 本地自检（内置测试页，8 项检查全过才算环境正常）
python -m webclick selftest

# 2. 看一个页面上有哪些可点元素，并生成选择器
python -m webclick inspect https://example.com

# 3. 录制：在弹出的浏览器里正常操作，按 F9 结束
python -m webclick record https://example.com --out my.json

# 4. 回放
python -m webclick run my.json

# 5. 循环：每 15 分钟跑一次
python -m webclick keep my.json --every 900

# 6. 循环：页面静止 60 秒后再跑下一轮
python -m webclick keep my.json --idle-trigger 60
```

图形界面：`python -m webclick gui`

## 工作流格式

```json
{
  "name": "demo",
  "start_url": "https://example.com",
  "viewport": { "width": 1280, "height": 900 },
  "defaults": { "timeout_ms": 8000, "retries": 1, "settle_ms": 350 },
  "steps": [
    {
      "do": "click",
      "name": "登录",
      "find": [
        { "by": "role", "role": "button", "value": "登录" },
        { "by": "text", "value": "登录" },
        { "by": "css", "value": "#login-btn" },
        { "by": "template", "value": "assets/login.png", "threshold": 0.88 },
        { "by": "ocr", "value": "登录", "min_score": 0.6 }
      ]
    },
    { "do": "fill", "name": "账号", "value": "demo",
      "find": [{ "by": "css", "value": "#username" }] },
    { "do": "assert", "name": "登录成功",
      "expect": { "text": "欢迎" } }
  ]
}
```

`do` 可用：`click` `fill` `press` `goto` `wait` `assert` `screenshot` `scroll`（相对滚动）
`scroll_to`（滚到绝对 y）`next_chapter`（列表型目录里点"当前项的下一项"，选择器可用 step 的
`selector` 覆盖）`play_video`（找 `<video>` 并验证 `currentTime` 真的在走）

`find` 是候选列表，按顺序试，第一个命中的就用；都失败时会把现场打印出来。

## 三个定位策略

| 策略 | 写法 | 适用 | 稳定性 |
|---|---|---|---|
| DOM | `{"by":"css","value":"#id"}`、`text`/`role`/`label`/`testid` | 普通网页 | 高 |
| 模板 | `{"by":"template","value":"assets/x.png","threshold":0.85}` | 图片按钮、canvas | 中（跟分辨率绑定） |
| OCR | `{"by":"ocr","value":"开始采集"}` | DOM 里没文字的按钮 | 中（首次加载模型约 6s） |

录制时会把这三种一起写进 `find`，所以页面改版后还有兜底。

## 示例

- `examples/workflow-template.json`：最小可跑模板（配合 `python -m webclick serve` 启动的本地测试页）

> 站点专用脚本不放在本仓库里，请按自己的场景写工作流。

## 目录结构

```
webclick/
├── browser.py     启动 Edge/Chrome（持久 profile，保留登录态；单实例锁）
├── locator.py     三级定位
├── vision.py      模板匹配 / OCR / 红框标注
├── engine.py      执行步骤、截图取证、报告；常驻循环 / 静止触发
├── recorder.py    录制（sessionStorage 缓冲，跳页不丢）
├── inspector.py   枚举可交互元素并生成选择器
├── gui.py         Tkinter 界面
├── selftest.py    内置测试页自检
└── tests/         端到端测试脚本（自检、录制、热键、静止触发、GUI）
```

## 已知限制

- 不处理验证码、滑块、Cloudflare 之类人机校验
- 模板匹配绑定录制时的窗口宽度和 DPI，换缩放比例要重录模板（DOM 策略不受影响）
- OCR 首次调用约 6 秒（加载模型），之后单步约 1 秒
- 视觉策略点不到 iframe 内部的具体控件（DOM 策略可以）
- 同一浏览器 profile 不能并发（内置锁会拒绝第二个进程）

## 免责声明

本项目只做"浏览器自动化"这一件事。请只在你自己的账号、你有权操作的页面上使用，
并遵守目标站点的服务条款与所在地法律。因使用本工具产生的任何后果由使用者自负。

## License

MIT
