"""Centralised filesystem locations shared across all modules.

Layout (relative to the repository root):

    GEX_suite/
        gex_suite/
            data/                # default data dir (DB, settings, state, logs)
                stocks.db        # chart database (migrated from GEX_tool)
                scraper/
                    settings.json
                    state.json
                    logs/
                tradingview/
                service_account.json   # optional, gitignored
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../gex_suite
PROJECT_ROOT = PACKAGE_ROOT.parent                      # .../GEX_suite

DATA_DIR = PACKAGE_ROOT / "data"
SCRAPER_DATA_DIR = DATA_DIR / "scraper"
SCRAPER_LOG_DIR = SCRAPER_DATA_DIR / "logs"
CHART_DATA_DIR = DATA_DIR
TRADINGVIEW_DATA_DIR = DATA_DIR / "tradingview"
TRADINGVIEW_LOG_DIR = TRADINGVIEW_DATA_DIR / "logs"

CHART_DB_PATH = DATA_DIR / "stocks.db"
SCRAPER_SETTINGS_PATH = SCRAPER_DATA_DIR / "settings.json"
SCRAPER_STATE_PATH = SCRAPER_DATA_DIR / "state.json"
SCRAPER_LAST_RESULT_PATH = SCRAPER_DATA_DIR / "last_result.json"
SCRAPER_STOP_FLAG_PATH = SCRAPER_DATA_DIR / ".stop_requested"
CHAIN_STATE_PATH = SCRAPER_DATA_DIR / "chain_state.json"
CHAIN_STOP_FLAG_PATH = SCRAPER_DATA_DIR / ".chain_stop_requested"
SCRAPER_TICKERS_DEFAULT = SCRAPER_DATA_DIR / "tickers_index.json"
SCRAPER_CME_TICKERS_DEFAULT = SCRAPER_DATA_DIR / "tickers_index_cme.json"
SUITE_CONFIG_PATH = DATA_DIR / "suite_config.json"
TRADINGVIEW_AUTO_PASTE_CONFIG_PATH = TRADINGVIEW_DATA_DIR / "auto_paste_config.json"

SERVICE_ACCOUNT_PATH = DATA_DIR / "service_account.json"


def ensure_dirs() -> None:
    """Make sure all default directories exist."""
    for p in (
        DATA_DIR,
        SCRAPER_DATA_DIR,
        SCRAPER_LOG_DIR,
        TRADINGVIEW_DATA_DIR,
        TRADINGVIEW_LOG_DIR,
    ):
        p.mkdir(parents=True, exist_ok=True)


def resolve(path_like: str | os.PathLike[str] | None) -> Path | None:
    """Return absolute path or ``None`` if ``path_like`` is empty."""
    if not path_like:
        return None
    return Path(path_like).expanduser().resolve()
