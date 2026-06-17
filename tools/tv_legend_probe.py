"""CDP probe：開 formula chart，dump legend DOM，幫忙設計 tv_legend extractor。

用法（需要先用 9222 啟動 Brave/Chrome 並登入 TradingView）：

    .venv/bin/python tools/tv_legend_probe.py                    # 三條規則全跑
    .venv/bin/python tools/tv_legend_probe.py ES1! index          # 只跑指定 rule
    .venv/bin/python tools/tv_legend_probe.py --cdp-url http://127.0.0.1:9222

輸出：
- 每條 rule 印一段：formula URL、legend symbol title、所有 valueTitle/valueValue 節點 + 文字、
  目前 JS extractor 算出的值、main-series HTML 片段（截斷）
- 同時把每條 rule 的 legend HTML 完整存到 gex_suite/data/tradingview/debug/legend_<root>_<mode>.html
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

# Ensure we can import gex_suite.* when run from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from playwright.async_api import async_playwright

from gex_suite.modules.tradingview.quote_source import (
    RULES,
    FuturesRule,
    _formula_chart_url,
    _formula_string,
)
from gex_suite.shared.paths import TRADINGVIEW_DATA_DIR


DEBUG_DIR = TRADINGVIEW_DATA_DIR / "debug"


_DUMP_JS = r"""
() => {
  function dumpNode(node, maxHtmlLen) {
    if (!node) return null;
    const titles = Array.from(node.querySelectorAll("[class*='valueTitle-']")).map(n => (n.innerText || "").trim());
    const values = Array.from(node.querySelectorAll("[class*='valueValue-']")).map(n => (n.innerText || "").trim());
    const symbolEls = node.querySelectorAll(
      "[class*='mainTitle-'], [data-name='legend-source-title-text'], [data-name='legend-source-item-title']"
    );
    const symbolText = symbolEls.length ? (symbolEls[0].innerText || "").trim() : "";
    const innerText = (node.innerText || "").replace(/\s+/g, " ").trim().slice(0, 400);
    const html = node.outerHTML || "";
    return {
      symbol_text: symbolText,
      value_titles: titles,
      value_values: values,
      inner_text_preview: innerText,
      outer_html_len: html.length,
      outer_html_head: html.slice(0, maxHtmlLen),
    };
  }
  const main = document.querySelector("[data-name='legend-source-item-main-series']");
  const items = Array.from(document.querySelectorAll("[data-name='legend-source-item']")).slice(0, 3);
  const priceAxisLabels = Array.from(
    document.querySelectorAll(
      "[class*='priceAxisStub'] [class*='valueValue'], [class*='priceAxisLabel'] [class*='value']"
    )
  ).slice(0, 6).map(n => (n.innerText || "").trim());
  return {
    title: document.title,
    main_series: dumpNode(main, 1500),
    other_items: items.map(it => dumpNode(it, 600)),
    price_axis_labels: priceAxisLabels,
  };
}
"""


async def probe_rule(context, key: tuple[str, str], rule: FuturesRule, kind: str) -> None:
    root, mode = key
    formula = _formula_string(rule)
    url = _formula_chart_url(rule)
    print(f"\n===== {root} [{mode}] {kind} =====")
    print(f"formula = {formula}")
    print(f"url     = {url}")

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)  # initial mount

        last_snapshot = None
        for attempt in range(20):  # ~10s of polling
            try:
                snapshot = await page.evaluate(_DUMP_JS)
            except Exception as exc:  # noqa: BLE001
                snapshot = {"error": str(exc)}
            last_snapshot = snapshot
            main = (snapshot or {}).get("main_series")
            sym = (main or {}).get("symbol_text") if isinstance(main, dict) else ""
            if isinstance(sym, str) and ("ES1" in sym.upper() or "NQ1" in sym.upper() or "RTY1" in sym.upper() or "SPX" in sym.upper() or "QQQ" in sym.upper() or "IWM" in sym.upper()):
                break
            await page.wait_for_timeout(500)

        snapshot = last_snapshot or {}
        print(f"page.title = {snapshot.get('title')!r}")
        ms = snapshot.get("main_series")
        if not ms:
            print("  main_series = None (沒抓到 main-series legend node)")
        else:
            print(f"  symbol_text       = {ms.get('symbol_text')!r}")
            print(f"  value_titles ({len(ms.get('value_titles') or [])}) = {ms.get('value_titles')}")
            print(f"  value_values ({len(ms.get('value_values') or [])}) = {ms.get('value_values')}")
            print(f"  inner_text head   = {ms.get('inner_text_preview')!r}")
            print(f"  outer_html (len={ms.get('outer_html_len')}) head:")
            print("    " + (ms.get("outer_html_head") or "").replace("\n", " ")[:600])
            # Save full HTML for offline inspection
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            fp = DEBUG_DIR / f"legend_{root.rstrip('!')}_{mode}.html"
            fp.write_text(
                (ms.get("outer_html_head") or ""),
                encoding="utf-8",
            )
            print(f"  saved → {fp}")
        print(f"  price_axis_labels = {snapshot.get('price_axis_labels')}")
        other = snapshot.get("other_items") or []
        if other:
            print(f"  other legend-source-items: {len(other)}")
            for i, it in enumerate(other):
                if not it:
                    continue
                print(
                    f"    [{i}] symbol={it.get('symbol_text')!r}  "
                    f"titles={it.get('value_titles')}  values={it.get('value_values')}"
                )
    finally:
        await page.close()


async def main(filter_key: tuple[str, str] | None, cdp_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("no context found via CDP — make sure browser was launched with --remote-debugging-port=9222")
        context = browser.contexts[0]
        for key, (rule, kind) in RULES.items():
            if filter_key and key != filter_key:
                continue
            await probe_rule(context, key, rule, kind)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", nargs="?", default=None, help="Futures root (e.g. ES1!)")
    p.add_argument("mode", nargs="?", default=None, help="Layout mode (e.g. index)")
    p.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    args = p.parse_args()
    filter_key = None
    if args.root and args.mode:
        filter_key = (args.root.upper(), args.mode.lower())
    asyncio.run(main(filter_key, args.cdp_url))
