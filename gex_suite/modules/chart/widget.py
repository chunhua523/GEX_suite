"""ChartPage — PySide6 port of GEX_tool/GEX_chart_new.py.

Embeds the chart in a QWebEngineView right-hand pane (falls back to "open
in default browser" if QtWebEngine isn't available).
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
import webbrowser
from typing import Optional

import pandas as pd
from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except Exception:  # pragma: no cover
    QWebEngineView = None  # type: ignore[assignment]
    HAS_WEBENGINE = False

from gex_suite.shared import db
from gex_suite.shared.paths import ensure_dirs

from . import importers, ohlc, plot

# Default Google Sheet IDs (mirrors GEX_chart_new.py)
DEFAULT_SHEET_IDS = [
    "1u1opYwj_2bhOBhAM96CB7kYz9prWKQtXhmjU1cG15Dg",
    "1H7MqEuVuu_xIN9B-rFMrevDLeaCn06z3dn0XBMt_-to",
]


class _ConflictDialog(QDialog):
    def __init__(self, ticker: str, date: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("資料衝突")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"{ticker} - {date} - {label} 已存在。是否要覆蓋？"))
        from PySide6.QtWidgets import QCheckBox
        self.apply_all = QCheckBox("套用至所有後續重複資料")
        v.addWidget(self.apply_all)

        bb = QHBoxLayout()
        v.addLayout(bb)
        b_overwrite = QPushButton("覆蓋")
        b_skip = QPushButton("跳過")
        b_cancel = QPushButton("取消")
        bb.addWidget(b_overwrite)
        bb.addWidget(b_skip)
        bb.addWidget(b_cancel)
        b_overwrite.clicked.connect(lambda: self._done("overwrite"))
        b_skip.clicked.connect(lambda: self._done("skip"))
        b_cancel.clicked.connect(lambda: self._done("cancel"))
        self.choice: str = "skip"

    def _done(self, what: str) -> None:
        self.choice = (what + "_all") if (what != "cancel" and self.apply_all.isChecked()) else what
        self.accept()


class ChartPage(QWidget):
    """Manage stocks.db: import TV codes, refresh OHLC, plot GEX charts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_dirs()
        db.init_db()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 15, 15, 15)

        # ---- Top: import + Google ----
        top = QHBoxLayout()
        outer.addLayout(top)

        btn_import = QToolButton()
        btn_import.setText("批次匯入 ▾")
        btn_import.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        menu.addAction("從 TXT 匯入", self._import_txt)
        menu.addAction("從 Excel 匯入", self._import_excel)
        menu.addAction("從 Google 試算表匯入（全部）", self._import_google_all)
        btn_import.setMenu(menu)
        top.addWidget(btn_import)

        btn_google_latest = QPushButton("從 Google sheet 更新最新 data")
        btn_google_latest.setStyleSheet("background:#2CC985; color:white;")
        btn_google_latest.clicked.connect(self._import_google_latest)
        top.addWidget(btn_google_latest)
        top.addStretch(1)

        # ---- Middle: split entry / plot ----
        split = QSplitter()
        split.setOrientation(Qt.Horizontal)
        outer.addWidget(split, 1)

        left = QWidget()
        l_layout = QVBoxLayout(left)
        split.addWidget(left)

        # Single entry box
        entry_box = QGroupBox("單筆輸入")
        eg = QVBoxLayout(entry_box)
        row1 = QHBoxLayout()
        eg.addLayout(row1)
        row1.addWidget(QLabel("日期:"))
        self.date_entry = QDateEdit(QDate.currentDate())
        self.date_entry.setCalendarPopup(True)
        self.date_entry.setDisplayFormat("yyyy-MM-dd")
        row1.addWidget(self.date_entry)
        row1.addStretch(1)

        row2 = QHBoxLayout()
        eg.addLayout(row2)
        row2.addWidget(QLabel("GEX Code:"))
        self.gex_entry = QLineEdit()
        row2.addWidget(self.gex_entry, 1)

        row3 = QHBoxLayout()
        eg.addLayout(row3)
        b_add = QPushButton("新增記錄")
        b_add.setStyleSheet("background:#2CC985; color:white;")
        b_add.clicked.connect(self._on_single_entry)
        row3.addWidget(b_add)
        b_ohlc_today = QPushButton("更新當日 OHLC")
        b_ohlc_today.setStyleSheet("background:#FFA500; color:white;")
        b_ohlc_today.clicked.connect(self._on_update_ohlc_for_date)
        row3.addWidget(b_ohlc_today)
        row3.addStretch(1)
        l_layout.addWidget(entry_box)

        # Filter box
        filt_box = QGroupBox("篩選條件")
        fg = QVBoxLayout(filt_box)
        f1 = QHBoxLayout()
        fg.addLayout(f1)
        f1.addWidget(QLabel("Ticker:"))
        self.ticker_filter = QComboBox()
        self.ticker_filter.setEditable(True)
        completer = QCompleter()
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.ticker_filter.setCompleter(completer)
        f1.addWidget(self.ticker_filter, 1)

        f2 = QHBoxLayout()
        fg.addLayout(f2)
        f2.addWidget(QLabel("起始日期:"))
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-3))
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        f2.addWidget(self.start_date)
        f2.addWidget(QLabel("結束日期:"))
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        f2.addWidget(self.end_date)

        f3 = QHBoxLayout()
        fg.addLayout(f3)
        b_filter = QPushButton("篩選")
        b_filter.clicked.connect(self._refresh_table)
        f3.addWidget(b_filter)
        b_range = QPushButton("更新 OHLC 區間")
        b_range.setStyleSheet("background:#FFA500; color:white;")
        b_range.clicked.connect(self._on_update_ohlc_range)
        f3.addWidget(b_range)
        b_reset = QPushButton("重置")
        b_reset.setStyleSheet("background:#FF4D4D; color:white;")
        b_reset.clicked.connect(self._on_reset)
        f3.addWidget(b_reset)
        f3.addStretch(1)
        l_layout.addWidget(filt_box)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Ticker", "Date", "Label", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        l_layout.addWidget(self.table, 1)

        # Action row
        ar = QHBoxLayout()
        l_layout.addLayout(ar)
        b_plot = QPushButton("📈 繪製圖表")
        b_plot.setStyleSheet("background:#3B8ED0; color:white;")
        b_plot.clicked.connect(self._on_plot)
        ar.addWidget(b_plot)
        b_del = QPushButton("🗑️ 刪除選定")
        b_del.setStyleSheet("background:#FF4D4D; color:white;")
        b_del.clicked.connect(self._on_delete_selected)
        ar.addWidget(b_del)
        ar.addStretch(1)

        # Right pane: plot
        right = QWidget()
        r_layout = QVBoxLayout(right)
        split.addWidget(right)

        if HAS_WEBENGINE:
            self.web = QWebEngineView()
            r_layout.addWidget(self.web, 1)
            self.web.setHtml("<html><body style='background:#1e1e1e;color:#aaa;font-family:sans-serif;padding:20px;'>"
                              "點選左側「📈 繪製圖表」開始</body></html>")
        else:
            self.web = None
            r_layout.addWidget(QLabel(
                "QtWebEngine 未安裝，圖表將另開外部瀏覽器顯示。\n"
                "可執行：pip install PySide6-Addons"
            ))
            r_layout.addStretch(1)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([520, 760])

        self._populate_ticker_dropdown()
        self._refresh_table()

    # ---------- Conflict resolver dialog ----------
    def _ask_conflict(self, ticker: str, date: str, label: str) -> str:
        dlg = _ConflictDialog(ticker, date, label, self)
        dlg.exec()
        return dlg.choice

    # ---------- Importers ----------
    def _import_txt(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select TV Code files", "", "Text/CSV (*.txt *.csv)")
        if not files:
            return
        report = importers.import_txt_files(files, resolver=self._ask_conflict)
        self._after_import(report)

    def _import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Excel file", "", "Excel (*.xlsx *.xls)")
        if not path:
            return
        report = importers.import_excel(path, resolver=self._ask_conflict)
        self._after_import(report)

    def _import_google_all(self) -> None:
        report = importers.import_google(DEFAULT_SHEET_IDS, resolver=self._ask_conflict, only_latest=False)
        self._after_import(report, label="Google Sheet")

    def _import_google_latest(self) -> None:
        report = importers.import_google(DEFAULT_SHEET_IDS, resolver=self._ask_conflict, only_latest=True)
        self._after_import(report, label="Google Sheet (latest)")

    def _after_import(self, report: importers.ImportReport, label: str = "Import") -> None:
        if report.errors:
            QMessageBox.warning(self, label + " — 部分錯誤", "\n".join(report.errors[:10]))
        if report.cancelled:
            QMessageBox.information(self, "已取消", f"已寫入 {report.total_written} 筆")
        else:
            QMessageBox.information(
                self,
                label + " 完成",
                f"新增 {report.inserted} 筆、覆寫 {report.overwritten} 筆、跳過 {report.skipped} 筆",
            )
        self._populate_ticker_dropdown()
        self._refresh_table()

    # ---------- Single entry ----------
    def _on_single_entry(self) -> None:
        date = self.date_entry.date().toString("yyyy-MM-dd")
        code = self.gex_entry.text().strip()
        if not date or not code:
            QMessageBox.warning(self, "輸入錯誤", "請輸入日期和 GEX TV Code")
            return
        from . import parser as gp
        inserter = importers._Inserter(self._ask_conflict)
        ticker = gp.parse_gex_code(date, code, inserter.insert)
        self._populate_ticker_dropdown()
        if ticker:
            idx = self.ticker_filter.findText(ticker)
            if idx >= 0:
                self.ticker_filter.setCurrentIndex(idx)
        self._refresh_table()
        QMessageBox.information(self, "完成", f"成功寫入 {inserter.report.total_written} 筆資料。")

    # ---------- Table ----------
    def _refresh_table(self) -> None:
        ticker = self.ticker_filter.currentText().strip()
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        rows = db.fetch_rows(filter_ticker=ticker, start_date=start, end_date=end)
        self.table.setRowCount(0)
        for r in rows:
            row_idx = self.table.rowCount()
            self.table.insertRow(row_idx)
            _, t, d, lbl, val = r
            for col, value in enumerate((t, d, lbl, val)):
                self.table.setItem(row_idx, col, QTableWidgetItem(str(value)))

    def _on_reset(self) -> None:
        self.ticker_filter.setCurrentText("")
        self.start_date.setDate(QDate.currentDate().addMonths(-3))
        self.end_date.setDate(QDate.currentDate())
        self._refresh_table()

    def _on_delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        if not rows:
            QMessageBox.warning(self, "錯誤", "請選擇要刪除的記錄")
            return
        for r in rows:
            t = self.table.item(r, 0).text()
            d = self.table.item(r, 1).text()
            lbl = self.table.item(r, 2).text()
            val = self.table.item(r, 3).text()
            try:
                db.delete_row(t, d, lbl, float(val))
            except ValueError:
                db.delete_row(t, d, lbl, val)
            self.table.removeRow(r)

    # ---------- OHLC ----------
    def _on_update_ohlc_for_date(self) -> None:
        date = self.date_entry.date().toPython()
        try:
            count = ohlc.update_ohlc_for_date(date)
        except Exception as exc:
            QMessageBox.critical(self, "更新失敗", str(exc))
            return
        self._refresh_table()
        QMessageBox.information(self, "更新完成", f"已成功為 {count} 支 ticker 更新 {date} 的 OHLC 資料。")

    def _on_update_ohlc_range(self) -> None:
        ticker = self.ticker_filter.currentText().strip()
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        if not ticker or not start or not end:
            QMessageBox.warning(self, "參數不足", "請先選擇 Ticker 及起始/結束日期")
            return
        try:
            updated = ohlc.update_ohlc_range(ticker, start, end)
        except Exception as exc:
            QMessageBox.critical(self, "更新失敗", str(exc))
            return
        self._refresh_table()
        QMessageBox.information(self, "更新完成", f"{ticker} 共更新 {updated} 天的 OHLC 資料 ({start} ~ {end})")

    # ---------- Plot ----------
    def _on_plot(self) -> None:
        ticker = self.ticker_filter.currentText().strip()
        if not ticker:
            QMessageBox.warning(self, "錯誤", "請選擇 Ticker")
            return
        try:
            fig, _has_ohlc = plot.build_figure(ticker)
        except Exception as exc:
            QMessageBox.critical(self, "繪圖失敗", str(exc))
            return
        if HAS_WEBENGINE and self.web is not None:
            html = plot.figure_to_html(fig)
            self.web.setHtml(html, QUrl("about:blank"))
        else:
            tmp = tempfile.NamedTemporaryFile(prefix=f"gex_{ticker}_", suffix=".html",
                                               delete=False, mode="w", encoding="utf-8")
            tmp.write(plot.figure_to_html(fig))
            tmp.close()
            webbrowser.open("file://" + tmp.name)

    # ---------- helpers ----------
    def _populate_ticker_dropdown(self) -> None:
        current = self.ticker_filter.currentText()
        tickers = db.get_all_tickers()
        self.ticker_filter.clear()
        self.ticker_filter.addItems(tickers)
        completer = self.ticker_filter.completer()
        if completer is not None:
            from PySide6.QtCore import QStringListModel
            completer.setModel(QStringListModel(tickers))
        if current:
            idx = self.ticker_filter.findText(current)
            if idx >= 0:
                self.ticker_filter.setCurrentIndex(idx)
            else:
                self.ticker_filter.setEditText(current)
