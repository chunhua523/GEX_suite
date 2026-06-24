"""TradingView paste retry endpoints.

The daily chain's paste step (and any manual all/urls-scope scan) records the
distinct chart-page URLs that failed into ``last_scan_failed.json`` via the
tradingview CLI. These endpoints let the Discord bot re-scan exactly those
failed pages (`/paste retry-failed`) using the CLI's ``--layout-url`` mode,
which is cache-aware (already-filled cells skip) and much faster than a full
re-run.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter

from gex_suite.shared.paths import (
    PROJECT_ROOT,
    TRADINGVIEW_LAST_FAILED_PATH,
)

logger = logging.getLogger("gex_suite.api.paste")

router = APIRouter(prefix="/paste", tags=["paste"])

# Guards against two overlapping retries (each drives the shared CDP browser).
_retry_lock = threading.Lock()
_DEFAULT_WEEKS = "last_4_weeks"
# Cap a stuck retry; a few pages normally finish in well under a minute each.
_RETRY_TIMEOUT_SEC = 900


def _read_last_failed() -> Dict[str, Any]:
    if not TRADINGVIEW_LAST_FAILED_PATH.exists():
        return {"failed_count": 0, "urls": [], "failed": [], "generated_at": None}
    try:
        return json.loads(TRADINGVIEW_LAST_FAILED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"last_scan_failed.json read failed: {exc}")
        return {"failed_count": 0, "urls": [], "failed": [], "generated_at": None}


@router.get("/status")
def paste_status() -> Any:
    """Last recorded failed pages + whether a retry is currently running."""
    record = _read_last_failed()
    return {
        "failed_count": int(record.get("failed_count") or 0),
        "urls": record.get("urls") or [],
        "failed": record.get("failed") or [],
        "generated_at": record.get("generated_at"),
        "weeks": record.get("weeks"),
        "retry_running": _retry_lock.locked(),
    }


@router.post("/retry-failed")
def paste_retry_failed() -> Any:
    """Re-scan exactly the pages that failed in the last all/urls-scope run.

    Synchronous: a handful of pages finish quickly and Discord's deferred reply
    gives ample time, so the caller gets the concrete outcome back directly.
    """
    record = _read_last_failed()
    urls: List[str] = [u for u in (record.get("urls") or []) if u]
    if not urls:
        return {"status": "no_failed", "failed_count": 0, "attempted": 0}

    if not _retry_lock.acquire(blocking=False):
        return {"status": "already_running", "attempted": 0, "failed_count": len(urls)}

    try:
        weeks = record.get("weeks") or _DEFAULT_WEEKS
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="paste_retry_", delete=False
        ) as tmp:
            result_json = Path(tmp.name)

        cmd = [sys.executable, "-m", "gex_suite.modules.tradingview.cli", "--weeks", weeks]
        for u in urls:
            cmd.extend(["--layout-url", u])
        cmd.extend(["--result-json", str(result_json)])

        logger.info(f"paste retry-failed: {len(urls)} page(s), weeks={weeks}")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=_RETRY_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "attempted": len(urls),
                "message": f"retry 逾時（>{_RETRY_TIMEOUT_SEC}s）",
            }

        data: Dict[str, Any] = {}
        try:
            if result_json.exists():
                data = json.loads(result_json.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"retry result json parse failed: {exc}")
        finally:
            try:
                result_json.unlink(missing_ok=True)
            except Exception:
                pass

        if not data.get("ok") and proc.returncode not in (0, 1):
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            return {
                "status": "error",
                "attempted": len(urls),
                "message": tail or f"CLI exit={proc.returncode}",
            }

        # CLI已重寫 last_scan_failed.json → 讀回剩餘仍失敗的頁面
        still = _read_last_failed()
        return {
            "status": "done",
            "attempted": len(urls),
            "total": data.get("total"),
            "done": data.get("done"),
            "skipped": data.get("skipped"),
            "failed": data.get("failed"),
            "still_failed_count": int(still.get("failed_count") or 0),
            "still_failed_urls": still.get("urls") or [],
            "run_log": data.get("run_log"),
        }
    finally:
        _retry_lock.release()
