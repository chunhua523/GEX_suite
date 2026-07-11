"""Unit tests for tradingview.layout_groups (pure Python, no Qt).

Run with: ``python tests/test_layout_groups.py``
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gex_suite.modules.tradingview import layout_groups as lg


def test_normalize_chart_url() -> None:
    canon = "https://www.tradingview.com/chart/AbCd1234/"
    assert lg.normalize_chart_url("https://www.tradingview.com/chart/AbCd1234/") == canon
    assert lg.normalize_chart_url("https://tw.tradingview.com/chart/AbCd1234") == canon
    assert lg.normalize_chart_url("http://www.tradingview.com/chart/AbCd1234/xyz?x=1") == canon
    assert lg.normalize_chart_url("//www.tradingview.com/chart/AbCd1234/") == canon
    assert lg.normalize_chart_url("/chart/AbCd1234/") == canon
    assert lg.normalize_chart_url("AbCd1234") == canon
    assert lg.normalize_chart_url("  AbCd1234  ") == canon
    assert lg.normalize_chart_url("") is None
    assert lg.normalize_chart_url("https://example.com/chart/AbCd1234/") is None
    assert lg.normalize_chart_url("https://www.tradingview.com/ideas/") is None
    assert lg.normalize_chart_url("ab") is None  # id too short


def test_chart_id_from_url() -> None:
    assert lg.chart_id_from_url("https://www.tradingview.com/chart/XyZ_9876/") == "XyZ_9876"
    assert lg.chart_id_from_url("https://www.tradingview.com/") is None


def test_group_layout_from_dict() -> None:
    gl = lg.GroupLayout.from_dict({"url": "https://tw.tradingview.com/chart/AbCd1234"})
    assert gl is not None
    assert gl.url == "https://www.tradingview.com/chart/AbCd1234/"
    assert gl.layout_id == "AbCd1234"
    assert gl.name == "URL:AbCd1234"
    assert gl.source == "scan"
    assert lg.GroupLayout.from_dict({"url": "not-a-url!"}) is None
    manual = lg.GroupLayout.from_dict(
        {"url": "AbCd1234", "name": "我的版面", "source": "manual"}
    )
    assert manual is not None and manual.source == "manual" and manual.name == "我的版面"


def test_group_dedup() -> None:
    a = lg.GroupLayout(name="A", url="https://www.tradingview.com/chart/AbCd1234/", layout_id="AbCd1234")
    a2 = lg.GroupLayout(name="A 改名", url="https://www.tradingview.com/chart/AbCd1234/", layout_id="AbCd1234")
    b = lg.GroupLayout(name="B", url="https://www.tradingview.com/chart/XyZw9876/", layout_id="XyZw9876")
    group = lg.LayoutGroup(group_id=lg.new_group_id(), name="G", layouts=[a])
    assert group.contains(a2)
    assert not group.contains(b)


def test_state_round_trip(tmp_dir: Path) -> None:
    lg.TRADINGVIEW_LAYOUT_GROUPS_PATH = tmp_dir / "layout_groups.json"

    state = lg.LayoutGroupsState()
    g = lg.LayoutGroup(group_id="g-11223344", name="指數期貨")
    g.layouts.append(
        lg.GroupLayout(name="ES [equity]", url="https://www.tradingview.com/chart/AbCd1234/", layout_id="AbCd1234")
    )
    g.layouts.append(
        lg.GroupLayout(name="手動", url="https://www.tradingview.com/chart/XyZw9876/", layout_id="XyZw9876", source="manual")
    )
    state.groups.append(g)
    state.scanned_layouts.append(
        lg.GroupLayout(name="ES [equity]", url="https://www.tradingview.com/chart/AbCd1234/", layout_id="AbCd1234")
    )
    state.scanned_at = "2026-07-11T09:00:00+08:00"
    state.settings["app_cdp_port"] = 9444
    lg.save_layout_groups(state)

    loaded = lg.load_layout_groups()
    assert [grp.name for grp in loaded.groups] == ["指數期貨"]
    assert loaded.groups[0].group_id == "g-11223344"
    assert [l.url for l in loaded.groups[0].layouts] == [
        "https://www.tradingview.com/chart/AbCd1234/",
        "https://www.tradingview.com/chart/XyZw9876/",
    ]
    assert loaded.groups[0].layouts[1].source == "manual"
    assert loaded.scanned_at == "2026-07-11T09:00:00+08:00"
    assert loaded.settings["app_cdp_port"] == 9444
    # Defaults still merged in.
    assert loaded.settings["delay_per_url_ms"] == 1800

    # Corrupt file → tolerant empty state.
    lg.TRADINGVIEW_LAYOUT_GROUPS_PATH.write_text("{not json", encoding="utf-8")
    broken = lg.load_layout_groups()
    assert broken.groups == [] and broken.scanned_layouts == []

    # Bad entries inside an otherwise valid file are dropped, not fatal.
    lg.TRADINGVIEW_LAYOUT_GROUPS_PATH.write_text(
        json.dumps(
            {
                "groups": [
                    {"id": "g-1", "name": "ok", "layouts": [{"url": "bad!!"}, {"url": "AbCd1234"}]},
                    "not-a-dict",
                ]
            }
        ),
        encoding="utf-8",
    )
    partial = lg.load_layout_groups()
    assert len(partial.groups) == 1
    assert [l.layout_id for l in partial.groups[0].layouts] == ["AbCd1234"]


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        tests = [
            ("normalize_chart_url", test_normalize_chart_url),
            ("chart_id_from_url", test_chart_id_from_url),
            ("group_layout_from_dict", test_group_layout_from_dict),
            ("group_dedup", test_group_dedup),
            ("state_round_trip", lambda: test_state_round_trip(tmp_dir)),
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
