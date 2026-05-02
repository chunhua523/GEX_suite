"""Unit checks for TradingView indicator row title matching guards."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


class TradingViewIndicatorMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.automator = PlaywrightCDPAutomator()

    def test_weekly_gex_keyword_requires_ordered_phrase(self) -> None:
        self.assertTrue(
            self.automator._indicator_title_matches_keyword(
                "Daily & Weekly GEX by daniel56_trade",
                "Daily & Weekly GEX",
            )
        )
        self.assertTrue(
            self.automator._indicator_title_matches_keyword(
                "My Daily / Weekly GEX Variant",
                "Daily & Weekly GEX",
            )
        )
        self.assertFalse(
            self.automator._indicator_title_matches_keyword(
                "Weekly GEX before Daily",
                "Daily & Weekly GEX",
            )
        )
        self.assertFalse(
            self.automator._indicator_title_matches_keyword(
                "Daily Weekly Levels",
                "Daily & Weekly GEX",
            )
        )

    def test_generic_keyword_uses_ordered_tokens(self) -> None:
        self.assertTrue(
            self.automator._indicator_title_matches_keyword(
                "alpha signal beta",
                "alpha beta",
            )
        )
        self.assertFalse(
            self.automator._indicator_title_matches_keyword(
                "beta signal alpha",
                "alpha beta",
            )
        )

    def test_normalize_start_date_text(self) -> None:
        self.assertEqual(
            self.automator._normalize_start_date_text("2026-4-6"),
            "2026-04-06",
        )
        self.assertEqual(
            self.automator._normalize_start_date_text("2026/04/06 04:00"),
            "2026-04-06",
        )
        self.assertEqual(
            self.automator._normalize_start_date_text("Start: 2026.04.06"),
            "2026-04-06",
        )
        self.assertIsNone(self.automator._normalize_start_date_text("n/a"))


if __name__ == "__main__":
    unittest.main()
