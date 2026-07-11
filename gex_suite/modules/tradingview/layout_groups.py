"""版面分組 model + persistence（無 Qt，可 headless 單元測試）.

Data lives in ``gex_suite/data/tradingview/layout_groups.json``:

- ``groups``: user-defined ordered groups; each group's ``layouts`` order is the
  tab order when the group is opened. Entries are copies, so one layout may
  belong to multiple groups and a re-scan never breaks group membership.
- ``scanned_layouts``: cache of the last CDP ``list_layouts()`` scan.
- ``settings``: app-launch delivery method + delays (merged over defaults).
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from gex_suite.shared.paths import TRADINGVIEW_LAYOUT_GROUPS_PATH, ensure_dirs

_CHART_ID_RE = re.compile(r"/chart/([A-Za-z0-9_-]{4,})")

_DEFAULT_SETTINGS: dict[str, Any] = {
    # App 模式走 CDP：TradingView 以 --remote-debugging-port=<app_cdp_port> 啟動，
    # 開視窗/分頁機制見 app_launcher.py docstring（AppleScript 選單與 open -a /
    # tradingview:// 已實測無效，勿走回頭路）。
    "app_cdp_port": 9333,
    "delay_new_window_ms": 1200,
    "delay_per_url_ms": 1800,
    "delay_browser_window_ms": 2000,
}


@dataclass
class GroupLayout:
    name: str
    url: str  # canonical https://www.tradingview.com/chart/<id>/
    layout_id: str | None = None
    source: str = "scan"  # "scan" | "manual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_id": self.layout_id,
            "name": self.name,
            "url": self.url,
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "GroupLayout | None":
        url = normalize_chart_url(str(data.get("url") or ""))
        if not url:
            return None
        return GroupLayout(
            name=str(data.get("name") or "").strip() or f"URL:{chart_id_from_url(url)}",
            url=url,
            layout_id=(str(data.get("layout_id")) if data.get("layout_id") else chart_id_from_url(url)),
            source=("manual" if str(data.get("source") or "") == "manual" else "scan"),
        )

    def dedup_key(self) -> str:
        return self.layout_id or self.url


@dataclass
class LayoutGroup:
    group_id: str
    name: str
    layouts: list[GroupLayout] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.group_id,
            "name": self.name,
            "layouts": [l.to_dict() for l in self.layouts],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LayoutGroup":
        layouts = [
            gl
            for raw in (data.get("layouts") or [])
            if isinstance(raw, dict) and (gl := GroupLayout.from_dict(raw)) is not None
        ]
        return LayoutGroup(
            group_id=str(data.get("id") or new_group_id()),
            name=str(data.get("name") or "").strip() or "未命名群組",
            layouts=layouts,
        )

    def contains(self, layout: GroupLayout) -> bool:
        key = layout.dedup_key()
        return any(l.dedup_key() == key for l in self.layouts)


@dataclass
class LayoutGroupsState:
    groups: list[LayoutGroup] = field(default_factory=list)
    scanned_layouts: list[GroupLayout] = field(default_factory=list)
    scanned_at: str | None = None
    settings: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_SETTINGS))

    def to_dict(self) -> dict[str, Any]:
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(self.settings or {})
        return {
            "version": 1,
            "settings": merged,
            "scanned_at": self.scanned_at,
            "scanned_layouts": [l.to_dict() for l in self.scanned_layouts],
            "groups": [g.to_dict() for g in self.groups],
        }

    def group_by_id(self, group_id: str) -> LayoutGroup | None:
        return next((g for g in self.groups if g.group_id == group_id), None)


def new_group_id() -> str:
    return "g-" + secrets.token_hex(4)


def chart_id_from_url(url: str) -> str | None:
    m = _CHART_ID_RE.search(url or "")
    return m.group(1) if m else None


def normalize_chart_url(raw: str) -> str | None:
    """Canonicalise any chart-URL-ish input to ``https://www.tradingview.com/chart/<id>/``.

    Accepts full http(s) URLs on any tradingview.com subdomain, protocol-relative
    or site-relative ``/chart/<id>`` paths, and bare chart ids.
    """
    val = (raw or "").strip()
    if not val:
        return None
    if val.startswith("//"):
        val = "https:" + val
    if val.startswith("http://") or val.startswith("https://"):
        if "tradingview.com" not in val.lower():
            return None
        cid = chart_id_from_url(val)
        return f"https://www.tradingview.com/chart/{cid}/" if cid else None
    if val.startswith("/"):
        cid = chart_id_from_url(val)
        return f"https://www.tradingview.com/chart/{cid}/" if cid else None
    if re.fullmatch(r"[A-Za-z0-9_-]{4,}", val):
        return f"https://www.tradingview.com/chart/{val}/"
    return None


def load_layout_groups() -> LayoutGroupsState:
    ensure_dirs()
    if not TRADINGVIEW_LAYOUT_GROUPS_PATH.exists():
        return LayoutGroupsState()
    try:
        with TRADINGVIEW_LAYOUT_GROUPS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        settings = dict(_DEFAULT_SETTINGS)
        raw_settings = data.get("settings")
        if isinstance(raw_settings, dict):
            settings.update(raw_settings)
        scanned = [
            gl
            for raw in (data.get("scanned_layouts") or [])
            if isinstance(raw, dict) and (gl := GroupLayout.from_dict(raw)) is not None
        ]
        groups = [
            LayoutGroup.from_dict(raw)
            for raw in (data.get("groups") or [])
            if isinstance(raw, dict)
        ]
        scanned_at = data.get("scanned_at")
        return LayoutGroupsState(
            groups=groups,
            scanned_layouts=scanned,
            scanned_at=str(scanned_at) if scanned_at else None,
            settings=settings,
        )
    except Exception:
        return LayoutGroupsState()


def save_layout_groups(state: LayoutGroupsState) -> None:
    ensure_dirs()
    with TRADINGVIEW_LAYOUT_GROUPS_PATH.open("w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
