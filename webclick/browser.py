"""浏览器启动：默认用本机 Edge（channel=msedge），保留登录态。"""
from __future__ import annotations

import contextlib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# WEBCLICK_PROFILE 可让测试用独立 profile，不跟正在跑的正式任务抢锁
DEFAULT_PROFILE = os.environ.get("WEBCLICK_PROFILE") or os.path.join(ROOT, ".webclick-profile")
TMP_DIR = os.path.join(ROOT, "webclick", ".tmp")


class BrowserError(RuntimeError):
    pass


def _prepare_tmp() -> None:
    """Playwright 的 driver 会 mkdtemp，系统 TEMP 不可写时启动会直接 EPERM。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = TMP_DIR


def _pid_alive(pid: int) -> bool:
    """Windows 上不能用 os.kill(pid, 0) —— 那会直接 TerminateProcess 掉对方。"""
    if os.name == "nt":
        import ctypes  # noqa: PLC0415

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_profile_lock(profile: str) -> str:
    """同一个浏览器 profile 不能被两个进程同时用，否则第二个直接起不来。"""
    lock = os.path.join(profile, "webclick.lock")
    os.makedirs(profile, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                with open(lock, encoding="ascii") as fh:
                    pid = int(fh.read().strip() or 0)
            except Exception:  # noqa: BLE001
                pid = 0
            if pid and _pid_alive(pid):
                raise BrowserError(
                    f"已有 webclick 进程（PID {pid}）正在使用浏览器 profile。"
                    "等它跑完，或先关掉另一个窗口/计划任务。"
                ) from None
            with contextlib.suppress(OSError):
                os.remove(lock)
            continue
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
        return lock
    raise BrowserError("无法获取浏览器 profile 锁")


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise BrowserError(
            "playwright 未安装。请运行: python -m pip install --target webclick/_libs playwright"
        ) from exc
    return sync_playwright


@contextlib.contextmanager
def open_page(spec: dict | None = None, headless: bool | None = None, log=print, url: str | None = None):
    spec = spec or {}
    _prepare_tmp()
    sync_playwright = _playwright()
    pw = sync_playwright().start()
    ctx = None
    lock_path = None
    try:
        channel_cfg = spec.get("channel")
        channels = [channel_cfg] if channel_cfg else ["msedge", "chrome", None]
        headless = spec.get("headless", False) if headless is None else headless
        viewport = spec.get("viewport") or {"width": 1280, "height": 800}
        profile = spec.get("profile_dir") or DEFAULT_PROFILE
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                "--no-default-browser-check"]
        if profile:
            lock_path = _acquire_profile_lock(profile)
        last_exc = None
        # Playwright 默认给 Chromium 加 --no-sandbox（源码 chromium.js: chromiumSandbox !== true），
        # Edge/Chrome 因此会在页面顶部挂一条"不受支持的命令行标志: --no-sandbox"的警告条。
        # 实测本机 Edge 打开沙箱后会卡在启动阶段（既不报错也不返回），所以默认仍用 Playwright 的
        # 无沙箱模式；想要那条警告条消失可在工作流里写 "sandbox": true（启动带 20s 超时）。
        want_sandbox = bool(spec.get("sandbox", False))
        last_exc = None
        for channel in channels:
            try:
                kwargs = dict(headless=headless, viewport=viewport, args=args,
                              locale="zh-CN", accept_downloads=True,
                              chromium_sandbox=want_sandbox, timeout=20000)
                if channel:
                    kwargs["channel"] = channel
                if profile:
                    ctx = pw.chromium.launch_persistent_context(profile, **kwargs)
                else:
                    browser = pw.chromium.launch(**kwargs)
                    ctx = browser.new_context(viewport=viewport, locale="zh-CN")
                log(f"浏览器已启动: channel={channel or 'chromium'} headless={headless} "
                    f"sandbox={'on' if want_sandbox else 'off'} profile={profile or '临时'}")
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                ctx = None
                continue
        if ctx is None:
            raise BrowserError(f"浏览器启动失败（尝试过 {channels}）: {last_exc}")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.set_default_timeout(int((spec.get("defaults") or {}).get("timeout_ms", 8000)))
        except Exception:  # noqa: BLE001
            pass
        if url:
            page.goto(url, wait_until="domcontentloaded")
        yield page, ctx
    finally:
        with contextlib.suppress(Exception):
            if ctx is not None:
                ctx.close()
        with contextlib.suppress(Exception):
            pw.stop()
        if lock_path:
            with contextlib.suppress(OSError):
                os.remove(lock_path)
