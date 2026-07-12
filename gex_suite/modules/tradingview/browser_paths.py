"""Chrome/Brave 定位與 9222 CDP 瀏覽器啟動（shared by paste + 版面分組）."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request

CDP_PROFILE_DIRNAME = "gex_tv_cdp_profile"
DEFAULT_CDP_PORT = 9222
_DEFAULT_LANDING_URL = "https://tw.tradingview.com/chart/"


def find_browser(browser_type: str) -> str | None:
    return next(
        (p for p in browser_candidates(browser_type) if p and Path(p).exists()), None
    )


def cdp_profile_dir() -> Path:
    profile_dir = Path(tempfile.gettempdir()) / CDP_PROFILE_DIRNAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def launch_cdp_browser(
    browser_type: str,
    *,
    urls: list[str] | None = None,
    port: int = DEFAULT_CDP_PORT,
) -> str | None:
    """以持久 CDP profile 冷啟瀏覽器（帶 --no-first-run，供自動化情境）.

    Returns the binary path used, or ``None`` if no browser executable found.
    不等待 9222 就緒 — 呼叫端視需要接 :func:`wait_cdp_ready`。
    """
    path = find_browser(browser_type)
    if not path:
        return None
    args = [
        path,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={cdp_profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        *(urls if urls else [_DEFAULT_LANDING_URL]),
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def cdp_ready(port: int = DEFAULT_CDP_PORT, timeout: float = 1.0) -> bool:
    try:
        with request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_cdp_ready(port: int = DEFAULT_CDP_PORT, timeout_sec: float = 15.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout_sec:
        if cdp_ready(port):
            return True
        time.sleep(0.25)
    return False


def browser_candidates(browser_type: str) -> list[str | None]:
    kind = "brave" if str(browser_type).strip().lower() == "brave" else "chrome"
    if kind == "brave":
        if sys.platform.startswith("win"):
            return [
                shutil.which("brave"),
                shutil.which("brave.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
            ]
        if sys.platform == "darwin":
            return [
                shutil.which("brave"),
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ]
        return [
            shutil.which("brave-browser"),
            shutil.which("brave"),
        ]
    if sys.platform.startswith("win"):
        return [
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
        ]
    if sys.platform == "darwin":
        return [
            shutil.which("google-chrome"),
            shutil.which("chrome"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    return [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium-browser"),
        shutil.which("chromium"),
        shutil.which("chrome"),
    ]
