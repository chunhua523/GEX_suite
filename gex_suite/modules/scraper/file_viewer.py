"""PySide6 port of GEX_scraper/gui.py ``open_file_viewer`` — browse scraped files.

Modes: **By Date** (calendar + model tabs) and **By Ticker & Model** (search,
model checkboxes, date range). Logic matches the original CustomTkinter dialog.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QDate
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import utils

_STANDARD_MODELS = ["Gamma", "Delta", "Theta", "Term", "Smile", "Levels", "Table", "TV Code"]
_CME_MODELS = ["Gamma", "Delta", "Smile", "Term", "TV Code"]


def open_file_cross_platform(filepath: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(filepath)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.call(("open", filepath))
        else:
            subprocess.call(("xdg-open", filepath))
    except Exception as exc:  # pragma: no cover
        print(f"Failed to open file: {exc}")


class ScrapedFilesDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        download_folder: str,
        ticker_filepath: Optional[str],
        cme_ticker_filepath: Optional[str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("View Downloaded Files")
        self.resize(1100, 700)

        self._dl = download_folder
        self._ticker_fp = ticker_filepath
        self._cme_fp = cme_ticker_filepath

        self._grouped_files: dict[str, dict[str, tuple[str, datetime]]] = {}
        self._date_file_vars: list[tuple[QCheckBox, Any]] = []

        self._bt_file_vars: list[tuple[QCheckBox, Any]] = []

        root = QVBoxLayout(self)
        top_bar = QFrame()
        top_bar.setFixedHeight(48)
        th = QHBoxLayout(top_bar)
        th.addWidget(QLabel("View Mode:"))
        self._mode_date = QRadioButton("By Date")
        self._mode_ticker = QRadioButton("By Ticker & Model")
        self._mode_date.setChecked(True)
        self._mode_grp = QButtonGroup(self)
        self._mode_grp.addButton(self._mode_date)
        self._mode_grp.addButton(self._mode_ticker)
        th.addWidget(self._mode_date)
        th.addWidget(self._mode_ticker)
        th.addStretch(1)
        root.addWidget(top_bar)

        self._stack_by_date = QWidget()
        self._stack_by_ticker = QWidget()
        self._build_by_date_ui(self._stack_by_date)
        self._build_by_ticker_ui(self._stack_by_ticker)

        self._w_date = self._stack_by_date
        self._w_ticker = self._stack_by_ticker
        root.addWidget(self._w_date, 1)
        root.addWidget(self._w_ticker, 1)
        self._w_ticker.hide()

        self._mode_date.toggled.connect(self._on_mode_toggled)
        self._mode_ticker.toggled.connect(self._on_mode_toggled)

        self._load_files_by_date()

    def _on_mode_toggled(self, _checked: bool) -> None:
        if self._mode_date.isChecked():
            self._w_ticker.hide()
            self._w_date.show()
            self._load_files_by_date()
        elif self._mode_ticker.isChecked():
            self._w_date.hide()
            self._w_ticker.show()

    # --- By Date -----------------------------------------------------------------
    def _build_by_date_ui(self, host: QWidget) -> None:
        grid = QHBoxLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)

        left = QFrame()
        left.setFixedWidth(280)
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>Control Panel</b>"))
        self._cal = QCalendarWidget()
        self._cal.setGridVisible(True)
        self._cal.selectionChanged.connect(self._load_files_by_date)
        lv.addWidget(self._cal)
        btn_today = QPushButton("Today")
        btn_today.clicked.connect(self._cal_set_today)
        lv.addWidget(btn_today)
        btn_refresh = QPushButton("Load / Refresh Files")
        btn_refresh.clicked.connect(self._load_files_by_date)
        lv.addWidget(btn_refresh)
        self._lbl_date_status = QLabel("Ready")
        self._lbl_date_status.setStyleSheet("color:gray;")
        lv.addWidget(self._lbl_date_status)
        lv.addStretch(1)
        grid.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        self._combo_models = QComboBox()
        self._combo_models.currentTextChanged.connect(self._rebuild_by_date_list)
        rv.addWidget(self._combo_models)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._date_scroll_inner = QWidget()
        self._date_scroll_layout = QVBoxLayout(self._date_scroll_inner)
        self._date_scroll_layout.addStretch(1)
        scroll.setWidget(self._date_scroll_inner)
        rv.addWidget(scroll, 1)

        bf = QHBoxLayout()
        bf.addWidget(QPushButton("Select All", clicked=self._date_select_all))
        bf.addWidget(QPushButton("Deselect All", clicked=self._date_deselect_all))
        bf.addStretch(1)
        btn_open = QPushButton("Open Selected")
        btn_open.setStyleSheet("background:#2CC985;color:white;font-weight:bold;")
        btn_open.clicked.connect(self._date_open_selected)
        bf.addWidget(btn_open)
        rv.addLayout(bf)
        grid.addWidget(right, 1)

    def _cal_set_today(self) -> None:
        self._cal.setSelectedDate(QDate.currentDate())
        self._load_files_by_date()

    def _clear_date_scroll(self) -> None:
        while self._date_scroll_layout.count() > 1:
            item = self._date_scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._date_file_vars.clear()

    def _load_files_by_date(self) -> None:
        qd = self._cal.selectedDate()
        date_str = qd.toString("yyyy-MM-dd")
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            search_str = target_date.strftime("%Y%m%d")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Invalid date: {exc}")
            return

        self._grouped_files.clear()
        try:
            for root, _dirs, files in os.walk(self._dl):
                for file in files:
                    if search_str in file and file.endswith((".html", ".txt", ".csv", ".pdf", ".png")):
                        fp = os.path.join(root, file)
                        try:
                            rel_path = os.path.relpath(fp, self._dl)
                            parts = rel_path.split(os.sep)
                            model_name = "Other"
                            ticker_name = file
                            if parts[0] == "CME":
                                if len(parts) >= 3:
                                    model_name = f"CME - {parts[1]}"
                                    ticker_name = parts[2]
                                elif "TV Code" in parts or file.lower().startswith("tv_codes"):
                                    model_name = "CME - TV Code"
                                    ticker_name = f"File_{file}"
                            else:
                                if len(parts) >= 2:
                                    model_name = parts[0]
                                    ticker_name = parts[1]
                                elif "TV Code" in parts or file.lower().startswith("tv_codes"):
                                    model_name = "TV Code"
                                    ticker_name = f"File_{file}"
                            try:
                                time_part = file.split(search_str + "_")[1].split(".")[0]
                                if len(time_part) >= 6:
                                    dt_time = datetime.strptime(time_part[:6], "%H%M%S")
                                else:
                                    dt_time = datetime.now()
                            except Exception:
                                mnow = os.path.getmtime(fp)
                                dt_time = datetime.fromtimestamp(mnow)
                            if model_name not in self._grouped_files:
                                self._grouped_files[model_name] = {}
                            cur = self._grouped_files[model_name].get(ticker_name)
                            if cur is None or dt_time > cur[1]:
                                self._grouped_files[model_name][ticker_name] = (fp, dt_time)
                        except Exception:
                            pass
        except Exception as exc:
            print(f"Error scanning files: {exc}")

        total_files = sum(len(v) for v in self._grouped_files.values())
        self._lbl_date_status.setText(f"Found {total_files} files ({date_str})")

        self._combo_models.blockSignals(True)
        self._combo_models.clear()
        sorted_models = sorted(self._grouped_files.keys())
        if not sorted_models:
            self._combo_models.addItem("No Data")
        else:
            self._combo_models.addItems(sorted_models)
        self._combo_models.blockSignals(False)
        self._rebuild_by_date_list(self._combo_models.currentText())

    def _ticker_groups_for_model(self, selected_model: str) -> dict:
        tg_std: dict = {}
        tg_cme: dict = {}
        if self._ticker_fp and os.path.exists(self._ticker_fp):
            tg_std = utils.load_tickers_with_groups(self._ticker_fp)
        if self._cme_fp and os.path.exists(self._cme_fp):
            tg_cme = utils.load_tickers_with_groups(self._cme_fp)
        return tg_cme if "CME" in selected_model else tg_std

    def _rebuild_by_date_list(self, selected_model: str) -> None:
        self._clear_date_scroll()
        gf = self._grouped_files

        if selected_model in ("", "No Data") or selected_model not in gf:
            lab = QLabel("No files found.")
            lab.setStyleSheet("color:gray;")
            self._date_scroll_layout.insertWidget(0, lab)
            return

        tickers = gf[selected_model]
        ticker_groups = self._ticker_groups_for_model(selected_model)

        if "TV Code" in selected_model:
            self._rebuild_tv_code_by_date(selected_model, tickers, ticker_groups)
            return

        grouped_tickers: dict[str, list[tuple[str, str, datetime]]] = {}
        ungrouped: list[tuple[str, str, datetime]] = []
        for ticker in tickers.keys():
            fp, dt_obj = tickers[ticker]
            found_group = None
            for group_name, ticker_list in ticker_groups.items():
                if ticker in ticker_list:
                    found_group = group_name
                    break
            if found_group:
                grouped_tickers.setdefault(found_group, []).append((ticker, fp, dt_obj))
            else:
                ungrouped.append((ticker, fp, dt_obj))

        for group_name in sorted(grouped_tickers.keys()):
            items = grouped_tickers[group_name]
            items.sort(key=lambda x: x[0])
            gb = QGroupBox(f"{group_name} ({len(items)} tickers)")
            gb.setCheckable(True)
            gb.setChecked(True)
            chk_list: list[tuple[QCheckBox, Any]] = []

            cont = QWidget()
            cl = QVBoxLayout(cont)
            for ticker, fp, dt_obj in items:
                time_str = dt_obj.strftime("%H:%M:%S")
                cb = QCheckBox(f"[{time_str}]  {ticker}")
                cb.setFont(QFont("Consolas", 11))
                cl.addWidget(cb)
                self._date_file_vars.append((cb, fp))
                chk_list.append((cb, fp))

            hdr = QHBoxLayout()
            btn_all = QPushButton("☑")
            btn_all.setFixedWidth(36)

            def sel_all(lst: list[tuple[QCheckBox, Any]]) -> None:
                def _() -> None:
                    all_on = all(x[0].isChecked() for x in lst)
                    for c, _ in lst:
                        c.setChecked(not all_on)

                return _

            btn_all.clicked.connect(sel_all(chk_list))
            hdr.addWidget(btn_all)
            hdr.addStretch(1)

            outer = QVBoxLayout()
            outer.addLayout(hdr)
            outer.addWidget(cont)
            gb.setLayout(outer)
            gb.toggled.connect(lambda c, dc=cont: dc.setVisible(c))
            self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, gb)

        if ungrouped:
            if grouped_tickers:
                oh = QLabel(f"其他 / Other ({len(ungrouped)} tickers)")
                oh.setStyleSheet("font-weight:bold;padding:4px;")
                self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, oh)
            ungrouped.sort(key=lambda x: x[0])
            for ticker, fp, dt_obj in ungrouped:
                time_str = dt_obj.strftime("%H:%M:%S")
                cb = QCheckBox(f"[{time_str}]  {ticker}")
                cb.setFont(QFont("Consolas", 11))
                self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, cb)
                self._date_file_vars.append((cb, fp))

    def _rebuild_tv_code_by_date(
        self,
        selected_model: str,
        tickers: dict[str, tuple[str, datetime]],
        ticker_groups: dict,
    ) -> None:
        tv_files: list[tuple[datetime, str]] = []
        for _file_key, (fp, dt_obj) in tickers.items():
            tv_files.append((dt_obj, fp))
        tv_files.sort(key=lambda x: x[0])

        merged_tv_items: dict[str, str] = {}
        try:
            for _dt, fp in tv_files:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for line in f.readlines():
                            line = line.strip()
                            if not line:
                                continue
                            ticker_label = "Unknown"
                            if '"' in line:
                                parts_q = line.split('"')
                                if len(parts_q) > 1:
                                    ticker_label = parts_q[1]
                            else:
                                ticker_label = line.split(" ")[0].replace(":", "")
                            merged_tv_items[ticker_label] = line
                except Exception as exc:
                    print(f"Error reading {fp}: {exc}")

            if not merged_tv_items:
                lab = QLabel("No content found in TV Code files.")
                lab.setStyleSheet("color:gray;")
                self._date_scroll_layout.insertWidget(0, lab)
                return

            grouped_tv: dict[str, list[tuple[str, str]]] = {}
            ungrouped_tv: list[tuple[str, str]] = []
            for ticker in sorted(merged_tv_items.keys()):
                content = merged_tv_items[ticker]
                found_group = None
                for group_name, ticker_list in ticker_groups.items():
                    if ticker in ticker_list:
                        found_group = group_name
                        break
                if found_group:
                    grouped_tv.setdefault(found_group, []).append((ticker, content))
                else:
                    ungrouped_tv.append((ticker, content))

            for group_name in sorted(grouped_tv.keys()):
                group_items = grouped_tv[group_name]
                gb = QGroupBox(f"{group_name} ({len(group_items)} tickers)")
                gb.setCheckable(True)
                gb.setChecked(True)
                cont = QWidget()
                cl = QVBoxLayout(cont)
                tv_group_checkboxes: list[tuple[QCheckBox, Any]] = []
                for t_label, content in group_items:
                    cb = QCheckBox(t_label)
                    cb.setFont(QFont("Consolas", 11))
                    cl.addWidget(cb)
                    self._date_file_vars.append((cb, ("TV_DATA", t_label, content)))
                    tv_group_checkboxes.append((cb, ("TV_DATA", t_label, content)))

                hdr = QHBoxLayout()
                btn_all = QPushButton("☑")
                btn_all.setFixedWidth(36)

                def sel_all_tv(lst: list[tuple[QCheckBox, Any]]) -> None:
                    def _() -> None:
                        all_on = all(x[0].isChecked() for x in lst)
                        for c, _ in lst:
                            c.setChecked(not all_on)

                    return _

                btn_all.clicked.connect(sel_all_tv(tv_group_checkboxes))
                hdr.addWidget(btn_all)
                hdr.addStretch(1)
                outer = QVBoxLayout()
                outer.addLayout(hdr)
                outer.addWidget(cont)
                gb.setLayout(outer)
                gb.toggled.connect(lambda c, dc=cont: dc.setVisible(c))
                self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, gb)

            if ungrouped_tv:
                if grouped_tv:
                    oh = QLabel(f"其他 / Other ({len(ungrouped_tv)} tickers)")
                    oh.setStyleSheet("font-weight:bold;")
                    self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, oh)
                for t_label, content in ungrouped_tv:
                    cb = QCheckBox(t_label)
                    cb.setFont(QFont("Consolas", 11))
                    self._date_scroll_layout.insertWidget(self._date_scroll_layout.count() - 1, cb)
                    self._date_file_vars.append((cb, ("TV_DATA", t_label, content)))
        except Exception as exc:
            err = QLabel(f"Error processing TV Code files: {exc}")
            err.setStyleSheet("color:red;")
            self._date_scroll_layout.insertWidget(0, err)

    def _date_select_all(self) -> None:
        for cb, _ in self._date_file_vars:
            cb.setChecked(True)

    def _date_deselect_all(self) -> None:
        for cb, _ in self._date_file_vars:
            cb.setChecked(False)

    def _date_open_selected(self) -> None:
        tv_data_to_show: list[tuple[Any, ...]] = []
        for cb, data in self._date_file_vars:
            if not cb.isChecked():
                continue
            try:
                if isinstance(data, tuple) and data[0] == "TV_DATA":
                    tv_data_to_show.append(data)
                else:
                    open_file_cross_platform(str(data))
            except Exception as exc:
                print(f"Error opening item: {exc}")
        if tv_data_to_show:
            try:
                fd, path = tempfile.mkstemp(prefix="TV_Selected_", suffix=".txt", text=True)
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    for item in tv_data_to_show:
                        t_label = item[1]
                        content = item[2]
                        clean_content = content
                        prefix = f"{t_label}:"
                        if clean_content.startswith(prefix):
                            clean_content = clean_content[len(prefix) :].strip()
                        elif clean_content.startswith(t_label):
                            clean_content = clean_content[len(t_label) :].strip()
                        tmp.write(f"{t_label}:\n\n")
                        tmp.write(f"{clean_content}\n\n")
                open_file_cross_platform(path)
            except Exception as exc:
                print(f"Error creating aggregate TV file: {exc}")

    # --- By Ticker & Model -------------------------------------------------------
    def _build_by_ticker_ui(self, host: QWidget) -> None:
        grid = QHBoxLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)

        left = QFrame()
        left.setMinimumWidth(300)
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>By Ticker & Model</b>"))

        lv.addWidget(QLabel("Ticker:"))
        self._bt_entry = QLineEdit()
        self._bt_entry.setPlaceholderText("Type to search...")
        lv.addWidget(self._bt_entry)

        self._bt_list = QListWidget()
        self._bt_list.setMaximumHeight(160)
        self._bt_list.itemClicked.connect(self._bt_on_list_pick)
        lv.addWidget(self._bt_list)
        self._bt_entry.textChanged.connect(self._bt_filter_list)

        self._all_tickers_std: list[str] = []
        self._all_tickers_cme: list[str] = []
        self._tickers_cme_set: set[str] = set()
        if self._ticker_fp and os.path.exists(self._ticker_fp):
            std_groups = utils.load_tickers_with_groups(self._ticker_fp)
            for _g, lst in std_groups.items():
                self._all_tickers_std.extend(lst)
        if self._cme_fp and os.path.exists(self._cme_fp):
            cme_groups = utils.load_tickers_with_groups(self._cme_fp)
            for _g, lst in cme_groups.items():
                self._all_tickers_cme.extend(lst)
                self._tickers_cme_set.update(lst)
        self._all_tickers_combined = sorted(set(self._all_tickers_std + self._all_tickers_cme))
        self._bt_filter_list()

        lv.addWidget(QLabel("Models:"))
        self._bt_model_grid_host = QWidget()
        self._bt_model_grid = QGridLayout(self._bt_model_grid_host)
        lv.addWidget(self._bt_model_grid_host)
        self._bt_model_checks: dict[str, QCheckBox] = {}
        self._bt_rebuild_model_checkboxes(False)

        lv.addWidget(QLabel("Date Range:"))
        dr = QHBoxLayout()
        dr.addWidget(QLabel("Start:"))
        self._bt_start = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        dr.addWidget(self._bt_start)
        lv.addLayout(dr)
        dr2 = QHBoxLayout()
        dr2.addWidget(QLabel("End:"))
        self._bt_end = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        dr2.addWidget(self._bt_end)
        lv.addLayout(dr2)

        self._bt_lbl_status = QLabel("Ready")
        self._bt_lbl_status.setStyleSheet("color:gray;")
        lv.addWidget(self._bt_lbl_status)
        btn_search = QPushButton("Search Files")
        btn_search.clicked.connect(self._load_files_by_ticker)
        lv.addWidget(btn_search)
        lv.addStretch(1)
        grid.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        self._combo_bt_models = QComboBox()
        self._combo_bt_models.currentTextChanged.connect(self._on_bt_model_combo)
        rv.addWidget(self._combo_bt_models)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._bt_scroll_inner = QWidget()
        self._bt_scroll_layout = QVBoxLayout(self._bt_scroll_inner)
        self._bt_scroll_layout.addStretch(1)
        scroll.setWidget(self._bt_scroll_inner)
        rv.addWidget(scroll, 1)

        bf = QHBoxLayout()
        bf.addWidget(QPushButton("Select All", clicked=self._bt_select_all))
        bf.addWidget(QPushButton("Deselect All", clicked=self._bt_deselect_all))
        bf.addStretch(1)
        bo = QPushButton("Open Selected")
        bo.setStyleSheet("background:#2CC985;color:white;font-weight:bold;")
        bo.clicked.connect(self._bt_open_selected)
        bf.addWidget(bo)
        rv.addLayout(bf)
        grid.addWidget(right, 1)

        self._bt_cached_grouped: dict = {}
        self._bt_cached_ticker = ""
        self._bt_cached_is_cme = False

    def _bt_rebuild_model_checkboxes(self, is_cme: bool) -> None:
        while self._bt_model_grid.count():
            item = self._bt_model_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._bt_model_checks.clear()
        models = _CME_MODELS if is_cme else _STANDARD_MODELS
        for i, m in enumerate(models):
            cb = QCheckBox(m)
            self._bt_model_grid.addWidget(cb, i // 2, i % 2)
            self._bt_model_checks[m] = cb

    def _bt_filter_list(self) -> None:
        q = self._bt_entry.text().strip().upper()
        self._bt_list.clear()
        matches = [t for t in self._all_tickers_combined if q in t.upper()] if q else self._all_tickers_combined
        for t in matches[:60]:
            self._bt_list.addItem(QListWidgetItem(t))

    def _bt_on_list_pick(self, item: QListWidgetItem) -> None:
        ticker_val = item.text()
        self._bt_entry.setText(ticker_val)
        is_cme = ticker_val in self._tickers_cme_set
        self._bt_rebuild_model_checkboxes(is_cme)

    def _clear_bt_scroll(self) -> None:
        while self._bt_scroll_layout.count() > 1:
            item = self._bt_scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._bt_file_vars.clear()

    def _load_files_by_ticker(self) -> None:
        ticker = self._bt_entry.text().strip()
        if not ticker:
            QMessageBox.warning(self, "Warning", "Please enter or select a ticker first.")
            return
        selected_models = [m for m, cb in self._bt_model_checks.items() if cb.isChecked()]
        if not selected_models:
            QMessageBox.warning(self, "Warning", "Please select at least one model.")
            return
        try:
            start_date = datetime.strptime(self._bt_start.text().strip(), "%Y-%m-%d")
            end_date = datetime.strptime(self._bt_end.text().strip(), "%Y-%m-%d")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Invalid date format (use YYYY-MM-DD): {exc}")
            return
        if start_date > end_date:
            QMessageBox.warning(self, "Warning", "Start date must be on or before end date.")
            return

        is_cme = ticker in self._tickers_cme_set
        bt_grouped: dict[str, dict[str, dict[str, tuple[str, datetime]]]] = {}
        self._bt_lbl_status.setText("Scanning...")

        try:
            for root, _dirs, files in os.walk(self._dl):
                for file in files:
                    if not file.endswith((".html", ".txt", ".csv", ".pdf", ".png")):
                        continue
                    fp = os.path.join(root, file)
                    try:
                        rel_path = os.path.relpath(fp, self._dl)
                        parts = rel_path.split(os.sep)
                        model_name = "Other"
                        ticker_name = file
                        if parts[0] == "CME":
                            if len(parts) >= 3:
                                model_name = f"CME - {parts[1]}"
                                ticker_name = parts[2]
                            elif "TV Code" in parts or file.lower().startswith("tv_codes"):
                                model_name = "CME - TV Code"
                                ticker_name = f"File_{file}"
                        else:
                            if len(parts) >= 2:
                                model_name = parts[0]
                                ticker_name = parts[1]
                            elif "TV Code" in parts or file.lower().startswith("tv_codes"):
                                model_name = "TV Code"
                                ticker_name = f"File_{file}"

                        if is_cme:
                            if not model_name.startswith("CME - "):
                                continue
                            raw_model = model_name[len("CME - ") :]
                        else:
                            if model_name.startswith("CME - "):
                                continue
                            raw_model = model_name

                        if raw_model not in selected_models:
                            continue

                        is_tv_code = "TV Code" in model_name
                        if not is_tv_code and ticker_name != ticker:
                            continue

                        date_match = re.search(r"(\d{8})_(\d{6})", file)
                        if not date_match:
                            continue
                        file_date_str = date_match.group(1)
                        time_str_raw = date_match.group(2)
                        try:
                            file_date = datetime.strptime(file_date_str, "%Y%m%d")
                            file_dt = datetime.strptime(file_date_str + "_" + time_str_raw, "%Y%m%d_%H%M%S")
                        except Exception:
                            continue

                        if not (start_date <= file_date <= end_date):
                            continue

                        date_key = file_date.strftime("%Y-%m-%d")
                        bt_grouped.setdefault(date_key, {}).setdefault(model_name, {})
                        if is_tv_code:
                            bt_grouped[date_key][model_name][f"File_{file}"] = (fp, file_dt)
                        else:
                            cur = bt_grouped[date_key][model_name].get(ticker_name)
                            if cur is None or file_dt > cur[1]:
                                bt_grouped[date_key][model_name][ticker_name] = (fp, file_dt)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"Error scanning files: {exc}")

        total = sum(len(t) for d in bt_grouped.values() for t in d.values())
        date_count = len(bt_grouped)
        self._bt_lbl_status.setText(f"Found {total} file(s) across {date_count} date(s)")
        self._bt_cached_grouped = bt_grouped
        self._bt_cached_ticker = ticker
        self._bt_cached_is_cme = is_cme

        self._combo_bt_models.blockSignals(True)
        self._combo_bt_models.clear()
        if not bt_grouped:
            self._combo_bt_models.addItem("No Data")
        else:
            by_model: dict[str, Any] = {}
            for date_key, models in bt_grouped.items():
                for model_name, tickers_map in models.items():
                    by_model.setdefault(model_name, {})[date_key] = tickers_map
            for m in sorted(by_model.keys()):
                self._combo_bt_models.addItem(m)
        self._combo_bt_models.blockSignals(False)
        self._on_bt_model_combo(self._combo_bt_models.currentText())

    def _on_bt_model_combo(self, selected_model: str) -> None:
        self._rebuild_bt_model_view(
            self._bt_cached_grouped,
            self._bt_cached_ticker,
            self._bt_cached_is_cme,
            selected_model,
        )

    def _rebuild_bt_model_view(
        self,
        bt_grouped: dict,
        ticker: str,
        _is_cme: bool,
        selected_model: str,
    ) -> None:
        self._clear_bt_scroll()
        if not bt_grouped or selected_model in ("", "No Data"):
            lab = QLabel(
                "No files found for the selected criteria."
                if not bt_grouped
                else "No files found."
            )
            lab.setStyleSheet("color:gray;")
            self._bt_scroll_layout.insertWidget(0, lab)
            return

        by_model: dict[str, dict] = {}
        for date_key, models in bt_grouped.items():
            for model_name, tickers_map in models.items():
                by_model.setdefault(model_name, {})[date_key] = tickers_map

        if selected_model not in by_model:
            lab = QLabel("No files found.")
            lab.setStyleSheet("color:gray;")
            self._bt_scroll_layout.insertWidget(0, lab)
            return

        dates_map = by_model[selected_model]
        is_tv = "TV Code" in selected_model

        for date_key in sorted(dates_map.keys()):
            tickers_on_date = dates_map[date_key]
            date_content = QWidget()
            dcl = QVBoxLayout(date_content)
            date_chk_list: list[tuple[QCheckBox, Any]] = []

            if is_tv:
                tv_files_sorted = sorted(tickers_on_date.values(), key=lambda x: x[1])
                merged_tv: dict[str, str] = {}
                for fp_tv, _ in tv_files_sorted:
                    try:
                        with open(fp_tv, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                if '"' in line:
                                    pq = line.split('"')
                                    lbl = pq[1] if len(pq) > 1 else "Unknown"
                                else:
                                    lbl = line.split(" ")[0].replace(":", "")
                                merged_tv[lbl] = line
                    except Exception as exc:
                        print(f"Error reading TV file: {exc}")

                filtered_tv = {lbl: c for lbl, c in merged_tv.items() if lbl == ticker}
                title_count = len(filtered_tv)

                if not filtered_tv:
                    lab = QLabel(f"  '{ticker}' not found in TV Code")
                    lab.setStyleSheet("color:gray;")
                    dcl.addWidget(lab)
                else:
                    for t_label, content in filtered_tv.items():
                        cb = QCheckBox(t_label)
                        cb.setChecked(True)
                        cb.setFont(QFont("Consolas", 11))
                        dcl.addWidget(cb)
                        tup: Any = ("TV_DATA", t_label, content, date_key)
                        self._bt_file_vars.append((cb, tup))
                        date_chk_list.append((cb, tup))
            else:
                title_count = len(tickers_on_date)
                for tn, (fp_n, dt_obj) in sorted(tickers_on_date.items()):
                    time_s = dt_obj.strftime("%H:%M:%S")
                    cb = QCheckBox(f"[{time_s}]  {tn}")
                    cb.setChecked(True)
                    cb.setFont(QFont("Consolas", 11))
                    dcl.addWidget(cb)
                    self._bt_file_vars.append((cb, fp_n))
                    date_chk_list.append((cb, fp_n))

            gb_date = QGroupBox(f"{date_key}  ({title_count})")
            gb_date.setCheckable(True)
            gb_date.setChecked(True)

            hdr = QHBoxLayout()
            btn_sel_date = QPushButton("☑")
            btn_sel_date.setFixedWidth(36)

            def make_date_sel_all(lst: list[tuple[QCheckBox, Any]]) -> None:
                def _() -> None:
                    if not lst:
                        return
                    all_sel = all(v.isChecked() for v, _ in lst)
                    for v, _ in lst:
                        v.setChecked(not all_sel)

                return _

            btn_sel_date.clicked.connect(make_date_sel_all(date_chk_list))
            hdr.addWidget(btn_sel_date)
            hdr.addStretch(1)

            box_layout = QVBoxLayout()
            box_layout.addLayout(hdr)
            box_layout.addWidget(date_content)
            gb_date.setLayout(box_layout)
            gb_date.toggled.connect(lambda c, dc=date_content: dc.setVisible(c))
            self._bt_scroll_layout.insertWidget(self._bt_scroll_layout.count() - 1, gb_date)

    def _bt_select_all(self) -> None:
        for cb, _ in self._bt_file_vars:
            cb.setChecked(True)

    def _bt_deselect_all(self) -> None:
        for cb, _ in self._bt_file_vars:
            cb.setChecked(False)

    def _bt_open_selected(self) -> None:
        tv_data_to_show: list[tuple[Any, ...]] = []
        for cb, data in self._bt_file_vars:
            if not cb.isChecked():
                continue
            try:
                if isinstance(data, tuple) and data[0] == "TV_DATA":
                    tv_data_to_show.append(data)
                else:
                    open_file_cross_platform(str(data))
            except Exception as exc:
                print(f"Error opening item: {exc}")
        if tv_data_to_show:
            try:
                fd, path = tempfile.mkstemp(prefix="TV_Selected_", suffix=".txt", text=True)
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    for item in tv_data_to_show:
                        t_label = item[1]
                        content = item[2]
                        date_key = item[3] if len(item) > 3 else None
                        date_stamp = date_key.replace("-", "") if date_key else ""
                        clean_content = content
                        prefix = f"{t_label}:"
                        if clean_content.startswith(prefix):
                            clean_content = clean_content[len(prefix) :].strip()
                        elif clean_content.startswith(t_label):
                            clean_content = clean_content[len(t_label) :].strip()
                        header = f"{date_stamp} {t_label}:" if date_stamp else f"{t_label}:"
                        tmp.write(f"{header}\n\n")
                        tmp.write(f"{clean_content}\n\n")
                open_file_cross_platform(path)
            except Exception as exc:
                print(f"Error creating aggregate TV file: {exc}")
