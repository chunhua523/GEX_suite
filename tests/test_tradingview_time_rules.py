"""Unit checks for TradingView start-time mapping."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gex_suite.shared import config


class TradingViewStartTimeRulesTests(unittest.TestCase):
    def test_default_rules_are_applied(self) -> None:
        self.assertEqual(config.get_tradingview_start_time("VIX"), "03:15")
        self.assertEqual(config.get_tradingview_start_time("SPX"), "09:30")
        self.assertEqual(config.get_tradingview_start_time("AAPL"), "04:00")

    def test_custom_rules_override_defaults(self) -> None:
        cfg = {
            "start_time_rules": {
                "VIX": "03:20",
                "SPX": "09:35",
                "default": "04:10",
            }
        }
        self.assertEqual(config.get_tradingview_start_time("VIX", cfg), "03:20")
        self.assertEqual(config.get_tradingview_start_time("SPX", cfg), "09:35")
        self.assertEqual(config.get_tradingview_start_time("TSLA", cfg), "04:10")

    def test_invalid_rule_falls_back_to_default(self) -> None:
        cfg = {
            "start_time_rules": {
                "VIX": "bad-value",
                "default": "04:30",
            }
        }
        self.assertEqual(config.get_tradingview_start_time("VIX", cfg), "04:30")


class TradingViewTzStartRulesTests(unittest.TestCase):
    """start_time_tz_rules：當地時間定義 → 換算紐約時間。"""

    def test_nk225_sunday_1700_kst(self) -> None:
        # 夏令（EDT, UTC-4）：週日 17:00 KST = 週日 04:00 NY
        self.assertEqual(
            config.get_tradingview_tz_start("NK2251!", dt.date(2026, 7, 13)),
            (dt.date(2026, 7, 12), "04:00"),
        )
        # 冬令（EST, UTC-5）：週日 17:00 KST = 週日 03:00 NY
        self.assertEqual(
            config.get_tradingview_tz_start("NK2251!", dt.date(2026, 1, 12)),
            (dt.date(2026, 1, 11), "03:00"),
        )

    def test_kospi200_monday_0900_kst(self) -> None:
        # 夏令：週一 09:00 KST = 週日 20:00 NY（換日提前一天）
        self.assertEqual(
            config.get_tradingview_tz_start("KOSPI200", dt.date(2026, 7, 13)),
            (dt.date(2026, 7, 12), "20:00"),
        )
        # 冬令：週一 09:00 KST = 週日 19:00 NY
        self.assertEqual(
            config.get_tradingview_tz_start("KOSPI200", dt.date(2026, 1, 12)),
            (dt.date(2026, 1, 11), "19:00"),
        )

    def test_cbot_grains_sunday_2000_ny(self) -> None:
        # 穀物（ZW/ZC/ZS）globex 週日 19:00 CT 開盤＝週日 20:00 NY；
        # 規則以 NY 時間定義 → 換算恆等，冬夏令皆同。
        for ticker in ("ZW1!", "ZC1!", "ZS1!"):
            self.assertEqual(
                config.get_tradingview_tz_start(ticker, dt.date(2026, 7, 13)),
                (dt.date(2026, 7, 12), "20:00"),
            )
            self.assertEqual(
                config.get_tradingview_tz_start(ticker, dt.date(2026, 1, 12)),
                (dt.date(2026, 1, 11), "20:00"),
            )

    def test_unlisted_ticker_returns_none(self) -> None:
        self.assertIsNone(config.get_tradingview_tz_start("ES1!", dt.date(2026, 7, 13)))
        self.assertIsNone(config.get_tradingview_tz_start("", dt.date(2026, 7, 13)))

    def test_custom_rule_and_invalid_rule(self) -> None:
        cfg = {
            "start_time_tz_rules": {
                "NK2251!": {"timezone": "Asia/Tokyo", "day_offset": -1, "time": "16:30"},
                "KOSPI200": {"timezone": "bad/zone", "day_offset": 0, "time": "09:00"},
            }
        }
        self.assertEqual(
            config.get_tradingview_tz_start("NK2251!", dt.date(2026, 7, 13), cfg),
            (dt.date(2026, 7, 12), "03:30"),
        )
        # 無效 timezone → None（fallback 給呼叫端）
        self.assertIsNone(config.get_tradingview_tz_start("KOSPI200", dt.date(2026, 7, 13), cfg))


if __name__ == "__main__":
    unittest.main()
