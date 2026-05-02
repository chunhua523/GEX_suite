"""Shared SQLite layer for the chart database (stocks.db).

This is a near-verbatim extraction of the schema and helpers used by
``GEX_tool/GEX_chart_new.py`` so the new chart module and the future
TradingView module can both read/write the same database.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path
from typing import Iterator

import pandas as pd

from .paths import CHART_DB_PATH, ensure_dirs


def _get_db_path() -> Path:
    ensure_dirs()
    return CHART_DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a SQLite connection."""
    conn = sqlite3.connect(_get_db_path())
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                date   TEXT NOT NULL,
                label  TEXT NOT NULL,
                value  REAL NOT NULL
            )
            """
        )
        conn.commit()


def get_all_tickers() -> list[str]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM stock_data ORDER BY ticker")
        return [r[0] for r in cur.fetchall()]


def get_latest_date_for_ticker(ticker: str):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(date) FROM stock_data WHERE ticker=?", (ticker,))
        row = cur.fetchone()
    if row and row[0]:
        return pd.to_datetime(row[0]).date()
    return None


def fetch_rows(
    filter_ticker: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[tuple]:
    query = "SELECT id, ticker, date, label, value FROM stock_data WHERE 1=1"
    params: list = []
    if filter_ticker:
        query += " AND ticker = ?"
        params.append(filter_ticker)
    if start_date and end_date:
        query += " AND date BETWEEN ? AND ?"
        params.extend([start_date, end_date])
    query += " ORDER BY date DESC"
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()


def fetch_historical_ohlc(ticker: str) -> pd.DataFrame:
    with connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT date, label, value FROM stock_data
            WHERE ticker = ? AND label IN ('Open','High','Low','Close')
            """,
            conn,
            params=(ticker,),
        )
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="label", values="value").sort_index()


def fetch_tv_codes(ticker: str | None = None) -> list[tuple[str, str, str]]:
    """Return ``(ticker, date, code)`` rows for ``label == 'TV Code'``.

    If ``ticker`` is None, returns all rows ordered by ticker, date desc.
    """
    query = (
        "SELECT ticker, date, value FROM stock_data WHERE label = 'TV Code'"
    )
    params: list = []
    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)
    query += " ORDER BY ticker ASC, date DESC"
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()


def fetch_tv_codes_for_week(ticker: str, monday: _date) -> dict[str, str | None]:
    """Return Monday~Friday TV codes for a ticker/week (missing => None)."""
    day_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    day_to_date = {
        day_names[idx]: (monday + _timedelta(days=idx)).isoformat()
        for idx in range(5)
    }

    placeholders = ",".join("?" for _ in range(5))
    sql = (
        f"SELECT date, value FROM stock_data "
        f"WHERE ticker = ? AND label = 'TV Code' AND date IN ({placeholders})"
    )
    params = [ticker, *day_to_date.values()]
    found_by_date: dict[str, str] = {}

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        for row_date, row_value in cur.fetchall():
            if row_date not in found_by_date:
                found_by_date[str(row_date)] = str(row_value)

    return {
        day: found_by_date.get(day_to_date[day])
        for day in day_names
    }


def delete_row(ticker: str, date: str, label: str, value) -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM stock_data WHERE ticker=? AND date=? AND label=? AND value=?",
            (ticker, date, label, value),
        )
        conn.commit()


def delete_ohlc(ticker: str, date_str: str) -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM stock_data WHERE ticker=? AND date=? AND label IN ('Open','High','Low','Close')",
            (ticker, date_str),
        )
        conn.commit()


def upsert(ticker: str, date: str, label: str, value) -> bool:
    """Insert or update a single row.

    Returns ``True`` if a row was actually written (insert or update).
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM stock_data WHERE ticker=? AND date=? AND label=?",
            (ticker, date, label),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE stock_data SET value=? WHERE id=?",
                (value, existing[0]),
            )
        else:
            cur.execute(
                "INSERT INTO stock_data (ticker, date, label, value) VALUES (?,?,?,?)",
                (ticker, date, label, value),
            )
        conn.commit()
    return True


def insert_only(ticker: str, date: str, label: str, value) -> bool:
    """Insert iff the row doesn't exist; return ``True`` on actual insert."""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM stock_data WHERE ticker=? AND date=? AND label=?",
            (ticker, date, label),
        )
        if cur.fetchone():
            return False
        cur.execute(
            "INSERT INTO stock_data (ticker, date, label, value) VALUES (?,?,?,?)",
            (ticker, date, label, value),
        )
        conn.commit()
    return True


def row_exists(ticker: str, date: str, label: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM stock_data WHERE ticker=? AND date=? AND label=? LIMIT 1",
            (ticker, date, label),
        )
        return cur.fetchone() is not None
