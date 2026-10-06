"""TradingView Auto-Paste headless CLI.

Reuses the existing ``TradingViewPage`` widget by instantiating it under
``QT_QPA_PLATFORM=offscreen``, then driving the same ``_phase_b_scan_flow``
that the GUI uses. This keeps logic in one place rather than re-implementing
the batch flow.

Examples::

    python -m gex_suite.modules.tradingview.cli --dry-run
    python -m gex_suite.modules.tradingview.cli --weeks this_week --result-json /tmp/r.json
    python -m gex_suite.modules.tradingview.cli --auto-launch-brave   # uses config 'browser'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from gex_suite.modules.tradingview import browser_paths
from gex_suite.shared.paths import TRADINGVIEW_LAST_FAILED_PATH

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# Which browser is used is driven by auto_paste_config.json -> "browser"
# (chrome | brave), matching the GUI. Launch, persistent CDP profile (login
# survives runs), 50% zoom and full-screen window all live in browser_paths —
# shared with the GUI so the two can't drift apart.

# Back-compat alias (kept so any external import keeps working).
BRAVE_CDP_USER_DATA_DIR = browser_paths.cdp_profile_dir("brave")


def _normalize_browser(browser: str | None) -> str:
    return browser_paths.normalize_browser(browser)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GEX Suite TradingView auto-paste CLI")
    p.add_argument("--weeks", default="", choices=["", "this_week", "last_4_weeks"],
                   help="Override weeks_mode from auto_paste_config.json.")
    p.add_argument("--layout-scope", default="", choices=["", "all", "active"],
                   help="Override layout_scope from auto_paste_config.json.")
    p.add_argument("--layout-url", dest="layout_url", action="append", default=[],
                   metavar="URL_OR_CHARTID",
                   help="Scan ONLY these chart pages (repeatable). Accepts a full "
                        "TradingView chart URL or a bare /chart/<id>. Forces "
                        "layout-scope=urls; ideal for re-scanning pages that failed "
                        "in an earlier run. Cache-aware: already-filled cells skip.")
    p.add_argument("--ticker-scope", default="", choices=["", "all", "ticker"],
                   help="Override ticker_scope from config.")
    p.add_argument("--ticker", default="", help="Used when --ticker-scope=ticker.")
    p.add_argument("--cdp-url", default="", help=f"Defaults to {DEFAULT_CDP_URL} or config.")
    p.add_argument("--browser", default="", choices=["", "chrome", "brave"],
                   help="Override auto_paste_config.json 'browser'. Selects which "
                        "browser + dedicated CDP profile to (auto-)launch.")
    p.add_argument("--auto-launch-brave", dest="auto_launch_brave", action="store_true",
                   help="If CDP probe fails, start the configured browser "
                        "(auto_paste_config.json 'browser', or --browser) with "
                        "--remote-debugging-port and wait up to --launch-timeout "
                        "seconds before connecting.")
    p.add_argument("--launch-timeout", type=int, default=30,
                   help="Seconds to wait for the browser/CDP to become reachable.")
    p.add_argument("--keep-browser", action="store_true",
                   help="Do NOT shut down the auto-launched CDP browser when the "
                        "run finishes. Default is to close it: Chrome 151 wedges "
                        "(half-dead CDP) when an idle instance sits across screen-"
                        "off periods, so leaving one resident between runs is the "
                        "main exposure (2026-08-07).")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview only — no writes to TradingView or DB.")
    p.add_argument("--result-json", default="", help="Write BatchReport summary as JSON.")
    return p.parse_args()


def _load_config() -> dict:
    # Same loader as the GUI (defaults merged under the saved file).
    from gex_suite.shared import config as shared_config
    return shared_config.load_tradingview_config()


def _build_options(config: dict, args: argparse.Namespace):
    """Config + CLI overrides → BatchOptions via the mapping the GUI also uses."""
    from .engine import batch_options_from_config
    from .layout_groups import normalize_chart_url
    cfg = dict(config)
    for key, val in (
        ("weeks_mode", args.weeks),
        ("layout_scope", args.layout_scope),
        ("ticker_scope", args.ticker_scope),
        ("ticker", args.ticker),
    ):
        if val:
            cfg[key] = val
    layout_urls = [u for u in (normalize_chart_url(x) for x in (args.layout_url or [])) if u]
    return batch_options_from_config(cfg, layout_urls=layout_urls, dry_run=args.dry_run)


def _make_offscreen_app():
    """Create a QApplication under offscreen platform (no display required)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0]] if sys.argv else [""])
    return app


def _create_widget_headless():
    """Instantiate TradingViewPage with stdout logging instead of UI log box.

    Free-form _exec_log messages are tee'd to the disk run-log writer (when
    one is open) so CLI runs produce the same HTML log file as GUI runs.
    Structured _log_event calls already write to ``_run_log_writer`` inside
    ``_log_event_main_thread`` — we leave that path untouched.
    """
    from .widget import TradingViewPage
    page = TradingViewPage()

    captured: list[str] = []

    def _log(msg: str) -> None:
        text = (msg or "").rstrip()
        if not text:
            return
        print(text)
        captured.append(text)
        writer = getattr(page, "_run_log_writer", None)
        if writer is None:
            return
        # Mirror _dispatch_freeform semantics: split off the leading 【tag】,
        # infer severity, write the rest as detail.
        lines = text.splitlines()
        first = lines[0] if lines else ""
        rest = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        try:
            severity, tag = page._infer_severity(first)
            if tag and first.startswith(f"【{tag}】"):
                body = first[len(f"【{tag}】"):].lstrip()
            else:
                body = first
            writer.event(
                severity=severity,
                tag=tag or "info",
                text=body,
                detail=rest or None,
            )
        except Exception:
            pass

    # The widget's _exec_log marshals to the Qt main thread via a signal; both
    # paths funnel through _exec_log_main_thread. Override both to short-circuit
    # the signal hop (no Qt event loop is spinning in CLI mode).
    page._exec_log = _log  # type: ignore[assignment]
    page._exec_log_main_thread = _log  # type: ignore[assignment]
    page._exec_log_clear = lambda: captured.clear()  # type: ignore[assignment]
    return page, captured


def _serialize_report(report: Any, elapsed: float, captured_log: list[str]) -> dict:
    items_out = []
    for r in getattr(report, "items", []) or []:
        item = getattr(r, "item", None)
        items_out.append({
            "status": getattr(r, "status", "?"),
            "message": getattr(r, "message", ""),
            "ticker": getattr(item, "ticker", None),
            "monday": str(getattr(item, "monday", "") or ""),
            "layout_name": getattr(item, "layout_name", None),
            "subchart_index": getattr(item, "subchart_index", None),
            "subchart_symbol": getattr(item, "subchart_symbol", None),
            "chart_url": getattr(item, "chart_url", None),
        })
    return {
        "ok": True,
        "elapsed_seconds": round(elapsed, 2),
        "total": getattr(report, "total", 0),
        "done": getattr(report, "done", 0),
        "skipped": getattr(report, "skipped", 0),
        "failed": getattr(report, "failed", 0),
        "items": items_out,
        "log_tail": captured_log[-50:],
    }


def _record_last_scan_failed(payload: dict, opts: Any) -> None:
    """Persist the distinct chart-page URLs that failed this run so the
    /paste/retry-failed endpoint (Discord `/paste retry-failed`) can re-scan
    exactly those pages.

    Only written for whole-run scopes ("all" full daily run, or "urls" retry) —
    a narrow ``--ticker-scope ticker`` scan covers only one ticker, so letting
    it overwrite the record would drop the other pages that failed in the daily
    run. A retry run ("urls") DOES overwrite, by design: it scanned exactly the
    previously-failed pages, so its remaining-failed set is the new truth (and
    becomes empty when everything passes).
    """
    if getattr(opts, "ticker_scope", "all") == "ticker":
        return
    if getattr(opts, "dry_run", False):
        return  # 預覽不是真實覆蓋，不可覆寫 retry-failed 清單
    seen: set[str] = set()
    failed: list[dict] = []
    for it in payload.get("items", []):
        if it.get("status") != "failed":
            continue
        url = (it.get("chart_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        failed.append({
            "url": url,
            "layout_name": it.get("layout_name"),
            "ticker": it.get("ticker"),
            "monday": it.get("monday"),
            "message": it.get("message"),
        })
    for cl in payload.get("crash_layouts", []):
        url = (cl.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        failed.append({
            "url": url,
            "layout_name": cl.get("layout_name"),
            "ticker": None,
            "monday": None,
            "message": cl.get("message")
            or "renderer 崩潰，本輪尾端重試仍失敗（版面未掃）",
        })
    record = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "weeks": getattr(opts, "weeks", None),
        "layout_scope": getattr(opts, "layout_scope", None),
        "failed_count": len(failed),
        "urls": [f["url"] for f in failed],
        "failed": failed,
    }
    try:
        TRADINGVIEW_LAST_FAILED_PATH.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"⚠️ 寫入 last_scan_failed.json 失敗：{exc}")


def main() -> int:
    args = parse_args()
    config = _load_config()
    cdp_url = (args.cdp_url or config.get("cdp_url") or DEFAULT_CDP_URL).rstrip("/")
    browser = _normalize_browser(args.browser or config.get("browser"))
    print(f"🌐 browser={browser} (CDP profile {browser_paths.cdp_profile_dir(browser).name})")
    try:
        port = int(cdp_url.rsplit(":", 1)[1].split("/")[0])
    except Exception:
        port = 9222

    def _kill_browser() -> bool:
        """watchdog 用：只砍行程，讓阻塞中的 playwright 呼叫拋錯解除阻塞。"""
        return browser_paths.kill_cdp_browser(port)

    def _respawn_browser() -> bool:
        return browser_paths.respawn_cdp_browser(
            browser, port=port, timeout_sec=args.launch_timeout
        )

    # Same gate the GUI hits inside automator.connect(); here it runs up front so
    # "no browser and no --auto-launch-brave" exits 2 with a result-json error.
    if not browser_paths.ensure_cdp_browser(
        browser, port=port, auto_launch=args.auto_launch_brave,
        timeout_sec=args.launch_timeout,
    ):
        msg = f"CDP not reachable at {cdp_url}"
        print(f"❌ {msg}")
        if args.result_json:
            Path(args.result_json).write_text(
                json.dumps({"ok": False, "error": msg, "cdp_url": cdp_url},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 2

    app = _make_offscreen_app()  # noqa: F841 — must outlive widget
    page, captured = _create_widget_headless()

    if args.auto_launch_brave:
        # 無人值守 watchdog：流程凍住（半死瀏覽器，指令無人回應）→ 砍行程解除
        # 阻塞 → 版面迴圈的 crash guard 走 respawn 重連續跑。GUI 路徑不設定
        # 這些 hook，行為不變。
        page._browser_kill = _kill_browser
        page._browser_respawn = _respawn_browser
        try:
            page._watchdog_stall_seconds = int(
                config.get("watchdog_stall_seconds") or 300
            )
        except Exception:
            pass

    try:
        opts = _build_options(config, args)
    except Exception as exc:
        msg = f"failed to build BatchOptions: {exc}"
        print(f"❌ {msg}")
        if args.result_json:
            Path(args.result_json).write_text(
                json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 1

    print(f"▶️ TV batch: scope={opts.layout_scope} ticker_scope={opts.ticker_scope} "
          f"weeks={opts.weeks} dry_run={opts.dry_run} cdp={cdp_url}")
    if opts.layout_urls:
        print(f"   只掃 {len(opts.layout_urls)} 個指定版面：")
        for u in opts.layout_urls:
            print(f"     • {u}")

    ticker_for_title = (opts.ticker or "all") if opts.ticker_scope == "ticker" else "all"
    layout_for_title = (
        f"urls({len(opts.layout_urls)})" if opts.layout_urls else opts.layout_scope
    )
    log_kind = "scan_cli_dry" if opts.dry_run else "scan_cli"
    title_suffix = (
        f"ticker={ticker_for_title} weeks={opts.weeks} "
        f"layout={layout_for_title} dry_run={opts.dry_run}"
    )

    start = time.monotonic()
    keepawake = browser_paths.start_display_keepawake()
    try:
        # 整輪最多跑 2 次：第 1 次因 crash 類例外炸掉、或版面清單降級成
        # Current-only（開場就接到半死瀏覽器的典型症狀）時，重啟瀏覽器後
        # 重試一次。已完成的版面靠快取 skip，重試成本低且冪等。
        report = None
        log_path = None
        for attempt in (1, 2):
            page._begin_run_log(log_kind, title_suffix=title_suffix)
            log_path = getattr(page, "_latest_log_path", None)
            if log_path:
                print(f"📄 run log: {log_path}")
            try:
                report = asyncio.run(page._phase_b_scan_flow(opts))
            except Exception as exc:
                elapsed = time.monotonic() - start
                msg = f"_phase_b_scan_flow raised: {type(exc).__name__}: {exc}"
                print(f"❌ {msg}")
                page._end_run_log({"note": msg})
                from .automator import PlaywrightCDPAutomator
                if (attempt == 1 and args.auto_launch_brave
                        and PlaywrightCDPAutomator._is_crash_exc(exc)):
                    print("♻️ crash 類失敗——重啟瀏覽器後整輪重試一次")
                    if _respawn_browser():
                        continue
                if args.result_json:
                    Path(args.result_json).write_text(
                        json.dumps({
                            "ok": False, "error": msg,
                            "elapsed_seconds": round(elapsed, 2),
                            "log_tail": captured[-50:],
                            "run_log": str(log_path) if log_path else None,
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                return 1
            degraded_now = bool(
                opts.layout_scope == "all"
                and getattr(page, "_last_phase_b_layout_list_degraded", False)
            )
            if attempt == 1 and args.auto_launch_brave and degraded_now:
                page._end_run_log(
                    {"note": "版面清單降級（Current-only）——重啟瀏覽器後整輪重試"}
                )
                print("♻️ 版面清單降級——重啟瀏覽器後整輪重試一次")
                if _respawn_browser():
                    report = None
                    continue
            break

        elapsed = time.monotonic() - start
        payload = _serialize_report(report, elapsed, captured)
        if log_path:
            payload["run_log"] = str(log_path)
        # A scope=all run that fell back to Current-only covered almost nothing
        # — surface it in the result JSON (chain marks the step FAIL) and the
        # exit code, instead of masquerading as a tiny successful run.
        payload["layout_list_degraded"] = bool(
            opts.layout_scope == "all"
            and getattr(page, "_last_phase_b_layout_list_degraded", False)
        )
        # renderer 崩潰、本輪尾端重試仍失敗的版面（該版面未掃）：記進
        # last_scan_failed 供 /paste retry-failed 補掃，且整輪以失敗計。
        payload["crash_layouts"] = list(
            getattr(page, "_last_phase_b_crash_layouts", []) or []
        )
        _record_last_scan_failed(payload, opts)
        print(f"✅ TV batch done in {elapsed:.1f}s — total={payload['total']} "
              f"done={payload['done']} skipped={payload['skipped']} failed={payload['failed']}")
        if payload["layout_list_degraded"]:
            print("⚠️ 版面清單降級為僅 Current — 本輪幾乎沒有覆蓋，以失敗計（exit=1）")
        if payload["crash_layouts"]:
            names = ", ".join(
                str(c.get("layout_name") or c.get("url") or "?")
                for c in payload["crash_layouts"]
            )
            print(f"⚠️ renderer 崩潰未掃完的版面：{names} — 以失敗計（exit=1）")

        page._end_run_log({
            "total": payload["total"],
            "done": payload["done"],
            "skipped": payload["skipped"],
            "failed": payload["failed"],
            "note": f"CLI 執行（耗時 {elapsed:.1f}s）",
        })

        if args.result_json:
            Path(args.result_json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        if (payload.get("failed", 0) > 0 or payload.get("layout_list_degraded")
                or payload.get("crash_layouts")):
            return 1
        return 0
    finally:
        keepawake.set()
        # Belt-and-suspenders: ensure the writer is closed even if something
        # unexpected slipped past the inner handlers.
        if getattr(page, "_run_log_writer", None) is not None:
            try:
                page._end_run_log()
            except Exception:
                pass
        if args.auto_launch_brave and not args.keep_browser:
            # 兩輪 paste 之間不留常駐 CDP 瀏覽器：Chrome 151 熄屏／久駐 wedge
            # 的暴露面直接歸零。kill_cdp_browser 只殺帶 debug 旗標的行程，
            # 使用者日常瀏覽器不受影響。
            print("🧹 收掉 CDP 瀏覽器（--keep-browser 可保留）")
            try:
                browser_paths.kill_cdp_browser(port)
            except Exception as exc:
                print(f"⚠️ 收掉 CDP 瀏覽器失敗：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
