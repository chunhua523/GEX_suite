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


def test_display_label_and_filter() -> None:
    gl = lg.GroupLayout(
        name="ES [equity]",
        url="https://www.tradingview.com/chart/AbCd1234/",
        layout_id="AbCd1234",
        subcharts=["ES1!", "VIX"],
    )
    assert gl.display_label() == "ES [equity]（ES1!, VIX）"
    assert gl.matches_filter("vix")
    assert gl.matches_filter("es [eq")
    assert not gl.matches_filter("QQQ")
    bare = lg.GroupLayout(name="無子圖", url="https://www.tradingview.com/chart/XyZw9876/")
    assert bare.display_label() == "無子圖"
    assert bare.matches_filter("")


def _gl(lid: str, name: str, subs: list[str] | None = None, source: str = "scan") -> lg.GroupLayout:
    return lg.GroupLayout(
        name=name,
        url=f"https://www.tradingview.com/chart/{lid}/",
        layout_id=lid,
        source=source,
        subcharts=list(subs or []),
    )


def test_apply_scan_results_full_prunes_and_updates() -> None:
    state = lg.LayoutGroupsState()
    state.scanned_layouts = [_gl("AAA11111", "舊名", ["ES1!"]), _gl("BBB22222", "會被刪", ["NQ1!"])]
    group = lg.LayoutGroup(group_id="g-1", name="G")
    group.layouts = [
        _gl("AAA11111", "舊名", ["ES1!"]),                       # 會更新名稱+子圖
        _gl("BBB22222", "會被刪", ["NQ1!"]),                     # scan 來源、已消失 → prune
        _gl("CCC33333", "手動的", ["RTY1!"], source="manual"),   # manual → 保留
    ]
    state.groups = [group]

    results = [_gl("AAA11111", "新名", ["ES1!", "VIX"])]
    summary = lg.apply_scan_results(state, results, full=True, scanned_at="2026-07-12T09:00:00+08:00")

    assert [l.layout_id for l in state.scanned_layouts] == ["AAA11111"]
    assert state.scanned_at == "2026-07-12T09:00:00+08:00"
    assert [l.layout_id for l in group.layouts] == ["AAA11111", "CCC33333"]
    assert group.layouts[0].name == "新名"
    assert group.layouts[0].subcharts == ["ES1!", "VIX"]
    assert summary["pruned"] == 1
    assert summary["cache"] == 1


def test_apply_scan_results_partial_upserts_no_prune() -> None:
    state = lg.LayoutGroupsState()
    state.scanned_layouts = [_gl("AAA11111", "A", ["ES1!"]), _gl("BBB22222", "B", ["NQ1!"])]
    group = lg.LayoutGroup(group_id="g-1", name="G")
    group.layouts = [_gl("BBB22222", "B", ["NQ1!"])]
    state.groups = [group]

    # 局部執行只看到 AAA（且沒讀到子圖 → 沿用舊值）＋ 新版面 DDD
    results = [_gl("AAA11111", "A 改名", []), _gl("DDD44444", "新版面", ["CL1!"])]
    lg.apply_scan_results(state, results, full=False)

    by_id = {l.layout_id: l for l in state.scanned_layouts}
    assert set(by_id) == {"AAA11111", "BBB22222", "DDD44444"}
    assert by_id["AAA11111"].name == "A 改名"
    assert by_id["AAA11111"].subcharts == ["ES1!"]  # 空子圖 → 沿用舊快取
    assert [l.layout_id for l in group.layouts] == ["BBB22222"]  # 不 prune


def test_state_round_trip(tmp_dir: Path) -> None:
    data_path = tmp_dir / "layout_groups.json"
    lg.set_path_override(data_path)

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
    data_path.write_text("{not json", encoding="utf-8")
    broken = lg.load_layout_groups()
    assert broken.groups == [] and broken.scanned_layouts == []

    # Bad entries inside an otherwise valid file are dropped, not fatal.
    data_path.write_text(
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
    lg.set_path_override(None)


def test_path_resolver_precedence(tmp_dir: Path) -> None:
    from gex_suite.shared import config as shared_config

    # 隔離 suite_config.json 到 temp，避免動到真實設定。
    orig_cfg_path = shared_config.SUITE_CONFIG_PATH
    shared_config.SUITE_CONFIG_PATH = tmp_dir / "suite_config.json"
    try:
        lg.set_path_override(None)
        default = lg.layout_groups_path()
        assert default == lg.TRADINGVIEW_LAYOUT_GROUPS_PATH
        assert lg.get_configured_path() is None

        # config 自訂 → 生效，且能被讀回。
        custom = tmp_dir / "synced" / "groups.json"
        lg.set_configured_path(custom)
        assert lg.get_configured_path() == str(custom)
        assert lg.layout_groups_path() == custom

        # 存進自訂路徑後父目錄自動建立、可讀回。
        st = lg.LayoutGroupsState()
        st.groups.append(lg.LayoutGroup(group_id="g-x", name="同步群組"))
        lg.save_layout_groups(st)
        assert custom.exists()
        assert [g.name for g in lg.load_layout_groups().groups] == ["同步群組"]

        # 行程覆寫優先於 config。
        override = tmp_dir / "override.json"
        lg.set_path_override(override)
        assert lg.layout_groups_path() == override

        # 還原 config（None）→ 回預設。
        lg.set_path_override(None)
        lg.set_configured_path(None)
        assert lg.get_configured_path() is None
        assert lg.layout_groups_path() == lg.TRADINGVIEW_LAYOUT_GROUPS_PATH
    finally:
        shared_config.SUITE_CONFIG_PATH = orig_cfg_path
        lg.set_path_override(None)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        tests = [
            ("normalize_chart_url", test_normalize_chart_url),
            ("chart_id_from_url", test_chart_id_from_url),
            ("group_layout_from_dict", test_group_layout_from_dict),
            ("group_dedup", test_group_dedup),
            ("display_label_and_filter", test_display_label_and_filter),
            ("apply_scan_full", test_apply_scan_results_full_prunes_and_updates),
            ("apply_scan_partial", test_apply_scan_results_partial_upserts_no_prune),
            ("state_round_trip", lambda: test_state_round_trip(tmp_dir)),
            ("path_resolver_precedence", lambda: test_path_resolver_precedence(tmp_dir)),
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
