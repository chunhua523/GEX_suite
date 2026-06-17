"""GEX Chart import CLI — headless TV code → SQLite.

Default behaviour: read scraper settings to discover the download folder,
scan ``<download_folder>/TV Code/`` and ``<download_folder>/CME/TV Code/``
for ``TV_Codes_YYYYMMDD_HHMMSS.txt`` files, import the latest one from each
into ``stocks.db``.

Examples::

    python -m gex_suite.modules.chart.cli
    python -m gex_suite.modules.chart.cli --all
    python -m gex_suite.modules.chart.cli --files /path/a.txt /path/b.txt
    python -m gex_suite.modules.chart.cli --input-dir /path/TV\\ Code/ --force-source std
    python -m gex_suite.modules.chart.cli --on-conflict overwrite --result-json /tmp/r.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

from gex_suite.shared import db
from gex_suite.shared.paths import CHART_DB_PATH, SCRAPER_SETTINGS_PATH

from .importers import ImportReport, import_txt_files
from .ohlc import update_ohlc_for_date


TV_CODE_FILENAME_RE = re.compile(r"^TV_Codes_(\d{8})_(\d{6})\.txt$")


def _load_scraper_download_folder() -> str | None:
    if not SCRAPER_SETTINGS_PATH.exists():
        return None
    try:
        return (json.loads(SCRAPER_SETTINGS_PATH.read_text(encoding="utf-8"))
                .get("download_folder") or None)
    except Exception:
        return None


def _scan_tv_code_files(input_dir: Path, *, latest_only: bool) -> list[Path]:
    """Return TV_Codes_*.txt files sorted by filename timestamp (newest last)."""
    if not input_dir.is_dir():
        return []
    matches = [
        p for p in input_dir.iterdir()
        if p.is_file() and TV_CODE_FILENAME_RE.match(p.name)
    ]
    matches.sort(key=lambda p: p.name)
    if latest_only and matches:
        return [matches[-1]]
    return matches


def _build_resolver(policy: str):
    """Return a ConflictResolver that always returns ``policy``.

    The importer's INSERT-OR-IGNORE path means the resolver is only consulted
    on UNIQUE-constraint conflicts; an "always overwrite" or "always skip"
    answer is enough for headless mode.
    """
    valid = {"overwrite", "skip", "cancel"}
    if policy not in valid:
        raise ValueError(f"--on-conflict must be one of {valid}; got {policy!r}")

    def _resolver(_ticker: str, _date: str, _label: str) -> str:
        return policy

    return _resolver


def _collect_files(args: argparse.Namespace) -> tuple[list[Path], list[str]]:
    """Return (files_to_import, errors). Errors are diagnostic strings."""
    errors: list[str] = []

    if args.files:
        files = [Path(f).expanduser().resolve() for f in args.files]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            errors.append(f"--files paths not found: {missing}")
        return [f for f in files if f.is_file()], errors

    if args.input_dir:
        directories = [Path(d).expanduser().resolve() for d in args.input_dir]
    else:
        download_folder = _load_scraper_download_folder()
        if not download_folder:
            errors.append(
                "scraper settings.json missing 'download_folder'; "
                "pass --input-dir or --files"
            )
            return [], errors
        base = Path(download_folder).expanduser()
        directories = [base / "TV Code", base / "CME" / "TV Code"]

    files: list[Path] = []
    for d in directories:
        found = _scan_tv_code_files(d, latest_only=not args.all)
        if not found:
            errors.append(f"no TV_Codes_*.txt under {d}")
        files.extend(found)
    return files, errors


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GEX Chart TV-code import CLI")
    p.add_argument("--input-dir", action="append", default=[],
                   help="Directory to scan for TV_Codes_*.txt (repeatable). "
                        "Default: scraper download_folder/TV Code and CME/TV Code.")
    p.add_argument("--files", nargs="+", default=[],
                   help="Explicit TV code files to import (overrides --input-dir).")
    p.add_argument("--all", action="store_true",
                   help="Import every matching file in each directory "
                        "(default: only the latest by filename timestamp).")
    p.add_argument("--force-source", choices=["cme", "std"], default=None,
                   help="Override path-based CME detection.")
    p.add_argument("--on-conflict", choices=["overwrite", "skip", "cancel"],
                   default="skip", help="UNIQUE-conflict resolution policy.")
    p.add_argument("--update-ohlc", default="skip",
                   help="After import, refresh OHLC for ALL tickers in DB for the given date "
                        "(YYYY-MM-DD, 'today', or 'skip'). Default: skip.")
    p.add_argument("--mirror-db", default="",
                   help="After all writes, atomically copy stocks.db to this path "
                        "(uses SQLite backup API; safe for Google Drive targets).")
    p.add_argument("--result-json", default="", help="Write ImportReport as JSON.")
    p.add_argument("--dry-run", action="store_true",
                   help="List files that would be imported, then exit without writing.")
    return p.parse_args()


def _parse_ohlc_date(spec: str) -> _dt.date | None:
    spec = (spec or "").strip().lower()
    if spec in ("", "skip", "off", "none", "false", "0"):
        return None
    if spec in ("today", "now"):
        return _dt.date.today()
    try:
        return _dt.datetime.strptime(spec, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--update-ohlc must be YYYY-MM-DD, 'today', or 'skip'; got {spec!r}")


def _mirror_db(target: str) -> dict[str, object]:
    """Atomic SQLite backup to ``target``. Creates parent dirs as needed."""
    target_path = Path(target).expanduser()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(CHART_DB_PATH))
    try:
        # Use a temp sibling file then atomically rename — avoids partial-state
        # reads if a Drive sync runs while we write.
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, target_path)
    finally:
        src.close()
    return {
        "ok": True,
        "target": str(target_path),
        "size_bytes": target_path.stat().st_size,
    }


def _write_result(path: str, report: ImportReport, *, dry_run: bool,
                  files: list[Path], extra_errors: list[str],
                  ohlc: dict[str, object] | None = None,
                  mirror: dict[str, object] | None = None) -> None:
    payload = asdict(report)
    payload["total_written"] = report.total_written
    payload["dry_run"] = dry_run
    payload["files"] = [str(f) for f in files]
    payload["errors"] = list(report.errors) + extra_errors
    if ohlc is not None:
        payload["ohlc"] = ohlc
    if mirror is not None:
        payload["mirror"] = mirror
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    ohlc_date = _parse_ohlc_date(args.update_ohlc)
    files, scan_errors = _collect_files(args)

    print(f"📋 found {len(files)} TV code file(s) to import")
    for f in files:
        print(f"   - {f}")
    for err in scan_errors:
        print(f"   ⚠️  {err}")

    if args.dry_run:
        print("ℹ️  dry-run: not importing")
        if ohlc_date:
            print(f"   would update OHLC for {ohlc_date.isoformat()} after import")
        if args.mirror_db:
            print(f"   would mirror DB to {args.mirror_db}")
        if args.result_json:
            _write_result(
                args.result_json,
                ImportReport(),
                dry_run=True,
                files=files,
                extra_errors=scan_errors,
            )
        return 0

    db.init_db()

    if files:
        resolver = _build_resolver(args.on_conflict)
        report = import_txt_files(
            [str(f) for f in files],
            resolver=resolver,
            force_source=args.force_source,
        )
    else:
        report = ImportReport()
        scan_errors = scan_errors or ["no_files_found"]
        print("⚠️  no TV code files — skipping import (OHLC/mirror still run if requested)")

    if files:
        print(
            f"✅ inserted={report.inserted} overwritten={report.overwritten} "
            f"skipped={report.skipped} errors={len(report.errors)}"
            + (" cancelled" if report.cancelled else "")
        )
    for err in report.errors:
        print(f"   ❌ {err}")

    ohlc_result: dict[str, object] | None = None
    if ohlc_date and not report.cancelled:
        ohlc_start = time.monotonic()
        try:
            updated = update_ohlc_for_date(ohlc_date)
            elapsed = time.monotonic() - ohlc_start
            ohlc_result = {
                "ok": True,
                "date": ohlc_date.isoformat(),
                "tickers_updated": updated,
                "elapsed_seconds": round(elapsed, 2),
            }
            print(f"📈 OHLC {ohlc_date.isoformat()}: {updated} tickers updated in {elapsed:.1f}s")
        except Exception as exc:
            ohlc_result = {
                "ok": False,
                "date": ohlc_date.isoformat(),
                "error": str(exc),
            }
            print(f"❌ OHLC update failed: {exc}")

    mirror_result: dict[str, object] | None = None
    if args.mirror_db and not report.cancelled:
        try:
            mirror_result = _mirror_db(args.mirror_db)
            print(f"🪞 DB mirrored → {mirror_result['target']} ({mirror_result['size_bytes']} bytes)")
        except Exception as exc:
            mirror_result = {"ok": False, "target": args.mirror_db, "error": str(exc)}
            print(f"❌ DB mirror failed: {exc}")

    if args.result_json:
        _write_result(
            args.result_json,
            report,
            dry_run=False,
            files=files,
            extra_errors=scan_errors,
            ohlc=ohlc_result,
            mirror=mirror_result,
        )

    # Exit 1 if importer reported errors, OHLC failed, mirror failed, or nothing was found.
    has_failure = bool(report.errors) or report.cancelled
    if ohlc_result and not ohlc_result.get("ok"):
        has_failure = True
    if mirror_result and not mirror_result.get("ok"):
        has_failure = True
    if not files and not (ohlc_result or mirror_result):
        has_failure = True
    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
