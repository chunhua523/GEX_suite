"""TXT / Excel / Google Sheet importers for TV codes."""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import pandas as pd

from gex_suite.shared import db
from gex_suite.shared.paths import SERVICE_ACCOUNT_PATH

from . import parser as gex_parser

ALLOWED_COLS = ["TV Code"]


@dataclass
class ImportReport:
    inserted: int = 0
    skipped: int = 0
    overwritten: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total_written(self) -> int:
        return self.inserted + self.overwritten


# ConflictResolver(ticker, date, label) -> 'overwrite' | 'skip' | 'cancel'
ConflictResolver = Callable[[str, str, str], str]


class _Inserter:
    """Insert with conflict policy callback. Tracks counters in an ImportReport."""

    def __init__(self, resolver: Optional[ConflictResolver] = None,
                 default_policy: str = "overwrite") -> None:
        self.resolver = resolver
        self.default_policy = default_policy
        self.report = ImportReport()
        self._global_choice: Optional[str] = None
        self._apply_to_all: bool = False

    def _choose(self, ticker: str, date: str, label: str) -> str:
        if self._apply_to_all and self._global_choice:
            return self._global_choice
        if self.resolver is None:
            return self.default_policy
        choice = self.resolver(ticker, date, label)
        if choice in ("overwrite_all", "skip_all"):
            self._apply_to_all = True
            self._global_choice = choice.replace("_all", "")
            return self._global_choice
        return choice

    def insert(self, ticker: str, date: str, label: str, value) -> None:
        if self.report.cancelled:
            return
        if db.row_exists(ticker, date, label):
            choice = self._choose(ticker, date, label)
            if choice == "cancel":
                self.report.cancelled = True
                return
            if choice == "skip":
                self.report.skipped += 1
                return
            db.upsert(ticker, date, label, value)
            self.report.overwritten += 1
        else:
            if db.insert_only(ticker, date, label, value):
                self.report.inserted += 1


# ---------- TXT (multi-file) ----------

def import_txt_files(file_paths: Iterable[str], resolver: Optional[ConflictResolver] = None) -> ImportReport:
    inserter = _Inserter(resolver)
    for fp in file_paths:
        if inserter.report.cancelled:
            break
        filename = os.path.basename(fp)

        default_date = None
        tv_match = re.search(r"TV_Codes_(\d{8})_\d{6}", filename)
        date_match = tv_match if tv_match else re.search(r"(\d{8})", filename)
        if date_match:
            try:
                default_date = pd.to_datetime(date_match.group(1), format="%Y%m%d").date().isoformat()
            except Exception:
                pass

        current_date = default_date
        try:
            with open(fp, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            for line in lines:
                if inserter.report.cancelled:
                    break
                if ":" in line:
                    use_date = current_date if current_date else _dt.date.today().isoformat()
                    gex_parser.parse_gex_code(use_date, line, inserter.insert)
                else:
                    try:
                        candidate = line.split("_")[0]
                        pd.to_datetime(candidate)
                        current_date = candidate
                    except Exception:
                        pass
        except Exception as exc:
            inserter.report.errors.append(f"{fp}: {exc}")
    return inserter.report


# ---------- Excel ----------

def _import_rows(ticker: str, df: pd.DataFrame, inserter: _Inserter,
                 latest_date: Optional[_dt.date] = None) -> None:
    if "TV Code" not in df.columns:
        return
    for _, row in df.iterrows():
        tv_code = str(row["TV Code"]).strip()
        if not tv_code or tv_code.lower() == "nan":
            continue
        date_obj = gex_parser.extract_date_from_tv_code(tv_code) or gex_parser.parse_date(row.get("Date"))
        if date_obj is None:
            continue
        if latest_date and date_obj < latest_date:
            continue
        gex_parser.parse_gex_code(date_obj.isoformat(), tv_code, inserter.insert)


def import_excel(file_path: str, resolver: Optional[ConflictResolver] = None) -> ImportReport:
    inserter = _Inserter(resolver)
    try:
        xls = pd.ExcelFile(file_path)
        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet)
            _import_rows(sheet.strip(), df, inserter)
    except Exception as exc:
        inserter.report.errors.append(str(exc))
    return inserter.report


# ---------- Google Sheets ----------

def import_google(sheet_ids: list[str], resolver: Optional[ConflictResolver] = None,
                  *, only_latest: bool = False) -> ImportReport:
    """Import all worksheets from given spreadsheet IDs.

    If ``only_latest`` is True, skip rows older than each ticker's latest
    date already in the database.
    """
    inserter = _Inserter(resolver)
    if not SERVICE_ACCOUNT_PATH.exists():
        inserter.report.errors.append("service_account.json not found at " + str(SERVICE_ACCOUNT_PATH))
        return inserter.report
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError as exc:
        inserter.report.errors.append(f"missing google deps: {exc}")
        return inserter.report

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(SERVICE_ACCOUNT_PATH), scope)
    client = gspread.authorize(creds)

    for sid in sheet_ids:
        try:
            spreadsheet = client.open_by_key(sid)
        except Exception as exc:
            inserter.report.errors.append(f"open_by_key({sid}): {exc}")
            continue

        for ws in spreadsheet.worksheets():
            ticker = ws.title.strip()
            try:
                values = ws.get_all_values()
                if not values:
                    continue
                headers = values[0]
                if not headers or all(not h.strip() for h in headers):
                    continue
                df = pd.DataFrame(values[1:], columns=headers)
            except Exception as exc:
                inserter.report.errors.append(f"{ticker}: {exc}")
                continue
            if "TV Code" not in df.columns:
                continue
            latest = db.get_latest_date_for_ticker(ticker) if only_latest else None
            _import_rows(ticker, df, inserter, latest)
    return inserter.report
