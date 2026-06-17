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

# Per-browser app bundle + a DEDICATED persistent CDP profile. The chain runs
# unattended, so it needs a profile whose TradingView login survives across runs
# (unlike the GUI's throwaway $TMPDIR/gex_tv_cdp_profile). Which browser is used
# is driven by auto_paste_config.json -> "browser" (chrome | brave), matching
# the GUI. The headless path historically hard-coded Brave and ignored the
# config, so a chrome-configured user got a Brave launch with no TV login.
_HOME = Path(os.path.expanduser("~"))
_BROWSER_APP_PATHS = {
    "brave": "/Applications/Brave Browser.app",
    "chrome": "/Applications/Google Chrome.app",
}
# The actual executable inside each bundle. We launch this directly (not via
# `open -na`) so the --remote-debugging-port reliably binds even when the user's
# normal browser instance is already running.
_BROWSER_BINARIES = {
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
}
_BROWSER_CDP_PROFILES = {
    "brave": _HOME / "Library/Application Support/BraveSoftware/Brave-Browser-CDP",
    "chrome": _HOME / "Library/Application Support/Google/Google-Chrome-CDP",
}
_DEFAULT_BROWSER = "chrome"

# Back-compat aliases (kept so any external import keeps working).
BRAVE_APP_PATH = _BROWSER_APP_PATHS["brave"]
BRAVE_CDP_USER_DATA_DIR = _BROWSER_CDP_PROFILES["brave"]


def _normalize_browser(browser: str | None) -> str:
    b = (browser or "").strip().lower()
    return b if b in _BROWSER_APP_PATHS else _DEFAULT_BROWSER


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


# Chrome zoom_level for exactly 50% page zoom == log(0.5)/log(1.2). Page zoom is
# stored per exact host, so we seed both the partition default (catch-all) and
# the specific TradingView hosts the automation loads, so dialogs (layout list /
# indicator settings) aren't clipped/obscured at the default 100% zoom.
_TV_ZOOM_LEVEL = -3.8017840169239308
_TV_ZOOM_HOSTS = ("tw.tradingview.com", "www.tradingview.com")


def _ensure_profile_zoom(profile: Path) -> None:
    """Pre-seed the profile's default + per-host page zoom to 50%. Chrome must be
    closed for this to stick, so only call right before launching (i.e. when the
    CDP probe already failed). Best-effort — never blocks the launch."""
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
        pref.parent.mkdir(parents=True, exist_ok=True)
        tmp = pref.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, pref)
    except Exception as exc:
        print(f"⚠️ could not pre-seed 50% zoom in {pref}: {exc}")


def _launch_browser_cdp(browser: str, port: int) -> None:
    """Start the configured browser with remote debugging on a dedicated,
    persistent user-data-dir (so the TradingView login survives across runs).

    Launches the binary directly (not ``open -na``) so the debug port reliably
    binds even when the user's normal browser is already running — this is what
    lets _ensure_cdp re-open a closed CDP browser on demand. start_new_session
    detaches it so it outlives this CLI process and is reusable across steps."""
    browser = _normalize_browser(browser)
    binary = _BROWSER_BINARIES[browser]
    profile = _BROWSER_CDP_PROFILES[browser]
    profile.mkdir(parents=True, exist_ok=True)
    _ensure_profile_zoom(profile)  # Chrome is down here (probe failed) → safe to write Preferences
    if not Path(binary).exists():
        raise SystemExit(f"❌ {browser} binary not found at {binary}")
    cmd = [
        binary,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
    ]
    print(f"🚀 launching {browser} (CDP profile {profile.name})")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _ensure_cdp(cdp_url: str, *, browser: str, auto_launch: bool,
                timeout_seconds: int) -> bool:
    """Probe CDP; optionally launch the configured browser and wait.
    Return True if reachable."""
    if _probe_cdp(cdp_url):
        return True
    if not auto_launch:
        return False
    # Extract port from URL for launch
    try:
        port = int(cdp_url.rsplit(":", 1)[1].split("/")[0])
    except Exception:
        port = 9222
    _launch_browser_cdp(browser, port)
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
        futures_quote_source=(
            str(config.get("futures_quote_source") or "yfinance").strip() or "yfinance"
        ),  # type: ignore[arg-type]
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
    browser = _normalize_browser(args.browser or config.get("browser"))
    print(f"🌐 browser={browser} (CDP profile {_BROWSER_CDP_PROFILES[browser].name})")

    if not _ensure_cdp(cdp_url, browser=browser, auto_launch=args.auto_launch_brave,
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

    ticker_for_title = (opts.ticker or "all") if opts.ticker_scope == "ticker" else "all"
    log_kind = "scan_cli_dry" if opts.dry_run else "scan_cli"
    page._begin_run_log(
        log_kind,
        title_suffix=(
            f"ticker={ticker_for_title} weeks={opts.weeks} "
            f"layout={opts.layout_scope} dry_run={opts.dry_run}"
        ),
    )
    log_path = getattr(page, "_latest_log_path", None)
    if log_path:
        print(f"📄 run log: {log_path}")

    start = time.monotonic()
    try:
        try:
            report = asyncio.run(page._phase_b_scan_flow(opts))
        except Exception as exc:
            elapsed = time.monotonic() - start
            msg = f"_phase_b_scan_flow raised: {type(exc).__name__}: {exc}"
            print(f"❌ {msg}")
            page._end_run_log({"note": msg})
            if args.result_json:
                Path(args.result_json).write_text(
                    json.dumps({
                        "ok": False, "error": msg, "elapsed_seconds": round(elapsed, 2),
                        "log_tail": captured[-50:],
                        "run_log": str(log_path) if log_path else None,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return 1

        elapsed = time.monotonic() - start
        payload = _serialize_report(report, elapsed, captured)
        if log_path:
            payload["run_log"] = str(log_path)
        print(f"✅ TV batch done in {elapsed:.1f}s — total={payload['total']} "
              f"done={payload['done']} skipped={payload['skipped']} failed={payload['failed']}")

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

        return 1 if payload.get("failed", 0) > 0 else 0
    finally:
        # Belt-and-suspenders: ensure the writer is closed even if something
        # unexpected slipped past the inner handlers.
        if getattr(page, "_run_log_writer", None) is not None:
            try:
                page._end_run_log()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
