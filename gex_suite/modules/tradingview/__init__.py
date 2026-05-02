"""TradingView Auto-Paste：CDP 連線、批次寫入 GEX 指標、手動複製 TV code。

Standalone usage::

    python -m gex_suite.modules.tradingview
"""
from .widget import TradingViewPage  # noqa: F401
from .automator import (  # noqa: F401
    IndicatorQuotaExceededError,
    NotImplementedAutomator,
    TVAutomator,
)
