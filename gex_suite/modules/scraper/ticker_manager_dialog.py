"""Full GUI ticker manager (groups + tickers), port of GEX_scraper/gui.py ``open_ticker_manager``."""
from __future__ import annotations

import copy
import os
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import utils


def _group_display_name(name: str, count: int) -> str:
    return f"{name} ({count})"


class MoveTickersDialog(QDialog):
    """Pick a target group and move tickers out of ``source_group``."""

    def __init__(
        self,
        groups: dict[str, list[str]],
        source_group: str,
        tickers: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._groups = groups
        self._source = source_group
        self._tickers = list(tickers)
        n = len(self._tickers)
        label = f"{n} 個 tickers" if n != 1 else f"'{self._tickers[0]}'"
        self.setWindowTitle("批次移動 Tickers")
        self.resize(420, 400)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("選擇目標群組:"), alignment=Qt.AlignLeft)
        layout.addWidget(
            QLabel(f"將 {label} 從「{source_group}」移動到:"),
            alignment=Qt.AlignLeft,
        )

        self._list = QListWidget()
        targets = sorted(g for g in self._groups.keys() if g != source_group)
        for g in targets:
            item = QListWidgetItem(_group_display_name(g, len(self._groups[g])))
            item.setData(Qt.UserRole, g)
            self._list.addItem(item)
        if targets:
            self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _on_ok(self) -> None:
        item = self._list.currentItem()
        if not item:
            QMessageBox.warning(self, "警告", "請選擇目標群組。")
            return
        target = item.data(Qt.UserRole)
        if not isinstance(target, str) or target not in self._groups:
            return
        for t in self._tickers:
            if t in self._groups[self._source]:
                self._groups[self._source].remove(t)
            if t not in self._groups[target]:
                self._groups[target].append(t)
        self.accept()


class TickerManagerDialog(QDialog):
    """Manage tickers by group (same behavior as legacy CustomTkinter UI)."""

    def __init__(self, filepath: str, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filepath = os.path.abspath(filepath)
        base = os.path.basename(filepath)
        self.setWindowTitle(title or f"Manage Tickers — {base}")
        self.resize(820, 640)

        try:
            loaded = utils.load_tickers_with_groups(self._filepath)
        except Exception:
            loaded = {"Default": []}
        if not isinstance(loaded, dict) or not loaded:
            loaded = {"Default": []}
        self._groups: dict[str, list[str]] = copy.deepcopy(loaded)

        splitter = QSplitter(Qt.Horizontal, self)
        root = QVBoxLayout(self)
        root.addWidget(splitter, 1)

        # ----- Left: groups -----
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>群組 (Groups)</b>"))
        self._group_list = QListWidget()
        self._group_list.currentItemChanged.connect(self._on_group_changed)
        lv.addWidget(self._group_list, 1)

        gbf = QHBoxLayout()
        lv.addLayout(gbf)
        gbf.addWidget(QPushButton("+ 新增", clicked=self._add_group))
        gbf.addWidget(QPushButton("✏️ 重新命名", clicked=self._rename_group))
        gbf.addWidget(QPushButton("🗑️ 刪除", clicked=self._delete_group))
        splitter.addWidget(left)

        # ----- Right: tickers -----
        right = QWidget()
        rv = QVBoxLayout(right)
        self._ticker_title = QLabel("Tickers")
        self._ticker_title.setStyleSheet("font-weight:bold;font-size:14px;")
        rv.addWidget(self._ticker_title)

        # QListWidget 自帶捲動；用 setItemWidget 管理列，避免 QScrollArea + deleteLater 殘影／重疊繪製
        self._ticker_list = QListWidget()
        self._ticker_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._ticker_list.setUniformItemSizes(True)
        self._ticker_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._ticker_list.setSpacing(2)
        rv.addWidget(self._ticker_list, 1)

        tbf = QHBoxLayout()
        rv.addLayout(tbf)
        tbf.addWidget(QPushButton("+ 新增 Ticker", clicked=self._add_tickers))
        tbf.addWidget(QPushButton("☑ 全選", clicked=self._select_all_tickers))
        tbf.addWidget(QPushButton("☐ 取消", clicked=self._deselect_all_tickers))
        tbf.addWidget(QPushButton("➜ 批次移動", clicked=self._batch_move_tickers))
        tbf.addStretch(1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 560])

        bottom = QHBoxLayout()
        root.addLayout(bottom)
        bottom.addStretch(1)
        btn_save = QPushButton("💾 儲存")
        btn_save.setStyleSheet("background:#2CC985;color:white;font-weight:bold;padding:8px 16px;")
        btn_save.clicked.connect(self._save)
        bottom.addWidget(btn_save)
        btn_cancel = QPushButton("✖️ 取消")
        btn_cancel.setStyleSheet("padding:8px 16px;")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)

        self._refresh_group_list(select_name=None)

    def _each_ticker_row(self):
        for i in range(self._ticker_list.count()):
            it = self._ticker_list.item(i)
            w = self._ticker_list.itemWidget(it)
            if w is None:
                continue
            cb = w.findChild(QCheckBox)
            if cb is None:
                continue
            t = it.data(Qt.ItemDataRole.UserRole)
            yield (str(t) if t else ""), cb

    def _current_group_name(self) -> Optional[str]:
        item = self._group_list.currentItem()
        if not item:
            return None
        name = item.data(Qt.UserRole)
        return str(name) if name else None

    def _refresh_group_list(self, select_name: Optional[str] = None) -> None:
        prev = select_name or self._current_group_name()
        self._group_list.blockSignals(True)
        self._group_list.clear()
        for name in self._groups.keys():
            item = QListWidgetItem(_group_display_name(name, len(self._groups[name])))
            item.setData(Qt.UserRole, name)
            self._group_list.addItem(item)
        self._group_list.blockSignals(False)
        if prev and prev in self._groups:
            for i in range(self._group_list.count()):
                it = self._group_list.item(i)
                if it and it.data(Qt.UserRole) == prev:
                    self._group_list.setCurrentItem(it)
                    break
        elif self._group_list.count() > 0:
            self._group_list.setCurrentRow(0)
        self._refresh_tickers()

    def _on_group_changed(self, _cur: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        self._refresh_tickers()

    def _refresh_tickers(self) -> None:
        self._ticker_list.clear()

        g = self._current_group_name()
        self._ticker_title.setText(f"Tickers — {g}" if g else "Tickers")
        if not g or g not in self._groups:
            return

        row_h = 40
        for ticker in self._groups[g]:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, ticker)
            item.setSizeHint(QSize(0, row_h))

            row = QWidget()
            row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row.setFixedHeight(row_h)
            hl = QHBoxLayout(row)
            hl.setContentsMargins(4, 2, 8, 2)
            hl.setSpacing(8)
            cb = QCheckBox(ticker)
            cb.setFont(QFont("Segoe UI", 11))
            hl.addWidget(cb, 0)
            hl.addStretch(1)
            del_btn = QPushButton("🗑️")
            del_btn.setFixedWidth(36)
            del_btn.setStyleSheet("background:#FF4D4D;color:white;")
            del_btn.clicked.connect(lambda *, t=ticker: self._delete_one_ticker(t))
            hl.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self._ticker_list.addItem(item)
            self._ticker_list.setItemWidget(item, row)

    def _add_group(self) -> None:
        name, ok = QInputDialog.getText(self, "新增群組", "輸入新群組名稱:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if name in self._groups:
            QMessageBox.warning(self, "警告", "群組名稱已存在。")
            return
        self._groups[name] = []
        self._refresh_group_list(select_name=name)

    def _rename_group(self) -> None:
        cur = self._current_group_name()
        if not cur:
            QMessageBox.warning(self, "警告", "請先選擇一個群組。")
            return
        new_name, ok = QInputDialog.getText(self, "重新命名群組", f"群組「{cur}」的新名稱:", text=cur)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == cur:
            return
        if new_name in self._groups:
            QMessageBox.warning(self, "警告", "群組名稱已存在。")
            return
        self._groups[new_name] = self._groups.pop(cur)
        self._refresh_group_list(select_name=new_name)

    def _delete_group(self) -> None:
        cur = self._current_group_name()
        if not cur:
            QMessageBox.warning(self, "警告", "請先選擇一個群組。")
            return
        n = len(self._groups[cur])
        if (
            QMessageBox.question(
                self,
                "確認",
                f"確定要刪除群組「{cur}」及其 {n} 個 tickers？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        del self._groups[cur]
        self._refresh_group_list(select_name=None)

    def _add_tickers(self) -> None:
        g = self._current_group_name()
        if not g:
            QMessageBox.warning(self, "警告", "請先選擇一個群組。")
            return
        text, ok = QInputDialog.getText(
            self,
            "新增 Ticker",
            "輸入 Ticker 代號（多個請用逗號分隔）:",
        )
        if not ok or not text.strip():
            return
        new_tickers = [t.strip().upper() for t in text.split(",") if t.strip()]
        for t in new_tickers:
            if t not in self._groups[g]:
                self._groups[g].append(t)
        self._refresh_group_list(select_name=g)

    def _delete_one_ticker(self, ticker: str) -> None:
        g = self._current_group_name()
        if g and ticker in self._groups[g]:
            self._groups[g].remove(ticker)
        self._refresh_group_list(select_name=g)

    def _select_all_tickers(self) -> None:
        for _t, cb in self._each_ticker_row():
            cb.setChecked(True)

    def _deselect_all_tickers(self) -> None:
        for _t, cb in self._each_ticker_row():
            cb.setChecked(False)

    def _batch_move_tickers(self) -> None:
        g = self._current_group_name()
        if not g:
            QMessageBox.warning(self, "警告", "請先選擇一個群組。")
            return
        selected = [t for t, cb in self._each_ticker_row() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "警告", "請先勾選要移動的 tickers。")
            return
        targets = [x for x in self._groups.keys() if x != g]
        if not targets:
            QMessageBox.warning(self, "警告", "沒有其他群組可以移動。請先建立新群組。")
            return
        dlg = MoveTickersDialog(self._groups, g, selected, self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_group_list(select_name=g)

    def _save(self) -> None:
        if utils.save_tickers_with_groups(self._filepath, self._groups):
            QMessageBox.information(self, "成功", "變更已儲存。")
            self.accept()
        else:
            QMessageBox.critical(self, "錯誤", "儲存失敗。")
