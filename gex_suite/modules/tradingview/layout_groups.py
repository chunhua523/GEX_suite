"""版面分組 model + persistence（無 Qt，可 headless 單元測試）.

資料拆成兩個檔案（同一目錄；放同步資料夾時把衝突面降到最小）：

- 群組檔 ``layout_groups.json``（位置可自訂）：``settings`` ＋ ``groups``。
  群組只存版面 id（``layout_ids``）；**只有使用者在分組頁的編輯會寫入**，
  掃描／貼上／每日排程永遠不碰，避免機器寫入和手動編輯在雲端同步時互蓋。
- 清單檔 ``layout_list.json``（固定在群組檔同目錄）：掃描快取
  ``scanned_layouts``（名稱、URL、子圖標題）。**只有掃描／貼上流程寫入**；
  同步衝突無所謂——下次掃描就會覆蓋。

顯示時群組內的 id 從清單檔解析；解析不到＝版面已不存在（或尚未掃描），
UI 標示「已過期」、開啟群組時略過，由使用者手動移除——不再自動 prune 群組。

舊版單檔格式（groups 內嵌 layouts ＋ scanned_layouts 同檔）在 load 時自動
一次性遷移拆檔。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gex_suite.shared.paths import TRADINGVIEW_LAYOUT_GROUPS_PATH, ensure_dirs

_CHART_ID_RE = re.compile(r"/chart/([A-Za-z0-9_-]{4,})")
_ID_ONLY_RE = re.compile(r"[A-Za-z0-9_-]{4,}")

# 清單檔預設檔名（未自訂路徑時放群組檔同目錄）。
_LIST_FILENAME = "layout_list.json"

# suite_config.json 內存放自訂路徑的鍵；空/None → 用預設。
_CONFIG_PATH_KEY = "layout_groups_path"
_LIST_CONFIG_PATH_KEY = "layout_list_path"
# 測試/驗證用的行程內覆寫（優先於 config）；正式流程不設。
_PATH_OVERRIDE: Path | None = None
_LIST_PATH_OVERRIDE: Path | None = None


def set_path_override(path: str | Path | None) -> None:
    """行程內強制指定群組檔位置（主要給測試用；設 None 還原為 config/預設）."""
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = Path(path).expanduser() if path else None


def set_list_path_override(path: str | Path | None) -> None:
    """行程內強制指定清單檔位置（主要給測試用；設 None 還原為 config/預設）."""
    global _LIST_PATH_OVERRIDE
    _LIST_PATH_OVERRIDE = Path(path).expanduser() if path else None


def _get_configured(key: str) -> str | None:
    try:
        from gex_suite.shared import config as _shared_config

        raw = _shared_config.load_config().get(key)
    except Exception:
        return None
    raw = str(raw or "").strip()
    return raw or None


def _set_configured(key: str, path: str | Path | None) -> None:
    from gex_suite.shared import config as _shared_config

    cfg = _shared_config.load_config()
    resolved = str(Path(path).expanduser()) if str(path or "").strip() else None
    cfg[key] = resolved
    _shared_config.save_config(cfg)


def get_configured_path() -> str | None:
    """讀 suite_config.json 內的群組檔自訂路徑（未設回 None）."""
    return _get_configured(_CONFIG_PATH_KEY)


def set_configured_path(path: str | Path | None) -> None:
    """把群組檔自訂路徑寫進 suite_config.json（GUI 與每日排程共讀；None/空＝還原預設）."""
    _set_configured(_CONFIG_PATH_KEY, path)


def get_configured_list_path() -> str | None:
    """讀 suite_config.json 內的清單檔自訂路徑（未設回 None）."""
    return _get_configured(_LIST_CONFIG_PATH_KEY)


def set_configured_list_path(path: str | Path | None) -> None:
    """把清單檔自訂路徑寫進 suite_config.json（None/空＝還原「群組檔同目錄」預設）."""
    _set_configured(_LIST_CONFIG_PATH_KEY, path)


def layout_groups_path() -> Path:
    """群組檔位置：行程覆寫 > config 自訂 > 內建預設."""
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    configured = get_configured_path()
    if configured:
        return Path(configured).expanduser()
    return TRADINGVIEW_LAYOUT_GROUPS_PATH


def layout_list_path() -> Path:
    """清單檔位置：行程覆寫 > config 自訂 > 預設（群組檔同目錄 ``layout_list.json``）."""
    if _LIST_PATH_OVERRIDE is not None:
        return _LIST_PATH_OVERRIDE
    configured = get_configured_list_path()
    if configured:
        return Path(configured).expanduser()
    return layout_groups_path().with_name(_LIST_FILENAME)

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
    """清單檔內的一個版面（掃描快取或手動新增）."""

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
    """使用者定義的群組——只存版面 id，其餘資訊顯示時從清單檔解析."""

    group_id: str
    name: str
    layout_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.group_id,
            "name": self.name,
            "layout_ids": list(self.layout_ids),
        }

    def contains_id(self, layout_id: str | None) -> bool:
        return bool(layout_id) and layout_id in self.layout_ids


@dataclass
class LayoutGroupsState:
    groups: list[LayoutGroup] = field(default_factory=list)
    scanned_layouts: list[GroupLayout] = field(default_factory=list)
    scanned_at: str | None = None
    settings: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_SETTINGS))

    def group_by_id(self, group_id: str) -> LayoutGroup | None:
        return next((g for g in self.groups if g.group_id == group_id), None)

    def list_by_id(self) -> dict[str, GroupLayout]:
        return {l.dedup_key(): l for l in self.scanned_layouts}

    def resolve(self, layout_id: str) -> GroupLayout | None:
        """由版面 id 解析清單檔項目；None＝已過期（版面不存在或尚未掃描）."""
        return next(
            (l for l in self.scanned_layouts if l.dedup_key() == layout_id), None
        )

    def count_expired(self) -> int:
        known = set(self.list_by_id())
        return sum(1 for g in self.groups for lid in g.layout_ids if lid not in known)


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


def chart_url_for_id(layout_id: str) -> str:
    return f"https://www.tradingview.com/chart/{layout_id}/"


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
    """把一次掃描/貼上流程看到的版面同步進清單快取（**不動群組**）.

    ``results``：本次實際看到的版面（``source="scan"``；沒讀到子圖的項目
    ``subcharts`` 留空，會沿用快取裡的舊值）。

    ``full=True``（全版面掃描，例如分組頁掃描、scope=all 的 auto-paste）：
    快取整批換成 ``results``；``source=="manual"`` 且本次沒看到的項目保留
    （手動新增的 URL 不因掃描而消失）。

    ``full=False``（scope=urls/ticker 等局部執行）：只 upsert 看到的版面，
    不刪任何東西。

    群組一律不修改——id 解析不到的項目由 UI 顯示「已過期」讓使用者手動移除。

    Returns counts: ``{"cache": …, "expired": …}``（expired＝群組內解析不到
    的 id 總數）。Caller 自行 ``save_list``.
    """
    old_by_key = {l.dedup_key(): l for l in state.scanned_layouts}
    # 沒讀到子圖（load 失敗、局部掃描）→ 沿用舊快取的子圖清單。
    for item in results:
        if not item.subcharts:
            old = old_by_key.get(item.dedup_key())
            if old is not None:
                item.subcharts = list(old.subcharts)

    if full:
        seen = {l.dedup_key() for l in results}
        kept_manual = [
            l
            for l in state.scanned_layouts
            if l.source == "manual" and l.dedup_key() not in seen
        ]
        state.scanned_layouts = list(results) + kept_manual
        state.scanned_at = scanned_at
    else:
        merged = {l.dedup_key(): l for l in state.scanned_layouts}
        for item in results:
            merged[item.dedup_key()] = item
        state.scanned_layouts = list(merged.values())

    return {"cache": len(state.scanned_layouts), "expired": state.count_expired()}


def apply_scan_results_to_disk(
    results: list[GroupLayout], *, full: bool, scanned_at: str | None = None
) -> dict[str, int]:
    """讀最新盤面 → 套用掃描結果 → **只寫清單檔**（供 paste 流程／每日 CLI 收尾呼叫）."""
    state = load_layout_groups()
    summary = apply_scan_results(state, results, full=full, scanned_at=scanned_at)
    save_list(state)
    return summary


def _parse_list_entries(raw_list: Any) -> list[GroupLayout]:
    return [
        gl
        for raw in (raw_list or [])
        if isinstance(raw, dict) and (gl := GroupLayout.from_dict(raw)) is not None
    ]


def _load_json_with_timeout(path: Path, timeout_s: float = 10.0) -> Any:
    """exists()+open()+json.load() 包進 daemon thread，逾時硬切。

    群組檔常被指到 CloudStorage（Google Drive）同步資料夾；FileProvider
    wedge 時連 stat/open 都會無限期卡死（2026-08-19：paste CLI 凍在
    TradingViewPage 建構、batch 根本沒開始）。逾時視同檔案不存在——流程
    以空狀態繼續；被放生的 daemon thread 不擋行程收尾。
    """
    box: list[Any] = []

    def _worker() -> None:
        try:
            if not path.exists():
                box.append(None)
                return
            with path.open("r", encoding="utf-8") as f:
                box.append(json.load(f))
        except Exception:
            box.append(None)

    t = threading.Thread(target=_worker, daemon=True, name="layout-groups-read")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        print(f"⚠️ 讀取逾時（{timeout_s:.0f}s）：{path} — 雲端掛載可能卡死，先以空狀態繼續")
        return None
    return box[0] if box else None


def _write_json_with_timeout(path: Path, payload: Any, timeout_s: float = 10.0) -> bool:
    """mkdir+open("w")+write 包 daemon thread，逾時硬切（同 _load_json_with_timeout）。

    2026-08-19 實錄：讀取有 guard 之後，流程收尾 `_flush_layout_groups_cache`
    的 save_list() 寫入 open() 仍在 wedge 掛載上永久卡死＝貼完資料行程不退。
    序列化在呼叫端先做完（不碰 FS），thread 裡只剩純 I/O；逾時放生 daemon
    thread——清單檔只是快取（下次掃描自癒）、群組檔僅使用者編輯時寫。
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    done: list[bool] = []

    def _worker() -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            done.append(True)
        except Exception as exc:
            print(f"⚠️ 寫入失敗：{path} — {exc!r}")
            done.append(False)

    t = threading.Thread(target=_worker, daemon=True, name="layout-groups-write")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        print(f"⚠️ 寫入逾時（{timeout_s:.0f}s）：{path} — 雲端掛載可能卡死，放棄本次寫入")
        return False
    return bool(done and done[0])


def load_layout_groups() -> LayoutGroupsState:
    """讀群組檔＋清單檔合成 state；偵測到舊版單檔格式時自動遷移拆檔."""
    ensure_dirs()
    state = LayoutGroupsState()
    legacy = False
    legacy_entries: list[GroupLayout] = []  # 舊格式內嵌的版面資訊 → 併入清單檔
    legacy_scanned_at: str | None = None

    gpath = layout_groups_path()
    data = _load_json_with_timeout(gpath)
    if isinstance(data, dict):
        raw_settings = data.get("settings")
        if isinstance(raw_settings, dict):
            state.settings.update(raw_settings)
        for raw in data.get("groups") or []:
            if not isinstance(raw, dict):
                continue
            if isinstance(raw.get("layout_ids"), list):
                ids = [
                    str(x).strip()
                    for x in raw["layout_ids"]
                    if _ID_ONLY_RE.fullmatch(str(x).strip())
                ]
            else:  # 舊格式：groups 內嵌完整 layouts
                legacy = True
                entries = _parse_list_entries(raw.get("layouts"))
                legacy_entries.extend(entries)
                ids = [e.dedup_key() for e in entries]
            state.groups.append(
                LayoutGroup(
                    group_id=str(raw.get("id") or new_group_id()),
                    name=str(raw.get("name") or "").strip() or "未命名群組",
                    layout_ids=ids,
                )
            )
        if "scanned_layouts" in data:  # 舊格式：快取與群組同檔
            legacy = True
            legacy_entries.extend(_parse_list_entries(data.get("scanned_layouts")))
            if data.get("scanned_at"):
                legacy_scanned_at = str(data["scanned_at"])

    lpath = layout_list_path()
    data = _load_json_with_timeout(lpath)
    if isinstance(data, dict):
        state.scanned_layouts = _parse_list_entries(data.get("layouts"))
        if data.get("scanned_at"):
            state.scanned_at = str(data["scanned_at"])

    if legacy:
        # 清單檔（較新、機器維護）優先；舊檔內容只補缺的 id。
        by_key = {l.dedup_key(): l for l in state.scanned_layouts}
        for entry in legacy_entries:
            if entry.dedup_key() not in by_key:
                by_key[entry.dedup_key()] = entry
                state.scanned_layouts.append(entry)
        if not state.scanned_at:
            state.scanned_at = legacy_scanned_at
        try:  # 一次性遷移：拆檔寫回（失敗不影響本次讀取）
            save_groups(state)
            save_list(state)
        except Exception:
            pass

    return state


def save_groups(state: LayoutGroupsState) -> None:
    """寫群組檔（settings ＋ groups）——只在使用者編輯群組時呼叫."""
    ensure_dirs()
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(state.settings or {})
    payload = {
        "version": 2,
        "settings": merged,
        "groups": [g.to_dict() for g in state.groups],
    }
    _write_json_with_timeout(layout_groups_path(), payload)


def save_list(state: LayoutGroupsState) -> None:
    """寫清單檔（掃描快取）——掃描／貼上流程與手動新增 URL 時呼叫."""
    ensure_dirs()
    payload = {
        "version": 1,
        "scanned_at": state.scanned_at,
        "layouts": [l.to_dict() for l in state.scanned_layouts],
    }
    _write_json_with_timeout(layout_list_path(), payload)


def save_layout_groups(state: LayoutGroupsState) -> None:
    """同時寫兩檔（遷移／測試用；一般流程請分別用 save_groups / save_list）."""
    save_groups(state)
    save_list(state)
