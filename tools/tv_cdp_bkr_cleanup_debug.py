from __future__ import annotations

import asyncio
from datetime import date, timedelta

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


def _keep_mondays_sunday_start(weeks: int = 4) -> list[date]:
    today = date.today()
    days_from_sunday = (today.weekday() + 1) % 7
    this_sunday = today - timedelta(days=days_from_sunday)
    out: list[date] = []
    for i in range(max(1, weeks)):
        sunday = this_sunday - timedelta(days=7 * i)
        out.append(sunday + timedelta(days=1))
    out.sort()
    return out


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        subcharts = await automator.enumerate_subcharts()
        print(f"[bkr-cleanup] subcharts={[(s.index, s.symbol) for s in subcharts]}")
        target = next((s for s in subcharts if (s.symbol or "").upper() == "BKR"), None)
        if target is None:
            print("[bkr-cleanup] BKR subchart not found")
            return
        await automator.activate_subchart(target.index)
        pinned = await automator.pin_indicator_scope_to_subchart(target.index)
        print(f"[bkr-cleanup] pinned={pinned} sub={target.index}")
        keep = _keep_mondays_sunday_start(weeks=4)
        print(f"[bkr-cleanup] keep_mondays={[d.isoformat() for d in keep]}")
        stats = await automator.cleanup_and_sort_weekly_gex_indicators(
            keep_mondays=keep,
            ticker="BKR",
            time_str="04:00",
        )
        print(f"[bkr-cleanup] stats={stats}")
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())

