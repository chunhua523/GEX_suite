"""Locate Chrome/Brave executables per platform (shared by paste + 版面分組)."""
from __future__ import annotations

import os
import shutil
import sys


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
