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
    assert lg.chart_url_for_id("AbCd1234") == "https://www.tradingview.com/chart/AbCd1234/"


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


def test_group_contains_and_resolve() -> None:
    group = lg.LayoutGroup(group_id=lg.new_group_id(), name="G", layout_ids=["AbCd1234"])
    assert group.contains_id("AbCd1234")
    assert not group.contains_id("XyZw9876")
    assert not group.contains_id(None)

    state = lg.LayoutGroupsState()
    state.scanned_layouts = [_gl("AbCd1234", "A")]
    state.groups = [group, lg.LayoutGroup(group_id="g-2", name="H", layout_ids=["Gone0000"])]
    assert state.resolve("AbCd1234") is not None
    assert state.resolve("Gone0000") is None  # 已過期
    assert state.count_expired() == 1


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


def test_apply_scan_results_full_replaces_keeps_manual_no_group_touch() -> None:
    state = lg.LayoutGroupsState()
    state.scanned_layouts = [
        _gl("AAA11111", "舊名", ["ES1!"]),
        _gl("BBB22222", "會被刪", ["NQ1!"]),
        _gl("MMM55555", "手動的", [], source="manual"),  # 手動項目 → full 掃描保留
    ]
    group = lg.LayoutGroup(
        group_id="g-1", name="G", layout_ids=["AAA11111", "BBB22222", "MMM55555"]
    )
    state.groups = [group]

    results = [_gl("AAA11111", "新名", ["ES1!", "VIX"])]
    summary = lg.apply_scan_results(state, results, full=True, scanned_at="2026-07-12T09:00:00+08:00")

    assert sorted(l.layout_id for l in state.scanned_layouts) == ["AAA11111", "MMM55555"]
    assert state.scanned_at == "2026-07-12T09:00:00+08:00"
    # 群組完全不動——BBB22222 留著、由 UI 顯示已過期。
    assert group.layout_ids == ["AAA11111", "BBB22222", "MMM55555"]
    assert state.resolve("AAA11111").name == "新名"
    assert state.resolve("BBB22222") is None
    assert summary["cache"] == 2
    assert summary["expired"] == 1


def test_apply_scan_results_partial_upserts_no_delete() -> None:
    state = lg.LayoutGroupsState()
    state.scanned_layouts = [_gl("AAA11111", "A", ["ES1!"]), _gl("BBB22222", "B", ["NQ1!"])]
    group = lg.LayoutGroup(group_id="g-1", name="G", layout_ids=["BBB22222"])
    state.groups = [group]

    # 局部執行只看到 AAA（且沒讀到子圖 → 沿用舊值）＋ 新版面 DDD
    results = [_gl("AAA11111", "A 改名", []), _gl("DDD44444", "新版面", ["CL1!"])]
    summary = lg.apply_scan_results(state, results, full=False)

    by_id = {l.layout_id: l for l in state.scanned_layouts}
    assert set(by_id) == {"AAA11111", "BBB22222", "DDD44444"}
    assert by_id["AAA11111"].name == "A 改名"
    assert by_id["AAA11111"].subcharts == ["ES1!"]  # 空子圖 → 沿用舊快取
    assert group.layout_ids == ["BBB22222"]
    assert summary["expired"] == 0


def test_two_file_round_trip(tmp_dir: Path) -> None:
    data_path = tmp_dir / "roundtrip" / "layout_groups.json"
    lg.set_path_override(data_path)
    try:
        state = lg.LayoutGroupsState()
        g = lg.LayoutGroup(
            group_id="g-11223344", name="指數期貨", layout_ids=["AbCd1234", "XyZw9876"]
        )
        state.groups.append(g)
        state.scanned_layouts.append(_gl("AbCd1234", "ES [equity]", ["ES1!"]))
        state.scanned_layouts.append(_gl("XyZw9876", "手動", [], source="manual"))
        state.scanned_at = "2026-07-11T09:00:00+08:00"
        state.settings["app_cdp_port"] = 9444
        lg.save_layout_groups(state)

        # 兩個檔案分開存在，群組檔只有 id、不含快取。
        assert data_path.exists()
        assert (data_path.parent / "layout_list.json").exists()
        raw_groups = json.loads(data_path.read_text(encoding="utf-8"))
        assert raw_groups["version"] == 2
        assert "scanned_layouts" not in raw_groups
        assert raw_groups["groups"][0]["layout_ids"] == ["AbCd1234", "XyZw9876"]

        loaded = lg.load_layout_groups()
        assert [grp.name for grp in loaded.groups] == ["指數期貨"]
        assert loaded.groups[0].group_id == "g-11223344"
        assert loaded.groups[0].layout_ids == ["AbCd1234", "XyZw9876"]
        assert loaded.resolve("XyZw9876").source == "manual"
        assert loaded.scanned_at == "2026-07-11T09:00:00+08:00"
        assert loaded.settings["app_cdp_port"] == 9444
        # Defaults still merged in.
        assert loaded.settings["delay_per_url_ms"] == 1800

        # 群組編輯只寫群組檔：save_groups 不碰清單檔。
        list_mtime = (data_path.parent / "layout_list.json").stat().st_mtime_ns
        loaded.groups[0].layout_ids.append("Newl0000")
        lg.save_groups(loaded)
        assert (data_path.parent / "layout_list.json").stat().st_mtime_ns == list_mtime
        assert lg.load_layout_groups().resolve("Newl0000") is None  # → UI 顯示已過期

        # Corrupt files → tolerant empty state.
        data_path.write_text("{not json", encoding="utf-8")
        (data_path.parent / "layout_list.json").write_text("{not json", encoding="utf-8")
        broken = lg.load_layout_groups()
        assert broken.groups == [] and broken.scanned_layouts == []

        # Bad ids inside groups are dropped, not fatal.
        data_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "groups": [
                        {"id": "g-1", "name": "ok", "layout_ids": ["bad!!", "AbCd1234", 5]},
                        "not-a-dict",
                    ],
                }
            ),
            encoding="utf-8",
        )
        partial = lg.load_layout_groups()
        assert len(partial.groups) == 1
        assert partial.groups[0].layout_ids == ["AbCd1234"]
    finally:
        lg.set_path_override(None)


def test_legacy_single_file_migration(tmp_dir: Path) -> None:
    """舊版單檔（groups 內嵌 layouts ＋ scanned_layouts 同檔）→ 自動拆檔."""
    data_path = tmp_dir / "legacy" / "layout_groups.json"
    data_path.parent.mkdir(parents=True)
    legacy = {
        "version": 1,
        "settings": {"app_cdp_port": 9555},
        "scanned_at": "2026-08-07T22:11:26+08:00",
        "scanned_layouts": [
            {"layout_id": "AAA11111", "name": "K2I", "url": "/chart/AAA11111/", "subcharts": ["K2I1!"]},
        ],
        "groups": [
            {
                "id": "g-main",
                "name": "Main",
                "layouts": [
                    {"layout_id": "AAA11111", "name": "K2I", "url": "/chart/AAA11111/", "subcharts": ["K2I1!"]},
                    # 群組裡有、但快取沒有的手動項目 → 遷移時併入清單檔
                    {"layout_id": "MMM55555", "name": "手動", "url": "/chart/MMM55555/", "source": "manual"},
                ],
            }
        ],
    }
    data_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    lg.set_path_override(data_path)
    try:
        state = lg.load_layout_groups()
        assert state.groups[0].layout_ids == ["AAA11111", "MMM55555"]
        assert state.resolve("AAA11111").subcharts == ["K2I1!"]
        assert state.resolve("MMM55555").source == "manual"
        assert state.scanned_at == "2026-08-07T22:11:26+08:00"
        assert state.settings["app_cdp_port"] == 9555
        assert state.count_expired() == 0  # 遷移後不得出現假性過期

        # 遷移已寫回：群組檔轉新格式、清單檔誕生。
        migrated = json.loads(data_path.read_text(encoding="utf-8"))
        assert migrated["version"] == 2
        assert "scanned_layouts" not in migrated
        assert migrated["groups"][0]["layout_ids"] == ["AAA11111", "MMM55555"]
        list_data = json.loads(
            (data_path.parent / "layout_list.json").read_text(encoding="utf-8")
        )
        assert {l["layout_id"] for l in list_data["layouts"]} == {"AAA11111", "MMM55555"}

        # 再 load 一次（已是新格式）結果一致。
        again = lg.load_layout_groups()
        assert again.groups[0].layout_ids == ["AAA11111", "MMM55555"]
    finally:
        lg.set_path_override(None)


def test_legacy_migration_list_file_wins(tmp_dir: Path) -> None:
    """舊群組檔＋既有清單檔並存（舊 GUI 寫回的過渡期）→ 清單檔內容優先."""
    data_path = tmp_dir / "mixed" / "layout_groups.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text(
        json.dumps(
            {
                "scanned_layouts": [
                    {"layout_id": "AAA11111", "name": "舊名", "url": "/chart/AAA11111/"},
                ],
                "groups": [
                    {"id": "g-1", "name": "G", "layouts": [
                        {"layout_id": "AAA11111", "name": "舊名", "url": "/chart/AAA11111/"},
                    ]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_path.parent / "layout_list.json").write_text(
        json.dumps(
            {
                "version": 1,
                "scanned_at": "2026-08-09T10:00:00+08:00",
                "layouts": [
                    {"layout_id": "AAA11111", "name": "新名", "url": "/chart/AAA11111/", "subcharts": ["ES1!"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lg.set_path_override(data_path)
    try:
        state = lg.load_layout_groups()
        assert state.resolve("AAA11111").name == "新名"  # 清單檔優先
        assert state.resolve("AAA11111").subcharts == ["ES1!"]
        assert state.scanned_at == "2026-08-09T10:00:00+08:00"
    finally:
        lg.set_path_override(None)


def test_apply_scan_results_to_disk_writes_list_only(tmp_dir: Path) -> None:
    data_path = tmp_dir / "diskflow" / "layout_groups.json"
    lg.set_path_override(data_path)
    try:
        state = lg.LayoutGroupsState()
        state.groups.append(
            lg.LayoutGroup(group_id="g-1", name="G", layout_ids=["AAA11111", "Gone0000"])
        )
        lg.save_groups(state)
        groups_mtime = data_path.stat().st_mtime_ns

        summary = lg.apply_scan_results_to_disk(
            [_gl("AAA11111", "A", ["ES1!"])], full=True, scanned_at="2026-08-10T06:00:00+08:00"
        )
        assert summary["cache"] == 1
        assert summary["expired"] == 1  # Gone0000 解析不到
        assert data_path.stat().st_mtime_ns == groups_mtime  # 群組檔未被觸碰
        loaded = lg.load_layout_groups()
        assert loaded.groups[0].layout_ids == ["AAA11111", "Gone0000"]
        assert loaded.resolve("AAA11111") is not None
        assert loaded.scanned_at == "2026-08-10T06:00:00+08:00"
    finally:
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

        # config 自訂 → 生效，且能被讀回；清單檔未自訂時跟著群組檔目錄走。
        custom = tmp_dir / "synced" / "groups.json"
        lg.set_configured_path(custom)
        assert lg.get_configured_path() == str(custom)
        assert lg.layout_groups_path() == custom
        assert lg.layout_list_path() == custom.parent / "layout_list.json"

        # 清單檔可獨立自訂（例：群組檔放同步資料夾、清單檔留本機）。
        custom_list = tmp_dir / "local" / "my_list.json"
        lg.set_configured_list_path(custom_list)
        assert lg.get_configured_list_path() == str(custom_list)
        assert lg.layout_list_path() == custom_list
        st_list = lg.LayoutGroupsState()
        st_list.scanned_layouts.append(_gl("AbCd1234", "A"))
        lg.save_list(st_list)
        assert custom_list.exists()
        assert lg.load_layout_groups().resolve("AbCd1234") is not None

        # 清單檔行程覆寫 > config；還原後回到 config 值。
        list_override = tmp_dir / "list_override.json"
        lg.set_list_path_override(list_override)
        assert lg.layout_list_path() == list_override
        lg.set_list_path_override(None)
        assert lg.layout_list_path() == custom_list

        # 還原清單檔 config → 回「群組檔同目錄」預設。
        lg.set_configured_list_path(None)
        assert lg.get_configured_list_path() is None
        assert lg.layout_list_path() == custom.parent / "layout_list.json"

        # 存進自訂路徑後父目錄自動建立、可讀回。
        st = lg.LayoutGroupsState()
        st.groups.append(lg.LayoutGroup(group_id="g-x", name="同步群組"))
        lg.save_groups(st)
        assert custom.exists()
        assert [g.name for g in lg.load_layout_groups().groups] == ["同步群組"]

        # 行程覆寫優先於 config。
        override = tmp_dir / "override.json"
        lg.set_path_override(override)
        assert lg.layout_groups_path() == override
        assert lg.layout_list_path() == override.parent / "layout_list.json"

        # 還原 config（None）→ 回預設。
        lg.set_path_override(None)
        lg.set_configured_path(None)
        assert lg.get_configured_path() is None
        assert lg.layout_groups_path() == lg.TRADINGVIEW_LAYOUT_GROUPS_PATH
    finally:
        shared_config.SUITE_CONFIG_PATH = orig_cfg_path
        lg.set_path_override(None)
        lg.set_list_path_override(None)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        tests = [
            ("normalize_chart_url", test_normalize_chart_url),
            ("chart_id_from_url", test_chart_id_from_url),
            ("group_layout_from_dict", test_group_layout_from_dict),
            ("group_contains_and_resolve", test_group_contains_and_resolve),
            ("display_label_and_filter", test_display_label_and_filter),
            ("apply_scan_full", test_apply_scan_results_full_replaces_keeps_manual_no_group_touch),
            ("apply_scan_partial", test_apply_scan_results_partial_upserts_no_delete),
            ("two_file_round_trip", lambda: test_two_file_round_trip(tmp_dir)),
            ("legacy_migration", lambda: test_legacy_single_file_migration(tmp_dir)),
            ("legacy_list_file_wins", lambda: test_legacy_migration_list_file_wins(tmp_dir)),
            ("disk_flow_list_only", lambda: test_apply_scan_results_to_disk_writes_list_only(tmp_dir)),
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
