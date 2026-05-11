"""TradingViewPage UI.

Reads TV codes that the chart module has already imported into
``stocks.db`` and can paste the selected code to TradingView by attaching
to a user-launched browser via CDP.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib import request

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gex_suite.shared import config as shared_config
from gex_suite.shared import db
from .automator import (
    IndicatorQuotaExceededError,
    LayoutInfo,
    PlaywrightCDPAutomator,
    WeeklyGexSubchartCache,
)
from .engine import (
    BatchOptions,
    BatchReport,
    BatchResultItem,
    WorkItem,
    compute_target_mondays,
)

_DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_DAY_ABBR = {
    "Monday": "一",
    "Tuesday": "二",
    "Wednesday": "三",
    "Thursday": "四",
    "Friday": "五",
}

# TradingView continuous-futures symbols → DB ticker (per layout mode).
#   "futures" : feed the chart with the actual futures TV codes (CME-imported, suffixed with 1!).
#   "equity"  : feed the chart with the related ETF / equity ticker (e.g. SPY for ES1!).
#   "index"   : feed the chart with the underlying index (e.g. SPX for ES1!).
# All three modes shift the indicator's "Start date" to Sunday 18:00, because the
# X-axis bar anchor depends on the chart symbol (always futures), not the data source.
# A None entry means "no DB ticker exists for this mode" → strict skip with a clear log.
_FUTURES_ALIAS_MAP: dict[str, dict[str, str | None]] = {
    # E-mini equity index futures (all 3 modes have a real counterpart)
    "ES1!":  {"futures": "ES1!",  "equity": "SPY",  "index": "SPX"},
    "NQ1!":  {"futures": "NQ1!",  "equity": "QQQ",  "index": "NDX"},
    "RTY1!": {"futures": "RTY1!", "equity": "IWM",  "index": "RUT"},

    # Precious metals (no broadly-tracked option-bearing index → index=None)
    "GC1!":  {"futures": "GC1!",  "equity": "GLD",  "index": None},
    "SI1!":  {"futures": "SI1!",  "equity": "SLV",  "index": None},
    "PL1!":  {"futures": "PL1!",  "equity": "PPLT", "index": None},
    "PA1!":  {"futures": "PA1!",  "equity": "PALL", "index": None},

    # Industrial metals
    "HG1!":  {"futures": "HG1!",  "equity": "CPER", "index": None},

    # Energy
    "CL1!":  {"futures": "CL1!",  "equity": "USO",  "index": None},

    # Grains
    "ZC1!":  {"futures": "ZC1!",  "equity": "CORN", "index": None},
    "ZS1!":  {"futures": "ZS1!",  "equity": "SOYB", "index": None},
    "ZW1!":  {"futures": "ZW1!",  "equity": "WEAT", "index": None},

    # Treasury futures (each maturity bucket → matching ETF)
    "ZT1!":  {"futures": "ZT1!",  "equity": "SHY",  "index": None},  # 2Y
    "ZF1!":  {"futures": "ZF1!",  "equity": "IEI",  "index": None},  # 5Y
    "ZN1!":  {"futures": "ZN1!",  "equity": "IEF",  "index": None},  # 10Y
    "TN1!":  {"futures": "TN1!",  "equity": "IEF",  "index": None},  # 10Y ultra → 同 IEF
    "ZB1!":  {"futures": "ZB1!",  "equity": "TLT",  "index": None},  # 30Y
    "UB1!":  {"futures": "UB1!",  "equity": "TLT",  "index": None},  # Ultra bond → 同 TLT

    # Crypto
    "BTC1!": {"futures": "BTC1!", "equity": "IBIT", "index": None},
}

_FUTURES_START_TIME = "18:00"
_FUTURES_DEFAULT_MODE = "equity"

# Layout-name markers (English, case-insensitive substring match).
# Multiple aliases per mode supported — pick whichever feels natural in your layout name.
_LAYOUT_MODE_MARKERS: dict[str, tuple[str, ...]] = {
    "futures": ("[futures]", "[future]", "[fut]"),
    "equity":  ("[equity]", "[etf]", "[eq]"),
    "index":   ("[index]", "[idx]", "[ix]"),
}

_PREVIEW_COL_CHART_URL = 1
# Narrow columns clip the left side of long URLs; keep this small so "…/chart/<id>/" tail stays visible.
_PREVIEW_URL_DISPLAY_MAX = 36


class _PhaseBScanThread(QThread):
    """Runs ``_phase_b_scan_flow`` on a dedicated event loop so the UI stays responsive."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, page: "TradingViewPage", opts: BatchOptions) -> None:
        super().__init__(page)
        self._page = page
        self._opts = opts

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            report = loop.run_until_complete(self._page._phase_b_scan_flow(self._opts))
            self.succeeded.emit(report)
        except BaseException as exc:  # noqa: BLE001
            self.failed.emit(exc)
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class _AsyncCoroThread(QThread):
    """Runs a coroutine factory on a dedicated event loop and emits its result.

    Use this for any TradingView async work triggered from the UI thread —
    calling ``asyncio.run`` directly on the GUI thread would freeze the window.
    """

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, parent: QWidget, coro_factory) -> None:
        super().__init__(parent)
        self._coro_factory = coro_factory

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._coro_factory())
            self.succeeded.emit(result)
        except BaseException as exc:  # noqa: BLE001
            self.failed.emit(exc)
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class TradingViewPage(QWidget):
    """Main TradingView helper page（批次 GEX 與手動檢視／複製 TV code）。"""

    _marshal_log = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._marshal_log.connect(self._exec_log_main_thread)
        db.init_db()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 15, 15, 15)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        # ----- Tab: 批次（版面／子圖） -----
        tab_batch = QWidget()
        batch_root = QVBoxLayout(tab_batch)
        batch_root.setContentsMargins(8, 8, 8, 8)

        grp_opts = QGroupBox("批次範圍與選項")
        go = QVBoxLayout(grp_opts)
        row_weeks = QHBoxLayout()
        row_weeks.addWidget(QLabel("週期："))
        self.cb_phase_weeks = QComboBox()
        self.cb_phase_weeks.addItem("本週", "this_week")
        self.cb_phase_weeks.addItem("最近 4 週", "last_4_weeks")
        self.cb_phase_weeks.setToolTip("要寫入 DB 有資料的週別")
        self.cb_phase_weeks.currentIndexChanged.connect(self._save_tv_prefs)
        row_weeks.addWidget(self.cb_phase_weeks)
        row_weeks.addStretch(1)
        go.addLayout(row_weeks)

        row_layout = QHBoxLayout()
        row_layout.addWidget(QLabel("版面："))
        self.cb_layout_scope = QComboBox()
        self.cb_layout_scope.addItem("全部 layouts", "all")
        self.cb_layout_scope.addItem("僅目前版面", "active")
        self.cb_layout_scope.currentIndexChanged.connect(self._save_tv_prefs)
        row_layout.addWidget(self.cb_layout_scope)
        row_layout.addStretch(1)
        go.addLayout(row_layout)

        row_tscope = QHBoxLayout()
        row_tscope.addWidget(QLabel("Ticker："))
        self.cb_ticker_scope = QComboBox()
        self.cb_ticker_scope.addItem("所有（由各子圖辨識）", "all")
        self.cb_ticker_scope.addItem("僅指定 ticker", "ticker")
        self.cb_ticker_scope.currentIndexChanged.connect(self._on_ticker_scope_changed)
        self.cb_ticker_scope.currentIndexChanged.connect(self._save_tv_prefs)
        row_tscope.addWidget(self.cb_ticker_scope)
        row_tscope.addStretch(1)
        go.addLayout(row_tscope)

        self.chk_skip_if_has_values = QCheckBox("已有值的天略過（不覆蓋）")
        self.chk_skip_if_has_values.setChecked(True)
        self.chk_skip_if_has_values.stateChanged.connect(self._save_tv_prefs)
        go.addWidget(self.chk_skip_if_has_values)

        self.chk_visibility_preset = QCheckBox("套用 Visibility 預設（關閉日／週／月線）")
        self.chk_visibility_preset.setChecked(True)
        self.chk_visibility_preset.stateChanged.connect(self._save_tv_prefs)
        go.addWidget(self.chk_visibility_preset)

        self.chk_organize_indicators = QCheckBox("寫入前先刪除過期 GEX 指標（近四週視窗外）")
        self.chk_organize_indicators.setChecked(False)
        self.chk_organize_indicators.stateChanged.connect(self._save_tv_prefs)
        go.addWidget(self.chk_organize_indicators)
        batch_root.addWidget(grp_opts)

        row_launch = QHBoxLayout()
        row_launch.addWidget(QLabel("瀏覽器："))
        self.radio_chrome = QRadioButton("Chrome")
        self.radio_brave = QRadioButton("Brave")
        self.radio_chrome.setChecked(True)
        self._browser_group = QButtonGroup(self)
        self._browser_group.addButton(self.radio_chrome)
        self._browser_group.addButton(self.radio_brave)
        self.radio_chrome.toggled.connect(self._save_tv_prefs)
        self.radio_brave.toggled.connect(self._save_tv_prefs)
        row_launch.addWidget(self.radio_chrome)
        row_launch.addWidget(self.radio_brave)
        self.b_launch_tv = QPushButton("啟動 Chrome／Brave（9222）")
        self.b_launch_tv.setToolTip("以 --remote-debugging-port=9222 啟動並開啟 TradingView")
        self.b_launch_tv.clicked.connect(self._on_launch_browser_9222)
        row_launch.addWidget(self.b_launch_tv)
        row_launch.addStretch(1)
        batch_root.addLayout(row_launch)

        grp_run = QGroupBox("動作")
        gr = QVBoxLayout(grp_run)
        row_btn1 = QHBoxLayout()
        self.b_phase_b = QPushButton("開始執行")
        self.b_phase_b.setToolTip("依「版面」與其他選項掃描並寫入 Daily & Weekly GEX（背景執行，可停止）")
        self.b_phase_b.clicked.connect(self._on_phase_b_scan)
        self.b_phase_b.setStyleSheet(
            "QPushButton { background-color: #238636; color: #ffffff; padding: 8px 14px; "
            "font-weight: 600; border-radius: 4px; border: 1px solid #2ea043; }"
            "QPushButton:hover:!disabled { background-color: #2ea043; }"
            "QPushButton:disabled { background-color: #3d444d; color: #8b949e; border-color: #444c56; }"
        )
        row_btn1.addWidget(self.b_phase_b, 1)

        self.b_stop = QPushButton("Stop")
        self.b_stop.setToolTip("中止目前的批次掃描（在下一個可中斷點停止）")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self._on_stop_batch_scan)
        self.b_stop.setStyleSheet(
            "QPushButton { background-color: #a02929; color: #ffffff; padding: 8px 14px; "
            "font-weight: 600; border-radius: 4px; border: 1px solid #c93c37; }"
            "QPushButton:hover:!disabled { background-color: #c93c37; }"
            "QPushButton:disabled { background-color: #3d444d; color: #8b949e; border-color: #444c56; }"
        )
        row_btn1.addWidget(self.b_stop)
        gr.addLayout(row_btn1)

        row_btn2 = QHBoxLayout()
        self.b_phase_b_preview = QPushButton("預覽將變更項目")
        self.b_phase_b_preview.setToolTip(
            "與「開始執行」相同流程掃描子圖與指標，但不寫入欄位、不刪過期指標、不儲存版面；結果顯示於下方表格"
        )
        self.b_phase_b_preview.clicked.connect(self._on_phase_b_preview)
        row_btn2.addWidget(self.b_phase_b_preview)

        self.b_phase_b_cleanup = QPushButton("整理：刪除過期 GEX 指標")
        self.b_phase_b_cleanup.setToolTip("刪除週起始早於近四週視窗的 Daily & Weekly GEX（不新增、不重填）")
        self.b_phase_b_cleanup.clicked.connect(self._on_phase_b_cleanup)
        row_btn2.addWidget(self.b_phase_b_cleanup)
        row_btn2.addStretch(1)
        gr.addLayout(row_btn2)
        batch_root.addWidget(grp_run)

        self.preview_table = QTableWidget(0, 7)
        self.preview_table.setHorizontalHeaderLabels(
            ["版面", "圖表 URL", "子圖#", "圖上商品", "DB ticker", "週一起", "將填入／缺資料"]
        )
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview_table.setTextElideMode(Qt.ElideNone)
        self.preview_table.setWordWrap(False)
        self.preview_table.setMaximumHeight(220)
        self.preview_table.setToolTip("「預覽將變更項目」執行後更新；圖表 URL 欄為尾端省略，滑鼠懸停可看完整網址。")
        preview_bar = QHBoxLayout()
        preview_bar.addWidget(QLabel("預覽表（僅預覽按鈕會填入）"))
        preview_bar.addStretch(1)
        self.b_open_preview_chart_url = QPushButton("開啟選取列之圖表 URL")
        self.b_open_preview_chart_url.setToolTip(
            "在已連線的 CDP（預設 127.0.0.1:9222）瀏覽器中開啟選取列的圖表網址；"
            "首個網址用目前分頁，其餘各開新分頁（可按住 Ctrl 多選）"
        )
        self.b_open_preview_chart_url.clicked.connect(self._on_open_preview_selected_chart_urls)
        preview_bar.addWidget(self.b_open_preview_chart_url)
        batch_root.addLayout(preview_bar)
        batch_root.addWidget(self.preview_table)

        tabs.addTab(tab_batch, "批次與整理")

        # ----- Tab: 手動 -----
        tab_manual = QWidget()
        man = QVBoxLayout(tab_manual)
        man.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Ticker："))
        self.cb_ticker = QComboBox()
        self.cb_ticker.setEditable(True)
        self.cb_ticker.currentTextChanged.connect(self._on_ticker_changed)
        top.addWidget(self.cb_ticker, 1)
        b_reload = QPushButton("從 DB 重新載入 ticker")
        b_reload.clicked.connect(self._reload_tickers)
        top.addWidget(b_reload)
        man.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        left = QGroupBox("歷史 TV Codes（同一 ticker）")
        l = QVBoxLayout(left)
        self.list_history = QListWidget()
        self.list_history.itemSelectionChanged.connect(self._on_history_selected)
        l.addWidget(self.list_history, 1)
        split.addWidget(left)

        right = QGroupBox("TV Code（可編輯）")
        r = QVBoxLayout(right)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 11))
        r.addWidget(self.editor, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 720])
        man.addWidget(split, 1)

        man_ar = QHBoxLayout()
        b_copy = QPushButton("複製到剪貼簿")
        b_copy.clicked.connect(self._copy_to_clipboard)
        man_ar.addWidget(b_copy)
        man_ar.addStretch(1)
        man.addLayout(man_ar)

        hint = QLabel(
            "請以已登入帳號啟動瀏覽器（遠端除錯埠 9222，見「批次與整理」分頁）。"
            "此分頁可檢視／複製 DB 內的 TV code；寫入圖表請使用「批次與整理」。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888; font-size:11px;")
        man.addWidget(hint)

        tabs.addTab(tab_manual, "手動貼上")

        # ----- Shared status / progress / log -----
        self.lbl_status = QLabel(
            "請先以 --remote-debugging-port=9222 啟動已登入的 TradingView 瀏覽器。"
        )
        self.lbl_status.setStyleSheet("color:#7FB3FF;")
        self.lbl_status.setWordWrap(True)
        outer.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        outer.addWidget(self.progress)

        outer.addWidget(QLabel("執行紀錄（精簡）"))
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(160)
        self.log_box.setPlaceholderText("每次執行會清空並只保留版面、URL、ticker、更新／新增／刪除等摘要…")
        outer.addWidget(self.log_box)

        self._reload_tickers()
        self._last_phase_b_symbols: list[str] = []
        self._last_phase_b_matched_subcharts: int = 0
        self._last_phase_b_layouts: list[str] = []
        self._last_phase_b_layout_list_degraded: bool = False
        self.cb_ticker.currentTextChanged.connect(self._save_tv_prefs)
        self._load_tv_prefs()
        self._on_ticker_scope_changed()

        self._cancel_batch_scan = False
        self._scan_thread: _PhaseBScanThread | None = None
        self._preview_thread: _AsyncCoroThread | None = None
        self._cleanup_thread: _AsyncCoroThread | None = None
        self._open_urls_thread: _AsyncCoroThread | None = None

    # ---------- 執行紀錄（產品向精簡） ----------
    def _exec_log_clear(self) -> None:
        self.log_box.clear()

    def _exec_log(self, message: str) -> None:
        if QThread.currentThread() is not self.thread():
            self._marshal_log.emit(message)
            return
        self._exec_log_main_thread(message)

    def _exec_log_main_thread(self, message: str) -> None:
        text = (message or "").rstrip()
        if text:
            self.log_box.appendPlainText(text)
        QApplication.processEvents()

    def _batch_should_stop(self) -> bool:
        return bool(self._cancel_batch_scan)

    @staticmethod
    def _abbr_weekday_labels(days: list[str]) -> str:
        if not days:
            return "—"
        return "、".join(_DAY_ABBR.get(d, d[:3]) for d in days)

    @staticmethod
    def _missing_weekday_labels(codes: dict[str, str | None]) -> str:
        missing = [d for d in _DAY_ORDER if not (codes.get(d) or "").strip()]
        if not missing:
            return ""
        return "尚缺 DB：" + "、".join(_DAY_ABBR[d] for d in missing)

    @staticmethod
    def _preview_item_is_actionable(item: WorkItem) -> bool:
        ps = (item.preview_status or "").strip()
        if not ps:
            return True
        if ps.startswith("略過"):
            return False
        return True

    @staticmethod
    def _format_url_cell_elide_tail(url: str, max_len: int = _PREVIEW_URL_DISPLAY_MAX) -> str:
        """Prefer keeping '/chart/<id>' tail visible in narrow table cells."""
        u = (url or "").strip()
        if not u:
            return "—"
        marker = "/chart/"
        pos = u.lower().rfind(marker)
        if pos >= 0:
            tail = u[pos:]
            if len(tail) + 3 <= max_len:
                return "..." + tail
        if len(u) <= max_len:
            return u
        keep = max(12, max_len - 3)
        return "..." + u[-keep:]

    def _on_open_preview_selected_chart_urls(self) -> None:
        sel = self.preview_table.selectionModel()
        if sel is None or not sel.hasSelection():
            QMessageBox.information(self, "未選取", "請先在預覽表中選取一列或多列。")
            return
        rows = sorted({ix.row() for ix in sel.selectedIndexes()})
        seen: set[str] = set()
        urls: list[str] = []
        for r in rows:
            item = self.preview_table.item(r, _PREVIEW_COL_CHART_URL)
            if item is None:
                continue
            raw = str(item.data(Qt.UserRole) or "").strip()
            low = raw.lower()
            if not raw or not (low.startswith("http://") or low.startswith("https://")):
                continue
            if raw in seen:
                continue
            seen.add(raw)
            urls.append(raw)
        if not urls:
            QMessageBox.information(self, "無 URL", "選取列沒有可開啟的 http(s) 圖表網址。")
            return
        cfg = shared_config.load_tradingview_config()
        cdp_url = str(cfg.get("cdp_url") or "http://127.0.0.1:9222").strip()
        thread = _AsyncCoroThread(
            self,
            lambda: self._open_preview_chart_urls_via_cdp(cdp_url, urls),
        )
        thread.failed.connect(lambda exc, u=cdp_url: self._on_open_preview_failed(u, exc))
        thread.finished.connect(thread.deleteLater)
        self._open_urls_thread = thread
        thread.start()

    def _on_open_preview_failed(self, cdp_url: str, exc: BaseException) -> None:
        QMessageBox.warning(
            self,
            "CDP 開啟失敗",
            f"無法透過 {cdp_url} 在已啟動的瀏覽器中開啟網址。\n"
            f"請確認已用遠端除錯埠啟動 TradingView，且設定檔 cdp_url 正確。\n\n錯誤：{exc}",
        )

    async def _open_preview_chart_urls_via_cdp(self, cdp_url: str, urls: list[str]) -> None:
        automator = PlaywrightCDPAutomator(cdp_url=cdp_url)
        try:
            await automator.open_chart_urls_via_cdp(urls)
        finally:
            await automator.close()

    def _populate_preview_table(self, items: list[WorkItem]) -> None:
        self.preview_table.setRowCount(0)
        for it in items:
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            ps = (it.preview_status or "").strip()
            if ps.startswith(("略過", "失敗")):
                fill_cell = ps
            elif ps.startswith("預覽："):
                # Do not prefix ``available_days`` here: that lists every DB-backed day for the
                # week, while log / preview_status use chart-vs-DB (e.g. cache) to show only
                # days that would actually be written — prefixing both misled users (e.g. 一~五
                # vs 執行將填 四、五).
                miss_txt = self._missing_weekday_labels(it.codes)
                fill_cell = f"{ps}　{miss_txt}".strip() if miss_txt else ps
            else:
                fill_txt = self._abbr_weekday_labels(it.available_days)
                miss_txt = self._missing_weekday_labels(it.codes)
                fill_cell = f"{fill_txt}　{miss_txt}".strip() if miss_txt else fill_txt
                if ps:
                    fill_cell = f"{fill_cell}　{ps}".strip()

            self.preview_table.setItem(
                row, 0, QTableWidgetItem(it.layout_name or it.layout_id or "—")
            )
            url_full = (it.chart_url or "").strip()
            if url_full:
                url_item = QTableWidgetItem(self._format_url_cell_elide_tail(url_full))
                url_item.setData(Qt.UserRole, url_full)
                url_item.setToolTip(url_full)
                url_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                url_item = QTableWidgetItem("—")
            self.preview_table.setItem(row, _PREVIEW_COL_CHART_URL, url_item)

            self.preview_table.setItem(
                row, 2, QTableWidgetItem(str(it.subchart_index) if it.subchart_index is not None else "—")
            )
            self.preview_table.setItem(row, 3, QTableWidgetItem(it.subchart_symbol or "—"))
            self.preview_table.setItem(row, 4, QTableWidgetItem(it.ticker))
            self.preview_table.setItem(row, 5, QTableWidgetItem(it.monday.isoformat()))
            self.preview_table.setItem(row, 6, QTableWidgetItem(fill_cell))

    @staticmethod
    def _format_batch_report_exec(report: BatchReport) -> str:
        if report.total == 0:
            return "【摘要】沒有執行任何子項目（0 筆）。"
        lines = [
            f"【摘要】成功 {report.done}／略過 {report.skipped}／失敗 {report.failed}（合計 {report.total} 筆）"
        ]
        shown = 0
        for ri in report.items:
            if ri.status != "failed":
                continue
            it = ri.item
            layout = it.layout_name or it.layout_id or "—"
            url = it.chart_url or "—"
            msg = (ri.message or "").strip().replace("\n", " ")
            if len(msg) > 160:
                msg = msg[:157] + "…"
            sub_n = it.subchart_index if it.subchart_index is not None else "—"
            lines.append(
                f"  失敗｜版面={layout} URL={url} 子圖#={sub_n} "
                f"ticker={it.ticker} 週一起={it.monday}：{msg}"
            )
            shown += 1
            if shown >= 8:
                rest = sum(1 for x in report.items if x.status == "failed") - shown
                if rest > 0:
                    lines.append(f"  …其餘失敗 {rest} 筆（請看狀態列或重跑單筆）")
                break
        return "\n".join(lines)

    # ---------- DB <-> UI ----------
    def _reload_tickers(self) -> None:
        current = self.cb_ticker.currentText().strip()
        rows = db.fetch_tv_codes()  # (ticker, date, code)
        tickers = sorted({r[0] for r in rows})
        self.cb_ticker.blockSignals(True)
        self.cb_ticker.clear()
        self.cb_ticker.addItems(tickers)
        self.cb_ticker.blockSignals(False)
        if current:
            idx = self.cb_ticker.findText(current)
            if idx >= 0:
                self.cb_ticker.setCurrentIndex(idx)
        self._on_ticker_changed(self.cb_ticker.currentText())

    def _on_ticker_changed(self, ticker: str) -> None:
        self.list_history.clear()
        if not ticker:
            self.editor.clear()
            return
        rows = db.fetch_tv_codes(ticker.strip())
        for _t, date, code in rows:
            item = QListWidgetItem(date)
            item.setData(Qt.UserRole, code)
            self.list_history.addItem(item)
        if self.list_history.count():
            self.list_history.setCurrentRow(0)

    def _load_tv_prefs(self) -> None:
        cfg = shared_config.load_tradingview_config()
        weeks_mode = str(cfg.get("weeks_mode") or "this_week")
        idx = self.cb_phase_weeks.findData(weeks_mode)
        if idx >= 0:
            self.cb_phase_weeks.setCurrentIndex(idx)
        layout_scope = str(cfg.get("layout_scope") or "all")
        layout_idx = self.cb_layout_scope.findData(layout_scope)
        if layout_idx >= 0:
            self.cb_layout_scope.setCurrentIndex(layout_idx)
        ticker_scope = str(cfg.get("ticker_scope") or "all")
        ticker_idx = self.cb_ticker_scope.findData(ticker_scope)
        if ticker_idx >= 0:
            self.cb_ticker_scope.setCurrentIndex(ticker_idx)
        self.chk_skip_if_has_values.setChecked(bool(cfg.get("skip_filled_days", True)))
        self.chk_visibility_preset.setChecked(bool(cfg.get("apply_visibility_preset", True)))
        self.chk_organize_indicators.setChecked(bool(cfg.get("organize_indicators", False)))
        if str(cfg.get("browser") or "chrome").strip().lower() == "brave":
            self.radio_brave.setChecked(True)
        else:
            self.radio_chrome.setChecked(True)
        pref_ticker = str(cfg.get("ticker") or "").strip().upper()
        if pref_ticker:
            i = self.cb_ticker.findText(pref_ticker)
            if i >= 0:
                self.cb_ticker.setCurrentIndex(i)
            else:
                self.cb_ticker.setEditText(pref_ticker)

    def _save_tv_prefs(self, *_args) -> None:
        shared_config.save_tradingview_config(
            {
                "weeks_mode": str(self.cb_phase_weeks.currentData() or "this_week"),
                "layout_scope": str(self.cb_layout_scope.currentData() or "all"),
                "ticker_scope": str(self.cb_ticker_scope.currentData() or "all"),
                "skip_filled_days": self.chk_skip_if_has_values.isChecked(),
                "apply_visibility_preset": self.chk_visibility_preset.isChecked(),
                "organize_indicators": self.chk_organize_indicators.isChecked(),
                "browser": "brave" if self.radio_brave.isChecked() else "chrome",
                "ticker": self.cb_ticker.currentText().strip().upper(),
            }
        )

    def _is_specific_ticker_mode(self) -> bool:
        return str(self.cb_ticker_scope.currentData() or "all") == "ticker"

    def _on_ticker_scope_changed(self, *_args) -> None:
        self.cb_ticker.setEnabled(self._is_specific_ticker_mode())

    def _on_history_selected(self) -> None:
        items = self.list_history.selectedItems()
        if not items:
            return
        code = items[0].data(Qt.UserRole) or ""
        self.editor.setPlainText(str(code))

    # ---------- Actions ----------
    def _on_launch_browser_9222(self) -> None:
        browser_type = "brave" if self.radio_brave.isChecked() else "chrome"
        app_name = "Brave" if browser_type == "brave" else "Chrome"
        target_url = "https://tw.tradingview.com/chart/"
        candidates = self._browser_candidates(browser_type)
        browser_path = next((p for p in candidates if p and Path(p).exists()), None)
        if not browser_path:
            manual_cmd = (
                'open -na "Brave Browser" --args --remote-debugging-port=9222 '
                '"https://tw.tradingview.com/chart/"'
                if sys.platform == "darwin" and browser_type == "brave"
                else (
                    'open -na "Google Chrome" --args --remote-debugging-port=9222 '
                    '"https://tw.tradingview.com/chart/"'
                    if sys.platform == "darwin"
                    else f'{browser_type} --remote-debugging-port=9222 "{target_url}"'
                )
            )
            QMessageBox.warning(
                self,
                "找不到瀏覽器",
                f"找不到 {app_name} 可執行檔。\n"
                "請手動啟動：\n"
                f"{manual_cmd}",
            )
            return

        profile_dir = Path(tempfile.gettempdir()) / "gex_tv_cdp_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.Popen(
                [
                    browser_path,
                    "--remote-debugging-port=9222",
                    "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={profile_dir}",
                    "--new-window",
                    target_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            QMessageBox.critical(self, "啟動失敗", f"無法啟動瀏覽器：{exc}")
            self.lbl_status.setText(f"啟動 {app_name} 失敗。")
            self.lbl_status.setStyleSheet("color:#FF6B6B;")
            return

        if self._wait_for_cdp_ready():
            self.lbl_status.setText(f"已啟動 {app_name}（9222）並開啟 TradingView chart。")
            self.lbl_status.setStyleSheet("color:#2CC985;")
            return

        self.lbl_status.setText(f"{app_name} 已啟動，但 9222 尚未可連線。")
        self.lbl_status.setStyleSheet("color:#FF6B6B;")
        QMessageBox.warning(
            self,
            "9222 未就緒",
            "偵測到瀏覽器啟動，但 CDP 9222 尚未可連線。\n"
            "請先完全關閉所有 Chrome/Brave 視窗後，再按一次本按鈕。",
        )

    def _browser_candidates(self, browser_type: str) -> list[str | None]:
        kind = "brave" if str(browser_type).strip().lower() == "brave" else "chrome"
        if kind == "brave":
            if sys.platform.startswith("win"):
                return [
                    shutil.which("brave"),
                    shutil.which("brave.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                ]
            if sys.platform == "darwin":
                return [
                    shutil.which("brave"),
                    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                ]
            return [
                shutil.which("brave-browser"),
                shutil.which("brave"),
            ]
        if sys.platform.startswith("win"):
            return [
                shutil.which("chrome"),
                shutil.which("chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Google\\Chrome\\Application\\chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google\\Chrome\\Application\\chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
            ]
        if sys.platform == "darwin":
            return [
                shutil.which("google-chrome"),
                shutil.which("chrome"),
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        return [
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium-browser"),
            shutil.which("chromium"),
            shutil.which("chrome"),
        ]

    def _copy_to_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self.editor.toPlainText()
        QApplication.clipboard().setText(text)
        self.lbl_status.setText("已複製到剪貼簿。")
        self.lbl_status.setStyleSheet("color:#2CC985;")

    def _on_stop_batch_scan(self) -> None:
        self._cancel_batch_scan = True
        self._exec_log("【Stop】已送出停止請求，將在目前步驟完成後結束批次。")

    def _on_phase_b_scan(self) -> None:
        opts = self._build_batch_options()
        if opts is None:
            QMessageBox.warning(self, "缺少 ticker", "請先選擇或輸入 ticker。")
            return
        if self._scan_thread is not None and self._scan_thread.isRunning():
            QMessageBox.information(self, "執行中", "已有批次在背景執行，請等待完成或先按 Stop。")
            return

        self._cancel_batch_scan = False
        self.b_phase_b.setEnabled(False)
        self.b_phase_b_preview.setEnabled(False)
        self.b_phase_b_cleanup.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.progress.setValue(0)
        self._exec_log_clear()
        if opts.ticker_scope == "all":
            self._exec_log(
                "── 批次執行 ──\n"
                f"  ticker 範圍：{opts.ticker_scope}（由各子圖辨識）\n"
                f"  版面範圍：{opts.layout_scope}｜寫入前先整理過期指標："
                f"{'是' if opts.organize_indicators else '否'}"
            )
            self.lbl_status.setText("批次執行中：各子圖辨識 ticker（版面／子圖）…")
        else:
            ticker_label = (opts.ticker or "").strip() or "（未指定）"
            self._exec_log(
                "── 批次執行 ──\n"
                f"  ticker 範圍：{opts.ticker_scope}（目標：{ticker_label}）\n"
                f"  版面範圍：{opts.layout_scope}｜寫入前先整理過期指標："
                f"{'是' if opts.organize_indicators else '否'}"
            )
            self.lbl_status.setText(f"批次執行中：{ticker_label}（版面／子圖）…")
        self.lbl_status.setStyleSheet("color:#FFA500;")

        self._scan_thread = _PhaseBScanThread(self, opts)
        self._scan_thread.succeeded.connect(self._on_phase_b_scan_succeeded)
        self._scan_thread.failed.connect(self._on_phase_b_scan_failed)
        self._scan_thread.finished.connect(self._on_phase_b_scan_thread_finished)
        self._scan_thread.start()

    def _on_phase_b_scan_succeeded(self, report: BatchReport) -> None:
        was_cancelled = self._cancel_batch_scan
        if was_cancelled:
            self._exec_log("【已停止】批次已由使用者中止（以下為已執行部分的摘要）。")
        if report.total == 0:
            sample = ", ".join(self._last_phase_b_symbols[:8]) if self._last_phase_b_symbols else "(無)"
            if self._last_phase_b_matched_subcharts > 0:
                self.lbl_status.setText(
                    "批次結束：symbol 已匹配，但所選週期無可用 TV code。"
                    f"（子圖約 {self._last_phase_b_matched_subcharts}）範例：{sample}"
                )
            else:
                self.lbl_status.setText(
                    f"批次結束：0 筆匹配（未更新）。掃描到的 symbol：{sample}"
                )
        else:
            self.lbl_status.setText(
                f"批次結束：成功 {report.done}／略過 {report.skipped}／失敗 {report.failed}（合計 {report.total}）"
            )
        self.lbl_status.setStyleSheet("color:#2CC985;" if report.failed == 0 else "color:#FFA500;")
        self.progress.setValue(100 if report.total > 0 else 0)
        self._exec_log(self._format_batch_report_exec(report))

    def _on_phase_b_scan_failed(self, exc: BaseException) -> None:
        QMessageBox.critical(self, "批次執行失敗", f"錯誤：{exc}")
        self.lbl_status.setText("批次執行失敗。")
        self.lbl_status.setStyleSheet("color:#FF6B6B;")
        self._exec_log(f"【批次失敗】{exc}")

    def _on_phase_b_scan_thread_finished(self) -> None:
        self.b_stop.setEnabled(False)
        self.b_phase_b.setEnabled(True)
        self.b_phase_b_preview.setEnabled(True)
        self.b_phase_b_cleanup.setEnabled(True)
        self._cancel_batch_scan = False
        self._scan_thread = None

    def _on_phase_b_preview(self) -> None:
        opts = self._build_batch_options()
        if opts is None:
            QMessageBox.warning(self, "缺少 ticker", "請先選擇或輸入 ticker。")
            return
        self.b_phase_b_preview.setEnabled(False)
        self._cancel_batch_scan = False
        self.b_stop.setEnabled(True)
        self._last_phase_b_layout_list_degraded = False
        self._exec_log_clear()
        if opts.ticker_scope == "all":
            self._exec_log(
                f"── 預覽（不寫入）── ticker 範圍={opts.ticker_scope} 版面={opts.layout_scope}"
            )
        else:
            ticker_label = (opts.ticker or "").strip() or "（未指定）"
            self._exec_log(
                f"── 預覽（不寫入）── ticker 範圍={opts.ticker_scope} 目標={ticker_label} 版面={opts.layout_scope}"
            )
        thread = _AsyncCoroThread(self, lambda: self._phase_b_preview_flow(opts=opts))
        thread.succeeded.connect(lambda items, o=opts: self._on_phase_b_preview_succeeded(o, items))
        thread.failed.connect(self._on_phase_b_preview_failed)
        thread.finished.connect(self._on_phase_b_preview_finished)
        thread.finished.connect(thread.deleteLater)
        self._preview_thread = thread
        thread.start()

    def _on_phase_b_preview_succeeded(self, opts: BatchOptions, items) -> None:
        all_items = items
        view_items = [it for it in all_items if self._preview_item_is_actionable(it)]
        if not view_items:
            self.preview_table.setRowCount(0)
            if all_items:
                self._exec_log(
                    f"【預覽】表格顯示 0 列（已隱藏 {len(all_items)} 列略過）。"
                )
            else:
                self._exec_log("【預覽】沒有符合條件的可執行項目（0 筆）。")
            self._warn_if_layout_list_degraded(opts)
            self.lbl_status.setText("預覽掃描完成：0 列。")
            self.lbl_status.setStyleSheet("color:#7FB3FF;")
        else:
            self._populate_preview_table(view_items)
            hidden = len(all_items) - len(view_items)
            base = (
                f"【預覽】表格顯示 {len(view_items)} 列（掃描共 {len(all_items)} 列；"
                f"與「開始執行」相同流程，僅不寫入、不刪過期、不儲存版面）。"
                f"掃描到 {len(self._last_phase_b_layouts)} 個版面。"
            )
            if hidden > 0:
                base += f" 已隱藏 {hidden} 列略過。"
            self._exec_log(base)
            self._warn_if_layout_list_degraded(opts)
            self.lbl_status.setText(f"預覽掃描完成：{len(view_items)} 列。")
            self.lbl_status.setStyleSheet("color:#2CC985;")

    def _on_phase_b_preview_failed(self, exc: BaseException) -> None:
        QMessageBox.critical(self, "預覽掃描失敗", f"錯誤：{exc}")
        self._exec_log(f"【預覽失敗】{exc}")

    def _on_phase_b_preview_finished(self) -> None:
        self.b_stop.setEnabled(False)
        self._cancel_batch_scan = False
        self.b_phase_b_preview.setEnabled(True)
        self._preview_thread = None

    def _on_phase_b_cleanup(self) -> None:
        opts = self._build_batch_options()
        if opts is None:
            QMessageBox.warning(self, "缺少 ticker", "請先選擇或輸入 ticker。")
            return
        self.b_phase_b_cleanup.setEnabled(False)
        self.b_phase_b.setEnabled(False)
        self.progress.setValue(0)
        self._last_phase_b_layout_list_degraded = False
        self._exec_log_clear()
        if opts.ticker_scope == "all":
            self._exec_log(
                f"── 整理過期 GEX 指標 ──\n"
                f"  ticker 範圍：{opts.ticker_scope}（由各子圖辨識）｜版面：{opts.layout_scope}"
            )
            self.lbl_status.setText("Cleanup 進行中：各子圖辨識（layouts/subcharts）")
        else:
            ticker_label = (opts.ticker or "").strip() or "（未指定）"
            self._exec_log(
                f"── 整理過期 GEX 指標 ──\n"
                f"  ticker：{ticker_label}（{opts.ticker_scope}）｜版面：{opts.layout_scope}"
            )
            self.lbl_status.setText(f"Cleanup 進行中：{ticker_label}（layouts/subcharts）")
        self.lbl_status.setStyleSheet("color:#FFA500;")
        thread = _AsyncCoroThread(self, lambda: self._phase_b_cleanup_flow(opts=opts))
        thread.succeeded.connect(self._on_phase_b_cleanup_succeeded)
        thread.failed.connect(self._on_phase_b_cleanup_failed)
        thread.finished.connect(self._on_phase_b_cleanup_finished)
        thread.finished.connect(thread.deleteLater)
        self._cleanup_thread = thread
        thread.start()

    def _on_phase_b_cleanup_succeeded(self, report) -> None:
        if report.total == 0:
            self.lbl_status.setText("Cleanup 完成：0 筆目標。")
        else:
            self.lbl_status.setText(
                f"Cleanup 完成：done={report.done} / skipped={report.skipped} / failed={report.failed}（total={report.total}）"
            )
        self.lbl_status.setStyleSheet("color:#2CC985;" if report.failed == 0 else "color:#FFA500;")
        self.progress.setValue(100 if report.total > 0 else 0)
        self._exec_log(self._format_batch_report_exec(report))

    def _on_phase_b_cleanup_failed(self, exc: BaseException) -> None:
        QMessageBox.critical(self, "Cleanup 執行失敗", f"錯誤：{exc}")
        self.lbl_status.setText("Cleanup 失敗。")
        self.lbl_status.setStyleSheet("color:#FF6B6B;")

    def _on_phase_b_cleanup_finished(self) -> None:
        self.b_phase_b_cleanup.setEnabled(True)
        self.b_phase_b.setEnabled(True)
        self._cleanup_thread = None

    async def _apply_work_item_with_automator(
        self,
        automator: PlaywrightCDPAutomator,
        item: WorkItem,
        *,
        skip_if_has_values: bool,
        subchart_cache: WeeklyGexSubchartCache | None = None,
        dry_run: bool = False,
    ) -> BatchResultItem:
        ticker = item.ticker
        monday = item.monday
        codes = item.codes
        # Futures alias (e.g. ES1!→SPX): indicator anchors at Sunday 18:00 instead of Monday <ticker_time>.
        # DB lookup still uses ``item.monday`` (the trading-week Monday).
        if item.is_futures:
            indicator_date = monday - timedelta(days=1)
            indicator_start_time = _FUTURES_START_TIME
        else:
            indicator_date = monday
            indicator_start_time = self._resolve_start_time_for_ticker(ticker)
        start_time = indicator_start_time
        layout_label = item.layout_name or item.layout_id or "（目前圖表）"
        sub_txt = str(item.subchart_index) if item.subchart_index is not None else "—"
        sym_txt = item.subchart_symbol or "—"

        async def _page_url() -> str:
            if item.chart_url:
                return str(item.chart_url)
            snap = await automator.get_runtime_snapshot()
            return str(snap.get("url") or "—")

        target_subchart = item.subchart_index
        if target_subchart is None:
            target_subchart = await self._infer_subchart_index_for_ticker(automator, ticker)

        automator.set_indicator_scope_subchart(target_subchart)
        if target_subchart is not None:
            expected_symbol = item.subchart_symbol or ticker
            locked = await self._lock_target_subchart_context(
                automator,
                subchart_index=target_subchart,
                expected_symbol=expected_symbol,
            )
            if not locked:
                u = await _page_url()
                self._exec_log(
                    f"【失敗｜子圖對位】版面={layout_label} URL={u} 子圖#{sub_txt} "
                    f"圖上={sym_txt} ticker={ticker}\n  原因：無法對準預期 symbol（漂移）"
                )
                rit = replace(item, preview_status="失敗：無法鎖定子圖 symbol") if dry_run else item
                return BatchResultItem(item=rit, status="failed", message="無法鎖定目標子圖（symbol 漂移）")
            pinned = await automator.pin_indicator_scope_to_subchart(target_subchart)
            if not pinned:
                u = await _page_url()
                self._exec_log(
                    f"【失敗｜scope】版面={layout_label} URL={u} 子圖#{sub_txt} ticker={ticker}\n"
                    "  原因：無法在圖表區標定該子圖（scope pin 失敗）"
                )
                rit = replace(item, preview_status="失敗：scope pin") if dry_run else item
                return BatchResultItem(item=rit, status="failed", message="無法鎖定目標子圖（scope pin 失敗）")

        try:
            target_iso = indicator_date.isoformat()
            if (
                subchart_cache is not None
                and subchart_cache.probe_complete
                and skip_if_has_values
            ):
                snaps = [r for r in subchart_cache.rows if r.start_iso == target_iso]
                if snaps:
                    def _snap_fill_score(s) -> int:
                        return sum(
                            1
                            for d in (
                                "Monday",
                                "Tuesday",
                                "Wednesday",
                                "Thursday",
                                "Friday",
                            )
                            if (s.levels.get(d) or "").strip()
                        )

                    snap = max(snaps, key=_snap_fill_score)
                    missing_only_codes = {
                        day: (
                            None
                            if (snap.levels.get(day) or "").strip()
                            else code
                        )
                        for day, code in codes.items()
                    }
                    if all(code is None for code in missing_only_codes.values()):
                        u = await _page_url()
                        self._exec_log(
                            f"【略過｜快取】版面={layout_label} URL={u} 子圖#{sub_txt} 圖上={sym_txt} "
                            f"ticker={ticker} 週一起={monday}\n  原因：該週可填欄位皆已有值（子圖載入前掃描）"
                        )
                        rit = (
                            replace(item, preview_status="略過：子圖快取顯示該週欄位已有值")
                            if dry_run
                            else item
                        )
                        return BatchResultItem(
                            item=rit,
                            status="skipped",
                            message="該週可用天皆已有值",
                        )
                    if dry_run:
                        planned = [d for d in _DAY_ORDER if missing_only_codes.get(d)]
                        fills = self._abbr_weekday_labels(planned)
                        u = await _page_url()
                        self._exec_log(
                            f"【預覽｜快取】版面={layout_label} URL={u} 子圖#{sub_txt} 圖上={sym_txt} "
                            f"ticker={ticker} 週一起={monday}\n  執行時將填：{fills}"
                        )
                        return BatchResultItem(
                            item=replace(item, preview_status=f"預覽：執行將填 {fills}"),
                            status="skipped",
                            message="preview_would_fill_from_cache",
                        )

            state = await automator.open_or_create_indicator_for_week(
                monday=indicator_date,
                subchart_cache=subchart_cache,
                dry_run=dry_run,
            )
            if state == "would_create":
                u = await _page_url()
                planned = [d for d in _DAY_ORDER if (codes.get(d) or "").strip()]
                fills = self._abbr_weekday_labels(planned)
                self._exec_log(
                    f"【預覽】版面={layout_label} URL={u} 子圖#{sub_txt} 圖上={sym_txt} "
                    f"ticker={ticker} 週一起={monday}\n  將新增指標並填：{fills}"
                )
                return BatchResultItem(
                    item=replace(
                        item,
                        preview_status=f"預覽：尚無該週指標，執行將新增並填 {fills}",
                    ),
                    status="skipped",
                    message="preview_would_create",
                )
            if state == "existing":
                opened_date, _opened_time = await automator.read_weekly_start_datetime()
                opened_start = (opened_date or "").strip()
                expected_start = indicator_date.isoformat()
                if opened_start and opened_start != expected_start:
                    await automator.close_settings(save=False)
                    msg = (
                        "existing 指標週期不符，已中止以避免誤判 skip: "
                        f"expected={expected_start}, opened={opened_start}"
                    )
                    u = await _page_url()
                    self._exec_log(
                        f"【失敗｜週期不符】版面={layout_label} URL={u} 子圖#{sub_txt} "
                        f"ticker={ticker} 週一起={monday}\n  原因：{msg}"
                    )
                    rit = replace(item, preview_status=f"失敗：{msg}") if dry_run else item
                    return BatchResultItem(item=rit, status="failed", message=msg)
            if state == "existing":
                existing_levels = await automator.read_weekly_levels()
                missing_only_codes = {
                    day: (
                        None
                        if (existing_levels.get(day) or "").strip()
                        else code
                    )
                    for day, code in codes.items()
                }
                if skip_if_has_values:
                    codes = missing_only_codes
                else:
                    codes = dict(codes)
                if all(code is None for code in missing_only_codes.values()):
                    await automator.close_settings(save=False)
                    u = await _page_url()
                    self._exec_log(
                        f"【略過】版面={layout_label} URL={u} 子圖#{sub_txt} 圖上={sym_txt} "
                        f"ticker={ticker} 週一起={monday}\n  原因：該週可填欄位皆已有值"
                    )
                    rit = replace(item, preview_status="略過：圖上該週欄位已有值") if dry_run else item
                    return BatchResultItem(item=rit, status="skipped", message="該週可用天皆已有值")
                if dry_run:
                    planned = [d for d in _DAY_ORDER if missing_only_codes.get(d)]
                    fills = self._abbr_weekday_labels(planned)
                    date_val, time_val = await automator.read_weekly_start_datetime()
                    got_date = (date_val or "").strip()
                    got_time = (time_val or "").strip()
                    expected_date = indicator_date.isoformat()
                    expected_time = indicator_start_time.strip()
                    if got_date != expected_date or got_time != expected_time:
                        await automator.close_settings(save=False)
                        msg = (
                            "開始時間驗證失敗(預覽): "
                            f"expected={expected_date} {expected_time}, got={got_date or '-'} {got_time or '-'}"
                        )
                        u = await _page_url()
                        self._exec_log(
                            f"【失敗｜起始時間】版面={layout_label} URL={u} ticker={ticker} 週一起={monday}\n"
                            f"  原因：{msg}"
                        )
                        rit = replace(item, preview_status=f"失敗：{msg}")
                        return BatchResultItem(item=rit, status="failed", message=msg)
                    await automator.close_settings(save=False)
                    u = await _page_url()
                    self._exec_log(
                        f"【預覽】版面={layout_label} URL={u} 子圖#{sub_txt} ticker={ticker} 週一起={monday}\n"
                        f"  執行時將填：{fills}"
                    )
                    return BatchResultItem(
                        item=replace(
                            item,
                            preview_status=f"預覽：執行將填 {fills}",
                        ),
                        status="skipped",
                        message="preview_would_fill",
                    )

            await automator.set_weekly_start_date(monday=indicator_date, time_str=indicator_start_time)
            date_val, time_val = await automator.read_weekly_start_datetime()
            got_date = (date_val or "").strip()
            got_time = (time_val or "").strip()
            expected_date = indicator_date.isoformat()
            expected_time = indicator_start_time.strip()
            if got_date != expected_date or got_time != expected_time:
                await automator.close_settings(save=False)
                msg = (
                    "開始時間驗證失敗: "
                    f"expected={expected_date} {expected_time}, got={got_date or '-'} {got_time or '-'}"
                )
                u = await _page_url()
                self._exec_log(
                    f"【失敗｜起始時間】版面={layout_label} URL={u} ticker={ticker} 週一起={monday}\n  原因：{msg}"
                )
                rit = replace(item, preview_status=f"失敗：{msg}") if dry_run else item
                return BatchResultItem(item=rit, status="failed", message=msg)
            filled_days = await automator.fill_weekly_levels(
                codes,
                clear_missing=(state == "created"),
            )
            await automator.close_settings(save=True)
            u = await _page_url()
            verb = "新增 GEX 指標並填欄位" if state == "created" else "更新 GEX 指標欄位"
            fills = self._abbr_weekday_labels(filled_days) if filled_days else "—"
            if item.is_futures:
                start_line = (
                    f"  週一起：{monday}  指標起始：{indicator_date} {indicator_start_time}"
                    f"（期貨：{sym_txt}）  已寫入：{fills}"
                )
            else:
                start_line = f"  週一起：{monday}  開盤時間：{indicator_start_time}  已寫入：{fills}"
            self._exec_log(
                f"【{verb}】\n"
                f"  版面：{layout_label}\n"
                f"  URL：{u}\n"
                f"  子圖#{sub_txt} 圖上商品：{sym_txt}  DB ticker：{ticker}\n"
                f"{start_line}"
            )
            return BatchResultItem(item=item, status="done")
        except IndicatorQuotaExceededError as exc:
            u = await _page_url()
            self._exec_log(
                f"【略過｜指標配額】版面={layout_label} URL={u} 子圖#{sub_txt} "
                f"ticker={ticker} 週一起={monday}\n  原因：{exc}"
            )
            rit = replace(item, preview_status=f"略過：{exc}") if dry_run else item
            return BatchResultItem(item=rit, status="skipped", message=f"skip_quota: {exc}")
        except Exception as exc:  # noqa: BLE001
            u = await _page_url()
            err = str(exc).replace("\n", " ")
            if len(err) > 200:
                err = err[:197] + "…"
            self._exec_log(
                f"【失敗】版面={layout_label} URL={u} 子圖#{sub_txt} ticker={ticker} 週一起={monday}\n  錯誤：{err}"
            )
            rit = replace(item, preview_status=f"失敗：{err}") if dry_run else item
            return BatchResultItem(item=rit, status="failed", message=str(exc))
        finally:
            automator.set_indicator_scope_subchart(None)
            await automator.clear_indicator_scope_marker()

    async def _infer_subchart_index_for_ticker(
        self,
        automator: PlaywrightCDPAutomator,
        ticker: str,
    ) -> int | None:
        """Best-effort infer subchart index by matching symbol to ticker."""
        try:
            subcharts = await automator.enumerate_subcharts()
        except Exception:
            return None
        for sub in subcharts:
            if self._symbol_matches_ticker(sub.symbol, ticker):
                return sub.index
        return None

    async def _lock_target_subchart_context(
        self,
        automator: PlaywrightCDPAutomator,
        *,
        subchart_index: int,
        expected_symbol: str,
        retries: int = 3,
    ) -> bool:
        """Ensure active subchart remains the intended one before editing."""
        expected = (expected_symbol or "").strip()
        for attempt in range(retries + 1):
            await automator.activate_subchart(subchart_index)
            actual = (await automator.get_symbol_search_value() or "").strip()
            if not expected:
                if actual:
                    return True
            elif self._symbols_compatible(expected, actual):
                return True
            if attempt < retries:
                await asyncio.sleep(0.25)
        return False

    @staticmethod
    def _symbols_compatible(expected: str, actual: str) -> bool:
        exp = expected.strip().upper()
        act = actual.strip().upper()
        if not exp or not act:
            return False
        if exp == act:
            return True
        if exp in act or act in exp:
            return True
        exp_tail = exp.split(":")[-1]
        act_tail = act.split(":")[-1]
        return exp_tail == act_tail or exp_tail in act_tail or act_tail in exp_tail

    async def _apply_work_item_with_retry(
        self,
        automator: PlaywrightCDPAutomator,
        item: WorkItem,
        *,
        skip_if_has_values: bool,
        max_retry: int = 1,
        subchart_cache: WeeklyGexSubchartCache | None = None,
        dry_run: bool = False,
    ) -> BatchResultItem:
        """Apply one item with bounded retries for transient TV UI failures."""
        result = await self._apply_work_item_with_automator(
            automator,
            item,
            skip_if_has_values=skip_if_has_values,
            subchart_cache=subchart_cache,
            dry_run=dry_run,
        )
        if dry_run or result.status != "failed":
            return result
        msg = (result.message or "").strip().lower()
        if "could not deterministically open newly added indicator settings" in msg:
            self._exec_log(
                f"【略過重試】ticker={item.ticker} 週一起={item.monday}：指標設定視窗無法穩定開啟（非暫態）。"
            )
            return result
        if "新建 indicator 欄位非空白" in (result.message or "") or "opened a pre-existing indicator after add" in msg:
            self._exec_log(
                f"【略過重試】ticker={item.ticker} 週一起={item.monday}：新增指標對位失敗（非暫態）。"
            )
            return result

        for _attempt in range(1, max_retry + 1):
            await asyncio.sleep(0.9)
            result = await self._apply_work_item_with_automator(
                automator,
                item,
                skip_if_has_values=skip_if_has_values,
                subchart_cache=subchart_cache,
                dry_run=dry_run,
            )
            if result.status != "failed":
                self._exec_log(
                    f"【重試後成功】ticker={item.ticker} 週一起={item.monday}（版面={item.layout_name or item.layout_id or '—'}）"
                )
                return result
        return result

    async def _enumerate_subcharts_with_retry(
        self,
        automator: PlaywrightCDPAutomator,
        *,
        label: str,
        retries: int = 1,
    ) -> list:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await automator.enumerate_subcharts()
            except Exception as exc:  # noqa: BLE001 - keep batch resilient
                last_exc = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(1.2)
        if last_exc is not None:
            self._exec_log(f"【警告】{label} 無法取得子圖清單：{last_exc}")
        return []

    def _build_batch_options(self) -> BatchOptions | None:
        weeks = str(self.cb_phase_weeks.currentData() or "this_week")
        layout_scope = str(self.cb_layout_scope.currentData() or "all")
        ticker_scope = str(self.cb_ticker_scope.currentData() or "all")
        ticker = self.cb_ticker.currentText().strip().upper()
        if ticker_scope == "ticker" and not ticker:
            return None
        return BatchOptions(
            layout_scope=layout_scope,  # type: ignore[arg-type]
            ticker_scope=ticker_scope,  # type: ignore[arg-type]
            ticker=ticker or None,
            weeks=weeks,  # type: ignore[arg-type]
            skip_filled_days=self.chk_skip_if_has_values.isChecked(),
            apply_visibility_preset=self.chk_visibility_preset.isChecked(),
            organize_indicators=self.chk_organize_indicators.isChecked(),
        )

    async def _resolve_target_layouts(
        self,
        automator: PlaywrightCDPAutomator,
        opts: BatchOptions,
    ) -> list[LayoutInfo]:
        if opts.layout_scope == "active":
            active_name = (await automator.get_current_layout_name() or "Current Layout").strip()
            return [LayoutInfo(id="active", name=active_name)]
        return await automator.list_layouts()

    def _sync_last_phase_b_layout_snap(self, layouts: list[LayoutInfo]) -> None:
        self._last_phase_b_layouts = [
            f"{layout.name}{f' | {layout.subtitle}' if layout.subtitle else ''}"
            for layout in layouts
        ]
        self._last_phase_b_layout_list_degraded = (
            len(layouts) == 1 and layouts[0].id == "current"
        )

    def _warn_if_layout_list_degraded(self, opts: BatchOptions) -> None:
        if opts.layout_scope != "all":
            return
        if not self._last_phase_b_layout_list_degraded:
            return
        self._exec_log(
            "【注意｜版面清單】無法從 TradingView 讀取已存版面列表（對話框未開啟或列為空），"
            "已降級為僅「目前頁面／Current」。請點圖表區、關閉其他選單；程式會依序試「.」、"
            "管理選單內「Open layout…／開啟版面」、以及標題列版面名稱按鈕，不會去點圖表屬性類的「版面設定」。"
        )

    def _resolve_target_ticker_for_subchart(
        self,
        symbol: str | None,
        opts: BatchOptions,
        layout_mode: str = _FUTURES_DEFAULT_MODE,
    ) -> tuple[str | None, bool]:
        """Resolve subchart symbol → (DB ticker, is_futures_alias) under a layout mode.

        Strict: if the symbol matches a futures alias entry but the requested
        ``layout_mode`` has no DB ticker for it (e.g. RTY1! in index mode →
        RUT not imported), returns ``(None, False)`` so the caller can emit a
        clear strict-skip log. The caller distinguishes "alias known but mode
        unmapped" from "symbol unknown" by calling :meth:`_futures_alias_lookup`.
        """
        entry = self._futures_alias_lookup(symbol)
        aliased = entry.get(layout_mode) if entry else None
        if opts.ticker_scope == "ticker":
            target = (opts.ticker or "").strip().upper()
            if not target:
                return (None, False)
            if aliased and aliased == target:
                return (target, True)
            # Known futures alias: do NOT fall through to plain symbol match.
            # User must use the alias-mode router (or change mode/target).
            if entry is not None:
                return (None, False)
            if self._symbol_matches_ticker(symbol, target):
                return (target, False)
            return (None, False)
        # scope == "all"
        if aliased:
            return (aliased, True)
        if entry is not None:
            # Known futures alias but this mode has no DB mapping → strict skip.
            return (None, False)
        return (self._extract_ticker_from_symbol(symbol), False)

    @staticmethod
    def _futures_alias_lookup(symbol: str | None) -> dict[str, str | None] | None:
        """Return the alias entry dict if symbol's tail matches the alias map, else None."""
        text = str(symbol or "").strip().upper()
        if not text:
            return None
        tail = text.split(":")[-1]
        return _FUTURES_ALIAS_MAP.get(tail)

    @classmethod
    def _futures_alias_for_symbol(
        cls,
        symbol: str | None,
        mode: str = _FUTURES_DEFAULT_MODE,
    ) -> str | None:
        """Strict: return DB ticker for symbol+mode, or None if no mapping."""
        entry = cls._futures_alias_lookup(symbol)
        if not entry:
            return None
        return entry.get(mode)

    @staticmethod
    def _parse_layout_marker_sequence(name: str | None) -> list[str]:
        """Return the ordered list of mode markers found in ``name`` (case-insensitive).

        Multiple occurrences of the same or different markers preserve order of
        appearance. e.g. ``"ES1! [equity] + ES1! [index] + ES1! [fut]"`` →
        ``["equity", "index", "futures"]``.
        """
        if not name:
            return []
        upper = name.upper()
        found: list[tuple[int, str]] = []
        for mode, markers in _LAYOUT_MODE_MARKERS.items():
            for marker in markers:
                mu = marker.upper()
                start = 0
                while True:
                    idx = upper.find(mu, start)
                    if idx < 0:
                        break
                    found.append((idx, mode))
                    start = idx + len(mu)
        found.sort(key=lambda x: x[0])
        return [m for _pos, m in found]

    @classmethod
    def _resolve_layout_mode_for_subchart(
        cls,
        name: str | None,
        subchart_index: int | None,
        default_mode: str = _FUTURES_DEFAULT_MODE,
    ) -> str:
        """Resolve the mode for a given subchart based on layout-name markers.

        - 0 markers: ``default_mode`` for every subchart.
        - 1 marker:  layout-level — that mode applies to every subchart.
        - 2+ markers: positional — subchart i ↔ markers[i]; subcharts beyond
          marker count fall back to ``default_mode``.
        """
        markers = cls._parse_layout_marker_sequence(name)
        if not markers:
            return default_mode
        if len(markers) == 1:
            return markers[0]
        idx = int(subchart_index) if subchart_index is not None else 0
        if 0 <= idx < len(markers):
            return markers[idx]
        return default_mode

    @staticmethod
    def _extract_ticker_from_symbol(symbol: str | None) -> str | None:
        text = str(symbol or "").strip().upper()
        if not text:
            return None
        tail = text.split(":")[-1]
        match = re.search(r"[A-Z][A-Z0-9._-]{0,14}", tail)
        if match:
            return match.group(0)
        fallback = re.search(r"[A-Z][A-Z0-9._-]{0,14}", text)
        return fallback.group(0) if fallback else None

    @staticmethod
    def _resolve_start_time_for_ticker(ticker: str) -> str:
        return shared_config.get_tradingview_start_time(ticker)

    @staticmethod
    def _compute_cleanup_keep_mondays(weeks: int = 4, today: date | None = None) -> list[date]:
        """Compute keep-window Mondays using Sunday as week start."""
        d = today or date.today()
        # Monday=0..Sunday=6 -> Sunday offset should be 0 on Sunday.
        days_from_sunday = (d.weekday() + 1) % 7
        this_sunday = d - timedelta(days=days_from_sunday)
        keep: list[date] = []
        for i in range(max(1, weeks)):
            sunday = this_sunday - timedelta(days=7 * i)
            keep.append(sunday + timedelta(days=1))
        keep.sort()
        return keep

    async def _run_cleanup_for_subchart(
        self,
        automator: PlaywrightCDPAutomator,
        *,
        layout_label: str,
        subchart_index: int,
        subchart_symbol: str | None,
        ticker: str,
    ) -> BatchResultItem:
        keep_mondays = self._compute_cleanup_keep_mondays(weeks=4)
        pivot = keep_mondays[-1]
        item = WorkItem(
            ticker=ticker,
            monday=pivot,
            codes={},
            available_days=[],
            layout_id=layout_label,
            layout_name=layout_label,
            subchart_index=subchart_index,
            subchart_symbol=subchart_symbol,
            note="cleanup_only",
        )
        automator.set_indicator_scope_subchart(subchart_index)
        try:
            locked = await self._lock_target_subchart_context(
                automator,
                subchart_index=subchart_index,
                expected_symbol=subchart_symbol or ticker,
            )
            if not locked:
                msg = "cleanup 無法鎖定目標子圖（symbol 漂移）"
                u = str((await automator.get_runtime_snapshot()).get("url") or "—")
                self._exec_log(
                    f"【整理失敗｜子圖】版面={layout_label} URL={u} 子圖#{subchart_index} "
                    f"ticker={ticker} 圖上={subchart_symbol or '—'}\n  原因：{msg}"
                )
                return BatchResultItem(item=item, status="failed", message=msg)
            pinned = await automator.pin_indicator_scope_to_subchart(subchart_index)
            if not pinned:
                msg = "cleanup 無法鎖定目標子圖（scope pin 失敗）"
                u = str((await automator.get_runtime_snapshot()).get("url") or "—")
                self._exec_log(
                    f"【整理失敗｜scope】版面={layout_label} URL={u} 子圖#{subchart_index} ticker={ticker}\n  原因：{msg}"
                )
                return BatchResultItem(item=item, status="failed", message=msg)

            stats = await automator.remove_expired_weekly_gex_indicators(
                keep_mondays=keep_mondays,
            )
            u = str((await automator.get_runtime_snapshot()).get("url") or "—")
            self._exec_log(
                f"【刪除過期指標】版面={layout_label} URL={u} 子圖#{subchart_index} "
                f"ticker={ticker} 圖上={subchart_symbol or '—'}\n"
                f"  刪除前 {stats.get('before', 0)} 個｜已移除 {stats.get('removed', 0)} 個｜"
                f"其他 {stats.get('recreated', 0)} 筆調整"
            )
            return BatchResultItem(item=item, status="done")
        except IndicatorQuotaExceededError as exc:
            u = str((await automator.get_runtime_snapshot()).get("url") or "—")
            self._exec_log(
                f"【略過｜指標配額】版面={layout_label} URL={u} 子圖#{subchart_index} ticker={ticker}：{exc}"
            )
            return BatchResultItem(item=item, status="skipped", message=f"skip_quota: {exc}")
        except Exception as exc:  # noqa: BLE001
            u = str((await automator.get_runtime_snapshot()).get("url") or "—")
            self._exec_log(
                f"【整理失敗】版面={layout_label} URL={u} 子圖#{subchart_index} ticker={ticker}：{exc}"
            )
            return BatchResultItem(item=item, status="failed", message=str(exc))
        finally:
            automator.set_indicator_scope_subchart(None)
            await automator.clear_indicator_scope_marker()

    async def _phase_b_cleanup_flow(self, opts: BatchOptions) -> BatchReport:
        automator = PlaywrightCDPAutomator()
        automator.set_apply_visibility_preset(opts.apply_visibility_preset)
        results: list[BatchResultItem] = []
        try:
            await automator.connect()
            layouts = await self._resolve_target_layouts(automator, opts)
            self._sync_last_phase_b_layout_snap(layouts)
            self._warn_if_layout_list_degraded(opts)
            seen_subchart_keys: set[tuple[str, int, str]] = set()
            for layout_idx, layout in enumerate(layouts):
                if opts.layout_scope == "active":
                    switched = True
                else:
                    switched = await automator.load_layout(layout)
                if not switched:
                    if layout_idx == 0:
                        self._exec_log(
                            f"【注意】無法切換至「{layout.name}」，改以目前瀏覽器頁面執行整理。"
                        )
                    else:
                        self._exec_log(f"【略過版面】無法載入：{layout.name}")
                        continue
                subcharts = await self._enumerate_subcharts_with_retry(
                    automator,
                    label="整理流程",
                    retries=1,
                )
                if not subcharts:
                    self._exec_log(f"【警告】版面「{layout.name}」無法取得子圖清單，已略過。")
                    continue

                for sub in subcharts:
                    await automator.activate_subchart(sub.index)
                    search_symbol = await self._read_subchart_symbol_with_retry(
                        automator,
                        expected_symbol=sub.symbol,
                    )
                    layout_mode = self._resolve_layout_mode_for_subchart(layout.name, sub.index)
                    target_ticker, _is_futures_unused = self._resolve_target_ticker_for_subchart(
                        search_symbol, opts, layout_mode=layout_mode
                    )
                    if not target_ticker:
                        continue
                    target_key = (
                        (layout.name or layout.id or "current").upper(),
                        int(sub.index),
                        target_ticker.upper(),
                    )
                    if target_key in seen_subchart_keys:
                        continue
                    seen_subchart_keys.add(target_key)
                    results.append(
                        await self._run_cleanup_for_subchart(
                            automator,
                            layout_label=layout.name or layout.id or "current",
                            subchart_index=sub.index,
                            subchart_symbol=search_symbol or sub.symbol,
                            ticker=target_ticker,
                        )
                    )
            done = sum(1 for r in results if r.status == "done")
            skipped = sum(1 for r in results if r.status == "skipped")
            failed = sum(1 for r in results if r.status == "failed")
            return BatchReport(
                total=len(results),
                done=done,
                skipped=skipped,
                failed=failed,
                items=results,
            )
        finally:
            await automator.close()

    async def _phase_b_scan_flow(
        self,
        opts: BatchOptions,
    ):
        automator = PlaywrightCDPAutomator()
        automator.set_apply_visibility_preset(opts.apply_visibility_preset)
        try:
            await automator.connect()
            mondays = sorted(compute_target_mondays(opts.weeks))
            layouts = await self._resolve_target_layouts(automator, opts)
            self._sync_last_phase_b_layout_snap(layouts)
            self._warn_if_layout_list_degraded(opts)

            try:
                known_tickers: set[str] = {t.upper() for t in db.get_all_tickers()}
            except Exception:
                known_tickers = set()

            seen_symbols: set[str] = set()
            matched_subcharts = 0
            results: list[BatchResultItem] = []
            prev_layout_label: str | None = None
            prev_layout_modified = False
            stop_all = False

            for layout_idx, layout in enumerate(layouts):
                if self._batch_should_stop():
                    self._exec_log("【已停止】使用者中止批次。")
                    stop_all = True
                    break
                if not opts.dry_run and layout_idx > 0 and prev_layout_modified:
                    self._exec_log(
                        f"【已儲存版面】{prev_layout_label or '—'}（切換至下一個版面之前）"
                    )
                    await automator.save_current_layout()
                if opts.layout_scope == "active":
                    switched = True
                else:
                    switched = await automator.load_layout(layout)
                post = await automator.get_runtime_snapshot()
                degraded_current_layout = False
                if not switched:
                    if layout_idx == 0:
                        degraded_current_layout = True
                        self._exec_log(
                            f"【注意】無法切換至「{layout.name}」，改以目前瀏覽器頁面執行批次。"
                        )
                    else:
                        self._exec_log(f"【略過版面】無法載入：{layout.name}")
                        continue
                else:
                    layout_marker_seq = self._parse_layout_marker_sequence(layout.name)
                    if not layout_marker_seq:
                        mode_annotation = f"模式：{_FUTURES_DEFAULT_MODE}（預設）"
                    elif len(layout_marker_seq) == 1:
                        mode_annotation = f"模式：{layout_marker_seq[0]}"
                    else:
                        mode_annotation = f"模式（依子圖序）：{', '.join(layout_marker_seq)}"
                    self._exec_log(
                        f"▸ 版面「{layout.name}」（{mode_annotation}）\n"
                        f"  URL：{str(post.get('url') or '—')}"
                    )
                locked_layout_name = (await automator.get_current_layout_name() or "").upper()
                if not locked_layout_name:
                    locked_layout_name = layout.name.upper()
                matched_keys_in_layout: set[tuple[str, str]] = set()
                subcharts = await self._enumerate_subcharts_with_retry(
                    automator,
                    label="批次掃描",
                    retries=1,
                )
                if not subcharts:
                    self._exec_log(
                        f"【警告】版面「{layout.name}」無法取得子圖清單"
                        f"{'（目前為降級頁面）' if degraded_current_layout else ''}。"
                    )
                    prev_layout_label = layout.name
                    prev_layout_modified = False
                    continue
                layout_modified = False
                for sub in subcharts:
                    if self._batch_should_stop():
                        self._exec_log("【已停止】使用者中止批次。")
                        stop_all = True
                        break
                    current_layout_name = (await automator.get_current_layout_name() or "").upper()
                    if locked_layout_name and current_layout_name and current_layout_name != locked_layout_name:
                        self._exec_log(
                            "【中止】偵測到版面已變更（預期與實際不符），停止此版面後續子圖。"
                        )
                        break
                    await automator.activate_subchart(sub.index)
                    search_symbol = await self._read_subchart_symbol_with_retry(
                        automator,
                        expected_symbol=sub.symbol,
                    )
                    if search_symbol:
                        seen_symbols.add(search_symbol)
                    layout_mode = self._resolve_layout_mode_for_subchart(
                        layout.name, sub.index
                    )
                    target_ticker, is_futures_alias = self._resolve_target_ticker_for_subchart(
                        search_symbol, opts, layout_mode=layout_mode
                    )
                    chosen = search_symbol if target_ticker else None
                    if not chosen:
                        alias_entry = self._futures_alias_lookup(search_symbol)
                        u = str(post.get("url") or "—")
                        if alias_entry is not None:
                            available = sorted(k for k, v in alias_entry.items() if v)
                            if opts.ticker_scope == "ticker":
                                mapped = alias_entry.get(layout_mode)
                                reason = (
                                    f"alias[{layout_mode}]={mapped or '—'}, "
                                    f"與目標 ticker={opts.ticker or '—'} 不符"
                                )
                            else:
                                reason = (
                                    f"alias map 中此 symbol 在 {layout_mode} 模式下無對應 ticker"
                                    + (f"（可用模式：{', '.join(available)}）" if available else "（此 root 三種模式皆無對應，需先匯入相關資料）")
                                )
                            self._exec_log(
                                f"【略過｜alias 缺項】版面={layout.name} URL={u} 子圖#{sub.index} "
                                f"圖上={search_symbol or '—'} 模式={layout_mode}\n  原因：{reason}"
                            )
                            continue
                        parsed = self._extract_ticker_from_symbol(search_symbol)
                        if opts.ticker_scope == "ticker":
                            reason = (
                                f"圖上 symbol 與目標 ticker 不符（解析={parsed or '—'}, "
                                f"目標={opts.ticker or '—'}）"
                            )
                        else:
                            reason = f"無法從 symbol 解析出 ticker（圖上={search_symbol or '—'}）"
                        self._exec_log(
                            f"【略過｜未匹配】版面={layout.name} URL={u} 子圖#{sub.index} "
                            f"圖上={search_symbol or '—'}\n  原因：{reason}"
                        )
                        continue
                    chosen_key = (chosen.upper(), layout_mode)
                    if chosen_key in matched_keys_in_layout:
                        u = str(post.get("url") or "—")
                        self._exec_log(
                            f"【略過｜重複】版面={layout.name} URL={u} 子圖#{sub.index} "
                            f"圖上={chosen} ticker={target_ticker} 模式={layout_mode}\n"
                            f"  原因：同版面前面子圖在相同模式下已處理過此 symbol"
                        )
                        continue
                    matched_keys_in_layout.add(chosen_key)
                    matched_subcharts += 1

                    if self._batch_should_stop():
                        self._exec_log("【已停止】使用者中止批次。")
                        stop_all = True
                        break
                    # Pin legend/indicator scope to this pane only; otherwise collection
                    # falls back to document and scans every subchart on the layout.
                    automator.set_indicator_scope_subchart(sub.index)
                    try:
                        locked_sc = await self._lock_target_subchart_context(
                            automator,
                            subchart_index=sub.index,
                            expected_symbol=sub.symbol or target_ticker,
                        )
                        if not locked_sc:
                            self._exec_log(
                                f"【警告】子圖#{sub.index} 無法鎖定 symbol（{sub.symbol or '—'}），"
                                "略過該子圖之 GEX 掃描與寫入。"
                            )
                            continue
                        if not await automator.pin_indicator_scope_to_subchart(sub.index):
                            self._exec_log(
                                f"【警告】子圖#{sub.index} scope pin 失敗，略過該子圖之 GEX 掃描與寫入。"
                            )
                            continue
                        org_cleanup = opts.organize_indicators and not opts.dry_run
                        keep_mondays = (
                            self._compute_cleanup_keep_mondays(4) if org_cleanup else None
                        )
                        subchart_cache = await automator.build_weekly_gex_subchart_cache(
                            keep_mondays=keep_mondays,
                        )
                        if org_cleanup:
                            removed = subchart_cache.removed_expired
                            pending = subchart_cache.expired_pending
                            if removed > 0:
                                layout_modified = True
                            if removed > 0 or pending > 0:
                                u = str(post.get("url") or "—")
                                cutoff_iso = keep_mondays[0].isoformat() if keep_mondays else "—"
                                self._exec_log(
                                    f"【清理｜寫入前】版面={layout.name} URL={u} 子圖#{sub.index} "
                                    f"圖上={chosen} ticker={target_ticker}\n"
                                    f"  cutoff={cutoff_iso}｜已移除 {removed} 個過期指標"
                                    + (f"｜未能刪除 {pending} 個" if pending > 0 else "")
                                )
                            if pending > 0:
                                u = str(post.get("url") or "—")
                                self._exec_log(
                                    f"【清理失敗｜DOM】版面={layout.name} URL={u} 子圖#{sub.index} "
                                    f"圖上={chosen} ticker={target_ticker}\n"
                                    f"  原因：Properties 對話框已開啟但 Delete 按鈕未生效（{pending} 個過期指標仍在）"
                                )
                        for monday in mondays:
                            if self._batch_should_stop():
                                self._exec_log("【已停止】使用者中止批次。")
                                stop_all = True
                                break
                            codes = db.fetch_tv_codes_for_week(ticker=target_ticker, monday=monday)
                            available = [day for day, code in codes.items() if code]
                            if not available:
                                if target_ticker.upper() not in known_tickers:
                                    reason = f"資料庫中無 ticker={target_ticker} 的資料"
                                else:
                                    reason = (
                                        f"資料庫有 ticker={target_ticker}，但此週（{monday}~"
                                        f"{(monday + timedelta(days=4))}）皆無 TV Code"
                                    )
                                u = str(post.get("url") or "—")
                                self._exec_log(
                                    f"【略過｜資料庫】版面={layout.name} URL={u} 子圖#{sub.index} "
                                    f"圖上={chosen} ticker={target_ticker} 週一起={monday}\n"
                                    f"  原因：{reason}"
                                )
                                continue
                            snap = await automator.get_runtime_snapshot()
                            chart_url = str(snap.get("url") or "") or None
                            item = WorkItem(
                                ticker=target_ticker,
                                monday=monday,
                                codes=codes,
                                available_days=available,
                                layout_id=layout.id,
                                layout_name=layout.name,
                                subchart_index=sub.index,
                                subchart_symbol=chosen,
                                chart_url=chart_url,
                                is_futures=is_futures_alias,
                            )
                            result = await self._apply_work_item_with_retry(
                                automator,
                                item,
                                skip_if_has_values=opts.skip_filled_days,
                                max_retry=1,
                                subchart_cache=subchart_cache,
                                dry_run=opts.dry_run,
                            )
                            results.append(result)
                            if not opts.dry_run and result.status == "done":
                                layout_modified = True
                    finally:
                        automator.set_indicator_scope_subchart(None)
                        await automator.clear_indicator_scope_marker()
                    if stop_all:
                        break

                prev_layout_label = layout.name
                prev_layout_modified = layout_modified
                if stop_all:
                    break

            if not opts.dry_run and prev_layout_modified:
                self._exec_log(f"【已儲存版面】{prev_layout_label or '—'}（批次結束）")
                await automator.save_current_layout()

            self._last_phase_b_symbols = sorted(seen_symbols)
            self._last_phase_b_matched_subcharts = matched_subcharts
            done = sum(1 for r in results if r.status == "done")
            skipped = sum(1 for r in results if r.status == "skipped")
            failed = sum(1 for r in results if r.status == "failed")
            return BatchReport(
                total=len(results),
                done=done,
                skipped=skipped,
                failed=failed,
                items=results,
            )
        finally:
            await automator.close()

    def _work_items_from_dry_run_report(self, report: BatchReport) -> list[WorkItem]:
        """Turn batch dry-run results into preview table rows (with ``preview_status``)."""
        out: list[WorkItem] = []
        for r in report.items:
            it = r.item
            if r.status == "failed":
                msg = (r.message or "").strip().replace("\n", " ")
                if len(msg) > 120:
                    msg = msg[:117] + "…"
                ps = it.preview_status or f"失敗：{msg}"
                out.append(replace(it, preview_status=ps))
                continue
            if r.status == "skipped":
                if it.preview_status:
                    out.append(it)
                    continue
                m = (r.message or "").strip()
                if m == "該週可用天皆已有值":
                    out.append(replace(it, preview_status="略過：圖上該週欄位已有值"))
                elif m.startswith("skip_quota"):
                    out.append(replace(it, preview_status="略過：指標配額"))
                else:
                    out.append(replace(it, preview_status=f"略過：{m[:96]}"))
                continue
            if r.status == "done":
                out.append(replace(it, preview_status="（預覽不應標記為完成）"))
        return out

    async def _phase_b_preview_flow(self, opts: BatchOptions) -> list[WorkItem]:
        dry = replace(
            opts,
            dry_run=True,
            apply_visibility_preset=False,
            organize_indicators=False,
        )
        report = await self._phase_b_scan_flow(dry)
        return self._dedupe_phase_b_items(self._work_items_from_dry_run_report(report))

    async def _build_phase_b_items(
        self,
        automator: PlaywrightCDPAutomator,
        opts: BatchOptions,
    ) -> list[WorkItem]:
        mondays = sorted(compute_target_mondays(opts.weeks))
        layouts = await self._resolve_target_layouts(automator, opts)
        self._sync_last_phase_b_layout_snap(layouts)
        items: list[WorkItem] = []
        seen_symbols: set[str] = set()
        matched_subcharts = 0
        for layout_idx, layout in enumerate(layouts):
            if opts.layout_scope == "active":
                switched = True
            else:
                switched = await automator.load_layout(layout)
            if not switched and layout_idx > 0:
                continue
            locked_layout_name = (await automator.get_current_layout_name() or "").upper()
            if not locked_layout_name:
                locked_layout_name = layout.name.upper()
            matched_keys_in_layout: set[tuple[str, str]] = set()
            subcharts = await self._enumerate_subcharts_with_retry(
                automator,
                label="預覽掃描",
                retries=1,
            )
            if not subcharts:
                continue
            for sub in subcharts:
                current_layout_name = (await automator.get_current_layout_name() or "").upper()
                if locked_layout_name and current_layout_name and current_layout_name != locked_layout_name:
                    break
                await automator.activate_subchart(sub.index)
                search_symbol = await self._read_subchart_symbol_with_retry(
                    automator,
                    expected_symbol=sub.symbol,
                )
                if search_symbol:
                    seen_symbols.add(search_symbol)
                layout_mode = self._resolve_layout_mode_for_subchart(layout.name, sub.index)
                target_ticker, is_futures_alias = self._resolve_target_ticker_for_subchart(
                    search_symbol, opts, layout_mode=layout_mode
                )
                chosen = search_symbol if target_ticker else None
                if not chosen:
                    continue
                chosen_key = (chosen.upper(), layout_mode)
                if chosen_key in matched_keys_in_layout:
                    continue
                matched_keys_in_layout.add(chosen_key)
                matched_subcharts += 1
                snap = await automator.get_runtime_snapshot()
                chart_url = str(snap.get("url") or "") or None
                for monday in mondays:
                    codes = db.fetch_tv_codes_for_week(ticker=target_ticker, monday=monday)
                    available = [day for day, code in codes.items() if code]
                    if not available:
                        continue
                    items.append(
                        WorkItem(
                            ticker=target_ticker,
                            monday=monday,
                            codes=codes,
                            available_days=available,
                            layout_id=layout.id,
                            layout_name=layout.name,
                            subchart_index=sub.index,
                            subchart_symbol=chosen,
                            chart_url=chart_url,
                            is_futures=is_futures_alias,
                        )
                    )
        self._last_phase_b_symbols = sorted(seen_symbols)
        self._last_phase_b_matched_subcharts = matched_subcharts
        return items

    @staticmethod
    def _symbol_matches_ticker(symbol: str | None, ticker: str) -> bool:
        if not symbol:
            return False
        token = ticker.strip().upper()
        if not token:
            return False
        text = symbol.upper()
        if token in text:
            return True
        if re.search(rf"\b{re.escape(token)}\b", text):
            return True

        # Alias fallback for common index naming differences.
        alias_map: dict[str, tuple[str, ...]] = {
            "VIX": ("VOLATILITY S&P 500 INDEX", "CBOE VOLATILITY INDEX"),
        }
        aliases = alias_map.get(token, ())
        if any(alias in text for alias in aliases):
            return True

        # Acronym fallback: e.g., "Advanced Micro Devices" -> AMD.
        words = re.findall(r"[A-Z]+", text)
        stop = {"INC", "CORP", "CORPORATION", "INDEX", "LTD", "PLC", "CO", "CLASS", "THE", "AND"}
        initials = "".join(w[0] for w in words if len(w) > 1 and w not in stop)
        return token == initials[: len(token)]

    @staticmethod
    def _dedupe_phase_b_items(items: list[WorkItem]) -> list[WorkItem]:
        """Remove accidental duplicate execution targets from scan results."""
        seen: set[tuple] = set()
        out: list[WorkItem] = []
        for it in items:
            key = (
                it.layout_name or it.layout_id or "current",
                it.subchart_index if it.subchart_index is not None else -1,
                it.monday.isoformat(),
                it.ticker,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    async def _read_subchart_symbol_with_retry(
        self,
        automator: PlaywrightCDPAutomator,
        *,
        expected_symbol: str | None,
        retries: int = 3,
        delay_sec: float = 0.18,
    ) -> str | None:
        """Read symbol after subchart activation with hydration retries."""
        fallback = (expected_symbol or "").strip()
        last: str | None = None
        for _ in range(retries + 1):
            current = (await automator.get_symbol_search_value() or "").strip()
            if current:
                if fallback and self._symbols_compatible(fallback, current):
                    return current
                if not fallback:
                    return current
                last = current
            await asyncio.sleep(delay_sec)
        # If header symbol is still unstable/blank, trust subchart enumeration value.
        if fallback:
            return fallback
        return last

    @staticmethod
    def _wait_for_cdp_ready(timeout_sec: float = 6.0) -> bool:
        endpoint = "http://127.0.0.1:9222/json/version"
        start = time.monotonic()
        while time.monotonic() - start < timeout_sec:
            try:
                with request.urlopen(endpoint, timeout=1.0) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.25)
        return False
