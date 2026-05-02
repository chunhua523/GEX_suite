from __future__ import annotations

import asyncio

from gex_suite.modules.tradingview.automator import PlaywrightCDPAutomator


async def _dump_row_controls(automator: PlaywrightCDPAutomator, cand, idx: int) -> None:
    page = automator._require_page()
    try:
        info = await cand.evaluate(
            """
            (el) => {
              const row = el.closest("[data-name='legend-source-item']")
                || el.closest("[class*='sourceItem']")
                || el;
              const ancestors = [];
              let cur = el;
              for (let i = 0; i < 8 && cur; i += 1) {
                ancestors.push({
                  tag: cur.tagName || "",
                  data_name: cur.getAttribute("data-name") || "",
                  class_name: (cur.className || "").toString(),
                });
                cur = cur.parentElement;
              }
              if (!row) return null;
              const attrs = {
                tag: row.tagName || "",
                data_name: row.getAttribute("data-name") || "",
                data_qa_id: row.getAttribute("data-qa-id") || "",
                class_name: (row.className || "").toString(),
                text: (row.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 180),
              };
              const controls = Array.from(row.querySelectorAll("button, [role='button'], [data-name], [class*='button']"))
                .slice(0, 24)
                .map((node) => ({
                  tag: node.tagName || "",
                  aria: node.getAttribute("aria-label") || "",
                  title: node.getAttribute("title") || "",
                  data_name: node.getAttribute("data-name") || "",
                  class_name: (node.className || "").toString(),
                  text: (node.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 80),
                }));
              return { attrs, controls, ancestors };
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[remove-debug] cand#{idx} row_info_error={exc}")
        return
    if not info:
        print(f"[remove-debug] cand#{idx} row_info=<none>")
        return
    attrs = info.get("attrs") or {}
    print(
        "[remove-debug] "
        f"cand#{idx} row tag={attrs.get('tag')} data-name={attrs.get('data_name')} "
        f"data-qa={attrs.get('data_qa_id')} class={str(attrs.get('class_name') or '')[:80]} "
        f"text={attrs.get('text')}"
    )
    controls = info.get("controls") or []
    ancestors = info.get("ancestors") or []
    for aidx, anc in enumerate(ancestors[:8]):
        print(
            "[remove-debug] "
            f"cand#{idx} anc#{aidx} tag={anc.get('tag')} data-name={anc.get('data_name')} "
            f"class={str(anc.get('class_name') or '')[:80]}"
        )
    for cidx, ctl in enumerate(controls[:12]):
        print(
            "[remove-debug] "
            f"cand#{idx} ctl#{cidx} tag={ctl.get('tag')} data-name={ctl.get('data_name')} "
            f"aria={ctl.get('aria')} title={ctl.get('title')} "
            f"class={str(ctl.get('class_name') or '')[:70]} text={ctl.get('text')}"
        )

    try:
        await cand.click(button="right", force=True, timeout=1200)
    except Exception:
        try:
            await cand.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el;
                  if (!row) return false;
                  row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
                  return true;
                }
                """
            )
        except Exception:
            pass
    await page.wait_for_timeout(180)
    menu_items = page.locator("[role='menuitem'], #overlap-manager-root [class*='item']")
    count = await menu_items.count()
    print(f"[remove-debug] cand#{idx} menu_count={count}")
    for midx in range(min(count, 15)):
        try:
            txt = ((await menu_items.nth(midx).inner_text(timeout=400)) or "").strip().replace("\n", " / ")
        except Exception:
            txt = ""
        if txt:
            print(f"[remove-debug] cand#{idx} menu#{midx} text={txt[:140]}")
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
    except Exception:
        pass


async def main() -> None:
    automator = PlaywrightCDPAutomator()
    automator.set_logger(print)
    await automator.connect()
    try:
        subcharts = await automator.enumerate_subcharts()
        print(f"[remove-debug] subcharts={[(s.index, s.symbol) for s in subcharts]}")
        target = next((s for s in subcharts if (s.symbol or "").upper() == "BKR"), None)
        if target is None:
            print("[remove-debug] BKR subchart not found")
            return
        await automator.activate_subchart(target.index)
        pinned = await automator.pin_indicator_scope_to_subchart(target.index)
        print(f"[remove-debug] pinned={pinned} sub={target.index}")
        await automator._ensure_indicator_legend_expanded(title_keyword="Daily & Weekly GEX")
        candidates = await automator._collect_any_indicator_locators("Daily & Weekly GEX", active_only=True)
        print(f"[remove-debug] scoped_candidates={len(candidates)}")
        for idx, cand in enumerate(candidates):
            await _dump_row_controls(automator, cand, idx)
    finally:
        await automator.clear_indicator_scope_marker()
        await automator.close()


if __name__ == "__main__":
    asyncio.run(main())

