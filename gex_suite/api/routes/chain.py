"""Chain (scraper + import + paste) control endpoints.

The full chain runs as an OS process spawned by launchd (scheduled) or by
POST /chain/start (manual). State lives in chain_state.json which the chain
process writes; this module reads it (plus liveness-checks pid) to answer
GET /chain/status, and uses pgid to SIGTERM the whole tree on /chain/stop.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from gex_suite.shared.paths import (
    CHAIN_STATE_PATH,
    CHAIN_STOP_FLAG_PATH,
    PROJECT_ROOT,
    ensure_dirs,
)

from ..models import (
    ChainStatus,
    ChainStepStatus,
    ChainStopResponse,
    ChainTriggerResponse,
)

logger = logging.getLogger("gex_suite.api.chain")

router = APIRouter(prefix="/chain", tags=["chain"])

# /Users/jeff/ai-agent — the repo root that contains both gex-suite and tools/.
AI_AGENT_ROOT = PROJECT_ROOT.parent
# launch.sh = launchd-scheduled (--use-settings-schedule, sleeps until schedule_time)
# launch_now.sh = manual /chain/start (runs immediately)
CHAIN_LAUNCH_NOW_SH = AI_AGENT_ROOT / "tools" / "gex_chain" / "launch_now.sh"


def _read_chain_state() -> Optional[Dict[str, Any]]:
    if not CHAIN_STATE_PATH.exists():
        return None
    try:
        return json.loads(CHAIN_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"chain_state.json read failed: {exc}")
        return None


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it; treat as alive.
        return True
    except Exception:
        return False


@router.get("/status", response_model=ChainStatus)
def chain_status() -> ChainStatus:
    state = _read_chain_state()
    if not state:
        return ChainStatus(running=False, status="idle", message="尚未跑過任何 chain")
    pid = state.get("pid")
    declared_status = str(state.get("status", "")).strip()
    alive = _is_pid_alive(pid) if declared_status == "running" else False
    # If chain_state claims running but PID is dead, mark as failed-stale.
    effective_status = declared_status
    message = None
    if declared_status == "running" and not alive:
        effective_status = "failed"
        message = "process not alive (chain may have crashed without writing final state)"
    per_step = [
        ChainStepStatus(
            name=s.get("name", "?"),
            status=str(s.get("status", "?")),
            detail=str(s.get("detail", "")),
            elapsed_seconds=s.get("elapsed_seconds"),
        )
        for s in (state.get("per_step") or [])
    ]
    return ChainStatus(
        running=(effective_status == "running"),
        status=effective_status or "idle",
        pid=pid,
        pgid=state.get("pgid"),
        started_at=state.get("started_at"),
        finished_at=state.get("finished_at"),
        elapsed_seconds=state.get("elapsed_seconds"),
        mode_summary=state.get("mode_summary"),
        steps=list(state.get("steps") or []),
        current_step=state.get("current_step"),
        per_step=per_step,
        success=state.get("success"),
        message=message,
    )


@router.post("/start", response_model=ChainTriggerResponse)
def chain_start() -> ChainTriggerResponse:
    state = _read_chain_state()
    if state and str(state.get("status")) == "running" and _is_pid_alive(state.get("pid")):
        return ChainTriggerResponse(
            status="already_running",
            pid=state.get("pid"),
            message=f"chain 已在執行中 (pid={state.get('pid')})",
        )
    if not CHAIN_LAUNCH_NOW_SH.exists():
        return ChainTriggerResponse(
            status="error", message=f"launch_now.sh not found at {CHAIN_LAUNCH_NOW_SH}"
        )
    ensure_dirs()
    # Drop any stale stop flag.
    try:
        if CHAIN_STOP_FLAG_PATH.exists():
            CHAIN_STOP_FLAG_PATH.unlink()
    except Exception:
        pass
    # Spawn as a new session so /chain/stop can kill the whole process tree
    # via killpg. launch.sh handles env (caffeinate, webhook, redirects).
    try:
        proc = subprocess.Popen(
            ["/bin/zsh", str(CHAIN_LAUNCH_NOW_SH)],
            cwd=str(AI_AGENT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        return ChainTriggerResponse(status="error", message=f"failed to spawn chain: {exc}")
    return ChainTriggerResponse(
        status="started", pid=proc.pid,
        message=f"chain 已啟動 (wrapper pid={proc.pid}); 真正執行 pid 會寫入 chain_state.json",
    )


@router.post("/stop", response_model=ChainStopResponse)
def chain_stop() -> ChainStopResponse:
    state = _read_chain_state()
    if not state or str(state.get("status")) != "running":
        return ChainStopResponse(success=False, message="目前沒有 chain 在執行")
    pid = state.get("pid")
    pgid = state.get("pgid") or (os.getpgid(pid) if pid else None)
    if not _is_pid_alive(pid):
        return ChainStopResponse(
            success=False,
            message=f"chain_state 標示 running 但 pid={pid} 已不存在；可能已崩潰",
        )
    # Also write a stop flag so the orchestrator can short-circuit cleanly
    # if it happens to be between subprocess.run() calls.
    try:
        ensure_dirs()
        CHAIN_STOP_FLAG_PATH.write_text("1", encoding="utf-8")
    except Exception:
        pass
    target = pgid or pid
    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
            method = f"killpg(pgid={pgid}, SIGTERM)"
        else:
            os.kill(pid, signal.SIGTERM)
            method = f"kill(pid={pid}, SIGTERM)"
        return ChainStopResponse(success=True, message=f"已送出 SIGTERM（{method}）")
    except ProcessLookupError:
        return ChainStopResponse(
            success=False, message=f"process group not found (target={target})",
        )
    except Exception as exc:
        return ChainStopResponse(success=False, message=f"kill failed: {exc}")
