"""Qt helper: run an asyncio coroutine off the GUI thread."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget


class AsyncCoroThread(QThread):
    """Runs a coroutine factory on a dedicated event loop and emits its result.

    Use this for any TradingView async work triggered from the UI thread —
    calling ``asyncio.run`` directly on the GUI thread would freeze the window.
    """

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, parent: QWidget, coro_factory) -> None:
        super().__init__(parent)
        self._coro_factory = coro_factory

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._coro_factory())
            self.succeeded.emit(result)
        except BaseException as exc:  # noqa: BLE001
            self.failed.emit(exc)
        finally:
            loop.close()
            asyncio.set_event_loop(None)
