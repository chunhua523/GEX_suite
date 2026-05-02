from __future__ import annotations

import asyncio

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        subcharts = await automator.enumerate_subcharts()
        print(f"[cdp-debug] subcharts={[(s.index, s.symbol) for s in subcharts]}")
        if not subcharts:
            return
        await automator.activate_subchart(0)
        pinned = await automator.pin_indicator_scope_to_subchart(0)
        print(f"[cdp-debug] pinned_sub0={pinned}")
        scoped = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
        global_matches = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=False)
        print(
            "[cdp-debug] "
            f"scoped_count={len(scoped)} global_count={len(global_matches)} "
            f"scoped_index={automator._scoped_subchart_index}"
        )
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())
