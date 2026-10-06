"""Chrome/Brave 定位與 9222 CDP 瀏覽器啟動 —— **唯一一份**。

GUI（批次貼上「啟動 9222」、版面分組）與每日排程 CLI（``cli.py``）都從這裡
冷啟同一個持久 profile，登入、50% 縮放、滿螢幕視窗兩邊一致。瀏覽器設定的任何
修正只准改這裡；不要在 cli／widget／groups_tab 各自加（2026-10-06：GUI 原本用
$TMPDIR 拋棄式 profile → 沒登入、100% 縮放、預設視窗大小，和 CLI 對不上）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib import request

DEFAULT_CDP_PORT = 9222
_DEFAULT_LANDING_URL = "https://tw.tradingview.com/chart/"

# Persistent CDP profile per browser (TradingView login survives runs/reboots).
# tools/gex_chain/preflight.py keeps a duplicated copy of the macOS paths
# (it stays gex_suite-free) — change both together.
_DARWIN_CDP_PROFILES = {
    "chrome": Path.home() / "Library/Application Support/Google/Google-Chrome-CDP",
    "brave": Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser-CDP",
}

# Chrome zoom_level for exactly 50% page zoom == log(0.5)/log(1.2). Page zoom is
# stored per exact host, so we seed both the partition default (catch-all) and
# the specific TradingView hosts the automation loads, so dialogs (layout list /
# indicator settings) aren't clipped/obscured at the default 100% zoom.
_TV_ZOOM_LEVEL = -3.8017840169239308
_TV_ZOOM_HOSTS = ("tw.tradingview.com", "www.tradingview.com")


def normalize_browser(browser_type: str | None) -> str:
    return "brave" if str(browser_type or "").strip().lower() == "brave" else "chrome"


def find_browser(browser_type: str) -> str | None:
    return next(
        (p for p in browser_candidates(browser_type) if p and Path(p).exists()), None
    )


def cdp_profile_dir(browser_type: str = "chrome") -> Path:
    """The shared persistent CDP ``--user-data-dir`` (pure; doesn't create it)."""
    kind = normalize_browser(browser_type)
    if sys.platform == "darwin":
        return _DARWIN_CDP_PROFILES[kind]
    return Path.home() / ".gex_suite" / f"{kind}-cdp-profile"


def _write_prefs(pref: Path, data: dict) -> None:
    pref.parent.mkdir(parents=True, exist_ok=True)
    tmp = pref.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, pref)


def seed_profile_zoom(profile: Path) -> None:
    """Pre-seed the profile's default + per-host page zoom to 50%. Chrome must be
    closed for this to stick, so only call right before a cold launch.
    Best-effort — never blocks the launch."""
    pref = profile / "Default" / "Preferences"
    try:
        data = json.loads(pref.read_text(encoding="utf-8")) if pref.exists() else {}
        part = data.setdefault("partition", {})
        part.setdefault("default_zoom_level", {})["x"] = _TV_ZOOM_LEVEL
        hosts = part.setdefault("per_host_zoom_levels", {}).setdefault("x", {})
        for h in _TV_ZOOM_HOSTS:
            entry = hosts.get(h) or {}
            entry["zoom_level"] = _TV_ZOOM_LEVEL
            entry.setdefault("last_modified", "13426082877612834")
            hosts[h] = entry
        _write_prefs(pref, data)
    except Exception as exc:
        print(f"⚠️ could not pre-seed 50% zoom in {pref}: {exc}")


def seed_profile_window_fills_screen(profile: Path) -> None:
    """Pre-seed the first window's bounds to the whole usable screen.

    Chrome otherwise reopens at whatever size it was last closed (1280×720 on the
    deploy Mac, ~2/3 of the screen). In a 6-pane layout each pane is then so
    short that TV folds legend rows into "+N", which the paste can't read
    (LITE 2026-10-05/06). Uses the work area Chrome itself recorded in
    ``window_placement``; a fresh profile has none yet → skipped this launch.
    Same constraint as the zoom seed: Chrome must be closed. Best-effort."""
    pref = profile / "Default" / "Preferences"
    try:
        if not pref.exists():
            return
        data = json.loads(pref.read_text(encoding="utf-8"))
        wp = data.get("browser", {}).get("window_placement")
        keys = ("work_area_left", "work_area_top", "work_area_right", "work_area_bottom")
        if not isinstance(wp, dict) or not all(isinstance(wp.get(k), int) for k in keys):
            return
        target = {
            "left": wp["work_area_left"],
            "top": wp["work_area_top"],
            "right": wp["work_area_right"],
            "bottom": wp["work_area_bottom"],
        }
        if all(wp.get(k) == v for k, v in target.items()):
            return
        # maximized stays False: on macOS Chrome's "maximized" is the zoom
        # toggle, which can flip a full-size window back to its smaller size.
        wp.update(target, maximized=False)
        _write_prefs(pref, data)
    except Exception as exc:
        print(f"⚠️ could not pre-seed window bounds in {pref}: {exc}")


def launch_cdp_browser(
    browser_type: str,
    *,
    urls: list[str] | None = None,
    port: int = DEFAULT_CDP_PORT,
) -> str | None:
    """以共用持久 CDP profile 冷啟瀏覽器（GUI 與 CLI 同一條）.

    Returns the binary path used, or ``None`` if no browser executable found.
    不等待 9222 就緒 — 呼叫端視需要接 :func:`wait_cdp_ready`。

    ``urls=None`` 開 TradingView 落地頁；``urls=[]`` 不帶任何網址（CLI：之後
    自己導航到版面）。冷啟前先寫入 50% 縮放＋滿螢幕視窗（Chrome 關著才寫得進）。
    ``start_new_session``：脫離呼叫端的 process group，chain 收尾 killpg 時
    不會連瀏覽器一起殺（CLI 原本就這樣，GUI 一併比照）。

    port 已被佔用時**不冷啟第二個 instance**：第二個綁不到 127.0.0.1:port，
    Chrome 會默默改綁 [::1]:port —— 看起來是 CDP 瀏覽器、也能登入，但 paste
    連的是 127.0.0.1，永遠打不到它（2026-07-16 踩坑：使用者在假 9222 登入，
    真 9222 仍未登入）。改為在既有 instance 逐一開分頁（URL 會落在該 instance
    最後使用的視窗，不另開新視窗）。
    """
    targets = [_DEFAULT_LANDING_URL] if urls is None else list(urls)
    if cdp_ready(port):
        for url in targets:
            cdp_open_tab(url, port)
        return f"(reused existing CDP instance on 127.0.0.1:{port})"
    path = find_browser(browser_type)
    if not path:
        return None
    profile = cdp_profile_dir(browser_type)
    profile.mkdir(parents=True, exist_ok=True)
    seed_profile_zoom(profile)
    seed_profile_window_fills_screen(profile)
    args = [
        path,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        *targets,
    ]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
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


def cdp_page_count(
    port: int = DEFAULT_CDP_PORT, timeout: float = 2.0, host: str = "127.0.0.1"
) -> int | None:
    """回傳 CDP 上 type=='page' 的 target 數；/json/list 打不到回 None."""
    try:
        with request.urlopen(
            f"http://{host}:{port}/json/list", timeout=timeout
        ) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        return sum(1 for t in targets if t.get("type") == "page")
    except Exception:
        return None


def cdp_open_tab(
    url: str,
    port: int = DEFAULT_CDP_PORT,
    host: str = "127.0.0.1",
    timeout: float = 5.0,
) -> bool:
    """PUT /json/new 在既有 CDP instance 開一個分頁（Chrome 會把該分頁帶到
    其視窗最前，可用來「標示」哪個視窗才是真正佔住 port 的 instance）。"""
    req = request.Request(f"http://{host}:{port}/json/new?{url}", method="PUT")
    try:
        with request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def revive_windowless_cdp(
    port: int = DEFAULT_CDP_PORT,
    url: str = _DEFAULT_LANDING_URL,
    timeout_sec: float = 10.0,
    host: str = "127.0.0.1",
) -> bool:
    """自癒「視窗全關但行程常駐」的 CDP 瀏覽器（macOS 關掉所有視窗後 Chrome
    留在 Dock，9222 仍佔線）。這種殭屍 instance 的 profile 已卸載：
    /json/version 照常回應，但 Playwright connect_over_cdp 一律炸
    「Browser.setDownloadBehavior: Browser context management is not
    supported」。PUT /json/new 逼它開一個分頁讓 profile 重新載入即可恢復。
    回傳自癒後是否至少有一個 page target。"""
    if not cdp_open_tab(url, port, host):
        return False
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if (cdp_page_count(port, host=host) or 0) > 0:
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
