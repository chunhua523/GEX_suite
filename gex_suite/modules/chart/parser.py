"""TV-Code parsing helpers (extracted from GEX_chart_new.parse_gex_code)."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Callable, Optional

import pandas as pd

InsertFn = Callable[[str, str, str, object], None]


def extract_date_from_tv_code(tv_code: str) -> Optional[_dt.date]:
    """If TV Code begins with ``TICKER YYYYMMDD ...``, return that date."""
    m = re.match(r"^[A-Za-z\.]+\s+(\d{8})\b", tv_code)
    if m:
        return pd.to_datetime(m.group(1), format="%Y%m%d").date()
    return None


def parse_date(val) -> Optional[_dt.date]:
    ts = pd.to_datetime(val, errors="coerce")
    return ts.date() if pd.notna(ts) else None


def parse_gex_code(orig_date: str, gex_code: str, insert: InsertFn) -> Optional[str]:
    """Parse a TV Code and emit rows via ``insert(ticker, date, label, value)``.

    Mirrors the original logic in ``GEX_chart_new.parse_gex_code``: if the
    TV Code embeds a YYYYMMDD prefix it wins, otherwise the supplied
    ``orig_date`` is used; the raw TV code is also stored under
    ``label='TV Code'``.
    """
    embedded = extract_date_from_tv_code(gex_code)
    date_str = embedded.isoformat() if embedded else (orig_date.split(" ")[0] if orig_date else "")
    if not date_str:
        return None

    m = re.search(r"([A-Za-z\.]+):", gex_code)
    if not m:
        return None
    ticker = m.group(1).upper()

    insert(ticker, date_str, "TV Code", gex_code.strip())

    body = gex_code[m.end():].strip()
    elements = re.split(r",\s*", body)
    i = 0
    while i < len(elements) - 1:
        labels = elements[i].strip()
        try:
            value = float(elements[i + 1].strip())
            for label in labels.split("&"):
                insert(ticker, date_str, label.strip(), value)
            i += 2
        except ValueError:
            i += 1
    return ticker
