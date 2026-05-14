"""TradingView Auto-Paste headless CLI.

Reuses the existing ``TradingViewPage`` widget by instantiating it under
``QT_QPA_PLATFORM=offscreen``, then driving the same ``_phase_b_scan_flow``
that the GUI uses. This keeps logic in one place rather than re-implementing
the batch flow.

Examples::

    python -m gex_suite.modules.tradingview.cli --dry-run
    python -m gex_suite.modules.tradingview.cli --weeks this_week --result-json /tmp/r.json
    python -m gex_suite.modules.tradingview.cli --auto-launch-brave
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gex_suite.shared.paths import TRADINGVIEW_AUTO_PASTE_CONFIG_PATH

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
BRAVE_APP_PATH = "/Applications/Brave Browser.app"
BRAVE_CDP_USER_DATA_DIR = (
    Path(os.path.expanduser("~"))
    / "Library/Application Support/BraveSoftware/Brave-Browser-CDP"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GEX Suite TradingView auto-paste CLI")
    p.add_argument("--weeks", default="", choices=["", "this_week", "last_4_weeks"],
                   help="Override weeks_mode from auto_paste_config.json.")
    p.add_argument("--layout-scope", default="", choices=["", "all", "active"],
                   help="Override layout_scope from auto_paste_config.json.")
    p.add_argument("--ticker-scope", default="", choices=["", "all", "ticker"],
                   help="Override ticker_scope from config.")
    p.add_argument("--ticker", default="", help="Used when --ticker-scope=ticker.")
    p.add_argument("--cdp-url", default="", help=f"Defaults to {DEFAULT_CDP_URL} or config.")
    p.add_argument("--auto-launch-brave", action="store_true",
                   help="If CDP probe fails, start Brave with --remote-debugging-port "
                        "and wait up to --launch-timeout seconds before connecting.")
    p.add_argument("--launch-timeout", type=int, default=30,
                   help="Seconds to wait for Brave/CDP to become reachable.")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview only — no writes to TradingView or DB.")
    p.add_argument("--result-json", default="", help="Write BatchReport summary as JSON.")
    return p.parse_args()


def _load_config() -> dict:
    if TRADINGVIEW_AUTO_PASTE_CONFIG_PATH.exists():
        try:
            return json.loads(TRADINGVIEW_AUTO_PASTE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"⚠️  failed to read auto_paste_config.json: {exc}")
    return {}


def _probe_cdp(url: str, timeout: float = 2.0) -> bool:
    """Return True if CDP /json/version responds."""
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, socket.timeout):
        return False
    except Exception:
        return False


def _launch_brave_cdp(port: int) -> None:
    """Start Brave with remote debugging using a dedicated user-data-dir."""
    BRAVE_CDP_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not Path(BRAVE_APP_PATH).exists():
        raise SystemExit(f"❌ Brave not found at {BRAVE_APP_PATH}")
    cmd = [
        "open", "-na", BRAVE_APP_PATH, "--args",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={BRAVE_CDP_USER_DATA_DIR}",
    ]
    print(f"🚀 launching Brave (CDP profile {BRAVE_CDP_USER_DATA_DIR.name})")
    subprocess.run(cmd, check=True)


def _ensure_cdp(cdp_url: str, *, auto_launch: bool, timeout_seconds: int) -> bool:
    """Probe CDP; optionally launch Brave and wait. Return True if reachable."""
    if _probe_cdp(cdp_url):
        return True
    if not auto_launch:
        return False
    # Extract port from URL for launch
    try:
        port = int(cdp_url.rsplit(":", 1)[1].split("/")[0])
    except Exception:
        port = 9222
    _launch_brave_cdp(port)
    deadline = time.monotonic() + max(5, timeout_seconds)
    while time.monotonic() < deadline:
        if _probe_cdp(cdp_url):
            print(f"✅ CDP reachable on {cdp_url}")
            return True
        time.sleep(1)
    return False


def _build_options(config: dict, args: argparse.Namespace):
    """Construct BatchOptions from config defaults + CLI overrides."""
    from .engine import BatchOptions
    weeks = args.weeks or config.get("weeks_mode") or "this_week"
    layout_scope = args.layout_scope or config.get("layout_scope") or "all"
    ticker_scope = args.ticker_scope or config.get("ticker_scope") or "all"
    ticker = args.ticker or config.get("ticker") or None
    start_rules = config.get("start_time_rules") or {}
    return BatchOptions(
        layout_scope=layout_scope,  # type: ignore[arg-type]
        ticker_scope=ticker_scope,  # type: ignore[arg-type]
        ticker=ticker,
        weeks=weeks,  # type: ignore[arg-type]
        skip_filled_days=bool(config.get("skip_filled_days", True)),
        apply_visibility_preset=bool(config.get("apply_visibility_preset", True)),
        organize_indicators=bool(config.get("organize_indicators", True)),
        dry_run=args.dry_run,
        market_open_time=str(start_rules.get("default", "04:00")),
    )


def _make_offscreen_app():
    """Create a QApplication under offscreen platform (no display required)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0]] if sys.argv else [""])
    return app


def _create_widget_headless():
    """Instantiate TradingViewPage with stdout logging instead of UI log box."""
    from .widget import TradingViewPage
    page = TradingViewPage()

    captured: list[str] = []

    def _log(msg: str) -> None:
        text = (msg or "").rstrip()
        if text:
            print(text)
            captured.append(text)

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


def main() -> int:
    args = parse_args()
    config = _load_config()
    cdp_url = (args.cdp_url or config.get("cdp_url") or DEFAULT_CDP_URL).rstrip("/")

    if not _ensure_cdp(cdp_url, auto_launch=args.auto_launch_brave,
                       timeout_seconds=args.launch_timeout):
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

    start = time.monotonic()
    try:
        report = asyncio.run(page._phase_b_scan_flow(opts))
    except Exception as exc:
        elapsed = time.monotonic() - start
        msg = f"_phase_b_scan_flow raised: {type(exc).__name__}: {exc}"
        print(f"❌ {msg}")
        if args.result_json:
            Path(args.result_json).write_text(
                json.dumps({
                    "ok": False, "error": msg, "elapsed_seconds": round(elapsed, 2),
                    "log_tail": captured[-50:],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 1

    elapsed = time.monotonic() - start
    payload = _serialize_report(report, elapsed, captured)
    print(f"✅ TV batch done in {elapsed:.1f}s — total={payload['total']} "
          f"done={payload['done']} skipped={payload['skipped']} failed={payload['failed']}")

    if args.result_json:
        Path(args.result_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return 1 if payload.get("failed", 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
