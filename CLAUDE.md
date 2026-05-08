# CLAUDE.md

Project-level guidance for Claude when working in this repo. Read this file before editing any of the modules listed below — it captures invariants and conventions that aren't obvious from the code alone.

## Quick architecture map

- `gex_suite/app/` — PySide6 main window, sidebar, theme.
- `gex_suite/shared/` — DB layer (`db.py`), config (`config.py`), paths.
- `gex_suite/modules/scraper/` — Lieta Research / CME Playwright scraper. **Backend (`runner.py`) is high-risk; do not refactor casually.** GUI shell is fair game.
- `gex_suite/modules/chart/` — TV Code parser, importers (TXT/Excel/Google), Plotly chart, OHLC fetcher.
- `gex_suite/modules/tradingview/` — Playwright CDP automation against a user-launched browser.
  - `automator.py` — DOM-level Playwright operations (~5000 lines, treat as black box unless surgically needed).
  - `engine.py` — pure logic (BatchOptions, WorkItem, week math).
  - `widget.py` — UI + scan/preview/cleanup orchestration.

## TradingView module — futures / equity / index 三模式

Most non-trivial work in this repo recently has been in `widget.py`. The mental model:

### Alias map structure ([`_FUTURES_ALIAS_MAP` in widget.py](gex_suite/modules/tradingview/widget.py))

```python
"ES1!": {"futures": "ES1!", "equity": "SPY", "index": "SPX"}
```

Each TradingView continuous-futures symbol (tail after `:` in subchart symbol) maps to **three possible DB tickers**:

- `futures` — the futures' own TV Code data (CME-imported into DB with `1!` suffix).
- `equity` — the related ETF or equity.
- `index` — the underlying index.

A `None` entry means "no DB ticker exists for this mode" → strict skip. **Adding new entries does not require those tickers to be in DB**; the `db.fetch_tv_codes_for_week()` empty-return path will handle missing tickers and log `【略過｜資料庫】`. So fill the alias map forward-looking when adding new futures roots.

Default mode is `equity` (configurable via `_FUTURES_DEFAULT_MODE`).

### Layout-name markers ([`_LAYOUT_MODE_MARKERS`](gex_suite/modules/tradingview/widget.py))

Case-insensitive substring match. Multiple aliases per mode supported (e.g. `[etf]` and `[equity]` both → `equity`).

Per-subchart resolution rule (in `_resolve_layout_mode_for_subchart`):

| markers in name | behavior |
|---|---|
| 0 | every subchart uses default mode |
| 1 | layout-level — that mode for every subchart |
| 2+ | positional — i-th marker → i-th subchart; subcharts past marker count fall back to default |

This rule is intentional: single-marker = "whole layout in this mode" matches user mental model, while multi-marker enables `ES1! [equity] + ES1! [index] + ES1! [future]` style fan-out.

### Resolver invariant ([`_resolve_target_ticker_for_subchart`](gex_suite/modules/tradingview/widget.py))

Returns `(target_ticker, is_futures_alias)`. Strict mode: when the symbol matches an alias entry (`_futures_alias_lookup` returns non-None) but the requested mode has no mapping, returns `(None, False)`. **Does not** silently fall through to `_extract_ticker_from_symbol`, because the user has been clear about wanting silent skips to surface as log entries.

The caller distinguishes "alias known, mode unmapped" (strict skip log `【略過｜alias 缺項】`) from "symbol unknown" (`【略過｜未匹配】`) by re-checking `_futures_alias_lookup` after the resolver returns None.

### `is_futures_alias` flag → indicator anchor shift

When `is_futures_alias=True` is returned (any of the three modes that hit the alias map), `WorkItem.is_futures` is set to `True` and `_apply_work_item_with_automator` shifts the indicator's **Start date (Monday)** to Sunday and the time to `_FUTURES_START_TIME` (= `"18:00"`). This is **purely about the chart's X-axis** (which is futures bars regardless of which DATA we feed it), not about the data source.

DB lookup still uses `item.monday` (the trading week's Monday). Only the TV-side write-and-verify uses the shifted `indicator_date`.

### Per-layout dedup uses `(symbol, mode)` tuple

`matched_keys_in_layout` stores `(chosen.upper(), layout_mode)`. This allows the same symbol to be processed multiple times in a single layout under different modes (e.g. `ES1! [equity] + ES1! [index]` runs both). Don't revert this to a plain symbol set.

There is no cross-layout dedup. A previous version had `seen_runtime_keys` that prevented the same `(ticker, monday, symbol)` from being processed in a second layout. **This was a bug** and has been removed — each layout's subcharts must be processed independently because TV-side state (the indicator on that particular chart) is per-layout.

### CME importer suffix logic ([importers.py](gex_suite/modules/chart/importers.py))

`import_txt_files()` detects CME source by checking if any **path component** is exactly `CME` (case-insensitive). Matches the scraper's output convention `download_folder/CME/TV Code/TV_Codes_*.txt`. CME-detected files run through `_make_cme_aware_insert()` which wraps the inserter to suffix `<root>` → `<root>1!` (idempotent).

`force_source="cme"` parameter overrides path detection. Excel/Google importers don't have this yet — add similar wrapping if the user starts using those for CME.

The parser (`gex_parser.parse_gex_code`) extracts ticker from the TV Code body via `[A-Za-z\.]+:` regex, so it produces `ES` from `ES:...`. The suffix transformation happens after parsing.

## Skip-reason log catalog (TradingView scan flow)

Every silent `continue` in `_phase_b_scan_flow` should produce a log line. Current categories:

| log tag | trigger |
|---|---|
| `【略過版面】無法載入` | `automator.load_layout()` returned False (non-first layout) |
| `【警告】版面…無法取得子圖清單` | `_enumerate_subcharts_with_retry` returned empty |
| `【略過｜alias 缺項】` | Symbol is in `_FUTURES_ALIAS_MAP` but the resolved mode has no mapping |
| `【略過｜未匹配】` | Symbol can't be parsed to a ticker (or scope=ticker mismatch and not an alias) |
| `【略過｜重複】` | `(chosen, layout_mode)` already processed in this layout |
| `【略過｜資料庫】` | `db.fetch_tv_codes_for_week()` returned all-None (DB missing ticker or week) |
| `【略過｜快取】` | Cache scan shows the week's fillable cells already have values |
| `【預覽｜快取】` | Cache scan shows partial fill needed (dry-run only) |
| `【預覽】` | Dry-run write would happen |
| `【略過｜指標配額】` | TradingView indicator quota exceeded |

If you add a new silent skip, add a corresponding log line — historical pattern is "every skip explains itself".

## Stop button (Preview / Scan / Cleanup)

`_cancel_batch_scan` flag is read at multiple checkpoint sites in `_phase_b_scan_flow`. To wire Stop to a new flow:

1. In the entry handler (`_on_phase_b_*`): set `self._cancel_batch_scan = False` and `self.b_stop.setEnabled(True)`.
2. In the finished handler: set `self.b_stop.setEnabled(False)` and reset `self._cancel_batch_scan = False`.
3. The flow itself must already poll `self._batch_should_stop()` at iteration boundaries.

`_phase_b_scan_flow` already has the right checkpoints. Preview reuses scan via `_phase_b_preview_flow(opts)` which forwards `dry_run=True` — so wiring Stop in preview = wiring start/finish handlers, the inner flow checks already exist.

## Naming + style conventions

- **Mode names**: `"futures"` / `"equity"` / `"index"` — these strings appear as dict keys and in log output. Don't rename without updating both the alias map and `_LAYOUT_MODE_MARKERS`.
- **Marker strings**: English-only, square-bracketed (`[fut]`, `[etf]`, `[ix]`). No Chinese markers (user explicitly removed them). Multiple aliases per mode are fine.
- **Logs**: 中文 + half-width bracket sentinel `【…】`. Format:
  ```
  【類別｜子類別】版面=... URL=... 子圖#N ...
    原因：...
  ```
  Two-space indent for the reason line. Keep this style consistent.
- **DB tickers**: stored uppercase. Always `.upper()` user input before DB lookups.

## Don't-do list

- Don't add silent fall-through paths in the resolver. If a symbol is identified as a futures alias and the mode has no mapping, it must surface as a log entry.
- Don't re-introduce cross-layout dedup. Each layout's subchart needs independent processing.
- Don't add Chinese layout markers (the design is English-only now).
- Don't migrate existing DB rows when changing the importer suffix logic — only new imports get the `1!` suffix; legacy rows stay as-is.
- Don't add a `merge equity into futures` fallback — the user wants strict separation between modes so they can compare side-by-side.

## Testing

```bash
python tests/smoke_test.py
```

Runs with `QT_QPA_PLATFORM=offscreen`; verifies modules import and widgets construct. There are no unit tests for the alias resolver or marker parser yet — when adding logic, consider adding cases under `tests/` (the smoke test won't catch routing bugs).
