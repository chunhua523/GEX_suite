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
