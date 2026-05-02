"""Standalone launcher::

    python -m gex_suite.modules.tradingview
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMainWindow

from gex_suite.app.theme import apply_dark_theme
from gex_suite.shared.db import init_db
from gex_suite.shared.paths import ensure_dirs

from .widget import TradingViewPage


def main() -> int:
    ensure_dirs()
    init_db()
    app = QApplication(sys.argv)
    app.setApplicationName("GEX TradingView")
    apply_dark_theme(app)

    win = QMainWindow()
    win.setWindowTitle("TradingView Auto-Paste")
    win.resize(960, 720)
    win.setCentralWidget(TradingViewPage())
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
