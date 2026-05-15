"""Pydantic request / response schemas for the scraper API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SetTimeRequest(BaseModel):
    time: str = Field(..., description="24h HH:MM, e.g. '07:00'")


class TriggerResponse(BaseModel):
    status: str
    mode: str
    message: str


class CancelResponse(BaseModel):
    success: bool
    message: str


class ScheduleStatus(BaseModel):
    schedule_enabled: bool
    schedule_time: str
    timezone: str = "local"
    only_scraper: bool = False


class ScheduleModeRequest(BaseModel):
    mode: str = Field(..., description="'chain' (full pipeline) or 'scraper' (scraper-only)")


class ScheduleModeResponse(BaseModel):
    success: bool
    mode: str


class ScheduleUpdateResponse(BaseModel):
    success: bool
    schedule_enabled: Optional[bool] = None
    schedule_time: Optional[str] = None


class LoginStatus(BaseModel):
    status: str
    reason: str


class ScraperStatus(BaseModel):
    """One-shot status payload — bot calls this and gets everything it needs."""
    running: bool
    mode: Optional[str]
    started_at: Optional[str]
    status_text: str
    schedule: ScheduleStatus
    login_status: LoginStatus
    last_result: Optional[Dict[str, Any]] = None
    last_result_text: Optional[str] = None
    failed_tasks: List[Dict[str, Any]] = []
    can_retry_failed: bool = False


class RetryResponse(BaseModel):
    status: str
    mode: str
    failed_count: int


class ChainStepStatus(BaseModel):
    name: str
    status: str  # OK | WARN | FAIL | SKIP | PENDING
    detail: str = ""
    elapsed_seconds: Optional[float] = None


class ChainStatus(BaseModel):
    running: bool
    status: str  # idle | running | completed | failed | stopped
    pid: Optional[int] = None
    pgid: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    mode_summary: Optional[str] = None
    steps: List[str] = []
    current_step: Optional[str] = None
    per_step: List[ChainStepStatus] = []
    success: Optional[bool] = None
    message: Optional[str] = None


class ChainTriggerResponse(BaseModel):
    status: str  # started | already_running | error
    pid: Optional[int] = None
    message: str


class ChainStopResponse(BaseModel):
    success: bool
    message: str
