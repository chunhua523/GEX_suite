from __future__ import annotations

import asyncio
from datetime import date, timedelta

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        subcharts = await automator.enumerate_subcharts()
        print(f"[bkr-debug] monday={monday.isoformat()} subcharts={[(s.index, s.symbol) for s in subcharts]}")
        target = next((s for s in subcharts if (s.symbol or "").upper() == "BKR"), None)
        if target is None:
            print("[bkr-debug] BKR subchart not found")
            return
        await automator.activate_subchart(target.index)
        pinned = await automator.pin_indicator_scope_to_subchart(target.index)
        print(f"[bkr-debug] pinned={pinned} sub={target.index}")

        cands = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
        print(f"[bkr-debug] scoped_candidates={len(cands)}")
        for idx, cand in enumerate(cands):
            opened = await automator._open_settings_for_locator(cand, "Daily & Weekly GEX")
            if not opened:
                print(f"[bkr-debug] cand#{idx} open=FAIL")
                continue
            d, t = await automator.read_weekly_start_datetime()
            print(f"[bkr-debug] cand#{idx} start={d} {t}")
            await automator.close_settings(save=False)

        state = await automator.open_or_create_indicator_for_week(monday=monday)
        print(f"[bkr-debug] open_or_create_state={state}")
        opened_date, opened_time = await automator.read_weekly_start_datetime()
        print(f"[bkr-debug] opened_start={opened_date} {opened_time}")
        levels = await automator.read_weekly_levels()
        print(f"[bkr-debug] levels={levels}")
        await automator.close_settings(save=False)
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())
