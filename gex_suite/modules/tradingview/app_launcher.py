"""macOS TradingView 桌面 app 開啟邏輯 — CDP 驅動（版面分組用，無 Qt）.

Verified against TradingView.app 3.3.0 (Electron 38) on 2026-07-11:

- The app accepts Chromium's ``--remote-debugging-port`` switch → full CDP.
- ``Runtime.evaluate("window.open(url)")`` on any chart tab → the app's
  window-open handler routes it to ``createAndAddTab`` — a REAL app tab in
  that tab's window (app log: ``Tab id: …, loading url: …``).
- ``Input.dispatchKeyEvent`` Cmd+N (modifiers=4) on any page → the app's key
  binding fires (``Invoke key binding [cmd+KeyN]``) → new window, whose single
  default tab is ``app.asar/app/new-tab/index.html``.
- Evaluating ``location.href = url`` on that new-tab page navigates it in
  place → the default tab becomes the group's first layout (no leftover
  empty tab), keeping the same CDP targetId.

So: one group = Cmd+N → navigate default tab to urls[0] → ``window.open``
urls[1:] from that tab. Tabs are addressed by targetId — no window focus
races, no Accessibility permission, no localized menus.

Dead ends verified on 3.3.0 (do not resurrect):

- ``open -a TradingView <https url>``: activates only; the macOS ``open-url``
  handler is ``e => { isAuthRedirect(e) && auth.acceptRedirectUrl(e) }`` —
  login redirect only, never a tab.
- ``tradingview://…``: same handler (routes to AuthenticationHandler).
- AX/System Events clicks on the native menu ("New window", "Open link from
  clipboard"): the item reports clicked but Electron menu handlers never fire.

The app must be RUNNING WITH the debug port. ``ensure_app_cdp`` quits a
port-less instance (plain quit Apple Event — window/tab state is restored by
the app on relaunch) and relaunches with the switch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import request as _urlreq

TV_APP_PATH = Path("/Applications/TradingView.app")
TV_BUNDLE_ID = "com.tradingview.tradingviewapp.desktop"
DEFAULT_APP_CDP_PORT = 9333

_NEW_TAB_URL_FRAG = "app/new-tab/"
_CHART_URL_FRAG = "tradingview.com/chart"


class TradingViewAppError(RuntimeError):
    pass


class AppNotInstalledError(TradingViewAppError):
    pass


class AppCDPUnavailableError(TradingViewAppError):
    pass


@dataclass
class AppLaunchSettings:
    app_cdp_port: int = DEFAULT_APP_CDP_PORT
    delay_new_window_ms: int = 1200
    delay_per_url_ms: int = 1800

    @staticmethod
    def from_settings(settings: dict[str, Any]) -> "AppLaunchSettings":
        def _num(key: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(settings.get(key, default)))
            except (TypeError, ValueError):
                return default

        return AppLaunchSettings(
            app_cdp_port=_num("app_cdp_port", DEFAULT_APP_CDP_PORT, minimum=1),
            delay_new_window_ms=_num("delay_new_window_ms", 1200),
            delay_per_url_ms=_num("delay_per_url_ms", 1800),
        )


# ---------- process / CDP-endpoint helpers ----------

def app_running() -> bool:
    try:
        return subprocess.run(["/usr/bin/pgrep", "-xq", "TradingView"]).returncode == 0
    except Exception:
        return False


def cdp_up(port: int, timeout: float = 1.5) -> bool:
    try:
        with _urlreq.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _page_targets(port: int) -> list[dict[str, Any]]:
    with _urlreq.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3.0) as resp:
        targets = json.load(resp)
    return [t for t in targets if t.get("type") == "page"]


async def _run(cmd: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
    )


async def ensure_app_cdp(
    settings: AppLaunchSettings, log: Callable[[str], None]
) -> None:
    """Make sure TradingView is running with the CDP port; restart if needed."""
    if sys.platform != "darwin":
        raise TradingViewAppError("App 模式僅支援 macOS")
    port = settings.app_cdp_port
    if cdp_up(port):
        return
    if not TV_APP_PATH.exists():
        raise AppNotInstalledError(str(TV_APP_PATH))
    if app_running():
        # App 在跑但沒帶 debug port → 乾淨結束（app 會自己還原視窗/分頁）再重啟。
        log(f"【版面分組｜App】TradingView 未帶 debug port {port}，自動重啟中…")
        await _run(
            ["/usr/bin/osascript", "-e", f'tell application id "{TV_BUNDLE_ID}" to quit'],
            timeout=30.0,
        )
        for _ in range(40):
            if not app_running():
                break
            await asyncio.sleep(0.5)
        if app_running():
            raise TradingViewAppError("無法結束 TradingView（可能有未回應的對話框），請手動處理後重試")
    cp = await _run(
        ["/usr/bin/open", "-a", "TradingView", "--args", f"--remote-debugging-port={port}"]
    )
    if cp.returncode != 0:
        raise TradingViewAppError((cp.stderr or "").strip() or "無法啟動 TradingView app")
    log("【版面分組｜App】啟動 TradingView（含 debug port），等待就緒…")
    for _ in range(80):
        if cdp_up(port):
            await asyncio.sleep(2.0)  # 首個視窗/分頁 target 需要一點時間註冊
            return
        await asyncio.sleep(0.5)
    raise AppCDPUnavailableError(
        f"TradingView CDP {port} 未就緒。請手動執行：\n"
        f'open -a TradingView --args --remote-debugging-port={port}'
    )


# ---------- raw CDP calls (only stdlib + websockets) ----------

async def _ws_send(ws_url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    import websockets  # yfinance 相依已帶入；requirements.txt 亦明列

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise TradingViewAppError(f"CDP {method} 失敗：{msg['error']}")
                return msg.get("result", {})


async def _evaluate(ws_url: str, expression: str) -> None:
    await _ws_send(
        ws_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True}
    )


async def _dispatch_cmd_n(ws_url: str) -> None:
    for typ in ("rawKeyDown", "keyUp"):
        await _ws_send(
            ws_url,
            "Input.dispatchKeyEvent",
            {
                "type": typ,
                "key": "n",
                "code": "KeyN",
                "windowsVirtualKeyCode": 78,
                "nativeVirtualKeyCode": 78,
                "modifiers": 4,  # Meta/Cmd
            },
        )


def _js_quote(url: str) -> str:
    return json.dumps(url)


# ---------- group open ----------

async def open_group_in_app(
    urls: list[str],
    settings: AppLaunchSettings,
    log: Callable[[str], None],
) -> None:
    """Open one group: Cmd+N → navigate default tab → window.open the rest."""
    if not urls:
        return
    await ensure_app_cdp(settings, log)
    port = settings.app_cdp_port

    pages = _page_targets(port)
    anchor = next(
        (
            t
            for t in pages
            if _CHART_URL_FRAG in (t.get("url") or "") or _NEW_TAB_URL_FRAG in (t.get("url") or "")
        ),
        None,
    )
    if anchor is None:
        raise TradingViewAppError("TradingView 沒有可用的視窗分頁（請先開一個圖表視窗）")

    before_ids = {
        t["id"] for t in pages if _NEW_TAB_URL_FRAG in (t.get("url") or "")
    }
    await _dispatch_cmd_n(anchor["webSocketDebuggerUrl"])

    new_tab = None
    for _ in range(30):
        await asyncio.sleep(0.5)
        candidates = [
            t
            for t in _page_targets(port)
            if _NEW_TAB_URL_FRAG in (t.get("url") or "") and t["id"] not in before_ids
        ]
        if candidates:
            new_tab = candidates[0]
            break
    if new_tab is None:
        raise TradingViewAppError("Cmd+N 後未出現新視窗的預設分頁（new-tab target）")
    await asyncio.sleep(settings.delay_new_window_ms / 1000)

    # 第一個版面：直接把新視窗的預設分頁導航過去（重用、不留空分頁）。
    # 注意 file:// → https 是跨行程導航，CDP targetId 會換 —— 導航後用
    # URL 片段（優先）或「新出現的 chart target」（備援）重新找到這個分頁。
    before_chart_ids = {
        t["id"] for t in pages if _CHART_URL_FRAG in (t.get("url") or "")
    }
    url_frag = urls[0].split("tradingview.com", 1)[-1].split("?", 1)[0].rstrip("/")
    log(f"【版面分組｜App】投遞 1/{len(urls)}：{urls[0]}")
    await _evaluate(
        new_tab["webSocketDebuggerUrl"], f"location.href = {_js_quote(urls[0])}"
    )
    first_tab_id: str | None = None
    for _ in range(40):
        await asyncio.sleep(0.5)
        charts = [
            t for t in _page_targets(port) if _CHART_URL_FRAG in (t.get("url") or "")
        ]
        if len(url_frag) > len("/chart"):
            hit = next((t for t in charts if url_frag in (t.get("url") or "")), None)
            if hit is not None:
                first_tab_id = hit["id"]
                break
        fresh = [t for t in charts if t["id"] not in before_chart_ids]
        if fresh:
            first_tab_id = fresh[0]["id"]
            break
    if first_tab_id is None:
        raise TradingViewAppError(f"第一個版面導航逾時：{urls[0]}")

    # 其餘版面：從第一個分頁 window.open → app 攔截成同視窗的新分頁。
    for i, url in enumerate(urls[1:], start=2):
        await asyncio.sleep(settings.delay_per_url_ms / 1000)
        cur = next((t for t in _page_targets(port) if t["id"] == first_tab_id), None)
        if cur is None:
            raise TradingViewAppError("群組第一個分頁被關閉，無法繼續投遞")
        log(f"【版面分組｜App】投遞 {i}/{len(urls)}：{url}")
        await _evaluate(
            cur["webSocketDebuggerUrl"], f"window.open({_js_quote(url)})"
        )


async def open_groups_in_app(
    groups: list[tuple[str, list[str]]],
    settings: AppLaunchSettings,
    log: Callable[[str], None],
) -> None:
    for name, urls in groups:
        if not urls:
            log(f"【版面分組｜App】群組「{name}」沒有版面，略過")
            continue
        log(f"【版面分組｜App】開啟群組「{name}」（{len(urls)} 個版面 → 1 視窗）")
        await open_group_in_app(urls, settings, log)


def main(argv: list[str] | None = None) -> int:
    """CLI probe：不開 GUI 直接驗證 App 模式。

    python -m gex_suite.modules.tradingview.app_launcher \
        https://www.tradingview.com/chart/XXXX/ https://www.tradingview.com/chart/YYYY/
    """
    parser = argparse.ArgumentParser(description="TradingView app CDP delivery probe")
    parser.add_argument("--port", type=int, default=DEFAULT_APP_CDP_PORT)
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args(argv)

    settings = AppLaunchSettings(app_cdp_port=args.port)
    try:
        asyncio.run(open_group_in_app(list(args.urls), settings, print))
    except TradingViewAppError as exc:
        print(f"FAILED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    print("done — 新視窗應包含所有版面分頁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
