"""Main window: sidebar + stacked pages.

Each page is the *full* widget of an underlying tool. The same widget classes
can be reused standalone via ``python -m gex_suite.modules.<name>``.
"""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from gex_suite.shared import config
from gex_suite.shared.db import init_db
from gex_suite.shared.paths import PROJECT_ROOT, ensure_dirs
from gex_suite.shared.updater import (
    GitHubVersionCheckThread,
    GitPullThread,
    UpdateCheckResult,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        init_db()

        self.setWindowTitle("GEX Suite")
        self.resize(1180, 760)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("suiteSidebar")
        self.sidebar.setFixedWidth(220)
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setSpacing(2)
        self.sidebar.setUniformItemSizes(True)

        self.stack = QStackedWidget()

        # Lazy imports avoid loading every module's heavy deps up-front.
        from gex_suite.modules.scraper.widget import ScraperPage
        from gex_suite.modules.chart.widget import ChartPage
        from gex_suite.modules.tradingview.widget import TradingViewPage

        self._pages: list[tuple[str, QWidget]] = [
            ("Scraper", ScraperPage()),
            ("GEX Chart", ChartPage()),
            ("TradingView Auto-Paste", TradingViewPage()),
        ]

        for label, page in self._pages:
            QListWidgetItem(label, self.sidebar)
            self.stack.addWidget(page)

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)

        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._update_check_thread: GitHubVersionCheckThread | None = None
        self._git_pull_thread: GitPullThread | None = None
        self._build_help_menu()

    def _build_help_menu(self) -> None:
        menu = self.menuBar().addMenu("說明")
        act = QAction("檢查更新…", self)
        act.triggered.connect(self._on_check_updates)
        menu.addAction(act)

    def _on_check_updates(self) -> None:
        cfg = config.load_config()
        th = GitHubVersionCheckThread(
            user=str(cfg.get("update_github_user") or ""),
            repo=str(cfg.get("update_github_repo") or ""),
            branch=str(cfg.get("update_github_branch") or "main"),
            remote_pyproject_path=str(cfg.get("update_remote_pyproject_path") or "pyproject.toml"),
            parent=self,
        )
        self._update_check_thread = th
        th.finished.connect(self._on_update_check_finished)
        th.start()

    def _on_update_check_finished(self, result: object) -> None:
        self._update_check_thread = None
        if not isinstance(result, UpdateCheckResult):
            return
        if result.error_message:
            extra = f"\n\n{result.remote_url}" if result.remote_url else ""
            QMessageBox.warning(self, "檢查更新", result.error_message + extra)
            return
        assert result.remote_version is not None and result.is_up_to_date is not None
        if result.is_up_to_date:
            QMessageBox.information(
                self,
                "檢查更新",
                f"已是最新版本。\n\n本機：{result.local_version}\n遠端：{result.remote_version}",
            )
            return
        msg = (
            f"發現較新版本。\n\n本機：{result.local_version}\n遠端：{result.remote_version}\n\n"
            "若目前是 git clone 的原始碼，可嘗試在專案根目錄執行 git pull。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("檢查更新")
        box.setText(msg)
        box.setIcon(QMessageBox.Information)
        pull_btn = box.addButton("執行 git pull（--ff-only）", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is pull_btn:
            self._run_git_pull()

    def _run_git_pull(self) -> None:
        th = GitPullThread(PROJECT_ROOT, parent=self)
        self._git_pull_thread = th
        th.finished.connect(self._on_git_pull_finished)
        th.start()

    def _on_git_pull_finished(self, ok: bool, message: str) -> None:
        self._git_pull_thread = None
        QMessageBox.information(
            self,
            "git pull",
            message if message else ("完成" if ok else "失敗"),
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Let pages persist their own state (e.g. ScraperPage.save_settings).
        for _, page in self._pages:
            saver = getattr(page, "on_app_closing", None)
            if callable(saver):
                try:
                    saver()
                except Exception as exc:  # pragma: no cover
                    print(f"[GEX Suite] page close hook failed: {exc}")
        super().closeEvent(event)
