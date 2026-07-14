"""Unit tests for chart parser ticker extraction + CME ``1!`` suffix logic.

Run with: ``python tests/test_chart_parser.py``
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gex_suite.modules.chart import parser as gex_parser
from gex_suite.modules.chart.importers import _is_cme_source_path, _suffix_futures_ticker


def _parse(line: str) -> tuple:
    rows: list[tuple] = []
    ticker = gex_parser.parse_gex_code("2026-07-14", line, lambda t, d, l, v: rows.append((t, d, l, v)))
    return ticker, rows


def test_parse_letter_ticker() -> None:
    ticker, rows = _parse("ES: Put Dominate, 5000.00, Call Wall & Call Wall CE, 5500.00")
    assert ticker == "ES"
    labels = {r[2] for r in rows}
    assert labels == {"TV Code", "Put Dominate", "Call Wall", "Call Wall CE"}


def test_parse_alnum_ticker() -> None:
    ticker, rows = _parse("KOSPI200: Put Dominate, 570.00, Implied Movement -2σ, 860.98")
    assert ticker == "KOSPI200"
    assert ("KOSPI200", "2026-07-14", "Put Dominate", 570.0) in rows
    assert ("KOSPI200", "2026-07-14", "Implied Movement -2σ", 860.98) in rows

    ticker, rows = _parse("NK225: Gamma Flip & Call Dominate, 70125.00")
    assert ticker == "NK225"
    assert ("NK225", "2026-07-14", "Call Dominate", 70125.0) in rows


def test_parse_rejects_no_ticker() -> None:
    ticker, rows = _parse("200: Put Dominate, 570.00")  # 純數字開頭不是 ticker
    assert ticker is None
    assert rows == []


def test_extract_date_alnum_ticker() -> None:
    assert gex_parser.extract_date_from_tv_code("NK225 20260714 xxx").isoformat() == "2026-07-14"
    assert gex_parser.extract_date_from_tv_code("ES 20260714 xxx").isoformat() == "2026-07-14"
    assert gex_parser.extract_date_from_tv_code("no date here") is None


def test_suffix_futures_ticker() -> None:
    assert _suffix_futures_ticker("NK225") == "NK2251!"
    assert _suffix_futures_ticker("ES") == "ES1!"
    assert _suffix_futures_ticker("ES1!") == "ES1!"  # idempotent
    assert _suffix_futures_ticker("") == ""
    # index 商品排除清單：不補 1!
    assert _suffix_futures_ticker("KOSPI200") == "KOSPI200"
    assert _suffix_futures_ticker("kospi200") == "kospi200"


def test_is_cme_source_path() -> None:
    assert _is_cme_source_path("/a/b/CME/TV Code/TV_Codes_20260714_161714.txt")
    assert _is_cme_source_path("C:\\a\\cme\\TV Code\\x.txt")
    assert not _is_cme_source_path("/a/b/TV Code/TV_Codes_20260714_161714.txt")
    assert not _is_cme_source_path("/a/ACME/TV Code/x.txt")  # 只認完整路徑段


def main() -> int:
    failures: list[str] = []
    tests = [
        ("parse_letter_ticker", test_parse_letter_ticker),
        ("parse_alnum_ticker", test_parse_alnum_ticker),
        ("parse_rejects_no_ticker", test_parse_rejects_no_ticker),
        ("extract_date_alnum_ticker", test_extract_date_alnum_ticker),
        ("suffix_futures_ticker", test_suffix_futures_ticker),
        ("is_cme_source_path", test_is_cme_source_path),
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK] {name}")
        except Exception as exc:
            traceback.print_exc()
            failures.append(f"{name}: {exc}")
            print(f"  [FAIL] {name}: {exc}")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
