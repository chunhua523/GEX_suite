"""ScraperPage — PySide6 port of GEX_scraper/gui.py:LietaApp.

Full workflow: login, ticker files, ticker manager, models, schedule, run,
stop, retry, **View Scraped Files** (by date / by ticker & model), and open
download folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import holidays

from gex_suite.shared.paths import (
    SCRAPER_DATA_DIR,
    SCRAPER_LOG_DIR,
    SCRAPER_SETTINGS_PATH,
    ensure_dirs,
)

from . import utils
from .file_viewer import ScrapedFilesDialog
from .runner import LietaScraper
from .ticker_manager_dialog import TickerManagerDialog

STANDARD_MODELS = ["Gamma", "Delta", "Theta", "Term", "Smile", "Levels", "Table", "TV Code"]
CME_MODELS = ["Gamma", "Delta", "Smile", "Term", "TV Code"]


class _LogEmitter(QObject):
    """Thread-safe bridge that lets background threads append to the log."""

    line = Signal(str)


# ---------- Ticker selection / retry / manager dialogs ---------------------


class TickerSelectionDialog(QDialog):
    """Pre-run dialog: pick which tickers (per group) to actually run."""

    def __init__(self, groups_std: dict, groups_cme: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("選擇要執行的 Tickers")
        self.resize(640, 600)

        self._std_vars: list[tuple[str, QCheckBox]] = []
        self._cme_vars: list[tuple[str, QCheckBox]] = []
        self.result_std: Optional[list[str]] = None
        self.result_cme: Optional[list[str]] = None

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        layout.addLayout(top)
        top.addStretch(1)
        btn_all = QPushButton("全選")
        btn_none = QPushButton("取消全選")
        btn_all.clicked.connect(lambda: self._toggle_all(True))
        btn_none.clicked.connect(lambda: self._toggle_all(False))
        top.addWidget(btn_all)
        top.addWidget(btn_none)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        scroll.setWidget(inner)
        inner_layout = QVBoxLayout(inner)
        layout.addWidget(scroll, 1)

        if groups_std:
            for name in sorted(groups_std):
                self._add_section(inner_layout, "Standard", name, groups_std[name], self._std_vars)
        if groups_cme:
            for name in sorted(groups_cme):
                self._add_section(inner_layout, "CME", name, groups_cme[name], self._cme_vars)
        inner_layout.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        confirm = QPushButton("確認並開始")
        confirm.setDefault(True)
        bb.addButton(confirm, QDialogButtonBox.AcceptRole)
        confirm.clicked.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _add_section(self, parent_layout, platform_label: str, group: str, tickers: list[str], var_list) -> None:
        if not tickers:
            return
        box = QGroupBox(f"{platform_label} - {group} ({len(tickers)} tickers)")
        v = QVBoxLayout(box)
        row_btn = QHBoxLayout()
        v.addLayout(row_btn)
        b_all = QPushButton("全選")
        b_none = QPushButton("取消全選")
        row_btn.addWidget(b_all)
        row_btn.addWidget(b_none)
        row_btn.addStretch(1)
        local: list[QCheckBox] = []
        for t in tickers:
            cb = QCheckBox(t)
            cb.setChecked(True)
            v.addWidget(cb)
            local.append(cb)
            var_list.append((t, cb))
        b_all.clicked.connect(lambda: [c.setChecked(True) for c in local])
        b_none.clicked.connect(lambda: [c.setChecked(False) for c in local])
        parent_layout.addWidget(box)

    def _toggle_all(self, on: bool) -> None:
        for _, cb in self._std_vars + self._cme_vars:
            cb.setChecked(on)

    def _on_accept(self) -> None:
        self.result_std = [t for t, c in self._std_vars if c.isChecked()]
        self.result_cme = [t for t, c in self._cme_vars if c.isChecked()]
        self.accept()


class RetrySelectionDialog(QDialog):
    def __init__(self, failed_tasks: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("選擇要重試的失敗項目")
        self.resize(600, 500)
        self.selected: Optional[list[dict]] = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"共 {len(failed_tasks)} 個失敗項目（預設全選）"))
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        for item in failed_tasks:
            platform_label = "Standard" if item.get("platform") == "std" else "CME"
            text = f"[{platform_label}] {item.get('model','')} - {item.get('ticker','')}"
            li = QListWidgetItem(text)
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Checked)
            li.setData(Qt.UserRole, item)
            self.list.addItem(li)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        layout.addLayout(row)
        b_all = QPushButton("全選")
        b_none = QPushButton("取消全選")
        b_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        b_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = QPushButton("確認並重試")
        ok.setDefault(True)
        bb.addButton(ok, QDialogButtonBox.AcceptRole)
        ok.clicked.connect(self._accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)

    def _set_all(self, state: Qt.CheckState) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _accept(self) -> None:
        out = []
        for i in range(self.list.count()):
            li = self.list.item(i)
            if li.checkState() == Qt.Checked:
                out.append(li.data(Qt.UserRole))
        self.selected = out
        self.accept()


# ---------- Main page ------------------------------------------------------


class ScraperPage(QWidget):
    """The full Scraper UI as a single QWidget (embeddable in the wrapper)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_dirs()

        self.ticker_filepath: Optional[str] = None
        self.cme_ticker_filepath: Optional[str] = None
        self.download_folder: Optional[str] = None
        self.scraper_instance: Optional[LietaScraper] = None
        self.last_failed_tasks: list[dict] = []
        self.current_log_file: Optional[str] = None
        self.last_run_date: Optional[str] = None
        self.last_market_skip_date: Optional[str] = None

        self._log_emitter = _LogEmitter()
        self._log_emitter.line.connect(self._append_log_line)

        self._build_ui()
        self._load_settings()

        # Schedule polling — every 10 s.
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(10_000)
        self._schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_timer.start()

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 15, 15, 15)

        # Top bar: login + browser selection
        top = QHBoxLayout()
        outer.addLayout(top)
        self.btn_login = QPushButton("Log in via Browser")
        self.btn_login.clicked.connect(self._on_login_click)
        top.addWidget(self.btn_login)
        self.lbl_login_status = QLabel("Not Logged In")
        self.lbl_login_status.setStyleSheet("color:#FF5C5C; font-weight:bold;")
        top.addWidget(self.lbl_login_status)
        top.addStretch(1)

        top.addWidget(QLabel("Browser:"))
        self.radio_chrome = QRadioButton("Chrome")
        self.radio_brave = QRadioButton("Brave")
        self.radio_chrome.setChecked(True)
        self._browser_group = QButtonGroup(self)
        self._browser_group.addButton(self.radio_chrome)
        self._browser_group.addButton(self.radio_brave)
        top.addWidget(self.radio_chrome)
        top.addWidget(self.radio_brave)

        # Two-column main config
        cols = QHBoxLayout()
        outer.addLayout(cols)
        cols.addWidget(self._build_platform_box(
            title="Standard Platform",
            models=STANDARD_MODELS,
            ticker_var="ticker_filepath",
            is_cme=False,
        ))
        cols.addWidget(self._build_platform_box(
            title="CME Platform",
            models=CME_MODELS,
            ticker_var="cme_ticker_filepath",
            is_cme=True,
        ))

        # Global config
        gbox = QGroupBox("Global Configuration")
        glayout = QFormLayout(gbox)

        path_row = QHBoxLayout()
        self.btn_dl_path = QPushButton("Select Download Folder")
        self.btn_dl_path.clicked.connect(self._select_download_folder)
        path_row.addWidget(self.btn_dl_path)
        self.btn_open_folder = QPushButton("📂 Open Folder")
        self.btn_open_folder.clicked.connect(self._open_download_folder)
        path_row.addWidget(self.btn_open_folder)
        self.btn_view_files = QPushButton("View Scraped Files")
        self.btn_view_files.clicked.connect(self._open_scraped_files_viewer)
        path_row.addWidget(self.btn_view_files)
        path_row.addStretch(1)
        glayout.addRow(path_row)

        self.lbl_dl_path = QLabel("No folder selected")
        self.lbl_dl_path.setStyleSheet("color:#DCE4EE;")
        glayout.addRow("Folder:", self.lbl_dl_path)

        self.chk_parallel = QCheckBox("Multi-window Mode (Scrape Std & CME in parallel)")
        glayout.addRow(self.chk_parallel)

        sched_row = QHBoxLayout()
        sched_row.addWidget(QLabel("Auto-Schedule (US trading days):"))
        self.entry_time = QLineEdit()
        self.entry_time.setPlaceholderText("HH:MM e.g. 09:00")
        self.entry_time.setFixedWidth(80)
        sched_row.addWidget(self.entry_time)
        sched_row.addWidget(QLabel("TZ:"))
        self.combo_schedule_tz = QComboBox()
        self.combo_schedule_tz.addItems(["America/New_York", "Local"])
        self.combo_schedule_tz.setFixedWidth(160)
        sched_row.addWidget(self.combo_schedule_tz)
        self.chk_schedule = QCheckBox("Enable Auto-Run")
        sched_row.addWidget(self.chk_schedule)
        sched_row.addStretch(1)
        glayout.addRow(sched_row)

        outer.addWidget(gbox)

        # Action row
        actions = QHBoxLayout()
        outer.addLayout(actions)
        self.btn_start = QPushButton("START SCRAPING")
        self.btn_start.setStyleSheet("background:#2CC985; color:white; font-weight:bold;")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(lambda: self._on_start(skip_selection_dialog=False))
        actions.addWidget(self.btn_start, 2)

        self.btn_retry = QPushButton("RETRY FAILED")
        self.btn_retry.setStyleSheet("background:#FFA500; color:white; font-weight:bold;")
        self.btn_retry.setMinimumHeight(40)
        self.btn_retry.setEnabled(False)
        self.btn_retry.clicked.connect(self._on_retry)
        actions.addWidget(self.btn_retry, 1)

        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet("background:#FF4D4D; color:white; font-weight:bold;")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        actions.addWidget(self.btn_stop, 1)

        # Console
        outer.addWidget(QLabel("Logs:"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        outer.addWidget(self.console, 1)

    def _build_platform_box(
        self, *, title: str, models: list[str], ticker_var: str, is_cme: bool
    ) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)

        # Ticker file row
        row1 = QHBoxLayout()
        layout.addLayout(row1)
        btn_select = QPushButton("Select CME Ticker List" if is_cme else "Select Ticker List")
        btn_manage = QPushButton("✏️ Manage")
        row1.addWidget(btn_select)
        row1.addWidget(btn_manage)
        row1.addStretch(1)

        lbl = QLabel("No file selected")
        lbl.setStyleSheet("color:#DCE4EE;")
        layout.addWidget(lbl)

        layout.addWidget(QLabel("Models:"))
        grid = QGridLayout()
        layout.addLayout(grid)

        var_dict: dict[str, QCheckBox] = {}
        for i, m in enumerate(models):
            cb = QCheckBox(m)
            grid.addWidget(cb, i // 2, i % 2)
            var_dict[m] = cb

        # Wire button callbacks
        if is_cme:
            self.lbl_cme_ticker = lbl
            self.cme_model_vars = var_dict
            btn_select.clicked.connect(self._select_cme_ticker_file)
            btn_manage.clicked.connect(self._manage_cme_tickers)
        else:
            self.lbl_ticker_file = lbl
            self.model_vars = var_dict
            btn_select.clicked.connect(self._select_ticker_file)
            btn_manage.clicked.connect(self._manage_std_tickers)

        return box

    # ---------- File pickers ----------
    def _select_ticker_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Ticker List", "", "Ticker files (*.txt *.csv *.json)")
        if path:
            self.ticker_filepath = os.path.abspath(path)
            self.lbl_ticker_file.setText(os.path.basename(path))
            self._log(f"Selected tickers: {self.ticker_filepath}")

    def _select_cme_ticker_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select CME Ticker List", "", "Ticker files (*.txt *.csv *.json)")
        if path:
            self.cme_ticker_filepath = os.path.abspath(path)
            self.lbl_cme_ticker.setText(os.path.basename(path))
            self._log(f"Selected CME tickers: {self.cme_ticker_filepath}")

    def _select_download_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Download Folder")
        if path:
            self.download_folder = os.path.abspath(path)
            self.lbl_dl_path.setText(self.download_folder)
            self._log(f"Selected download folder: {self.download_folder}")

    def _open_download_folder(self) -> None:
        if not self.download_folder or not os.path.isdir(self.download_folder):
            QMessageBox.warning(self, "Warning", "Download folder is not set or invalid.")
            return
        path = self.download_folder
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif os.name == "posix":
                import subprocess
                subprocess.Popen(["xdg-open" if os.uname().sysname != "Darwin" else "open", path])
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Cannot open folder: {exc}")

    def _open_scraped_files_viewer(self) -> None:
        if not self.download_folder or not os.path.isdir(self.download_folder):
            QMessageBox.warning(self, "Warning", "Download folder is not set or invalid.")
            return
        dlg = ScrapedFilesDialog(
            self,
            self.download_folder,
            self.ticker_filepath,
            self.cme_ticker_filepath,
        )
        dlg.exec()

    def _manage_std_tickers(self) -> None:
        self._open_ticker_manager(self.ticker_filepath, "Standard Platform Tickers")

    def _manage_cme_tickers(self) -> None:
        self._open_ticker_manager(self.cme_ticker_filepath, "CME Platform Tickers")

    def _open_ticker_manager(self, current_path: Optional[str], title: str = "") -> None:
        if not current_path:
            QMessageBox.information(
                self,
                "未選擇檔案",
                "請先用「Select Ticker List」選擇一個檔案，再使用 Manage 編輯。",
            )
            return
        dlg = TickerManagerDialog(current_path, title, self)
        if dlg.exec() == QDialog.Accepted:
            self._log(f"Ticker file updated: {current_path}")

    # ---------- Login ----------
    def _on_login_click(self) -> None:
        self.btn_login.setEnabled(False)
        browser_type = "brave" if self.radio_brave.isChecked() else "chrome"
        self._log(f"Initializing Login Browser ({browser_type})...")
        threading.Thread(target=self._run_login_thread, args=(browser_type,), daemon=True).start()

    def _run_login_thread(self, browser_type: str) -> None:
        try:
            scraper = LietaScraper(logger_func=self._log_safe, browser_type=browser_type)
            asyncio.run(scraper.perform_login_flow())
            self._log_safe("Login flow finished. Session saved.")
            self._post_to_main(self._mark_logged_in)
        except Exception as e:
            self._log_safe(f"Login error: {e}")
        finally:
            self._post_to_main(lambda: self.btn_login.setEnabled(True))

    def _mark_logged_in(self) -> None:
        self.lbl_login_status.setText("Session Saved")
        self.lbl_login_status.setStyleSheet("color:#2CC985; font-weight:bold;")

    # ---------- Start / Stop / Retry ----------
    def _on_start(self, skip_selection_dialog: bool = False) -> None:
        if not self.download_folder:
            self._log("Error: Please select a download folder.")
            return

        selected_models = [m for m, cb in self.model_vars.items() if cb.isChecked()]
        selected_cme = [m for m, cb in self.cme_model_vars.items() if cb.isChecked()]

        groups_std: dict = {}
        groups_cme: dict = {}
        tickers: list[str] = []
        cme_tickers: list[str] = []

        if selected_models:
            if not self.ticker_filepath:
                self._log("Error: Standard models selected but no Ticker list provided.")
                return
            groups_std = utils.load_tickers_with_groups(self.ticker_filepath)
            tickers = utils.load_tickers_from_file(self.ticker_filepath)
        if selected_cme:
            if not self.cme_ticker_filepath:
                self._log("Error: CME models selected but no CME Ticker list provided.")
                return
            groups_cme = utils.load_tickers_with_groups(self.cme_ticker_filepath)
            cme_tickers = utils.load_tickers_from_file(self.cme_ticker_filepath)

        if not selected_models and not selected_cme:
            self._log("Error: Please select at least one model (Standard or CME).")
            return
        if not tickers and not cme_tickers:
            return

        if skip_selection_dialog:
            tickers_filtered, cme_filtered = tickers, cme_tickers
        else:
            dlg = TickerSelectionDialog(groups_std, groups_cme, self)
            if dlg.exec() != QDialog.Accepted:
                return
            tickers_filtered = dlg.result_std or []
            cme_filtered = dlg.result_cme or []

        parallel = self.chk_parallel.isChecked()
        browser_type = "brave" if self.radio_brave.isChecked() else "chrome"

        self._set_running(True)
        self.last_failed_tasks = []

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_log_file = str(SCRAPER_LOG_DIR / f"run_{ts}.log")
        self._log(
            f"Starting job... (Std: {len(tickers_filtered)} tickers, "
            f"CME: {len(cme_filtered)} tickers) Browser: {browser_type}"
        )
        self._log(f"Logging to: {self.current_log_file}")

        threading.Thread(
            target=self._run_job_thread,
            args=(tickers_filtered, selected_models, cme_filtered, selected_cme,
                  self.download_folder, parallel, browser_type),
            daemon=True,
        ).start()

    def _run_job_thread(
        self,
        tickers: list[str],
        models: list[str],
        cme_tickers: list[str],
        cme_models: list[str],
        download_folder: str,
        parallel: bool,
        browser_type: str,
    ) -> None:
        self.scraper_instance = LietaScraper(logger_func=self._log_safe, browser_type=browser_type)
        try:
            self.last_failed_tasks = asyncio.run(
                self.scraper_instance.perform_full_job(
                    tickers, models, cme_tickers, cme_models, download_folder, parallel
                )
            )
        except Exception as e:
            self._log_safe(f"Job Critical Error: {e}")
        finally:
            self.scraper_instance = None
            self._post_to_main(self._job_finished)

    def _job_finished(self) -> None:
        self._set_running(False)
        if self.last_failed_tasks:
            self.btn_retry.setEnabled(True)
            self._log(f"Job finished with {len(self.last_failed_tasks)} failures. You can Retry Failed items.")
        else:
            self.btn_retry.setEnabled(False)
            self._log("Job finished successfully.")
        self.current_log_file = None

    def _on_stop(self) -> None:
        if self.scraper_instance:
            self._log("Blocking new requests. Stopping...")
            self.scraper_instance.stop_requested = True

    def _on_retry(self) -> None:
        if not self.last_failed_tasks:
            self._log("No failed items to retry.")
            return
        dlg = RetrySelectionDialog(self.last_failed_tasks, self)
        if dlg.exec() != QDialog.Accepted or not dlg.selected:
            return
        selected = dlg.selected

        self._set_running(True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_log_file = str(SCRAPER_LOG_DIR / f"retry_{ts}.log")
        browser_type = "brave" if self.radio_brave.isChecked() else "chrome"
        parallel = self.chk_parallel.isChecked()
        self._log(f"Starting RETRY job... ({len(selected)} items) Browser: {browser_type}")
        threading.Thread(
            target=self._run_retry_thread,
            args=(selected, self.download_folder, parallel, browser_type),
            daemon=True,
        ).start()

    def _run_retry_thread(self, failed_tasks, download_folder, parallel, browser_type) -> None:
        self.scraper_instance = LietaScraper(logger_func=self._log_safe, browser_type=browser_type)
        try:
            self.last_failed_tasks = asyncio.run(
                self.scraper_instance.perform_retry_job(failed_tasks, download_folder, parallel)
            )
        except Exception as e:
            self._log_safe(f"Retry Job Critical Error: {e}")
        finally:
            self.scraper_instance = None
            self._post_to_main(self._job_finished)

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_retry.setEnabled(False if running else bool(self.last_failed_tasks))
        self.btn_stop.setEnabled(running)

    # ---------- Schedule ----------
    def _is_us_market_trading_day(self, now_local: datetime) -> tuple[bool, str, str]:
        us_now = now_local.astimezone(ZoneInfo("America/New_York"))
        us_date = us_now.date()
        us_date_str = us_date.isoformat()
        if us_date.weekday() >= 5:
            return False, us_date_str, "Weekend (US/Eastern)"
        nyse = holidays.NYSE(years=[us_date.year])
        if us_date in nyse:
            return False, us_date_str, f"NYSE holiday: {nyse.get(us_date, 'NYSE Holiday')}"
        return True, us_date_str, ""

    def _check_schedule(self) -> None:
        if not self.chk_schedule.isChecked():
            return
        tz_choice = self.combo_schedule_tz.currentText()
        if tz_choice == "Local":
            now = datetime.now().astimezone()
        else:
            try:
                now = datetime.now(ZoneInfo(tz_choice))
            except Exception:
                now = datetime.now().astimezone()
        target = self.entry_time.text().strip()
        if not target:
            return
        if now.strftime("%H:%M") != target:
            return
        today = now.strftime("%Y-%m-%d")
        if self.last_run_date == today:
            return
        if not self.btn_start.isEnabled():
            self._log("Skipping Schedule: Job already running.")
            return
        is_open, us_date_str, reason = self._is_us_market_trading_day(now)
        if not is_open:
            key = f"{today}|{target}|{us_date_str}"
            if self.last_market_skip_date != key:
                self._log(f"Auto-Schedule Skipped: US market closed ({reason})")
                self.last_market_skip_date = key
            return
        self._log(f"Auto-Schedule Triggered at {target} {tz_choice} (US date: {us_date_str})")
        self.last_run_date = today
        self._on_start(skip_selection_dialog=True)

    # ---------- Logging ----------
    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("[%H:%M:%S] ")
        self.console.appendPlainText(ts + message)
        self.console.moveCursor(QTextCursor.End)
        if self.current_log_file:
            try:
                with open(self.current_log_file, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
            except Exception:
                pass

    def _log_safe(self, message: str) -> None:
        self._log_emitter.line.emit(message)

    def _append_log_line(self, message: str) -> None:
        self._log(message)

    def _post_to_main(self, fn) -> None:
        QTimer.singleShot(0, fn)

    # ---------- Settings persistence ----------
    def _load_settings(self) -> None:
        if not SCRAPER_SETTINGS_PATH.exists():
            return
        try:
            with SCRAPER_SETTINGS_PATH.open("r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception as exc:
            print(f"[Scraper] failed to load settings: {exc}")
            return

        if s.get("ticker_filepath") and os.path.exists(s["ticker_filepath"]):
            self.ticker_filepath = os.path.abspath(s["ticker_filepath"])
            self.lbl_ticker_file.setText(os.path.basename(self.ticker_filepath))
        if s.get("cme_ticker_filepath") and os.path.exists(s["cme_ticker_filepath"]):
            self.cme_ticker_filepath = os.path.abspath(s["cme_ticker_filepath"])
            self.lbl_cme_ticker.setText(os.path.basename(self.cme_ticker_filepath))
        if s.get("download_folder") and os.path.isdir(s["download_folder"]):
            self.download_folder = os.path.abspath(s["download_folder"])
            self.lbl_dl_path.setText(self.download_folder)
        for m in s.get("selected_models", []):
            if m in self.model_vars:
                self.model_vars[m].setChecked(True)
        for m in s.get("selected_cme_models", []):
            if m in self.cme_model_vars:
                self.cme_model_vars[m].setChecked(True)
        self.chk_parallel.setChecked(bool(s.get("parallel", False)))
        if s.get("browser") == "brave":
            self.radio_brave.setChecked(True)
        else:
            self.radio_chrome.setChecked(True)
        self.chk_schedule.setChecked(bool(s.get("schedule_enabled", False)))
        if s.get("schedule_time"):
            self.entry_time.setText(s["schedule_time"])
        tz_val = str(s.get("schedule_timezone", "")).strip()
        if not tz_val or tz_val.lower() == "local":
            self.combo_schedule_tz.setCurrentText("Local")
        else:
            if self.combo_schedule_tz.findText(tz_val) < 0:
                self.combo_schedule_tz.addItem(tz_val)
            self.combo_schedule_tz.setCurrentText(tz_val)

    def _save_settings(self) -> None:
        ensure_dirs()
        # Merge over existing settings instead of overwriting — preserves keys
        # this widget has no UI for (e.g. schedule_timezone), which the API
        # layer and gex_chain rely on.
        existing: dict = {}
        if SCRAPER_SETTINGS_PATH.exists():
            try:
                with SCRAPER_SETTINGS_PATH.open("r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except Exception as exc:
                print(f"[Scraper] failed to read existing settings, will rewrite from scratch: {exc}")
        existing.update({
            "ticker_filepath": self.ticker_filepath,
            "cme_ticker_filepath": self.cme_ticker_filepath,
            "download_folder": self.download_folder,
            "selected_models": [m for m, cb in self.model_vars.items() if cb.isChecked()],
            "selected_cme_models": [m for m, cb in self.cme_model_vars.items() if cb.isChecked()],
            "parallel": self.chk_parallel.isChecked(),
            "browser": "brave" if self.radio_brave.isChecked() else "chrome",
            "schedule_enabled": self.chk_schedule.isChecked(),
            "schedule_time": self.entry_time.text(),
            "schedule_timezone": (
                "local" if self.combo_schedule_tz.currentText() == "Local"
                else self.combo_schedule_tz.currentText()
            ),
        })
        try:
            with SCRAPER_SETTINGS_PATH.open("w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[Scraper] failed to save settings: {exc}")

    def on_app_closing(self) -> None:
        self._save_settings()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        super().closeEvent(event)
