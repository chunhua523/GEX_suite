"""Planning/execution helpers for TradingView auto-paste batch workflow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Awaitable, Callable, Iterable, Literal

from gex_suite.shared import db


LayoutScopeMode = Literal["active", "all"]
TickerScopeMode = Literal["all", "ticker"]
WeeksMode = Literal["this_week", "last_4_weeks"]


@dataclass(frozen=True)
class BatchOptions:
    layout_scope: LayoutScopeMode = "all"
    ticker_scope: TickerScopeMode = "all"
    ticker: str | None = None
    weeks: WeeksMode = "this_week"
    skip_filled_days: bool = True
    apply_visibility_preset: bool = True
    organize_indicators: bool = False
    dry_run: bool = False  # preview: same scan path, no chart/DB writes
    market_open_time: str = "04:00"

    @property
    def scope(self) -> TickerScopeMode:
        """Backward-compatible alias for legacy call-sites."""
        return self.ticker_scope


@dataclass(frozen=True)
class WorkItem:
    ticker: str
    monday: date
    codes: dict[str, str | None]
    available_days: list[str]
    layout_id: str | None = None
    layout_name: str | None = None
    subchart_index: int | None = None
    subchart_symbol: str | None = None
    chart_url: str | None = None
    note: str | None = None
    preview_status: str | None = None
    is_futures: bool = False  # alias-mapped futures (e.g. ES1! → SPX); shifts indicator start to Sun 18:00


@dataclass(frozen=True)
class BatchResultItem:
    item: WorkItem
    status: Literal["done", "skipped", "failed"]
    message: str = ""


@dataclass(frozen=True)
class BatchReport:
    total: int
    done: int
    skipped: int
    failed: int
    items: list[BatchResultItem]


def compute_target_mondays(mode: WeeksMode, today: date | None = None) -> list[date]:
    d = today or date.today()
    this_monday = d - timedelta(days=d.weekday())
    if mode == "this_week":
        return [this_monday]
    if mode == "last_4_weeks":
        return [this_monday - timedelta(days=7 * i) for i in range(4)]
    raise ValueError(f"Unsupported weeks mode: {mode}")


def build_workplan(
    opts: BatchOptions,
    *,
    today: date | None = None,
    ticker_source: Callable[[], Iterable[str]] | None = None,
) -> list[WorkItem]:
    """Build db-backed plan; only weeks with >=1 available day are kept."""
    if opts.ticker_scope == "ticker":
        if not opts.ticker:
            return []
        tickers = [opts.ticker.strip().upper()]
    else:
        source = ticker_source or db.get_all_tickers
        tickers = [str(t).strip().upper() for t in source() if str(t).strip()]

    mondays = compute_target_mondays(opts.weeks, today=today)
    items: list[WorkItem] = []

    for ticker in tickers:
        for monday in mondays:
            codes = db.fetch_tv_codes_for_week(ticker=ticker, monday=monday)
            available_days = [day for day, code in codes.items() if code]
            if not available_days:
                continue
            items.append(
                WorkItem(
                    ticker=ticker,
                    monday=monday,
                    codes=codes,
                    available_days=available_days,
                )
            )
    return items


def pick_phase_a_item(
    ticker: str,
    *,
    allow_fallback: bool = True,
    today: date | None = None,
) -> tuple[WorkItem | None, bool]:
    """Pick first usable work item for active-chart Phase A.

    Returns `(item, used_fallback)`.
    """
    normalized = ticker.strip().upper()
    if not normalized:
        return None, False

    current_items = build_workplan(
        BatchOptions(ticker_scope="ticker", ticker=normalized, weeks="this_week"),
        today=today,
    )
    if current_items:
        return current_items[0], False
    if not allow_fallback:
        return None, False

    all_rows = db.fetch_tv_codes(normalized)
    candidate_mondays: list[date] = []
    for _ticker, date_str, _code in all_rows:
        try:
            day = date.fromisoformat(str(date_str)[:10])
        except ValueError:
            continue
        monday = day - timedelta(days=day.weekday())
        if monday not in candidate_mondays:
            candidate_mondays.append(monday)

    for monday in candidate_mondays:
        codes = db.fetch_tv_codes_for_week(ticker=normalized, monday=monday)
        available = [day for day, code in codes.items() if code]
        if available:
            return WorkItem(
                ticker=normalized,
                monday=monday,
                codes=codes,
                available_days=available,
                note="fallback_latest_available_week",
            ), True
    return None, True


async def run_batch(
    items: list[WorkItem],
    runner: Callable[[WorkItem], Awaitable[BatchResultItem]],
    progress_cb: Callable[[int, int, WorkItem], None] | None = None,
) -> BatchReport:
    """Run work items sequentially with progress callback."""
    results: list[BatchResultItem] = []
    total = len(items)
    for idx, item in enumerate(items, start=1):
        if progress_cb:
            progress_cb(idx, total, item)
        try:
            result = await runner(item)
        except Exception as exc:  # noqa: BLE001 - keep batch resilient
            result = BatchResultItem(item=item, status="failed", message=str(exc))
        results.append(result)

    done = sum(1 for r in results if r.status == "done")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    return BatchReport(
        total=total,
        done=done,
        skipped=skipped,
        failed=failed,
        items=results,
    )
