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
        print(f"[open-or-create-smoke] monday={monday.isoformat()} subcharts={[(s.index, s.symbol) for s in subcharts]}")
        for sub in subcharts:
            await automator.activate_subchart(sub.index)
            pinned = await automator.pin_indicator_scope_to_subchart(sub.index)
            try:
                state = await automator.open_or_create_indicator_for_week(monday=monday)
                print(
                    "[open-or-create-smoke] "
                    f"sub={sub.index} symbol={sub.symbol or '-'} pinned={pinned} state={state}"
                )
                await automator.close_settings(save=False)
            except Exception as exc:  # noqa: BLE001
                print(
                    "[open-or-create-smoke] "
                    f"sub={sub.index} symbol={sub.symbol or '-'} pinned={pinned} error={exc}"
                )
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())
