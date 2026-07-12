"""「版面分組」分頁：整理 TradingView 版面成群組，一組一視窗開啟.

開啟目標二選一：

- App 模式：macOS TradingView 桌面 app（見 ``app_launcher`` 的機制說明）。
- 瀏覽器模式：Chrome/Brave argv forwarding — 對同一個 ``--user-data-dir``
  再次以 ``--new-window url1 url2 …`` 呼叫 binary，正在跑的 instance 會開
  一個新視窗、所有 URL 依序成為 tabs；沿用已登入的 9222 CDP profile，
  不影響 auto-paste session。（不用 Playwright ``ctx.new_page()``：那些 tab
  會落在最近的既有視窗，無法保證開新視窗。）
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gex_suite.shared import config as shared_config
from . import app_launcher
from . import browser_paths
from .automator import PlaywrightCDPAutomator
from .layout_groups import (
    GroupLayout,
    LayoutGroup,
    LayoutGroupsState,
    apply_scan_results_to_disk,
    chart_id_from_url,
    load_layout_groups,
    new_group_id,
    normalize_chart_url,
    save_layout_groups,
    subchart_title,
)
from .qt_async import AsyncCoroThread



class _LayoutPickerDialog(QDialog):
    """從掃描快取多選版面加入群組（已在群組內的項目停用）。"""

    def __init__(self, scanned: list[GroupLayout], group: LayoutGroup, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"加入版面到「{group.name}」")
        self.resize(460, 480)
        root = QVBoxLayout(self)

        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("輸入關鍵字過濾（名稱或 chart id）")
        self.edit_filter.textChanged.connect(self._apply_filter)
        root.addWidget(self.edit_filter)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for layout in scanned:
            item = QListWidgetItem(layout.display_label())
            item.setData(Qt.UserRole, layout)
            item.setToolTip(layout.url)
            if group.contains(layout):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)
                item.setText(item.text() + "（已在群組）")
            self.list_widget.addItem(item)
        root.addWidget(self.list_widget, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            layout: GroupLayout | None = item.data(Qt.UserRole)
            visible = layout.matches_filter(text) if layout is not None else True
            item.setHidden(not visible)

    def selected_layouts(self) -> list[GroupLayout]:
        return [
            item.data(Qt.UserRole)
            for item in self.list_widget.selectedItems()
            if item.data(Qt.UserRole) is not None
        ]


class LayoutGroupsTab(QWidget):
    def __init__(self, log_fn: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log = log_fn
        self._state: LayoutGroupsState = load_layout_groups()
        self._busy_thread: AsyncCoroThread | None = None
        self._scan_cancelled = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        split = QSplitter(Qt.Horizontal)

        # ----- 左：群組 -----
        box_groups = QGroupBox("群組（開啟＝一組一視窗）")
        gl = QVBoxLayout(box_groups)
        self.list_groups = QListWidget()
        self.list_groups.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_groups.currentItemChanged.connect(lambda *_: self._refresh_layouts_list())
        self.list_groups.model().rowsMoved.connect(self._on_groups_reordered)
        gl.addWidget(self.list_groups, 1)
        row_g = QHBoxLayout()
        b_g_add = QPushButton("新增群組")
        b_g_add.clicked.connect(self._on_add_group)
        b_g_rename = QPushButton("重新命名")
        b_g_rename.clicked.connect(self._on_rename_group)
        b_g_del = QPushButton("刪除")
        b_g_del.clicked.connect(self._on_delete_group)
        b_g_up = QPushButton("↑")
        b_g_up.clicked.connect(lambda: self._move_group(-1))
        b_g_down = QPushButton("↓")
        b_g_down.clicked.connect(lambda: self._move_group(+1))
        for b in (b_g_add, b_g_rename, b_g_del, b_g_up, b_g_down):
            row_g.addWidget(b)
        row_g.addStretch(1)
        gl.addLayout(row_g)
        split.addWidget(box_groups)

        # ----- 右：群組內版面 -----
        box_layouts = QGroupBox("群組內版面（分頁順序）")
        ll = QVBoxLayout(box_layouts)
        self.list_layouts = QListWidget()
        self.list_layouts.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_layouts.model().rowsMoved.connect(self._on_layouts_reordered)
        ll.addWidget(self.list_layouts, 1)
        row_l = QHBoxLayout()
        b_l_pick = QPushButton("加入版面…")
        b_l_pick.clicked.connect(self._on_pick_layouts)
        b_l_manual = QPushButton("手動新增 URL…")
        b_l_manual.clicked.connect(self._on_add_manual_url)
        b_l_remove = QPushButton("移除")
        b_l_remove.clicked.connect(self._on_remove_layout)
        b_l_up = QPushButton("↑")
        b_l_up.clicked.connect(lambda: self._move_layout(-1))
        b_l_down = QPushButton("↓")
        b_l_down.clicked.connect(lambda: self._move_layout(+1))
        for b in (b_l_pick, b_l_manual, b_l_remove, b_l_up, b_l_down):
            row_l.addWidget(b)
        row_l.addStretch(1)
        ll.addLayout(row_l)
        split.addWidget(box_layouts)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 560])
        root.addWidget(split, 1)

        # ----- 掃描列 -----
        row_scan = QHBoxLayout()
        self.b_scan = QPushButton("掃描版面（含子圖標題）")
        self.b_scan.clicked.connect(self._on_scan_layouts)
        row_scan.addWidget(self.b_scan)
        self.b_scan_stop = QPushButton("停止掃描")
        self.b_scan_stop.setEnabled(False)
        self.b_scan_stop.clicked.connect(self._on_scan_stop)
        row_scan.addWidget(self.b_scan_stop)
        self.lbl_scan_info = QLabel()
        row_scan.addWidget(self.lbl_scan_info)
        row_scan.addStretch(1)
        root.addLayout(row_scan)

        # ----- 開啟列 -----
        row_open = QHBoxLayout()
        self.b_open_app = QPushButton("開啟群組（App）")
        self.b_open_app.clicked.connect(lambda: self._on_open_app(all_groups=False))
        self.b_open_browser = QPushButton("開啟群組（瀏覽器）")
        self.b_open_browser.clicked.connect(lambda: self._on_open_browser(all_groups=False))
        self.b_open_all_app = QPushButton("開啟全部群組（App）")
        self.b_open_all_app.clicked.connect(lambda: self._on_open_app(all_groups=True))
        self.b_open_all_browser = QPushButton("開啟全部群組（瀏覽器）")
        self.b_open_all_browser.clicked.connect(lambda: self._on_open_browser(all_groups=True))
        for b in (self.b_open_app, self.b_open_browser, self.b_open_all_app, self.b_open_all_browser):
            row_open.addWidget(b)
        row_open.addStretch(1)
        root.addLayout(row_open)

        hint = QLabel(
            "App 模式以 CDP 驅動 TradingView 桌面版：若 app 未帶 debug port 會自動"
            "重啟（視窗與分頁由 app 自行還原）。瀏覽器模式沿用「批次與整理」的 "
            "9222 已登入 profile。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(hint)

        if sys.platform != "darwin":
            for b in (self.b_open_app, self.b_open_all_app):
                b.setEnabled(False)
                b.setToolTip("僅支援 macOS + TradingView 桌面版")

        self._refresh_groups_list()
        self._refresh_scan_info()

    # ---------- 共用 ----------
    def _save(self) -> None:
        save_layout_groups(self._state)

    def _busy(self) -> bool:
        thread = self._busy_thread
        try:
            running = thread is not None and thread.isRunning()
        except RuntimeError:
            # deleteLater 已銷毀 C++ 物件，wrapper 殘留 → 視同已結束。
            running = False
        if running:
            QMessageBox.information(self, "執行中", "請等待目前動作完成。")
            return True
        return False

    def _set_running(self, running: bool) -> None:
        for b in (
            self.b_scan,
            self.b_open_app,
            self.b_open_browser,
            self.b_open_all_app,
            self.b_open_all_browser,
        ):
            b.setEnabled(not running)
        if not running and sys.platform != "darwin":
            for b in (self.b_open_app, self.b_open_all_app):
                b.setEnabled(False)

    def _start_thread(self, coro_factory, on_done, on_fail) -> None:
        thread = AsyncCoroThread(self, coro_factory)
        thread.succeeded.connect(on_done)
        thread.failed.connect(on_fail)
        # 先清掉引用再 deleteLater，避免殘留 wrapper 指向已銷毀的 C++ 物件。
        thread.finished.connect(lambda: self._on_thread_finished(thread))
        thread.finished.connect(thread.deleteLater)
        self._busy_thread = thread
        self._set_running(True)
        thread.start()

    def _on_thread_finished(self, thread: AsyncCoroThread) -> None:
        if self._busy_thread is thread:
            self._busy_thread = None
        self.b_scan_stop.setEnabled(False)
        self._set_running(False)

    def _reload_state(self) -> None:
        """從磁碟重載（每日排程／paste 流程也會更新同一份檔案）並刷新 UI."""
        self._state = load_layout_groups()
        self._refresh_scan_info()
        self._refresh_groups_list()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        # 切到本分頁時撿起外部行程（每日 CLI、批次貼上）寫入的最新快取。
        if self._busy_thread is None:
            self._reload_state()

    def _current_group(self) -> LayoutGroup | None:
        item = self.list_groups.currentItem()
        if item is None:
            return None
        return self._state.group_by_id(str(item.data(Qt.UserRole) or ""))

    # ---------- 清單刷新 ----------
    def _refresh_groups_list(self, select_group_id: str | None = None) -> None:
        prev = select_group_id
        if prev is None:
            cur = self.list_groups.currentItem()
            prev = str(cur.data(Qt.UserRole)) if cur else None
        self.list_groups.blockSignals(True)
        self.list_groups.clear()
        for group in self._state.groups:
            item = QListWidgetItem(f"{group.name}（{len(group.layouts)}）")
            item.setData(Qt.UserRole, group.group_id)
            self.list_groups.addItem(item)
            if group.group_id == prev:
                self.list_groups.setCurrentItem(item)
        self.list_groups.blockSignals(False)
        if self.list_groups.currentItem() is None and self.list_groups.count() > 0:
            self.list_groups.setCurrentRow(0)
        self._refresh_layouts_list()

    def _refresh_layouts_list(self) -> None:
        self.list_layouts.blockSignals(True)
        self.list_layouts.clear()
        group = self._current_group()
        if group is not None:
            for layout in group.layouts:
                suffix = "〔手動〕" if layout.source == "manual" else ""
                item = QListWidgetItem(f"{layout.display_label()}{suffix}")
                item.setData(Qt.UserRole, layout.dedup_key())
                item.setToolTip(layout.url)
                self.list_layouts.addItem(item)
        self.list_layouts.blockSignals(False)

    def _refresh_scan_info(self) -> None:
        if self._state.scanned_at:
            ts = self._state.scanned_at
            try:
                ts = datetime.fromisoformat(self._state.scanned_at).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            self.lbl_scan_info.setText(
                f"上次掃描：{len(self._state.scanned_layouts)} 個版面（{ts}）"
            )
        else:
            self.lbl_scan_info.setText("尚未掃描（也可手動新增 URL）")

    # ---------- 群組操作 ----------
    def _on_add_group(self) -> None:
        name, ok = QInputDialog.getText(self, "新增群組", "群組名稱：")
        name = (name or "").strip()
        if not ok or not name:
            return
        group = LayoutGroup(group_id=new_group_id(), name=name)
        self._state.groups.append(group)
        self._save()
        self._refresh_groups_list(select_group_id=group.group_id)

    def _on_rename_group(self) -> None:
        group = self._current_group()
        if group is None:
            return
        name, ok = QInputDialog.getText(self, "重新命名", "群組名稱：", text=group.name)
        name = (name or "").strip()
        if not ok or not name:
            return
        group.name = name
        self._save()
        self._refresh_groups_list(select_group_id=group.group_id)

    def _on_delete_group(self) -> None:
        group = self._current_group()
        if group is None:
            return
        answer = QMessageBox.question(
            self,
            "刪除群組",
            f"確定刪除群組「{group.name}」？（不影響 TradingView 上的版面本身）",
        )
        if answer != QMessageBox.Yes:
            return
        self._state.groups = [g for g in self._state.groups if g.group_id != group.group_id]
        self._save()
        self._refresh_groups_list()

    def _move_group(self, delta: int) -> None:
        group = self._current_group()
        if group is None:
            return
        idx = next(i for i, g in enumerate(self._state.groups) if g.group_id == group.group_id)
        new_idx = idx + delta
        if not 0 <= new_idx < len(self._state.groups):
            return
        self._state.groups.insert(new_idx, self._state.groups.pop(idx))
        self._save()
        self._refresh_groups_list(select_group_id=group.group_id)

    def _on_groups_reordered(self, *_args) -> None:
        order = [
            str(self.list_groups.item(i).data(Qt.UserRole))
            for i in range(self.list_groups.count())
        ]
        by_id = {g.group_id: g for g in self._state.groups}
        self._state.groups = [by_id[gid] for gid in order if gid in by_id]
        self._save()

    # ---------- 版面操作 ----------
    def _on_pick_layouts(self) -> None:
        group = self._current_group()
        if group is None:
            QMessageBox.information(self, "未選群組", "請先選取（或新增）一個群組。")
            return
        if not self._state.scanned_layouts:
            QMessageBox.information(
                self, "沒有掃描快取", "請先按「掃描版面（CDP）」，或改用「手動新增 URL…」。"
            )
            return
        dlg = _LayoutPickerDialog(self._state.scanned_layouts, group, self)
        if dlg.exec() != QDialog.Accepted:
            return
        added = 0
        for layout in dlg.selected_layouts():
            if not group.contains(layout):
                group.layouts.append(
                    GroupLayout(
                        name=layout.name,
                        url=layout.url,
                        layout_id=layout.layout_id,
                        source=layout.source,
                    )
                )
                added += 1
        if added:
            self._save()
            self._refresh_groups_list(select_group_id=group.group_id)

    def _on_add_manual_url(self) -> None:
        group = self._current_group()
        if group is None:
            QMessageBox.information(self, "未選群組", "請先選取（或新增）一個群組。")
            return
        raw, ok = QInputDialog.getText(
            self, "手動新增 URL", "版面網址（或 chart id）："
        )
        if not ok:
            return
        url = normalize_chart_url(raw)
        if not url:
            QMessageBox.warning(
                self,
                "無效網址",
                "無法解析為 TradingView 版面網址。\n"
                "格式：https://www.tradingview.com/chart/<id>/ 或直接貼 chart id。",
            )
            return
        layout = GroupLayout(
            name=f"URL:{chart_id_from_url(url)}",
            url=url,
            layout_id=chart_id_from_url(url),
            source="manual",
        )
        if group.contains(layout):
            QMessageBox.information(self, "重複", "這個版面已在群組內。")
            return
        group.layouts.append(layout)
        self._save()
        self._refresh_groups_list(select_group_id=group.group_id)

    def _on_remove_layout(self) -> None:
        group = self._current_group()
        row = self.list_layouts.currentRow()
        if group is None or not 0 <= row < len(group.layouts):
            return
        group.layouts.pop(row)
        self._save()
        self._refresh_groups_list(select_group_id=group.group_id)

    def _move_layout(self, delta: int) -> None:
        group = self._current_group()
        row = self.list_layouts.currentRow()
        if group is None or not 0 <= row < len(group.layouts):
            return
        new_row = row + delta
        if not 0 <= new_row < len(group.layouts):
            return
        group.layouts.insert(new_row, group.layouts.pop(row))
        self._save()
        self._refresh_layouts_list()
        self.list_layouts.setCurrentRow(new_row)

    def _on_layouts_reordered(self, *_args) -> None:
        group = self._current_group()
        if group is None:
            return
        order = [
            str(self.list_layouts.item(i).data(Qt.UserRole))
            for i in range(self.list_layouts.count())
        ]
        by_key = {l.dedup_key(): l for l in group.layouts}
        group.layouts = [by_key[key] for key in order if key in by_key]
        self._save()

    # ---------- 掃描（走訪每個版面，讀子圖標題） ----------
    def _on_scan_layouts(self) -> None:
        if self._busy():
            return
        cfg = shared_config.load_tradingview_config()
        cdp_url = str(cfg.get("cdp_url") or "http://127.0.0.1:9222").strip()
        kind = str(cfg.get("browser") or "chrome")
        self._scan_cancelled = False
        self.b_scan_stop.setEnabled(True)
        self._log("【版面分組｜掃描】開始（逐版面讀取子圖標題，版面多時需數分鐘）")
        self._start_thread(
            lambda: self._scan_layouts_coro(cdp_url, kind),
            self._on_scan_done,
            lambda exc: self._on_scan_failed(cdp_url, exc),
        )

    def _on_scan_stop(self) -> None:
        self._scan_cancelled = True
        self.b_scan_stop.setEnabled(False)
        self._log("【版面分組｜掃描】已送出停止請求，將在目前版面完成後結束。")

    async def _read_subchart_titles(self, automator: PlaywrightCDPAutomator) -> list[str]:
        """走訪目前版面的每張子圖，讀 header symbol 作為標題（含公式圖原樣）."""
        titles: list[str] = []
        subs = await automator.enumerate_subcharts()
        for sub in subs:
            await automator.activate_subchart(sub.index)
            raw = ""
            for _ in range(3):  # hydration retry（同批次流程的讀法）
                raw = (await automator.get_symbol_search_value() or "").strip()
                if raw:
                    break
                await asyncio.sleep(0.2)
            title = subchart_title(raw or sub.symbol or "")
            if title:
                titles.append(title)
        return titles

    async def _scan_layouts_coro(self, cdp_url: str, kind: str):
        # 9222 沒開 → 自動啟動瀏覽器（沿用登入 profile）。
        if not browser_paths.cdp_ready():
            self._log("【版面分組｜掃描】9222 瀏覽器未啟動，自動啟動中…")
            if browser_paths.launch_cdp_browser(kind) is None:
                raise RuntimeError(f"找不到 {kind} 瀏覽器執行檔")
            if not await asyncio.to_thread(browser_paths.wait_cdp_ready, 9222, 20.0):
                raise RuntimeError(
                    "瀏覽器已啟動但 9222 未就緒（可能已有非 CDP instance 在跑，請全部關閉後重試）"
                )
            await asyncio.sleep(5)  # 落地頁 hydration
        automator = PlaywrightCDPAutomator(cdp_url=cdp_url)
        automator.set_logger(lambda _m: None)
        try:
            await automator.connect()
            await automator.clear_blocking_overlay()
            layouts = await automator.list_layouts()
            if len(layouts) == 1 and layouts[0].id == "current":
                return {"degraded": True, "results": [], "full": False, "skipped": 0}
            results: list[GroupLayout] = []
            skipped = 0
            cancelled = False
            for i, info in enumerate(layouts, 1):
                if self._scan_cancelled:
                    cancelled = True
                    break
                url = normalize_chart_url(info.url or "")
                if not url:
                    skipped += 1
                    continue
                self._log(f"【版面分組｜掃描】{i}/{len(layouts)}：{info.name}")
                subcharts: list[str] = []
                if await automator.load_layout(info):
                    subcharts = await self._read_subchart_titles(automator)
                else:
                    self._log(f"【版面分組｜掃描】版面載入失敗，子圖沿用舊快取：{info.name}")
                results.append(
                    GroupLayout(
                        name=info.name or f"URL:{chart_id_from_url(url)}",
                        url=url,
                        layout_id=chart_id_from_url(url),
                        source="scan",
                        subcharts=subcharts,
                    )
                )
            return {
                "degraded": False,
                "results": results,
                "full": not cancelled,
                "skipped": skipped,
            }
        finally:
            await automator.close()

    def _on_scan_done(self, payload) -> None:
        payload = payload or {}
        if payload.get("degraded"):
            QMessageBox.warning(
                self,
                "掃描不完整",
                "無法開啟版面清單對話框（請確認 9222 瀏覽器已登入並停在圖表頁），\n"
                "已保留原有掃描快取。",
            )
            return
        results = list(payload.get("results") or [])
        full = bool(payload.get("full"))
        skipped = int(payload.get("skipped") or 0)
        if not results:
            self._log("【版面分組｜掃描】沒有掃到任何版面，快取未變更。")
            return
        summary = apply_scan_results_to_disk(
            results,
            full=full,
            scanned_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._reload_state()
        msg = (
            f"【版面分組｜掃描】{'完成' if full else '中止（已保留部分結果）'}："
            f"{len(results)} 個版面已更新"
        )
        if summary["pruned"]:
            msg += f"\n  已自群組移除 {summary['pruned']} 個已刪除的版面"
        if skipped:
            msg += f"\n  {skipped} 個無 URL 已略過"
        self._log(msg)

    def _on_scan_failed(self, cdp_url: str, exc: BaseException) -> None:
        self._log(f"【版面分組｜掃描】失敗：{exc}")
        QMessageBox.warning(
            self,
            "掃描失敗",
            f"無法透過 {cdp_url} 完成版面掃描。\n"
            f"若瀏覽器是剛自動啟動的，請確認 TradingView 已登入後再掃一次。\n\n錯誤：{exc}",
        )

    # ---------- App 模式 ----------
    def _launch_settings(self) -> app_launcher.AppLaunchSettings:
        return app_launcher.AppLaunchSettings.from_settings(self._state.settings)

    def _groups_to_open(self, all_groups: bool) -> list[tuple[str, list[str]]]:
        if all_groups:
            selected = self._state.groups
        else:
            group = self._current_group()
            if group is None:
                QMessageBox.information(self, "未選群組", "請先選取一個群組。")
                return []
            selected = [group]
        result = [(g.name, [l.url for l in g.layouts]) for g in selected if g.layouts]
        if not result:
            QMessageBox.information(self, "沒有版面", "群組內沒有版面可開啟。")
        return result

    def _on_open_app(self, all_groups: bool) -> None:
        if self._busy():
            return
        groups = self._groups_to_open(all_groups)
        if not groups:
            return
        settings = self._launch_settings()
        self._start_thread(
            lambda: app_launcher.open_groups_in_app(groups, settings, self._log),
            lambda _result: self._log("【版面分組｜App】全部群組開啟完成"),
            self._on_open_app_failed,
        )

    def _on_open_app_failed(self, exc: BaseException) -> None:
        self._log(f"【版面分組｜App】失敗：{exc}")
        if isinstance(exc, app_launcher.AppNotInstalledError):
            QMessageBox.warning(
                self, "未安裝", "找不到 /Applications/TradingView.app，請先安裝 TradingView 桌面版。"
            )
        elif isinstance(exc, app_launcher.AppCDPUnavailableError):
            QMessageBox.warning(
                self,
                "TradingView CDP 未就緒",
                f"{exc}\n\n啟動完成後再按一次「開啟群組（App）」。",
            )
        else:
            QMessageBox.warning(self, "App 開啟失敗", f"錯誤：{exc}")

    # ---------- 瀏覽器模式 ----------
    def _on_open_browser(self, all_groups: bool) -> None:
        if self._busy():
            return
        groups = self._groups_to_open(all_groups)
        if not groups:
            return
        cfg = shared_config.load_tradingview_config()
        kind = str(cfg.get("browser") or "chrome")
        browser_path = browser_paths.find_browser(kind)
        if not browser_path:
            QMessageBox.warning(
                self,
                "找不到瀏覽器",
                f"找不到 {'Brave' if kind == 'brave' else 'Chrome'} 可執行檔，"
                "請先在「批次與整理」確認瀏覽器設定。",
            )
            return
        delay_ms = self._launch_settings_delay_browser()
        self._start_thread(
            lambda: self._open_groups_in_browser_coro(kind, browser_path, groups, delay_ms),
            lambda _r: self._log("【版面分組｜瀏覽器】全部群組開啟完成"),
            lambda exc: QMessageBox.warning(self, "瀏覽器開啟失敗", f"錯誤：{exc}"),
        )

    def _launch_settings_delay_browser(self) -> int:
        try:
            return max(0, int(self._state.settings.get("delay_browser_window_ms", 2000)))
        except (TypeError, ValueError):
            return 2000

    async def _open_groups_in_browser_coro(
        self, kind: str, browser_path: str, groups: list[tuple[str, list[str]]], delay_ms: int
    ) -> None:
        for i, (name, urls) in enumerate(groups):
            self._log(
                f"【版面分組｜瀏覽器】開啟群組「{name}」（{len(urls)} 個版面 → 1 視窗）"
            )
            if not browser_paths.cdp_ready():
                # 冷啟：讓這個 instance 兼任 auto-paste 的 9222 CDP 瀏覽器。
                browser_paths.launch_cdp_browser(kind, urls=urls)
                if not await asyncio.to_thread(browser_paths.wait_cdp_ready, 9222, 8.0):
                    self._log(
                        "【版面分組｜瀏覽器】9222 尚未就緒\n"
                        "  原因：瀏覽器可能已有非 CDP instance 在跑，後續群組仍會轉送開啟"
                    )
            else:
                # Warm：argv forwarding 給既有 instance（同 user-data-dir）。
                subprocess.Popen(
                    [
                        browser_path,
                        f"--user-data-dir={browser_paths.cdp_profile_dir()}",
                        "--new-window",
                        *urls,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if i < len(groups) - 1:
                await asyncio.sleep(delay_ms / 1000)
