# GEX Suite

整合 `GEX_scraper`（Lieta Research / CME 自動爬蟲）與 `GEX_tool`（GEX 資料庫 + 圖表）為單一 PySide6 桌面應用；**TradingView** 分頁可透過 Chrome／Brave 的 **CDP** 將 DB 中的 TV code 批次寫入圖表上的 **Daily & Weekly GEX** 指標。

## 功能分頁

| 分頁 | 對應原專案 | 重點 |
|---|---|---|
| **Scraper** | `GEX_scraper/gui.py` | Playwright 登入 / 排程 / 抓取 Standard + CME 模型（Gamma, Delta, Theta, Term, Smile, Levels, Table, TV Code） |
| **GEX Chart** | `GEX_tool/GEX_chart_new.py` | 解析 TV Code → SQLite → 用 Plotly 畫 GEX Levels + OHLC |
| **TradingView Auto-Paste** | _new_ | 從 SQLite 讀 TV code；以 Playwright `connect_over_cdp` 連本機瀏覽器，批次掃版面／子圖並填入／更新 GEX 指標 |

## 安裝

```bash
cd GEX_suite
python -m venv .venv
.\.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
playwright install chromium
```

> 第一次跑會自動下載 Chromium，請預留幾分鐘。

## 啟動方式

### 1) 主應用（含三個分頁）

```bash
python main.py
```

### 2) 單獨打開某一個工具

每個子模組都自帶 `__main__.py`，可直接以模組執行：

```bash
python -m gex_suite.modules.scraper        # 只開 Scraper 視窗
python -m gex_suite.modules.chart          # 只開 GEX Chart 視窗
python -m gex_suite.modules.tradingview    # 只開 TradingView 分頁視窗
```

### 3) Headless / 排程：Scraper CLI

```bash
python -m gex_suite.modules.scraper.cli --tv-code-only --headless
python -m gex_suite.modules.scraper.cli --models "TV Code,Gamma" --groups "Index,科技股"
python -m gex_suite.modules.scraper.cli --dry-run
```

CLI 行為與旗標完全沿用 `GEX_scraper/cli.py`。

## 檢查更新（GitHub）

1. 編輯 `gex_suite/data/suite_config.json`，填入例如：`update_github_user`、`update_github_repo`、`update_github_branch`（預設會抓儲存庫根目錄的 `pyproject.toml`，路徑可改 `update_remote_pyproject_path`）。
2. 在應用程式選單 **說明 → 檢查更新**。
3. 若顯示有新版且 `GEX_suite` 專案根目錄為 git clone（該目錄含 `.git`），可選 **執行 git pull（--ff-only）**。

## 打包（PyInstaller）

```bash
pip install -r requirements-build.txt
pyinstaller scripts/gex_suite.spec
```

產物在 `dist/GEXSuite/`。macOS 可再自行：`hdiutil create -volname "GEX Suite" -srcfolder dist/GEXSuite -ov -format UDZO GEXSuite.dmg`。凍結版未內建 Playwright 瀏覽器下載路徑，Scraper／TradingView CDP 流程請以原始碼環境或另行設定為主。

## 目錄結構

```
GEX_suite/
├── main.py                              # 主入口
├── requirements.txt
├── requirements-build.txt               # PyInstaller（可選）
├── pyproject.toml
├── scripts/
│   └── gex_suite.spec                   # PyInstaller one-folder 設定
├── gex_suite/
│   ├── app/
│   │   ├── main_window.py               # QMainWindow + Sidebar + StackedWidget
│   │   └── theme.py                     # Fusion + dark palette
│   ├── shared/
│   │   ├── paths.py                     # 統一檔案位置
│   │   ├── config.py                    # ~/data/suite_config.json
│   │   └── db.py                        # SQLite 共用層
│   ├── modules/
│   │   ├── scraper/
│   │   │   ├── runner.py                # LietaScraper（從 GEX_scraper/scraper.py 搬）
│   │   │   ├── utils.py                 # ticker JSON / 路徑工具
│   │   │   ├── cli.py                   # headless 排程
│   │   │   ├── widget.py                # ScraperPage(QWidget)
│   │   │   ├── ticker_manager_dialog.py # 群組 + ticker 管理（舊版 GUI 對齊）
│   │   │   ├── file_viewer.py           # View Scraped Files
│   │   │   └── __main__.py
│   │   ├── chart/
│   │   │   ├── parser.py                # parse_gex_code / extract_date_from_tv_code
│   │   │   ├── importers.py             # TXT / Excel / Google Sheet 匯入
│   │   │   ├── ohlc.py                  # yfinance OHLC 補資料
│   │   │   ├── plot.py                  # Plotly Figure 生成
│   │   │   ├── widget.py                # ChartPage(QWidget) + QWebEngineView
│   │   │   └── __main__.py
│   │   └── tradingview/
│   │       ├── automator.py             # PlaywrightCDPAutomator（CDP、版面、子圖、指標）
│   │       ├── engine.py                # 批次選項與週一計算等純邏輯
│   │       ├── widget.py                # TradingViewPage（批次 + 手動）
│   │       └── __main__.py
│   └── data/                            # 預設資料目錄（DB、設定、state、logs）
│       ├── stocks.db
│       ├── service_account.json         # 不要 commit
│       ├── tradingview/                 # TV 批次設定、除錯 dump
│       └── scraper/
│           ├── settings.json
│           ├── state.json
│           ├── tickers_index.json
│           └── logs/
└── tests/
    └── smoke_test.py
```

## 共享資源

不論主視窗還是 `python -m ...` 單啟動：

- **DB** 都指到 [gex_suite/data/stocks.db](gex_suite/data/stocks.db)（從 `GEX_tool/stocks.db` 搬入）
- **登入 state** 都指到 [gex_suite/data/scraper/state.json](gex_suite/data/scraper/state.json)
- **設定** Scraper 用 [gex_suite/data/scraper/settings.json](gex_suite/data/scraper/settings.json)；全域用 [gex_suite/data/suite_config.json](gex_suite/data/suite_config.json)；TradingView 批次選項與 CDP URL 用 [gex_suite/data/tradingview/auto_paste_config.json](gex_suite/data/tradingview/auto_paste_config.json)（首次執行會自動建立）

## 與舊專案的差異 / 取捨

- **GUI 框架**：原本 `customtkinter`（scraper）+ `ttkbootstrap`（tool）→ 統一改用 PySide6。
- **Scraper 後端不動**：`scraper.py` 的 Playwright 互動邏輯是高風險區，**完整保留**。只重寫 GUI 殼。
- **Chart 邏輯純函式化**：拆出 `parser.py`、`importers.py`、`ohlc.py`、`plot.py`，UI 與資料層解耦。
- **TradingView**：已支援 CDP 連線與批次流程（多版面／多子圖、本週或最近四週、預覽表與 log）。請先在 TradingView 將 **Daily & Weekly GEX by daniel56_trade** 加入 **Favorites（★）**，否則無法從選單新增該指標。若帳戶達指標數量上限，批次會將該筆記為 **略過（skip_quota）** 並繼續其餘項目。
- **已加回**：主視窗選單 **說明 → 檢查更新**（比對 Raw 上 `pyproject.toml` 的 `version` 與本機 `gex_suite.__version__`；可選在含 `.git` 的原始碼目錄執行 `git pull --ff-only`）。請在 `gex_suite/data/suite_config.json` 設定 `update_github_user`、`update_github_repo`、`update_github_branch`（及必要時 `update_remote_pyproject_path`）。
- **已加回**：Scraper 分頁 **View Scraped Files**（依日期／依 ticker 與模型），對應舊版 `GEX_scraper/gui.py` 的檔案瀏覽器行為。
- **ticker manager**：已還原與舊版類似的群組 GUI（左側群組、右側 ticker 勾選／刪除／批次移動），見 `gex_suite/modules/scraper/ticker_manager_dialog.py`。

## TradingView 批次貼上（簡要）

1. 用 **遠端偵錯埠** 啟動 Chrome 或 Brave（預設 `http://127.0.0.1:9222`，可於 `auto_paste_config.json` 或 UI 調整），並登入 [TradingView](https://www.tradingview.com/chart/) 開好圖表。
2. 在 TV 的指標選單中，把 **Daily & Weekly GEX by daniel56_trade** 加入 **我的最愛**，以便批次從 Favorites 新增。
3. 於應用程式 **TradingView** 分頁選擇週期、版面範圍、ticker 範圍等，可先 **預覽將變更項目** 再執行批次；執行中可用 **停止** 中止。
4. 除錯截圖與 HTML dump 預設寫入 `gex_suite/data/tradingview/debug/`（依執行時標籤分子目錄）。

## 未來工作

- [x] TradingView 自動貼上：以 Playwright `connect_over_cdp(...)` 接到使用者瀏覽器（批次寫入 GEX 指標欄位）
- [ ] **TradingView 架構（選做）**：新增 `gex_suite/modules/tradingview/_selectors.py`（或等價模組）集中 DOM／Playwright 定位字串，單點因應 TradingView 介面改版；可分批從 `automator.py` 遷出以降低一次性 diff 風險。
- [ ] **TradingView 架構（選做）**：將批次執行主迴圈收斂到 `engine.run_batch`，`widget.py` 僅負責選項、進度、停止旗標與注入 `runner`，便於單測與日後 CLI／第二入口重用同一套流程。
- [x] 重新加回 `View Scraped Files` 完整檔案瀏覽器（依日期 / ticker & model）
- [x] 自動更新（從 GitHub Raw 比對版本；可選 `git pull --ff-only`）
- [x] 打包 PyInstaller one-folder（`scripts/gex_suite.spec`）；macOS 可再用 `hdiutil` 自製 `.dmg`（未內建簽章流程）

## 煙霧測試

```bash
python tests/smoke_test.py
```

會自動以 `QT_QPA_PLATFORM=offscreen` 啟動，驗證所有模組可載入且 widget 可建構。
