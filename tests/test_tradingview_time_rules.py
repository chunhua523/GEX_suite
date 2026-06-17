"""Unit checks for TradingView start-time mapping."""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
