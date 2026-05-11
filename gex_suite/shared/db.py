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


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # journal_mode=WAL is persistent (DB-file level); re-applying is a no-op.
    # synchronous=NORMAL is per-connection and must be set every open.
    # Both together cut fsync cost dramatically for bulk writes on Windows.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a SQLite connection."""
    conn = sqlite3.connect(_get_db_path())
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


def open_batch() -> sqlite3.Connection:
    """Open a long-lived connection for bulk writes.

    Caller owns commit + close. Used by importers so a whole batch becomes
    one transaction instead of one-fsync-per-row.
    """
    conn = sqlite3.connect(_get_db_path())
    _apply_pragmas(conn)
    return conn


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
        # The (ticker, date, label) lookup runs on every insert/update via
        # row_exists / insert_only / upsert / insert_or_existing. Without
        # an index this is a full table scan — O(N²) for bulk imports.
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_data_key "
            "ON stock_data(ticker, date, label)"
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


def _upsert_on(conn: sqlite3.Connection, ticker: str, date: str, label: str, value) -> None:
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


def upsert(ticker: str, date: str, label: str, value, *, conn: sqlite3.Connection | None = None) -> bool:
    """Insert or update a single row.

    Pass ``conn`` to skip auto-commit (caller batches multiple writes into
    one transaction). When ``conn=None`` opens its own connection and
    commits per call — backwards-compatible single-write path.
    """
    if conn is not None:
        _upsert_on(conn, ticker, date, label, value)
        return True
    with connect() as c:
        _upsert_on(c, ticker, date, label, value)
        c.commit()
    return True


def _insert_only_on(conn: sqlite3.Connection, ticker: str, date: str, label: str, value) -> bool:
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
    return True


def insert_only(ticker: str, date: str, label: str, value, *, conn: sqlite3.Connection | None = None) -> bool:
    """Insert iff the row doesn't exist; return ``True`` on actual insert.

    See :func:`upsert` for ``conn`` semantics.
    """
    if conn is not None:
        return _insert_only_on(conn, ticker, date, label, value)
    with connect() as c:
        wrote = _insert_only_on(c, ticker, date, label, value)
        if wrote:
            c.commit()
        return wrote


def _insert_or_existing_on(conn: sqlite3.Connection, ticker: str, date: str, label: str, value) -> bool:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO stock_data (ticker, date, label, value) VALUES (?,?,?,?)",
        (ticker, date, label, value),
    )
    return cur.rowcount > 0


def insert_or_existing(ticker: str, date: str, label: str, value,
                       *, conn: sqlite3.Connection | None = None) -> bool:
    """Atomic single-statement insert.

    Returns ``True`` if the row was newly inserted, ``False`` if the
    ``(ticker, date, label)`` key already existed. Relies on the
    ``idx_stock_data_key`` UNIQUE index. Pair with :func:`update_value`
    for the overwrite branch.
    """
    if conn is not None:
        return _insert_or_existing_on(conn, ticker, date, label, value)
    with connect() as c:
        wrote = _insert_or_existing_on(c, ticker, date, label, value)
        if wrote:
            c.commit()
        return wrote


def _update_value_on(conn: sqlite3.Connection, ticker: str, date: str, label: str, value) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE stock_data SET value=? WHERE ticker=? AND date=? AND label=?",
        (value, ticker, date, label),
    )


def update_value(ticker: str, date: str, label: str, value,
                 *, conn: sqlite3.Connection | None = None) -> None:
    """Update an existing row's value. No-op if the row doesn't exist."""
    if conn is not None:
        _update_value_on(conn, ticker, date, label, value)
        return
    with connect() as c:
        _update_value_on(c, ticker, date, label, value)
        c.commit()


def _row_exists_on(conn: sqlite3.Connection, ticker: str, date: str, label: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM stock_data WHERE ticker=? AND date=? AND label=? LIMIT 1",
        (ticker, date, label),
    )
    return cur.fetchone() is not None


def row_exists(ticker: str, date: str, label: str, *, conn: sqlite3.Connection | None = None) -> bool:
    if conn is not None:
        return _row_exists_on(conn, ticker, date, label)
    with connect() as c:
        return _row_exists_on(c, ticker, date, label)
