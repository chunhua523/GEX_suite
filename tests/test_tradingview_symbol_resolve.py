"""Subchart symbol → DB ticker 解析（symbol_resolve.py）的路由測試。

重點：「1!」尾碼隱含 alias（2026-10-19 YM1! 案例）、明確表優先、equity／index 缺表要
明確略過而不是拿掉 1! 找裸名、scope=ticker 接受 YM1!／YM1／YM 三種寫法、gex-chain 的
候選 ticker 與 widget 轉呼叫同一份實作。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # package __init__ 會載入 widget（Qt）

from gex_suite.modules.tradingview import symbol_resolve as sr  # noqa: E402


class ContinuousFuturesPatternTests(unittest.TestCase):
    def test_pattern_matches_tv_continuous_symbols(self) -> None:
        for tail in ("ES1!", "NQ2!", "NK2251!", "K2I1!", "YM1!", "6E1!"):
            self.assertIsNotNone(sr.CONTINUOUS_FUTURES_RE.match(tail), tail)

    def test_pattern_rejects_non_futures(self) -> None:
        for tail in ("SPX", "AAPL", "IX0001", "BRK.B", "ES!", "KOSPI200", "TXO", ""):
            self.assertIsNone(sr.CONTINUOUS_FUTURES_RE.match(tail), tail)


class AliasLookupTests(unittest.TestCase):
    def test_explicit_entry_wins_over_implicit(self) -> None:
        entry, source = sr.alias_lookup("CME_MINI:MNQ1!")
        self.assertEqual(source, "explicit")
        self.assertEqual(entry["futures"], "NQ1!")
        entry, source = sr.alias_lookup("KRX:K2I1!")
        self.assertEqual(source, "explicit")
        self.assertEqual(entry["futures"], "KOSPI200")

    def test_implicit_entry_for_unlisted_continuous_futures(self) -> None:
        entry, source = sr.alias_lookup("CBOT:ZM1!")
        self.assertEqual(source, "implicit")
        self.assertEqual(entry, {"futures": "ZM1!", "equity": None, "index": None})

    def test_no_entry_for_plain_symbols(self) -> None:
        self.assertEqual(sr.alias_lookup("NASDAQ:AAPL"), (None, None))
        self.assertEqual(sr.alias_lookup(""), (None, None))
        self.assertEqual(sr.alias_lookup(None), (None, None))

    def test_ym1_is_now_explicit_with_dia(self) -> None:
        entry, source = sr.alias_lookup("CBOT_MINI:YM1!")
        self.assertEqual(source, "explicit")
        self.assertEqual(entry, {"futures": "YM1!", "equity": "DIA", "index": None})


class ResolveTargetTickerTests(unittest.TestCase):
    def test_implicit_futures_mode_uses_tail_as_db_ticker(self) -> None:
        self.assertEqual(
            sr.resolve_target_ticker("CBOT:ZM1!", layout_mode="futures"), ("ZM1!", True)
        )

    def test_implicit_equity_index_mode_strict_skip_not_bare_root(self) -> None:
        # 不准拿掉 1! 去找 ZM：裸名不是沒有就是別的商品（ZS＝Zscaler）
        self.assertEqual(sr.resolve_target_ticker("CBOT:ZM1!", layout_mode="equity"), (None, False))
        self.assertEqual(sr.resolve_target_ticker("CBOT:ZM1!", layout_mode="index"), (None, False))

    def test_explicit_entries_unchanged(self) -> None:
        self.assertEqual(sr.resolve_target_ticker("CME_MINI:ES1!", layout_mode="equity"), ("SPY", True))
        self.assertEqual(sr.resolve_target_ticker("CME_MINI:ES1!", layout_mode="index"), ("SPX", True))
        self.assertEqual(sr.resolve_target_ticker("CME_MINI:MNQ1!", layout_mode="futures"), ("NQ1!", True))
        self.assertEqual(sr.resolve_target_ticker("KRX:K2I1!", layout_mode="futures"), ("KOSPI200", True))
        self.assertEqual(sr.resolve_target_ticker("CME_MINI:RTY1!", layout_mode="index"), ("RUT", True))
        self.assertEqual(sr.resolve_target_ticker("COMEX:GC1!", layout_mode="index"), (None, False))

    def test_ym1_all_modes(self) -> None:
        self.assertEqual(sr.resolve_target_ticker("CBOT_MINI:YM1!", layout_mode="futures"), ("YM1!", True))
        self.assertEqual(sr.resolve_target_ticker("CBOT_MINI:YM1!", layout_mode="equity"), ("DIA", True))
        self.assertEqual(sr.resolve_target_ticker("CBOT_MINI:YM1!", layout_mode="index"), (None, False))

    def test_plain_symbols_extract_ticker_without_futures_flag(self) -> None:
        self.assertEqual(sr.resolve_target_ticker("NASDAQ:AAPL", layout_mode="equity"), ("AAPL", False))
        self.assertEqual(sr.resolve_target_ticker("SP:SPX", layout_mode="futures"), ("SPX", False))

    def test_ticker_scope_accepts_suffix_variants(self) -> None:
        for typed in ("YM1!", "YM1", "YM", "ym"):
            self.assertEqual(
                sr.resolve_target_ticker(
                    "CBOT_MINI:YM1!", layout_mode="futures", ticker_scope="ticker", ticker=typed
                ),
                ("YM1!", True),
                typed,
            )
        self.assertEqual(
            sr.resolve_target_ticker(
                "CBOT:ZM1!", layout_mode="futures", ticker_scope="ticker", ticker="ZM"
            ),
            ("ZM1!", True),
        )

    def test_ticker_scope_mismatch_on_alias_does_not_fall_through(self) -> None:
        self.assertEqual(
            sr.resolve_target_ticker(
                "CBOT_MINI:YM1!", layout_mode="futures", ticker_scope="ticker", ticker="ES"
            ),
            (None, False),
        )
        self.assertEqual(
            sr.resolve_target_ticker(
                "CBOT_MINI:YM1!", layout_mode="equity", ticker_scope="ticker", ticker="YM"
            ),
            (None, False),
        )

    def test_ticker_scope_plain_symbol_exact_token_only(self) -> None:
        self.assertEqual(
            sr.resolve_target_ticker("NASDAQ:SOXX", layout_mode="equity", ticker_scope="ticker", ticker="SOX"),
            (None, False),
        )
        self.assertEqual(
            sr.resolve_target_ticker("NASDAQ:SOXX", layout_mode="equity", ticker_scope="ticker", ticker="SOXX"),
            ("SOXX", False),
        )


class ChainCandidateTests(unittest.TestCase):
    def test_candidates_cover_all_modes(self) -> None:
        self.assertEqual(sr.candidate_db_tickers("CBOT_MINI:YM1!"), {"YM1!", "DIA"})
        self.assertEqual(sr.candidate_db_tickers("CBOT:ZM1!"), {"ZM1!"})
        self.assertEqual(sr.candidate_db_tickers("NASDAQ:AAPL"), {"AAPL"})

    def test_formula_and_empty_give_nothing(self) -> None:
        self.assertEqual(sr.candidate_db_tickers("ES1!-SPX500"), set())
        self.assertEqual(sr.candidate_db_tickers(""), set())
        self.assertEqual(sr.candidate_db_tickers(None), set())


class WidgetDelegationTests(unittest.TestCase):
    """widget.py 的 TradingViewPage 只轉呼叫 symbol_resolve（GUI／CLI 同一份實作）。"""

    def test_widget_static_wrappers_delegate(self) -> None:
        from gex_suite.modules.tradingview.widget import TradingViewPage, _FUTURES_ALIAS_MAP

        self.assertIs(_FUTURES_ALIAS_MAP, sr.FUTURES_ALIAS_MAP)
        self.assertEqual(
            TradingViewPage._futures_alias_lookup("CBOT:ZM1!"),
            {"futures": "ZM1!", "equity": None, "index": None},
        )
        self.assertEqual(TradingViewPage._futures_alias_for_symbol("CBOT:ZM1!", "futures"), "ZM1!")
        self.assertEqual(TradingViewPage._extract_ticker_from_symbol("CBOT_MINI:YM1!"), "YM1")
        self.assertTrue(TradingViewPage._is_formula_symbol("ES1!-SPX500"))
        self.assertFalse(TradingViewPage._symbol_matches_ticker("NASDAQ:SOXX", "SOX"))


if __name__ == "__main__":
    unittest.main()
