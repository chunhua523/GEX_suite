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
    subcharts: list[str] = field(default_factory=list)  # 各子圖標題（symbol 尾碼）

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_id": self.layout_id,
            "name": self.name,
            "url": self.url,
            "source": self.source,
            "subcharts": list(self.subcharts),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "GroupLayout | None":
        url = normalize_chart_url(str(data.get("url") or ""))
        if not url:
            return None
        raw_subs = data.get("subcharts")
        subcharts = (
            [str(s).strip() for s in raw_subs if str(s).strip()]
            if isinstance(raw_subs, list)
            else []
        )
        return GroupLayout(
            name=str(data.get("name") or "").strip() or f"URL:{chart_id_from_url(url)}",
            url=url,
            layout_id=(str(data.get("layout_id")) if data.get("layout_id") else chart_id_from_url(url)),
            source=("manual" if str(data.get("source") or "") == "manual" else "scan"),
            subcharts=subcharts,
        )

    def dedup_key(self) -> str:
        return self.layout_id or self.url

    def display_label(self) -> str:
        """清單顯示：名稱（子圖標題, …）；沒有子圖資訊時只顯示名稱。"""
        if self.subcharts:
            return f"{self.name}（{', '.join(self.subcharts)}）"
        return self.name

    def matches_filter(self, needle: str) -> bool:
        needle = (needle or "").strip().lower()
        if not needle:
            return True
        if needle in self.name.lower():
            return True
        return any(needle in s.lower() for s in self.subcharts)


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


# 公式圖（多商品算術組合）偵測：+ * / 一律視為公式；- 需兩側都是 2+ 字元 token
#（避免誤判 BRK-B 這類 class share）。
_FORMULA_OPS_RE = re.compile(r"[+*/]|(?<=[A-Za-z0-9!])-(?=[A-Za-z0-9]{2,})")


def subchart_title(raw: str) -> str:
    """子圖顯示標題：一般 symbol 取冒號尾碼（ES1!、AAPL）；公式圖保留原樣."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if _FORMULA_OPS_RE.search(raw):
        return raw
    return raw.split(":")[-1].strip()


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


def apply_scan_results(
    state: LayoutGroupsState,
    results: list[GroupLayout],
    *,
    full: bool,
    scanned_at: str | None = None,
) -> dict[str, int]:
    """把一次掃描/貼上流程看到的版面同步進快取與群組.

    ``results``：本次實際看到的版面（``source="scan"``；沒讀到子圖的項目
    ``subcharts`` 留空，會沿用快取裡的舊值）。

    ``full=True``（全版面掃描，例如分組頁掃描、scope=all 的 auto-paste）：
    快取整批換成 ``results``，且群組內 ``source=="scan"`` 而版面已不存在的
    項目會被移除（``source=="manual"`` 一律保留）。

    ``full=False``（scope=urls/ticker 等局部執行）：只 upsert 看到的版面，
    不刪任何東西。

    Returns counts: ``{"cache": …, "group_updates": …, "pruned": …}``.
    Caller 自行 ``save_layout_groups``.
    """
    old_by_key = {l.dedup_key(): l for l in state.scanned_layouts}
    # 沒讀到子圖（load 失敗、局部掃描）→ 沿用舊快取的子圖清單。
    for item in results:
        if not item.subcharts:
            old = old_by_key.get(item.dedup_key())
            if old is not None:
                item.subcharts = list(old.subcharts)

    if full:
        state.scanned_layouts = list(results)
        state.scanned_at = scanned_at
    else:
        merged = {l.dedup_key(): l for l in state.scanned_layouts}
        for item in results:
            merged[item.dedup_key()] = item
        state.scanned_layouts = list(merged.values())

    new_by_key = {l.dedup_key(): l for l in results}
    group_updates = 0
    pruned = 0
    for group in state.groups:
        kept: list[GroupLayout] = []
        for layout in group.layouts:
            fresh = new_by_key.get(layout.dedup_key())
            if fresh is not None:
                if (fresh.name != layout.name and fresh.name) or (
                    fresh.subcharts != layout.subcharts and fresh.subcharts
                ):
                    group_updates += 1
                if fresh.name:
                    layout.name = fresh.name
                if fresh.subcharts:
                    layout.subcharts = list(fresh.subcharts)
                kept.append(layout)
            elif full and layout.source == "scan":
                pruned += 1  # 版面已從 TradingView 刪除 → 移出群組
            else:
                kept.append(layout)
        group.layouts = kept
    return {"cache": len(state.scanned_layouts), "group_updates": group_updates, "pruned": pruned}


def apply_scan_results_to_disk(
    results: list[GroupLayout], *, full: bool, scanned_at: str | None = None
) -> dict[str, int]:
    """讀最新盤面 → 套用掃描結果 → 寫回（供 paste 流程／每日 CLI 收尾呼叫）."""
    state = load_layout_groups()
    summary = apply_scan_results(state, results, full=full, scanned_at=scanned_at)
    save_layout_groups(state)
    return summary


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
