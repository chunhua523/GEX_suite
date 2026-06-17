"""TO FUTURE Ratio/Offset 自動填入的報價來源層。

對於 ES1!/NQ1!/RTY1! 這類在現貨/ETF 資料源上疊期貨 X 軸的子圖，
GEX indicator 的 "TO FUTURE" 區段需要寫入 (期貨 - 現貨) 或 (期貨 / 現貨)
作為對齊參數。本模組封裝報價取得（yfinance 為預設；tv_legend 預留）。

新增規則：在 RULES 加 entry 即可，widget / automator 不必動。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
import urllib.parse

QuoteSource = Literal["yfinance", "tv_legend"]


@dataclass(frozen=True)
class FuturesRule:
    futures_yf: str       # yfinance ticker for futures, e.g. "ES=F"
    compare_yf: str       # yfinance ticker for cash/ETF, e.g. "^GSPC" / "QQQ"
    futures_tv: str       # TradingView symbol, e.g. "CME_MINI:ES1!"
    compare_tv: str       # TradingView symbol, e.g. "FOREXCOM:SPX500"
    op: Literal["diff", "div"]   # diff → futures - compare; div → futures / compare


# Key: (futures_root, layout_mode) where futures_root is the tail-after-':'
# of the subchart symbol (e.g. "NQ1!" from "CME_MINI:NQ1!"), and layout_mode
# is the resolved mode ("equity"/"index"/"futures") from _LAYOUT_MODE_MARKERS.
#
# Value: (FuturesRule, target_field_kind) where field_kind ∈ {"Ratio", "Offset"}.
RULES: dict[tuple[str, str], tuple[FuturesRule, str]] = {
    ("ES1!",  "index"):  (FuturesRule("ES=F",  "^GSPC", "CME_MINI:ES1!",  "FOREXCOM:SPX500", "diff"), "Offset"),
    ("NQ1!",  "equity"): (FuturesRule("NQ=F",  "QQQ",   "CME_MINI:NQ1!",  "BATS:QQQ",        "div"),  "Ratio"),
    ("RTY1!", "equity"): (FuturesRule("RTY=F", "IWM",   "CME_MINI:RTY1!", "AMEX:IWM",        "div"),  "Ratio"),
}


@dataclass(frozen=True)
class QuoteResult:
    """One quote attempt outcome.

    ``value=None`` always carries a non-empty ``reason`` so the caller can log
    a specific cause (stale cash leg, missing quote, source error, …).
    """
    value: float | None
    reason: str | None = None  # None iff value is a valid number


def compute(rule: FuturesRule, futures_price: float, compare_price: float) -> float | None:
    if rule.op == "diff":
        return futures_price - compare_price
    if rule.op == "div":
        if compare_price == 0:
            return None
        return futures_price / compare_price
    return None


# Cash legs (^GSPC / QQQ / IWM on yfinance) only update during US RTH.
# Outside RTH their fast_info.last_price stays at the prior close, which makes
# diff/ratio against a still-moving futures leg silently meaningless.
# Threshold is generous so a minute-bar lag during RTH doesn't trip the guard,
# but short enough that pre-/post-market stale quotes are caught (cash bars
# stop updating immediately at 16:00 ET; next valid bar is ~17.5h later).
_MAX_STALE_SECONDS = 15 * 60


def _yf_last(symbol: str) -> tuple[float | None, datetime | None]:
    """Return (last_price, last_bar_timestamp_utc); both ``None`` on failure.

    ``fast_info["last_price"]`` returns a price but no timestamp, so we always
    fetch a 1-minute history bar to learn the freshness of the data and use its
    Close as the canonical last price (consistent across symbols, RTH vs not).
    """
    import math
    import yfinance as yf  # lazy import; keep widget/smoke-test import light
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d", interval="1m", auto_adjust=False)
        if hist is None or hist.empty:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None, None
        close_series = hist["Close"].dropna()
        if close_series.empty:
            return None, None
        last_idx = close_series.index[-1]
        price = float(close_series.iloc[-1])
        if math.isnan(price) or math.isinf(price):
            return None, None
        # Normalize to UTC datetime regardless of pandas Timestamp tz.
        ts = last_idx.to_pydatetime() if hasattr(last_idx, "to_pydatetime") else last_idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return price, ts
    except Exception:
        return None, None


def _yf_compute(rule: FuturesRule) -> QuoteResult:
    futures, futures_ts = _yf_last(rule.futures_yf)
    compare, compare_ts = _yf_last(rule.compare_yf)
    if futures is None:
        return QuoteResult(None, f"yfinance 取不到 {rule.futures_yf}")
    if compare is None:
        return QuoteResult(None, f"yfinance 取不到 {rule.compare_yf}")
    now = datetime.now(timezone.utc)
    # The cash leg (^GSPC/QQQ/IWM) is the one that goes stale outside US RTH.
    # Futures trade 23h so their freshness is rarely an issue, but check anyway
    # for symmetry — a truly broken yfinance quote should also surface.
    if compare_ts is None:
        return QuoteResult(None, f"{rule.compare_yf} 報價無時間戳")
    compare_age = (now - compare_ts).total_seconds()
    if compare_age > _MAX_STALE_SECONDS:
        return QuoteResult(
            None,
            f"{rule.compare_yf} 報價凍結 {compare_age/60:.1f}min（cash 盤外無更新；請於 US RTH 跑或切 tv_legend）",
        )
    if futures_ts is not None:
        futures_age = (now - futures_ts).total_seconds()
        if futures_age > _MAX_STALE_SECONDS:
            return QuoteResult(None, f"{rule.futures_yf} 報價凍結 {futures_age/60:.1f}min")
    value = compute(rule, futures, compare)
    if value is None:
        return QuoteResult(None, f"compute({rule.op}) 失敗")
    return QuoteResult(value)


def _formula_string(rule: FuturesRule) -> str:
    op = "-" if rule.op == "diff" else "/"
    return f"{rule.futures_tv}{op}{rule.compare_tv}"


def _formula_chart_url(rule: FuturesRule) -> str:
    return "https://www.tradingview.com/chart/?symbol=" + urllib.parse.quote(_formula_string(rule))


async def _tv_legend_compute(rule: FuturesRule, automator) -> QuoteResult:
    """Open a temporary TV tab with the formula symbol, parse price from page title.

    Why the title (not the legend DOM): when the user's saved layout has the
    formula as a subchart, the standard ``[data-name='legend-source-item-main-series']``
    selector returns nothing. But TradingView always renders
    ``"<short_formula> <price> ▲/▼ <change>% …"`` as the page title once the
    formula resolves — much more stable across layout variations.
    """
    if automator is None or automator._context is None:  # noqa: SLF001
        return QuoteResult(None, "tv_legend 需要 active automator + CDP context")
    context = automator._context  # noqa: SLF001
    short = _formula_short_token(rule)
    formula = _formula_string(rule)
    url = _formula_chart_url(rule)
    page = None
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        last_title = ""
        # Poll until the title contains our formula short form AND a parseable
        # price; total budget ~10s. TV usually fills the title within 1-2s.
        for _ in range(40):
            try:
                last_title = await page.title()
            except Exception:
                last_title = ""
            t = last_title or ""
            if short in t or formula in t:
                price = _parse_price_from_title(t)
                if price is not None:
                    return QuoteResult(price)
            await page.wait_for_timeout(250)
        # Fallback: if short form never matched (TV used a different alias),
        # try parsing the final title anyway.
        price = _parse_price_from_title(last_title)
        if price is not None:
            return QuoteResult(price)
        return QuoteResult(
            None,
            f"tv_legend 讀不到 {short} 的 title 數值（title={last_title!r}）",
        )
    except Exception as exc:  # noqa: BLE001
        return QuoteResult(None, f"tv_legend 失敗：{exc}")
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


def _formula_short_token(rule: FuturesRule) -> str:
    """The bare formula form TV renders in the page title (no exchange prefixes).

    e.g. ``CME_MINI:ES1!-FOREXCOM:SPX500`` → ``ES1!-SPX500``.
    """
    op = "-" if rule.op == "diff" else "/"
    fut = rule.futures_tv.split(":", 1)[-1]
    cmp_ = rule.compare_tv.split(":", 1)[-1]
    return f"{fut}{op}{cmp_}"


# TradingView page title format when a formula is the chart symbol:
#   "ES1!-SPX500 20.25 ▲ +2.37% <layout title…>"
#   "RTY1!/IWM 10.0 ▼ −0.5% …"
# The price always sits right before the trend arrow (▲ / ▼ / △ / ▽), so
# anchor on the arrow to avoid grabbing the "1" in "ES1!" or the change-%.
_TITLE_PRICE_RX = re.compile(r"(-?\d+(?:\.\d+)?)\s*[▲▼△▽⏶⏷↑↓]")


def _parse_price_from_title(title: str) -> float | None:
    if not title:
        return None
    m = _TITLE_PRICE_RX.search(title)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None



async def fetch_value(
    rule: FuturesRule,
    source: QuoteSource = "yfinance",
    *,
    automator=None,
) -> QuoteResult:
    """Return a QuoteResult — caller handles ``value is None`` via ``reason``."""
    if source == "yfinance":
        return await asyncio.to_thread(_yf_compute, rule)
    if source == "tv_legend":
        if automator is None:
            return QuoteResult(None, "tv_legend 需要 active automator")
        return await _tv_legend_compute(rule, automator)
    return QuoteResult(None, f"unknown quote source: {source}")


def format_value(value: float, kind: str) -> str:
    """TV-style formatting: Offset 2dp, Ratio 4dp."""
    if kind == "Offset":
        return f"{value:.2f}"
    return f"{value:.4f}"


def is_nondefault(raw: str | None, kind: str) -> bool:
    """True if `raw` looks like a user-modified value (not at TV's default)."""
    if raw is None or not str(raw).strip():
        return False
    try:
        f = float(str(raw).strip())
    except ValueError:
        return True
    default = 1.0 if kind == "Ratio" else 0.0
    return abs(f - default) > 1e-9


if __name__ == "__main__":  # manual smoke: `python -m gex_suite.modules.tradingview.quote_source`
    import sys

    async def _main() -> None:
        for (root, mode), (rule, kind) in RULES.items():
            r = await fetch_value(rule, "yfinance")
            shown = f"{r.value}" if r.value is not None else f"None ({r.reason})"
            print(f"{root:6s} [{mode:6s}] {kind:6s} = {shown}")

    asyncio.run(_main())
    sys.exit(0)
