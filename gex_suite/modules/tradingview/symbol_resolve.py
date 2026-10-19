"""Subchart symbol → DB ticker 解析（GUI／CLI／gex-chain 共用，純函式、不依賴 Qt）。

正本只有這一份：widget.py 的 ``TradingViewPage`` 與 ``tools/gex_chain/run.py`` 都呼叫
這裡，不准各做各的（agent-memory/feedback_gui_cli_sync.md）。

規則優先序：

1. ``FUTURES_ALIAS_MAP`` 明確表（subchart symbol 冒號後的 tail 完全相等）。
2. 隱含 alias：tail 符合 TradingView 連續期貨寫法（``<ROOT><序號>!``，例 ``YM1!``／
   ``NK2251!``／``6E1!``）但不在明確表 → ``{"futures": tail, "equity": None,
   "index": None}``。importer 端 ``_suffix_futures_ticker`` 把 CME 抓到的 root 存成
   ``<ROOT>1!``，所以 futures 模式直接拿 tail 當 DB ticker 就對得上。
   equity／index 對應從 symbol 推不出來（YM1! 的 ETF 是 DIA），只能靠明確表，缺就明確
   略過（【略過｜alias 缺項】）。⚠️ 不准拿掉 ``1!`` 去找裸名：裸名在 DB 裡不是沒有
   （ES／YM），就是 2026-05-11 改後綴前的舊資料，或根本是別的商品（ZS＝Zscaler，
   ZS1!＝黃豆）。
3. 其他 symbol：regex 抽第一個 ticker-like token（``!`` 不算 ticker 字元，所以沒走
   前兩條的 ``X1!`` 會變成 ``X1``——這就是 2026-07 K2I1!／2026-10 YM1! 默默略過的根因）。
"""
from __future__ import annotations

import re
from typing import Dict, Optional

# 模組層會真的執行，venv 是 3.9 → 不能寫 dict[str, str | None]
AliasEntry = Dict[str, Optional[str]]

FUTURES_DEFAULT_MODE = "equity"
FUTURES_MODES: tuple[str, ...] = ("futures", "equity", "index")

# TradingView continuous-futures symbols → DB ticker (per layout mode).
#   "futures" : feed the chart with the actual futures TV codes (CME-imported, suffixed with 1!).
#   "equity"  : feed the chart with the related ETF / equity ticker (e.g. SPY for ES1!).
#   "index"   : feed the chart with the underlying index (e.g. SPX for ES1!).
# All three modes shift the indicator's "Start date" to Sunday 18:00, because the
# X-axis bar anchor depends on the chart symbol (always futures), not the data source.
# A None entry means "no DB ticker exists for this mode" → strict skip with a clear log.
FUTURES_ALIAS_MAP: dict[str, AliasEntry] = {
    # E-mini equity index futures (all 3 modes have a real counterpart)
    "ES1!":  {"futures": "ES1!",  "equity": "SPY",  "index": "SPX"},
    "NQ1!":  {"futures": "NQ1!",  "equity": "QQQ",  "index": "NDX"},
    "RTY1!": {"futures": "RTY1!", "equity": "IWM",  "index": "RUT"},
    # Dow E-mini（CBOT）— DB 沒有 DJI 選擇權資料 → index=None（2026-10-19 補）
    "YM1!":  {"futures": "YM1!",  "equity": "DIA",  "index": None},

    # Micro contracts Jeff charts instead of the full-size ones — paste the
    # full-size contract's DB data (scrape/import side still NQ/GC).
    "MNQ1!": {"futures": "NQ1!",  "equity": "QQQ",  "index": "NDX"},
    "MYM1!": {"futures": "YM1!",  "equity": "DIA",  "index": None},  # Jeff 10-19 把 YM1! 圖換成 Micro Dow

    # Precious metals (no broadly-tracked option-bearing index → index=None)
    "GC1!":  {"futures": "GC1!",  "equity": "GLD",  "index": None},
    "MGC1!": {"futures": "GC1!",  "equity": "GLD",  "index": None},
    "SI1!":  {"futures": "SI1!",  "equity": "SLV",  "index": None},
    "PL1!":  {"futures": "PL1!",  "equity": "PPLT", "index": None},
    "PA1!":  {"futures": "PA1!",  "equity": "PALL", "index": None},

    # Industrial metals
    "HG1!":  {"futures": "HG1!",  "equity": "CPER", "index": None},

    # Energy
    "CL1!":  {"futures": "CL1!",  "equity": "USO",  "index": None},

    # Grains
    "ZC1!":  {"futures": "ZC1!",  "equity": "CORN", "index": None},
    "ZS1!":  {"futures": "ZS1!",  "equity": "SOYB", "index": None},
    "ZW1!":  {"futures": "ZW1!",  "equity": "WEAT", "index": None},

    # Treasury futures (each maturity bucket → matching ETF)
    "ZT1!":  {"futures": "ZT1!",  "equity": "SHY",  "index": None},  # 2Y
    "ZF1!":  {"futures": "ZF1!",  "equity": "IEI",  "index": None},  # 5Y
    "ZN1!":  {"futures": "ZN1!",  "equity": "IEF",  "index": None},  # 10Y
    "TN1!":  {"futures": "TN1!",  "equity": "IEF",  "index": None},  # 10Y ultra → 同 IEF
    "ZB1!":  {"futures": "ZB1!",  "equity": "TLT",  "index": None},  # 30Y
    "UB1!":  {"futures": "UB1!",  "equity": "TLT",  "index": None},  # Ultra bond → 同 TLT

    # Crypto
    "BTC1!": {"futures": "BTC1!", "equity": "IBIT", "index": None},

    # Nikkei 225 — TV chart is OSE continuous (NK2251!); DB rows come from the
    # Lieta CME platform import (NK225 + "1!" suffix), so the names coincide.
    "NK2251!": {"futures": "NK2251!", "equity": None, "index": None},

    # KOSPI 200 — the subchart watches the KRX futures continuous K2I1!
    # (switched from the KOSPI200 spot symbol 2026-07); DB rows are still the
    # Lieta CME import "KOSPI200" (index-product exception, no "1!" suffix).
    # Start anchor comes from the start_time_tz_rules "KOSPI200" entry.
    "K2I1!": {"futures": "KOSPI200", "equity": "EWY", "index": None},

    # 台指選擇權（TXO，TAIFEX）— 貼在 TWSE 加權指數現貨圖（IX0001）或台指期連續
    # （TXF1!）上；DB ticker 為裸名 TXO（選擇權 root 非期貨，免 "1!" 後綴，見
    # importers._CME_NO_SUFFIX_TICKERS）。只有一份 TXO 資料 → 三模式同指 TXO。
    # start anchor 走 start_time_tz_rules 的 "TXO"。
    "IX0001": {"futures": "TXO", "equity": "TXO", "index": "TXO"},
    "TXF1!":  {"futures": "TXO", "equity": "TXO", "index": "TXO"},
}


# TradingView 連續期貨寫法：root（字母或數字開頭）＋ 合約序號（數字）＋ "!"。
# ES1!、NQ1!、NK2251!、K2I1!、YM1!、6E1! 都中；SPX、AAPL、BRK.B、IX0001、ES! 不中。
CONTINUOUS_FUTURES_RE = re.compile(r"^[A-Z0-9]{1,10}\d!$")


def symbol_tail(symbol: str | None) -> str:
    """``CBOT_MINI:YM1!`` → ``YM1!``（大寫、去空白；空輸入回空字串）。"""
    text = str(symbol or "").strip().upper()
    return text.split(":")[-1].strip() if text else ""


def implicit_futures_entry(symbol: str | None) -> AliasEntry | None:
    """「1!」尾碼隱含 alias：只有 futures 模式有對應（＝tail 本身）。"""
    tail = symbol_tail(symbol)
    if not tail or not CONTINUOUS_FUTURES_RE.match(tail):
        return None
    return {"futures": tail, "equity": None, "index": None}


def alias_lookup(symbol: str | None) -> tuple[AliasEntry | None, str | None]:
    """→ ``(entry, source)``；``source`` ∈ {"explicit", "implicit", None}。明確表優先。"""
    tail = symbol_tail(symbol)
    if not tail:
        return None, None
    entry = FUTURES_ALIAS_MAP.get(tail)
    if entry is not None:
        return entry, "explicit"
    implicit = implicit_futures_entry(tail)
    if implicit is not None:
        return implicit, "implicit"
    return None, None


def alias_for_symbol(symbol: str | None, mode: str = FUTURES_DEFAULT_MODE) -> str | None:
    """Strict: DB ticker for symbol+mode, or None if no mapping."""
    entry, _ = alias_lookup(symbol)
    return entry.get(mode) if entry else None


def extract_ticker_from_symbol(symbol: str | None) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    tail = text.split(":")[-1]
    match = re.search(r"[A-Z][A-Z0-9._-]{0,14}", tail)
    if match:
        return match.group(0)
    fallback = re.search(r"[A-Z][A-Z0-9._-]{0,14}", text)
    return fallback.group(0) if fallback else None


def symbol_matches_ticker(symbol: str | None, ticker: str) -> bool:
    if not symbol:
        return False
    token = ticker.strip().upper()
    if not token:
        return False
    text = symbol.upper()
    if token == text.split(":")[-1].strip():
        return True
    # Standalone-token match only — a ticker must not match as a prefix of
    # a longer one (SOX must not hit SOXX). ``\b`` breaks on tokens ending
    # in "!" (ES1!), so bound with alphanumeric lookarounds instead.
    if re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", text):
        return True

    # Alias fallback for common index naming differences.
    alias_map: dict[str, tuple[str, ...]] = {
        "VIX": ("VOLATILITY S&P 500 INDEX", "CBOE VOLATILITY INDEX"),
    }
    aliases = alias_map.get(token, ())
    if any(alias in text for alias in aliases):
        return True

    # Acronym fallback: e.g., "Advanced Micro Devices" -> AMD.
    words = re.findall(r"[A-Z]+", text)
    stop = {"INC", "CORP", "CORPORATION", "INDEX", "LTD", "PLC", "CO", "CLASS", "THE", "AND"}
    initials = "".join(w[0] for w in words if len(w) > 1 and w not in stop)
    return token == initials[: len(token)]


def is_formula_symbol(symbol: str | None) -> bool:
    """True if symbol looks like a TradingView formula (e.g. ``ES1!-SPX500``).

    Formula charts join multiple tickers with arithmetic operators and
    cannot host a single-ticker indicator, so we skip them upstream.

    Heuristic: any of ``+ * /`` between two ticker-like tokens, or ``-``
    between two tokens that are both 2+ chars (so real tickers like
    ``BRK-B`` / ``BF-A`` with a single-letter right side are not flagged).
    """
    text = str(symbol or "").strip().upper()
    if not text:
        return False
    if re.search(r"[A-Z0-9!._:][\s]*[+*/][\s]*[A-Z0-9!._:]", text):
        return True
    if re.search(r"[A-Z0-9!._:]{2,}[\s]*-[\s]*[A-Z0-9!._:]{2,}", text):
        return True
    return False


def _ticker_variants(db_ticker: str) -> set[str]:
    """scope=ticker 時接受的寫法：DB 名本身、去 ``!``、去 ``1!``（YM1! ← YM1!／YM1／YM）。"""
    out = {db_ticker}
    if db_ticker.endswith("!"):
        out.add(db_ticker[:-1])
    if db_ticker.endswith("1!"):
        out.add(db_ticker[:-2])
    return {v for v in out if v}


def resolve_target_ticker(
    symbol: str | None,
    *,
    layout_mode: str = FUTURES_DEFAULT_MODE,
    ticker_scope: str = "all",
    ticker: str | None = None,
) -> tuple[str | None, bool]:
    """Resolve subchart symbol → ``(DB ticker, is_futures_alias)`` under a layout mode.

    Strict: if the symbol matches an alias entry（明確表或隱含）but the requested
    ``layout_mode`` has no DB ticker for it (e.g. RTY1! in index mode → RUT not
    imported; ZM1! in equity mode → 沒登錄), returns ``(None, False)`` so the caller
    can emit a clear strict-skip log. The caller distinguishes "alias known but mode
    unmapped" from "symbol unknown" by calling :func:`alias_lookup`.

    ``is_futures_alias=True`` → 指標起始錨點移到週日 18:00（圖的 X 軸是期貨 bar）。
    """
    entry, _source = alias_lookup(symbol)
    aliased = entry.get(layout_mode) if entry else None
    if ticker_scope == "ticker":
        target = (ticker or "").strip().upper()
        if not target:
            return (None, False)
        if aliased and target in _ticker_variants(aliased):
            return (aliased, True)
        # Known futures alias: do NOT fall through to plain symbol match.
        # User must use the alias-mode router (or change mode/target).
        if entry is not None:
            return (None, False)
        if symbol_matches_ticker(symbol, target):
            return (target, False)
        return (None, False)
    # scope == "all"
    if aliased:
        return (aliased, True)
    if entry is not None:
        # Known futures alias but this mode has no DB mapping → strict skip.
        return (None, False)
    return (extract_ticker_from_symbol(symbol), False)


def candidate_db_tickers(symbol: str | None) -> set[str]:
    """gex-chain 補貼用：這個子圖「可能」貼到的所有 DB ticker（不看版面 marker，
    寧可多開一個版面也不漏）。公式圖回空集合。"""
    if is_formula_symbol(symbol):
        return set()
    entry, _ = alias_lookup(symbol)
    if entry is not None:
        return {v.upper() for v in entry.values() if v}
    extracted = extract_ticker_from_symbol(symbol)
    return {extracted} if extracted else set()
