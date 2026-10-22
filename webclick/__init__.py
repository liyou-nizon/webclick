"""webclick: 浏览器界面按钮识别 + 点击自动化。

优先用真实 DOM 语义（css / text / role / label ...）定位，
DOM 里拿不到的东西（canvas 画出来的按钮、图片按钮、跨域遮挡）
再用视觉兜底（模板匹配 / OCR）。

依赖安装在 webclick/_libs（pip --target），启动时自动加入 sys.path。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBS = os.path.join(_HERE, "_libs")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

__version__ = "0.1.0"
