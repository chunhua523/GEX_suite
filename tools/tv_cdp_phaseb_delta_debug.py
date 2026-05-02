from __future__ import annotations

import asyncio
from datetime import date
import os

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


TARGET_MONDAYS = (
    date(2026, 4, 6),
    date(2026, 4, 13),
    date(2026, 4, 20),
    date(2026, 4, 27),
)
TARGET_SYMBOLS = ("SLB", "HAL")
ALLOW_MUTATION = os.getenv("TV_PHASEB_DEBUG_MUTATE", "").strip() in {"1", "true", "TRUE", "yes", "YES"}


async def _probe_existing_start_dates(
    automator: PlaywrightCDPAutomator,
    *,
    title_keyword: str = "Daily & Weekly GEX",
    symbol: str = "?",
) -> tuple[set[str], int, int, int]:
    candidates = await automator._collect_any_indicator_locators(title_keyword, active_only=True)
    observed: set[str] = set()
    opened_rows = 0
    unreadable_rows = 0
    total_rows = len(candidates)
    page = automator._require_page()
    for tag_idx, cand in enumerate(candidates):
        try:
            geom = await cand.evaluate(
                "(el) => { const r = el.getBoundingClientRect();"
                " return { top: Math.round(r.top), left: Math.round(r.left),"
                " w: Math.round(r.width), h: Math.round(r.height) }; }"
            )
        except Exception:
            geom = None
        try:
            row_meta = await cand.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  const innerText = ((row.innerText || row.textContent) || "")
                    .replace(/\\s+/g, " ").trim().slice(0, 200);
                  return {
                    sourceId: row.getAttribute("data-source-id") || "",
                    studyId: row.getAttribute("data-study-id") || "",
                    instanceId: row.getAttribute("data-instance-id") || "",
                    qaId: row.getAttribute("data-qa-id") || "",
                    ariaLabel: row.getAttribute("aria-label") || "",
                    innerText: innerText
                  };
                }
                """
            )
        except Exception:
            row_meta = None
        print(
            f"[phaseb-delta] symbol={symbol} probe tag={tag_idx} "
            f"row_meta={row_meta!r}"
        )
        opened = await automator._open_settings_for_locator(cand, title_keyword)
        if not opened:
            unreadable_rows += 1
            print(
                f"[phaseb-delta] symbol={symbol} probe tag={tag_idx} "
                f"open=fail geom={geom!r}"
            )
            continue
        opened_rows += 1
        date_val, _ = await automator.read_weekly_start_datetime()
        normalized = automator._normalize_start_date_text(date_val)
        if normalized:
            observed.add(normalized)
        else:
            unreadable_rows += 1
        print(
            f"[phaseb-delta] symbol={symbol} probe tag={tag_idx} "
            f"open=ok start_date={normalized!r} raw={date_val!r} geom={geom!r}"
        )
        await automator.close_settings(save=False)
        await page.wait_for_timeout(120)
    return observed, opened_rows, unreadable_rows, total_rows


async def _debug_symbol(automator: PlaywrightCDPAutomator, symbol: str) -> None:
    subcharts = await automator.enumerate_subcharts()
    target = next((s for s in subcharts if (s.symbol or "").upper() == symbol.upper()), None)
    if target is None:
        print(f"[phaseb-delta] symbol={symbol} not-found")
        return
    await automator.activate_subchart(target.index)
    pinned = await automator.pin_indicator_scope_to_subchart(target.index)
    print(f"[phaseb-delta] symbol={symbol} sub={target.index} pinned={pinned}")
    await automator._ensure_indicator_legend_expanded(title_keyword="Daily & Weekly GEX")
    scoped_visible = await automator._collect_visible_indicator_locators("Daily & Weekly GEX", active_only=True)
    scoped_any = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
    print(f"[phaseb-delta] symbol={symbol} scoped_visible={len(scoped_visible)} scoped_any={len(scoped_any)}")
    # Global raw scan: every legend-source-item across the entire document with
    # title containing "Daily" "Weekly" "GEX" — used to detect whether a row
    # exists outside the pinned subchart scope.
    page = automator._require_page()
    try:
        global_rows = await page.evaluate(
            """
            () => {
              const matches = [];
              const widgets = Array.from(
                document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
              ).filter((el) => {
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                return r.width > 20 && r.height > 20
                  && st.display !== "none" && st.visibility !== "hidden";
              });
              const widgetRect = (el) => el.getBoundingClientRect();
              const widgetIndexFor = (rect) => {
                let bestIdx = -1;
                let bestDist = Number.POSITIVE_INFINITY;
                for (let i = 0; i < widgets.length; i += 1) {
                  const wr = widgetRect(widgets[i]);
                  const cx = (rect.left + rect.right) / 2;
                  const cy = (rect.top + rect.bottom) / 2;
                  const nx = Math.max(wr.left, Math.min(wr.right, cx));
                  const ny = Math.max(wr.top, Math.min(wr.bottom, cy));
                  const dx = cx - nx;
                  const dy = cy - ny;
                  const d = Math.sqrt(dx * dx + dy * dy);
                  if (d < bestDist) { bestDist = d; bestIdx = i; }
                }
                return bestIdx;
              };
              const seen = new Set();
              const rows = [];
              const candidates = document.querySelectorAll(
                "[data-name='legend-source-item'], " +
                "[class*='sourceItem'], " +
                "[class*='item'][class*='study']"
              );
              for (const el of candidates) {
                const row = el.closest("[data-name='legend-source-item']")
                  || el.closest("[class*='sourceItem']")
                  || el.closest("[class*='item'][class*='study']")
                  || el;
                if (!row || seen.has(row)) continue;
                seen.add(row);
                rows.push(row);
              }
              for (const row of rows) {
                const txt = ((row.innerText || row.textContent) || "")
                  .replace(/\\s+/g, " ").trim();
                const lower = txt.toLowerCase();
                if (!/daily/.test(lower) || !/weekly/.test(lower) || !/gex/.test(lower)) {
                  continue;
                }
                const r = row.getBoundingClientRect();
                matches.push({
                  text: txt.slice(0, 200),
                  top: Math.round(r.top),
                  left: Math.round(r.left),
                  w: Math.round(r.width),
                  h: Math.round(r.height),
                  widgetIdx: widgetIndexFor(r),
                  qaId: row.getAttribute("data-qa-id") || "",
                  scanIdx: row.getAttribute("data-gex-scan-row") || ""
                });
              }
              return { widgetCount: widgets.length, matches };
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        global_rows = {"widgetCount": -1, "matches": [], "error": str(exc)}
    print(
        f"[phaseb-delta] symbol={symbol} global_scan widgets={global_rows.get('widgetCount')} "
        f"matches={len(global_rows.get('matches') or [])}"
    )
    for m in global_rows.get("matches") or []:
        print(
            f"[phaseb-delta] symbol={symbol} global_row top={m.get('top')} "
            f"left={m.get('left')} w={m.get('w')} h={m.get('h')} "
            f"widget={m.get('widgetIdx')} scanIdx={m.get('scanIdx')!r} "
            f"text={m.get('text')!r}"
        )

    snapshot_rows = await automator._snapshot_weekly_gex_rows(
        title_keyword="Daily & Weekly GEX",
        allow_global_fallback=False,
    )
    snapshot_dates = sorted(d.isoformat() for d in snapshot_rows.keys())
    snapshot_date_set = set(snapshot_dates)
    probe_dates, probe_opened, probe_unreadable, probe_total = await _probe_existing_start_dates(
        automator,
        title_keyword="Daily & Weekly GEX",
        symbol=symbol,
    )
    probe_dates_sorted = sorted(probe_dates)
    print(
        f"[phaseb-delta] symbol={symbol} snapshot_start_dates="
        f"{','.join(snapshot_dates) if snapshot_dates else '-'}"
    )
    print(
        f"[phaseb-delta] symbol={symbol} probe_start_dates="
        f"{','.join(probe_dates_sorted) if probe_dates_sorted else '-'} "
        f"opened={probe_opened}/{probe_total} unique={len(probe_dates_sorted)} "
        f"unreadable={probe_unreadable}"
    )

    for monday in TARGET_MONDAYS:
        try:
            if not ALLOW_MUTATION:
                opened = await automator._open_settings_for_target_start_date(
                    title_keyword="Daily & Weekly GEX",
                    target_start_date=monday.isoformat(),
                    allow_global_fallback=False,
                )
                target_iso = monday.isoformat()
                if opened:
                    state = "existing"
                elif target_iso in snapshot_date_set or target_iso in probe_dates:
                    # Don't report missing when deterministic reopen fails but
                    # snapshot confirms this week row exists.
                    state = "existing_snapshot_only"
                elif probe_unreadable > 0:
                    state = "inconclusive_unreadable_row"
                else:
                    # Target week not present in any successful probe; it's
                    # safe to report missing even when other weeks have
                    # duplicate rows on the chart.
                    state = "missing"
                print(f"[phaseb-delta] symbol={symbol} monday={monday.isoformat()} state={state} dry_run=1")
                if opened:
                    await automator.close_settings(save=False)
                continue

            state = await automator.open_or_create_indicator_for_week(monday=monday)
            print(f"[phaseb-delta] symbol={symbol} monday={monday.isoformat()} state={state} dry_run=0")
            date_val, time_val = await automator.read_weekly_start_datetime()
            print(
                f"[phaseb-delta] symbol={symbol} monday={monday.isoformat()} "
                f"opened_start={date_val!r} opened_time={time_val!r}"
            )
            await automator.set_weekly_start_date(monday=monday, time_str="04:00")
            verify_date, verify_time = await automator.read_weekly_start_datetime()
            print(
                f"[phaseb-delta] symbol={symbol} monday={monday.isoformat()} "
                f"after_set_start={verify_date!r} after_set_time={verify_time!r}"
            )
            await automator.close_settings(save=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[phaseb-delta] symbol={symbol} monday={monday.isoformat()} FAIL={exc}")


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        subcharts = await automator.enumerate_subcharts()
        print(f"[phaseb-delta] subcharts={[(s.index, s.symbol) for s in subcharts]}")
        for symbol in TARGET_SYMBOLS:
            await _debug_symbol(automator, symbol)
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())
