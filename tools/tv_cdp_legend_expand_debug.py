from __future__ import annotations

import asyncio

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        subcharts = await automator.enumerate_subcharts()
        print(f"[legend-debug] subcharts={[(s.index, s.symbol) for s in subcharts]}")
        for sub in subcharts:
            await automator.activate_subchart(sub.index)
            pinned = await automator.pin_indicator_scope_to_subchart(sub.index)
            before = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
            await automator._ensure_indicator_legend_expanded(title_keyword="Daily & Weekly GEX")
            after = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
            print(
                "[legend-debug] "
                f"sub={sub.index} symbol={sub.symbol or '-'} pinned={pinned} "
                f"before={len(before)} after={len(after)}"
            )
        global_matches = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=False)
        print(f"[legend-debug] global_count={len(global_matches)}")
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())
