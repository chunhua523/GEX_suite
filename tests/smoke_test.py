"""Lightweight smoke test.

Run with: ``python tests/smoke_test.py``

Checks that:
1. Every module imports cleanly.
2. The shared SQLite layer can ``init_db()`` and ``get_all_tickers()``.
3. The MainWindow and each module's standalone QMainWindow construct
   successfully (no widgets actually shown — uses ``QT_QPA_PLATFORM=offscreen``
   automatically when DISPLAY is missing).
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Headless on CI / when no display; harmless on Windows desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    failures: list[str] = []

    def step(name: str, fn) -> None:
        try:
            fn()
            print(f"  [OK] {name}")
        except Exception as exc:
            traceback.print_exc()
            failures.append(f"{name}: {exc}")
            print(f"  [FAIL] {name}: {exc}")

    print("Importing modules...")
    step("import gex_suite", lambda: __import__("gex_suite"))
    step("import shared.db", lambda: __import__("gex_suite.shared.db", fromlist=["*"]))
    step("import shared.paths", lambda: __import__("gex_suite.shared.paths", fromlist=["*"]))
    step("import scraper.runner", lambda: __import__("gex_suite.modules.scraper.runner", fromlist=["*"]))
    step("import scraper.utils", lambda: __import__("gex_suite.modules.scraper.utils", fromlist=["*"]))
    step("import scraper.cli", lambda: __import__("gex_suite.modules.scraper.cli", fromlist=["*"]))
    step("import scraper.widget", lambda: __import__("gex_suite.modules.scraper.widget", fromlist=["*"]))
    step("import scraper.gamma_parse", lambda: __import__("gex_suite.modules.scraper.gamma_parse", fromlist=["*"]))
    step("import chart.parser", lambda: __import__("gex_suite.modules.chart.parser", fromlist=["*"]))
    step("import chart.importers", lambda: __import__("gex_suite.modules.chart.importers", fromlist=["*"]))
    step("import chart.ohlc", lambda: __import__("gex_suite.modules.chart.ohlc", fromlist=["*"]))
    step("import chart.plot", lambda: __import__("gex_suite.modules.chart.plot", fromlist=["*"]))
    step("import chart.widget", lambda: __import__("gex_suite.modules.chart.widget", fromlist=["*"]))
    step("import tradingview.widget", lambda: __import__("gex_suite.modules.tradingview.widget", fromlist=["*"]))
    step("import tradingview.automator", lambda: __import__("gex_suite.modules.tradingview.automator", fromlist=["*"]))

    print("DB sanity...")
    from gex_suite.shared import db
    step("init_db", db.init_db)
    step("get_all_tickers", db.get_all_tickers)

    print("Constructing widgets...")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from gex_suite.app.main_window import MainWindow
    from gex_suite.modules.scraper.widget import ScraperPage
    from gex_suite.modules.chart.widget import ChartPage
    from gex_suite.modules.tradingview.widget import TradingViewPage

    step("MainWindow()", lambda: MainWindow())
    step("ScraperPage()", lambda: ScraperPage())
    step("ChartPage()", lambda: ChartPage())
    step("TradingViewPage()", lambda: TradingViewPage())

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
