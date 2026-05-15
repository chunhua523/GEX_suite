"""Scraper control / schedule / status endpoints.

Replaces the legacy tools/gex_reader/api.py (which targeted the retired
gex-scraper project). All filesystem state lives under
gex_suite/data/scraper/, using the constants in gex_suite.shared.paths.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from gex_suite.shared.paths import (
    SCRAPER_DATA_DIR,
    SCRAPER_LAST_RESULT_PATH,
    SCRAPER_LOG_DIR,
    SCRAPER_SETTINGS_PATH,
    SCRAPER_STATE_PATH,
    SCRAPER_STOP_FLAG_PATH,
    ensure_dirs,
)

from ..models import (
    CancelResponse,
    LoginStatus,
    RetryResponse,
    ScheduleStatus,
    ScheduleUpdateResponse,
    ScraperStatus,
    SetTimeRequest,
    TriggerResponse,
)

logger = logging.getLogger("gex_suite.api.scraper")

router = APIRouter(prefix="/scraper", tags=["scraper"])


# ---------------------------------------------------------------------------
# Process-local state. Single FastAPI process, so plain module globals are OK.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_running: bool = False
_started_at: Optional[str] = None
_mode: Optional[str] = None
_last_result: Optional[Dict[str, Any]] = None
_last_failed_tasks: List[Dict[str, Any]] = []
_stop_summary_pending: bool = False
_login_cache: Dict[str, Any] = {
    "checked_at": 0.0,
    "data": {"status": "unknown", "reason": "not_checked"},
}
_LOGIN_TTL_SECONDS = 20

# Used by /scraper/schedule/time HH:MM validation.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------------------
# settings.json helpers
# ---------------------------------------------------------------------------

def _load_settings() -> Dict[str, Any]:
    if not SCRAPER_SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SCRAPER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"settings.json load failed: {exc}")
        return {}


def _save_settings(settings: Dict[str, Any]) -> None:
    ensure_dirs()
    SCRAPER_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# last_result.json persistence
# ---------------------------------------------------------------------------

def _save_last_result_to_disk() -> None:
    try:
        ensure_dirs()
        SCRAPER_LAST_RESULT_PATH.write_text(
            json.dumps(
                {"last_result": _last_result, "failed_tasks": _last_failed_tasks},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"last_result.json write failed: {exc}")


def _load_last_result_from_disk() -> None:
    global _last_result, _last_failed_tasks
    if not SCRAPER_LAST_RESULT_PATH.exists():
        return
    try:
        payload = json.loads(SCRAPER_LAST_RESULT_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            _last_result = payload.get("last_result")
            failed = payload.get("failed_tasks")
            _last_failed_tasks = failed if isinstance(failed, list) else []
    except Exception as exc:
        logger.warning(f"last_result.json read failed: {exc}")


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def _webhook_url() -> str:
    return (os.environ.get("DISCORD_NOTIFY_WEBHOOK") or "").strip()


def _send_webhook(message: str) -> Dict[str, Any]:
    url = _webhook_url()
    if not url:
        logger.info("[Webhook] DISCORD_NOTIFY_WEBHOOK not set; skipping notify")
        return {"ok": False, "error": "webhook_not_configured"}
    try:
        # curl is more reliable than urllib for some Discord SSL quirks.
        payload = json.dumps({"content": message}, ensure_ascii=False)
        cmd = [
            "/usr/bin/curl",
            "-sS",
            "-o", "/tmp/gex_suite_webhook_body.txt",
            "-w", "%{http_code}",
            "-H", "Content-Type: application/json",
            "-d", payload,
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        status_text = (result.stdout or "").strip()
        status = int(status_text) if status_text.isdigit() else None
        if status in (200, 204):
            return {"ok": True, "status": status}
        logger.warning(f"[Webhook] failed status={status} stderr={(result.stderr or '').strip()[:200]}")
        return {"ok": False, "status": status}
    except Exception as exc:
        logger.warning(f"[Webhook] error: {exc}")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _fmt_ts(raw: Any) -> str:
    if not raw:
        return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S") + " TW"
    s = str(raw).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TW_TZ).strftime("%Y-%m-%d %H:%M:%S") + " TW"
    except Exception:
        return s


def _format_result_text(result: Dict[str, Any]) -> str:
    """Render JOB SUMMARY block. Mirrors legacy api._format_scraper_result."""
    failed = result.get("failed_tasks", []) or []
    failed_count = int(result.get("failed_count", result.get("initial_failed_count", len(failed)) or 0))
    success_count = int(result.get("success_count", 0) or 0)
    total_processed = int(result.get("total_processed", success_count + failed_count) or 0)
    sep = "━" * 22
    lines = [
        sep,
        "📊 JOB SUMMARY",
        "```",
        f"Timestamp       : {_fmt_ts(result.get('finished_at'))}",
        f"Mode            : {result.get('mode', '-')}",
        f"Elapsed         : {result.get('elapsed_seconds', '-')} 秒",
        f"Total Processed : {total_processed}",
        f"Success ✅      : {success_count}",
        f"Failed  ❌      : {failed_count}",
    ]
    if failed:
        reasons = []
        for x in failed:
            r = (x.get("reason") or "").strip()
            if r and r not in reasons:
                reasons.append(r)
        if reasons:
            lines.append(f"Failed Reason   : {', '.join(reasons[:3])}")
        preview = ", ".join([f"{x.get('model')}/{x.get('ticker')}" for x in failed[:5]])
        if len(failed) > 5:
            preview += f" ... 共 {len(failed)} 筆"
        lines.append(f"Failed Items    : {preview}")
    if failed_count > 0:
        lines.append("Retry Command   : /scraper retry-failed")
    lines.append("```")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Login status (subprocess for Playwright isolation from FastAPI's loop)
# ---------------------------------------------------------------------------

_LOGIN_CHECK_SCRIPT = r"""
import asyncio, json, os, sys
from playwright.async_api import async_playwright

BASE_URL = "https://www.lietaresearch.com/platform"
BRAVE_PATH = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

async def main():
    state_path = sys.argv[1]
    browser_pref = (sys.argv[2] if len(sys.argv) > 2 else "brave").lower()
    out = {"status": "unknown", "reason": "unclassified"}
    pw = await async_playwright().start()
    browser = None
    try:
        launch_args = {"headless": True}
        if browser_pref == "brave" and os.path.exists(BRAVE_PATH):
            launch_args["executable_path"] = BRAVE_PATH
        else:
            launch_args["channel"] = "chrome"
        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(storage_state=state_path)
        page = await context.new_page()
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")
        for _ in range(20):
            if (await page.get_by_text("Select model", exact=False).count() > 0
                or await page.get_by_text("選擇模型", exact=False).count() > 0):
                out = {"status": "valid", "reason": "platform_ready"}
                break
            body = await page.evaluate("() => document.body.innerText")
            lowered = body.lower()
            if ("login" in lowered) or ("log in" in lowered) or ("sign in" in lowered) or ("登入" in body) or ("掌握數據" in body):
                out = {"status": "expired", "reason": "login_required"}
                break
            await asyncio.sleep(0.5)
        else:
            out = {"status": "unknown", "reason": "platform_not_ready_within_10s"}
        await context.close()
    except Exception as e:
        msg = str(e).strip().replace("\n", " ")[:160]
        out = {"status": "unknown", "reason": f"check_error:{type(e).__name__}:{msg}"}
    finally:
        if browser:
            await browser.close()
        await pw.stop()
    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
"""


def _check_login_status(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and (now - float(_login_cache.get("checked_at", 0.0)) < _LOGIN_TTL_SECONDS):
        return dict(_login_cache.get("data", {}))

    if not SCRAPER_STATE_PATH.exists():
        data = {"status": "missing_state", "reason": "state.json_not_found"}
        _login_cache["checked_at"] = now
        _login_cache["data"] = data
        return data

    settings = _load_settings()
    browser_pref = str(settings.get("browser", "brave") or "brave").strip().lower()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _LOGIN_CHECK_SCRIPT, str(SCRAPER_STATE_PATH), browser_pref],
            capture_output=True, text=True, timeout=20,
        )
        raw = (proc.stdout or "").strip()
        if raw:
            data = json.loads(raw)
        elif proc.returncode != 0:
            err = (proc.stderr or "").strip().replace("\n", " ")[:180]
            data = {"status": "unknown", "reason": f"check_subprocess_failed:{proc.returncode}:{err or 'no_stderr'}"}
        else:
            data = {"status": "unknown", "reason": "empty_output"}
        if not isinstance(data, dict):
            data = {"status": "unknown", "reason": "invalid_output"}
    except subprocess.TimeoutExpired:
        data = {"status": "unknown", "reason": "check_timeout"}
    except Exception as exc:
        data = {"status": "unknown", "reason": f"check_failed:{type(exc).__name__}"}

    _login_cache["checked_at"] = now
    _login_cache["data"] = data
    return data


# ---------------------------------------------------------------------------
# Scraper execution (in-process, thread + asyncio.run)
# ---------------------------------------------------------------------------

def _build_run_params(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compute kwargs for cli.run_scraper from settings.json. Returns None if invalid."""
    from gex_suite.modules.scraper.cli import (
        DEFAULT_SETTINGS,
        get_tickers_for_groups,
    )
    merged = dict(DEFAULT_SETTINGS)
    merged.update(settings or {})

    ticker_fp = merged.get("ticker_filepath", "")
    if not ticker_fp or not os.path.exists(ticker_fp):
        logger.error(f"ticker_filepath invalid: {ticker_fp}")
        return None
    download = merged.get("download_folder", "")
    if not download or not os.path.isdir(download):
        logger.error(f"download_folder invalid: {download}")
        return None
    if not SCRAPER_STATE_PATH.exists():
        logger.error("state.json missing; log in via GUI first")
        return None

    tickers = get_tickers_for_groups(ticker_fp, [])
    cme_fp = merged.get("cme_ticker_filepath", "")
    cme_tickers = get_tickers_for_groups(cme_fp, []) if cme_fp and os.path.exists(cme_fp) else []

    return {
        "tickers": tickers,
        "models": merged.get("selected_models", []),
        "cme_tickers": cme_tickers,
        "cme_models": merged.get("selected_cme_models", []),
        "download_folder": download,
        "parallel": merged.get("parallel", True),
        "headless": True,
    }


def _scraper_thread(mode: str, retry_failed_tasks: Optional[List[Dict[str, Any]]] = None) -> None:
    """Run scraper to completion in its own thread (with its own asyncio loop)."""
    global _running, _started_at, _mode, _last_result, _last_failed_tasks, _stop_summary_pending

    from gex_suite.modules.scraper.cli import (
        run_retry_only,
        run_scraper,
        setup_logging,
    )

    with _lock:
        if _running:
            logger.warning("scraper already running; thread aborting")
            return
        _running = True
        _started_at = datetime.now(timezone.utc).isoformat()
        _mode = mode
        _last_result = None
        if retry_failed_tasks is None:
            _last_failed_tasks = []

    _send_webhook(f"🚀 GEX Scraper 開始執行（mode={mode}）")

    # Clear any stale stop flag.
    try:
        if SCRAPER_STOP_FLAG_PATH.exists():
            SCRAPER_STOP_FLAG_PATH.unlink()
    except Exception:
        pass

    started = datetime.now()
    cli_logger = setup_logging()
    try:
        if retry_failed_tasks is not None:
            params = _build_run_params(_load_settings())
            if params is None:
                raise RuntimeError("settings validation failed (see logs)")
            result = asyncio.run(run_retry_only(
                failed_tasks=retry_failed_tasks,
                download_folder=params["download_folder"],
                parallel=params["parallel"],
                headless=True,
                logger=cli_logger,
            ))
        else:
            params = _build_run_params(_load_settings())
            if params is None:
                raise RuntimeError("settings validation failed (see logs)")
            result = asyncio.run(run_scraper(
                tickers=params["tickers"],
                models=params["models"],
                cme_tickers=params["cme_tickers"],
                cme_models=params["cme_models"],
                download_folder=params["download_folder"],
                parallel=params["parallel"],
                headless=True,
                logger=cli_logger,
            ))

        elapsed = (datetime.now() - started).total_seconds()
        final_failed = result.get("retry_failed_tasks") or result.get("initial_failed_tasks", [])
        summary = {
            "success": len(final_failed) == 0,
            "elapsed_seconds": round(elapsed, 2),
            "initial_failed_count": len(result.get("initial_failed_tasks", [])),
            "retry_failed_count": len(result.get("retry_failed_tasks", [])),
            "failed_tasks": final_failed,
            "failed_count": len(final_failed),
            "success_count": int(result.get("success_count", 0)),
            "total_processed": int(result.get("total_processed", 0)),
            "retried": bool(result.get("retried")),
            "retry_only": bool(result.get("retry_only")),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
        }
        _last_failed_tasks = final_failed
        formatted = _format_result_text(summary)
        _last_result = {
            "exit_code": 0,
            "finished_at": summary["finished_at"],
            "success": summary["success"],
            "mode": mode,
            "result": summary,
            "result_text": formatted,
        }
        _save_last_result_to_disk()
        if _stop_summary_pending:
            _send_webhook(f"🛑 已收到 STOP 指令，任務已結束\n{formatted}")
            _stop_summary_pending = False
        else:
            _send_webhook(f"✅ GEX Scraper 執行完成\n{formatted}")
    except Exception as exc:
        logger.exception("scraper thread crashed")
        _last_result = {
            "exit_code": -1,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "success": False,
            "mode": mode,
            "message": str(exc),
        }
        _save_last_result_to_disk()
        if _stop_summary_pending:
            _send_webhook(f"🛑 STOP 後任務異常結束：{exc}")
            _stop_summary_pending = False
        else:
            _send_webhook(f"❌ GEX Scraper 執行失敗：{exc}")
    finally:
        with _lock:
            _running = False


def _launch_scraper(mode: str, retry_failed_tasks: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Start scraper in a daemon thread. Returns False if already running."""
    with _lock:
        if _running:
            return False
    t = threading.Thread(
        target=_scraper_thread,
        args=(mode, retry_failed_tasks),
        daemon=True,
        name=f"scraper-{mode}",
    )
    t.start()
    return True


# ---------------------------------------------------------------------------
# Schedule worker
# ---------------------------------------------------------------------------

def _now_in_zone(tz_name: str) -> datetime:
    """Return current time in the named tz. Falls back to local on bad/empty input."""
    if not tz_name or tz_name.lower() == "local":
        return datetime.now()
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception as exc:
        logger.warning(f"[Schedule] invalid timezone '{tz_name}', falling back to local: {exc}")
        return datetime.now()


def _schedule_worker() -> None:
    """Mon-Fri, fires once when target-tz time == settings.schedule_time.

    Weekday + HH:MM + 'today' tracking all evaluated in schedule_timezone
    (defaults to 'local'). Set schedule_timezone='America/New_York' to peg
    runs to NY time and survive EDT/EST transitions automatically.
    """
    last_run_date = ""
    while True:
        try:
            settings = _load_settings()
            enabled = bool(settings.get("schedule_enabled", False))
            target = str(settings.get("schedule_time", "20:00")).strip()
            tz_name = str(settings.get("schedule_timezone", "local")).strip()
            now = _now_in_zone(tz_name)
            if enabled and now.weekday() <= 4 and now.strftime("%H:%M") == target:
                today = now.strftime("%Y-%m-%d")
                if today != last_run_date:
                    with _lock:
                        currently_running = _running
                    if not currently_running:
                        logger.info(f"[Schedule] firing at {target} ({tz_name or 'local'})")
                        last_run_date = today
                        _launch_scraper("settings")
        except Exception as exc:
            logger.warning(f"[Schedule] error: {exc}")
        time.sleep(10)


def start_schedule_worker() -> None:
    """Called once at FastAPI startup."""
    _load_last_result_from_disk()
    t = threading.Thread(target=_schedule_worker, daemon=True, name="scraper-schedule")
    t.start()
    logger.info("[Schedule] worker thread started")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/start", response_model=TriggerResponse)
def start_scraper() -> Any:
    if not _launch_scraper("settings"):
        return JSONResponse(
            status_code=409,
            content={"status": "already_running", "mode": "settings",
                     "message": "Scraper 正在執行中，請稍後再試"},
        )
    return TriggerResponse(status="started", mode="settings",
                           message="GEX Scraper（依 settings.json）已在背景啟動")


@router.post("/stop", response_model=CancelResponse)
def stop_scraper() -> Any:
    global _stop_summary_pending
    with _lock:
        running = _running
    if not running:
        return CancelResponse(success=False, message="Scraper 目前沒有在執行")
    try:
        _stop_summary_pending = True
        _send_webhook("🛑 已收到 STOP 指令，等待任務收尾並回報 JOB SUMMARY...")
        ensure_dirs()
        SCRAPER_STOP_FLAG_PATH.write_text("1", encoding="utf-8")
        return CancelResponse(success=True, message="Scraper 已送出 STOP（優雅停止，等待當前任務收尾）")
    except Exception as exc:
        return CancelResponse(success=False, message=f"停止失敗：{exc}")


@router.get("/status", response_model=ScraperStatus)
def status_scraper() -> ScraperStatus:
    settings = _load_settings()
    login = _check_login_status(force=False)
    schedule = ScheduleStatus(
        schedule_enabled=bool(settings.get("schedule_enabled", False)),
        schedule_time=str(settings.get("schedule_time", "20:00")),
        timezone=str(settings.get("schedule_timezone", "local")),
    )

    with _lock:
        running = _running
        mode = _mode
        started_at = _started_at
        last_result = _last_result
        failed_tasks = list(_last_failed_tasks)

    if running and started_at:
        status_text = f"Running ({mode}, since {_fmt_ts(started_at)})"
    elif last_result:
        status_text = "Completed" if last_result.get("success") else "Completed (with errors)"
    else:
        status_text = "Idle"

    last_result_text = last_result.get("result_text") if last_result else None

    return ScraperStatus(
        running=running,
        mode=mode,
        started_at=started_at,
        status_text=status_text,
        schedule=schedule,
        login_status=LoginStatus(**login) if {"status", "reason"} <= set(login.keys()) else LoginStatus(status="unknown", reason=str(login)),
        last_result=last_result,
        last_result_text=last_result_text,
        failed_tasks=failed_tasks,
        can_retry_failed=(len(failed_tasks) > 0 and not running),
    )


@router.post("/retry-failed", response_model=RetryResponse)
def retry_failed() -> Any:
    with _lock:
        running = _running
        failed = list(_last_failed_tasks)
    if running:
        return JSONResponse(status_code=409,
                            content={"status": "already_running", "mode": "retry-failed",
                                     "message": "Scraper 正在執行中，無法重試"})
    if not failed:
        return JSONResponse(status_code=400,
                            content={"status": "no_failed_tasks", "mode": "retry-failed",
                                     "message": "沒有可重試的失敗項目"})
    _launch_scraper("retry-failed", retry_failed_tasks=failed)
    return RetryResponse(status="started", mode="retry-failed", failed_count=len(failed))


@router.get("/schedule", response_model=ScheduleStatus)
def get_schedule() -> ScheduleStatus:
    s = _load_settings()
    return ScheduleStatus(
        schedule_enabled=bool(s.get("schedule_enabled", False)),
        schedule_time=str(s.get("schedule_time", "20:00")),
        timezone=str(s.get("schedule_timezone", "local")),
    )


@router.post("/schedule/enable", response_model=ScheduleUpdateResponse)
def enable_schedule() -> ScheduleUpdateResponse:
    s = _load_settings()
    s["schedule_enabled"] = True
    _save_settings(s)
    return ScheduleUpdateResponse(success=True, schedule_enabled=True)


@router.post("/schedule/disable", response_model=ScheduleUpdateResponse)
def disable_schedule() -> ScheduleUpdateResponse:
    s = _load_settings()
    s["schedule_enabled"] = False
    _save_settings(s)
    return ScheduleUpdateResponse(success=True, schedule_enabled=False)


@router.post("/schedule/time", response_model=ScheduleUpdateResponse)
def set_schedule_time(body: SetTimeRequest) -> Any:
    if not _HHMM_RE.match(body.time):
        raise HTTPException(status_code=400, detail="time must be HH:MM (24h)")
    s = _load_settings()
    s["schedule_time"] = body.time
    _save_settings(s)
    return ScheduleUpdateResponse(success=True, schedule_time=body.time)
