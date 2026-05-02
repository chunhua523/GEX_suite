"""yfinance-backed OHLC updater for the shared stocks.db."""
from __future__ import annotations

import datetime as _dt
from typing import Optional

import pandas as pd
import yfinance as yf

from gex_suite.shared import db


def _yf_name(ticker: str) -> str:
    if ticker in ("SPX", "NDX", "VIX"):
        return f"^{ticker}"
    return ticker.replace(".", "-")


def update_ohlc_for_date(date: _dt.date) -> int:
    """Refresh OHLC for *all* tickers in DB for a single trading date.

    Returns the count of tickers that were updated.
    """
    tickers = db.get_all_tickers()
    if not tickers:
        return 0

    ticker_map = {t: _yf_name(t) for t in tickers}
    yf_tickers = list(ticker_map.values())
    next_day = date + pd.Timedelta(days=1)

    df = yf.download(
        tickers=yf_tickers,
        start=date,
        end=next_day,
        interval="1d",
        group_by="ticker",
        progress=False,
        auto_adjust=False,
    )

    count = 0
    for t, name in ticker_map.items():
        if len(yf_tickers) > 1 and isinstance(df.columns, pd.MultiIndex):
            try:
                data = df[name]
            except KeyError:
                continue
        else:
            data = df
        if data.empty:
            continue
        try:
            row = data.iloc[0]
            ohlc = {
                "Open": float(row["Open"]),
                "High": float(row["High"]),
                "Low": float(row["Low"]),
                "Close": float(row["Close"]),
            }
        except (IndexError, KeyError, ValueError):
            continue
        db.delete_ohlc(t, str(date))
        for label, value in ohlc.items():
            db.upsert(t, str(date), label, value)
        count += 1
    return count


def update_ohlc_range(ticker: str, start: str, end: str) -> int:
    """Update OHLC for a single ticker across a date range. Returns row count."""
    yf_ticker = _yf_name(ticker)
    df = yf.download(
        tickers=yf_ticker,
        start=start,
        end=pd.to_datetime(end) + pd.Timedelta(days=1),
        interval="1d",
        group_by="ticker",
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        return 0
    data = df if not isinstance(df.columns, pd.MultiIndex) else df[yf_ticker]
    updated = 0
    for idx, row in data.iterrows():
        date_str = idx.date().isoformat()
        ohlc = {
            "Open": float(row["Open"]),
            "High": float(row["High"]),
            "Low": float(row["Low"]),
            "Close": float(row["Close"]),
        }
        db.delete_ohlc(ticker, date_str)
        for label, value in ohlc.items():
            db.upsert(ticker, date_str, label, value)
        updated += 1
    return updated
