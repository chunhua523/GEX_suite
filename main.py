"""Entry point for GEX Suite (PySide6).

Run:
    python main.py

Or run an individual module standalone:
    python -m gex_suite.modules.scraper
    python -m gex_suite.modules.chart
    python -m gex_suite.modules.tradingview
"""
import sys
from pathlib import Path

# Make sure the package is importable when running from source.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from gex_suite.app.main_window import MainWindow
from gex_suite.app.theme import apply_dark_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("GEX Suite")
    apply_dark_theme(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
