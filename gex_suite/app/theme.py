"""Dark theme using Qt's built-in Fusion style + a dark palette.

No third-party dependency required.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(45, 45, 45))
    pal.setColor(QPalette.WindowText, QColor(220, 228, 238))
    pal.setColor(QPalette.Base, QColor(30, 30, 30))
    pal.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    pal.setColor(QPalette.ToolTipBase, QColor(220, 228, 238))
    pal.setColor(QPalette.ToolTipText, QColor(220, 228, 238))
    pal.setColor(QPalette.Text, QColor(220, 228, 238))
    pal.setColor(QPalette.Button, QColor(53, 53, 53))
    pal.setColor(QPalette.ButtonText, QColor(220, 228, 238))
    pal.setColor(QPalette.BrightText, QColor(255, 0, 0))
    pal.setColor(QPalette.Highlight, QColor(38, 79, 120))
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.Link, QColor(56, 134, 222))
    pal.setColor(QPalette.PlaceholderText, QColor(150, 150, 150))

    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(120, 120, 120))

    app.setPalette(pal)

    app.setStyleSheet(
        """
        QGroupBox {
            border: 1px solid #555;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 6px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }
        QPushButton {
            padding: 6px 12px;
        }
        QListWidget#suiteSidebar {
            background-color: #2a2a2a;
            border: none;
            outline: 0;
            font-size: 14px;
            padding: 8px 0;
        }
        QListWidget#suiteSidebar::item {
            padding: 12px 18px;
            margin: 2px 6px;
            border-radius: 6px;
            color: #DCE4EE;
            min-height: 22px;
        }
        QListWidget#suiteSidebar::item:selected {
            background-color: #264F78;
            color: white;
        }
        QListWidget#suiteSidebar::item:hover:!selected {
            background-color: #3A3A3A;
        }
        """
    )
