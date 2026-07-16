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

When `is_futures_alias=True` is returned (any of the three modes that hit the alias map), `WorkItem.is_futures` is set to `True` and the indicator's **Start date (Monday)** shifts to Sunday with time `_FUTURES_START_TIME` (= `"18:00"`). This is **purely about the chart's X-axis** (which is futures bars regardless of which DATA we feed it), not about the data source.

DB lookup still uses `item.monday` (the trading week's Monday). Only the TV-side write-and-verify uses the shifted `indicator_date`.

**Start (date, time) resolution is centralized in `_resolve_indicator_start`** (widget.py) — both computation sites (`_apply_work_item_with_automator` and the cache/verify path in `_phase_b_scan_flow`) go through it; don't reintroduce inline date math. Priority: (1) `start_time_tz_rules` in `auto_paste_config.json` (`shared/config.get_tradingview_tz_start`) — start defined in the instrument's **local timezone** and converted to America/New_York, e.g. NK2251! = Sunday 17:00 Asia/Seoul, KOSPI200 = Monday 09:00 Asia/Seoul (dates can roll back to Sunday; DST-aware); (2) futures alias → Sunday 18:00; (3) `start_time_rules` per-ticker HH:MM on Monday.

### Per-layout dedup uses `(symbol, mode)` tuple

`matched_keys_in_layout` stores `(chosen.upper(), layout_mode)`. This allows the same symbol to be processed multiple times in a single layout under different modes (e.g. `ES1! [equity] + ES1! [index]` runs both). Don't revert this to a plain symbol set.

There is no cross-layout dedup. A previous version had `seen_runtime_keys` that prevented the same `(ticker, monday, symbol)` from being processed in a second layout. **This was a bug** and has been removed — each layout's subcharts must be processed independently because TV-side state (the indicator on that particular chart) is per-layout.

### CME importer suffix logic ([importers.py](gex_suite/modules/chart/importers.py))

`import_txt_files()` detects CME source by checking if any **path component** is exactly `CME` (case-insensitive). Matches the scraper's output convention `download_folder/CME/TV Code/TV_Codes_*.txt`. CME-detected files run through `_make_cme_aware_insert()` which wraps the inserter to suffix `<root>` → `<root>1!` (idempotent).

`force_source="cme"` parameter overrides path detection. Excel/Google importers don't have this yet — add similar wrapping if the user starts using those for CME.

The parser (`gex_parser.parse_gex_code`) extracts ticker from the TV Code body via `[A-Za-z][A-Za-z0-9\.]*:` regex (開頭須字母、其後可含數字), so it produces `ES` from `ES:...` and `KOSPI200` from `KOSPI200:...`. The suffix transformation happens after parsing.

**Per-ticker index exception**: tickers in `_CME_INDEX_TICKERS` (importers.py, e.g. `KOSPI200`) are index products scraped from the CME platform — they are exempt from the `1!` suffix even when the file path is CME-detected. Add new CME-platform index products to that set.

## TO FUTURE 自動填入 (TradingView)

When `(futures_root, layout_mode)` hits [`quote_source.RULES`](gex_suite/modules/tradingview/quote_source.py), the indicator-properties-dialog is **not** closed immediately after `fill_weekly_levels` — `_maybe_fill_to_future` (in [widget.py](gex_suite/modules/tradingview/widget.py)) runs first to write today's column of the TO FUTURE section. Both writes are persisted in the same save click.

Strict trigger guards (all must hold; failing any short-circuits silently except for weekend which logs once):

1. Subchart symbol's tail (e.g. `NQ1!` from `CME_MINI:NQ1!`) + resolved `layout_mode` are in `RULES`.
2. `row_monday == 本機今日所在週一` — only the current week's indicator row is touched.
3. Today is Mon~Fri (Sat/Sun → `【略過｜週末】` log, no DOM read).
4. Both `Daily <kind>` and `<today_weekday> <kind>` are still at TV defaults (Ratio=1 / Offset=0). Any non-default → `【略過｜TO FUTURE 已有值】`.

Rules table maps each `(futures_root, mode)` to a `FuturesRule` (yfinance + TV symbol pairs) and a target field kind:

| key | field kind | yfinance |
|---|---|---|
| `("ES1!", "index")`  | Offset | `ES=F − ^GSPC` |
| `("NQ1!", "equity")` | Ratio  | `NQ=F / QQQ`   |
| `("RTY1!", "equity")`| Ratio  | `RTY=F / IWM`  |

**Adding a rule**: extend `quote_source.RULES`; no widget/automator changes needed. The DOM helpers (`automator.read_to_future_value` / `fill_to_future_value`) accept any `(day, kind)` in `{Daily, Monday..Friday} × {Ratio, Offset}`.

**Quote source**: `auto_paste_config.json` → `futures_quote_source`.

- `"yfinance"` (default): uses `Ticker.history(1d, 1m)` for both legs so we get a timestamp alongside the price. **Cash legs (`^GSPC` / `QQQ` / `IWM`) only update during US RTH (09:30–16:00 ET = 21:30–04:00 TPE during DST)** — the freshness guard in `_yf_compute` rejects the result if the cash bar is more than 15 minutes old, surfacing `【略過｜TO FUTURE 報價失敗】... 報價凍結 …min`. Run during RTH or switch source.
- `"tv_legend"`: opens a hidden new tab via the same CDP context with the formula symbol (e.g. `CME_MINI:ES1!-FOREXCOM:SPX500`), polls for the main-series legend's last value via `page.evaluate`, then closes the tab. Works 24/5 because `FOREXCOM:SPX500` / `BATS:QQQ` / `AMEX:IWM` are all TV-live; pre-market / post-market values will then match what you'd manually read off TV. Trade-off: ~1–2s per rule and a brief tab flash (user-visible in the browser).

`fetch_value` returns a `QuoteResult(value, reason)` — `value is None` always carries a non-empty `reason` so the log line says exactly *why* (stale cash, no legend value, formula parse error, …). Both source paths funnel through the same widget log catalog.

**Why "current week only"**: the same indicator script is instantiated once per week (each weekly row is its own indicator). Ratio/Offset is a chart-display alignment value computed from *current* spot prices, so back-filling past weeks would be meaningless.

## Skip-reason log catalog (TradingView scan flow)

Every silent `continue` in `_phase_b_scan_flow` should produce a log line. Current categories:

| log tag | trigger |
|---|---|
| `【略過版面】無法載入` | `automator.load_layout()` returned False (non-first layout) |
| `【警告】版面…無法取得子圖清單` | `_enumerate_subcharts_with_retry` returned empty |
| `【略過｜公式圖】` | Subchart symbol is a TV formula (e.g. `ES1!-SPX500`) — multi-ticker arithmetic combo, not a single instrument |
| `【略過｜alias 缺項】` | Symbol is in `_FUTURES_ALIAS_MAP` but the resolved mode has no mapping |
| `【略過｜未匹配】` | Symbol can't be parsed to a ticker (or scope=ticker mismatch and not an alias) |
| `【略過｜重複】` | `(chosen, layout_mode)` already processed in this layout |
| `【略過｜資料庫】` | `db.fetch_tv_codes_for_week()` returned all-None (DB missing ticker or week) |
| `【略過｜快取】` | Cache scan shows the week's fillable cells already have values |
| `【預覽｜快取】` | Cache scan shows partial fill needed (dry-run only) |
| `【預覽】` | Dry-run write would happen |
| `【略過｜指標配額】` | TradingView indicator quota exceeded |
| `【更新 TO FUTURE】` | TO FUTURE Ratio/Offset written successfully for today |
| `【預覽｜TO FUTURE】` | Dry-run: TO FUTURE write would happen |
| `【中止｜未登入】` | `automator.connect()` 後 TradingView 登入檢查失敗（profile 無 `sessionid` cookie）— 中止前自動在「正確的」CDP instance 開一個 TradingView 分頁標示要登入的視窗（多開瀏覽器時使用者常登到錯的 instance），fail fast，整個 flow 中止 |
| `【CDP 自癒】` | connect 前偵測到殭屍 CDP 瀏覽器（0 個 page target、profile 已卸載）→ `PUT /json/new` 開頁復原＋等 5s hydration |
| `【略過｜週末】` | Today is Sat/Sun — TO FUTURE auto-fill skipped |
| `【略過｜TO FUTURE 已有值】` | Daily or today's Ratio/Offset already non-default |
| `【略過｜TO FUTURE 報價失敗】` | yfinance returned None for futures or compare leg |
| `【略過｜TO FUTURE 等於 default】` | Computed value ≈ default (Ratio≈1 / Offset≈0) — no-op write avoided |

If you add a new silent skip, add a corresponding log line — historical pattern is "every skip explains itself".

## 版面分組（layout groups）— TradingView 頁第三分頁

Files: `layout_groups.py`（model/persistence，無 Qt）、`app_launcher.py`（桌面 app
CDP 驅動，無 Qt）、`groups_tab.py`（UI）、`qt_async.py`/`browser_paths.py`（自
widget.py 搬出的共用件）。資料檔 `data/tradingview/layout_groups.json`。
一組＝一視窗、組內版面＝分頁；App 與瀏覽器兩種開啟模式。

**版面清單 = 掃描快取（`scanned_layouts`），三處會更新同一份檔案**：

- 分組頁「掃描版面」：`groups_tab._scan_layouts_coro` 逐版面 `load_layout` →
  `enumerate_subcharts` 讀每張子圖 header symbol 當標題（`subchart_title` 取冒號
  尾碼、公式圖原樣）。9222 沒開會自動 `launch_cdp_browser`；可中途停止。
- 批次貼上（GUI）＋每日排程（`cli.py` 重用同一個 `_phase_b_scan_flow`）：流程
  收尾呼叫 `widget._flush_layout_groups_cache`，把掃圖時看到的版面（含子圖標題）
  同步進快取。
- 同步語意在 `layout_groups.apply_scan_results`：`full=True`（scope=all 且完整
  跑完）整批替換快取、且**群組內 `source=="scan"` 而版面已消失者一併移除**
  （`source=="manual"` 永遠保留）；`full=False`（scope=urls/active/中止/降級）
  只 upsert 不刪。子圖迴圈沒跑完的版面 `complete=False` → 不覆蓋舊子圖清單。
- UI 顯示與 picker filter 都用 `GroupLayout.display_label()`／`matches_filter()`
  （名稱＋子圖標題，不顯示 chart id；filter 可用子圖標題篩）。`groups_tab`
  的 `showEvent` 會 reload，撿起外部行程（每日 CLI、批次貼上）寫的最新快取。

**資料檔位置可自訂**（分組頁底部「設定檔：… / 變更… / 還原預設」）：

- 生效路徑由 `layout_groups.layout_groups_path()` 解析：行程覆寫
  （`set_path_override`，只給測試）> `suite_config.json` 的 `layout_groups_path`
  鍵（`get/set_configured_path`）> 內建 `TRADINGVIEW_LAYOUT_GROUPS_PATH`。
  `load/save_layout_groups`、`apply_scan_results_to_disk` 全走解析器，所以 GUI
  與每日排程 CLI 讀寫同一份檔（把兩機的 GUI 都指到同一個雲端/同步資料夾即可
  跨機共用群組）。
- 變更＝選一個既有的 json 檔（`QFileDialog.getOpenFileName`），只改指向、不搬移
  現有檔；典型用法是指到雲端/同步資料夾裡那份 `layout_groups.json`。
- `suite_config.json` 是 per-machine（gitignored），自訂路徑不會自動同步到另一台，
  需在該機 GUI 各自設定一次（或指向同一個已同步的資料夾）。

**TradingView 桌面 app 自動化 — 實測結論（3.3.0，勿走回頭路）**：

- app 接受 `--remote-debugging-port` → 全功能 CDP。開新視窗＝對任一 page target
  `Input.dispatchKeyEvent` Cmd+N（modifiers=4）；開分頁＝在圖表 tab 裡
  `Runtime.evaluate("window.open(url)")`（app 攔截成原生分頁）；新視窗的預設
  new-tab 分頁用 `location.href` 就地導航（不留空分頁）。
- **file:// → https 導航會換 CDP targetId** — 導航後要用 URL 片段或
  「新出現的 chart target」重新尋標（`app_launcher.open_group_in_app`）。
- 死路：`open -a TradingView <url>` 只 activate（macOS open-url handler 只餵
  AuthenticationHandler）；`tradingview://` scheme 同樣只做登入 redirect；
  AX/System Events 點原生選單（New window、Open link from clipboard）點得到
  但 Electron handler 不會觸發。
- app 未帶 debug port 時 `ensure_app_cdp` 會自動 quit（Apple Event）＋
  `open -a … --args --remote-debugging-port=9333` 重啟；視窗分頁由 app 自行還原。

**瀏覽器模式**：同 `--user-data-dir`（`$TMPDIR/gex_tv_cdp_profile`）argv
forwarding —— `Popen([bin, --user-data-dir=…, --new-window, url1, url2…])` 由
既有 instance 開一個新視窗、URL 依序成分頁；冷啟補 9222 debug 旗標＋
`--no-first-run`。不可用 Playwright `ctx.new_page()`（tab 會落在既有視窗）。
**9222 已被別的 instance 佔住時，`launch_cdp_browser` 不會冷啟第二個**（第二
個綁不到 127.0.0.1 只會默默綁 `[::1]` 成假 9222，paste 連不到），改用
`PUT /json/new` 在既有 instance 開分頁 — 此時「一組一視窗」語意降級為
「分頁落在該 instance 最後使用的視窗」。

CLI 探測（不開 GUI）：`python -m gex_suite.modules.tradingview.app_launcher <url>…`。

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
