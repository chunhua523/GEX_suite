"""Suite-wide settings (theme, default folders).

Per-module settings live in their own files (e.g. scraper's ``settings.json``).
"""
from __future__ import annotations

import json
import re
from typing import Any

from .paths import SUITE_CONFIG_PATH, TRADINGVIEW_AUTO_PASTE_CONFIG_PATH, ensure_dirs

_DEFAULT: dict[str, Any] = {
    "theme": "dark",
    "default_download_folder": None,
    # Optional: 主視窗「說明 → 檢查更新」會讀取 Raw 上的 pyproject.toml 與本機 gex_suite.__version__ 比對
    "update_github_user": "",
    "update_github_repo": "",
    "update_github_branch": "main",
    "update_remote_pyproject_path": "pyproject.toml",
}

_TRADINGVIEW_DEFAULT: dict[str, Any] = {
    "weeks_mode": "this_week",
    "layout_scope": "all",
    "ticker_scope": "all",
    "skip_filled_days": True,
    "apply_visibility_preset": True,
    "organize_indicators": False,
    "browser": "chrome",
    "ticker": "",
    "cdp_url": "http://127.0.0.1:9222",
    # TO FUTURE auto-fill: "yfinance" (default) | "tv_legend" (reserved).
    "futures_quote_source": "yfinance",
    "start_time_rules": {
        "VIX": "03:15",
        "SPX": "09:30",
        "default": "04:00",
    },
}


def load_config() -> dict[str, Any]:
    ensure_dirs()
    if not SUITE_CONFIG_PATH.exists():
        return dict(_DEFAULT)
    try:
        with SUITE_CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_DEFAULT)
        merged.update(data or {})
        return merged
    except Exception:
        return dict(_DEFAULT)


def save_config(cfg: dict[str, Any]) -> None:
    ensure_dirs()
    with SUITE_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_tradingview_config() -> dict[str, Any]:
    ensure_dirs()
    if not TRADINGVIEW_AUTO_PASTE_CONFIG_PATH.exists():
        return dict(_TRADINGVIEW_DEFAULT)
    try:
        with TRADINGVIEW_AUTO_PASTE_CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_TRADINGVIEW_DEFAULT)
        merged.update(data or {})
        return merged
    except Exception:
        return dict(_TRADINGVIEW_DEFAULT)


def save_tradingview_config(cfg: dict[str, Any]) -> None:
    ensure_dirs()
    merged = dict(_TRADINGVIEW_DEFAULT)
    merged.update(cfg or {})
    with TRADINGVIEW_AUTO_PASTE_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)


def get_tradingview_start_time(ticker: str, cfg: dict[str, Any] | None = None) -> str:
    """Resolve start time by ticker from TradingView settings."""
    merged_cfg = cfg if cfg is not None else load_tradingview_config()
    rules = merged_cfg.get("start_time_rules", {})
    if not isinstance(rules, dict):
        rules = {}

    token = str(ticker or "").strip().upper()
    default_value = _normalize_hhmm(rules.get("default")) or "04:00"
    if not token:
        return default_value
    specific = _normalize_hhmm(rules.get(token))
    return specific or default_value


def _normalize_hhmm(raw: Any) -> str | None:
    val = str(raw or "").strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", val):
        return val
    return None
