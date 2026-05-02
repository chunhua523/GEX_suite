"""Build a Plotly Figure for a ticker (GEX levels + OHLC candles)."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from gex_suite.shared import db

COLOR_MAP = {
    "Call Dominate":  "#FFD700",
    "Call Wall":      "#FFA500",
    "Call Wall CE":   "#FF7F50",
    "Gamma Field":    "#D75BF6",
    "Gamma Field CE": "#EAA1F8",
    "Key Delta":      "#ADFF2F",
    "Gamma Flip":     "#CBCBCB",
    "Gamma Flip CE":  "#FFFFFF",
    "Put Wall CE":    "#FF1493",
    "Put Wall":       "#DC143C",
    "Put Dominate":   "#8B0000",
}


def build_figure(ticker: str) -> tuple[go.Figure, bool]:
    """Return (figure, has_ohlc). Empty rows still produce an empty figure."""
    rows = db.fetch_rows(filter_ticker=ticker)
    if not rows:
        fig = go.Figure()
        fig.update_layout(title=f"{ticker} — no data", template="plotly_dark")
        return fig, False

    df = pd.DataFrame(rows, columns=["id", "ticker", "date", "label", "value"])
    df = df.drop(columns=["id"])
    df["date"] = pd.to_datetime(df["date"])
    df.sort_values("date", inplace=True)

    fig = go.Figure()
    for label, color in COLOR_MAP.items():
        if label in df["label"].unique():
            sub = df[df["label"] == label]
            fig.add_trace(go.Scatter(
                x=sub["date"], y=sub["value"], mode="lines+markers",
                name=label, line=dict(color=color),
            ))

    ohlc = db.fetch_historical_ohlc(ticker)
    has_ohlc = (not ohlc.empty) and all(c in ohlc.columns for c in ("Open", "High", "Low", "Close"))
    if has_ohlc:
        fig.add_trace(go.Candlestick(
            x=ohlc.index,
            open=ohlc["Open"],
            high=ohlc["High"],
            low=ohlc["Low"],
            close=ohlc["Close"],
            name=f"{ticker} OHLC",
        ))

    fig.update_layout(
        title=f"{ticker} OHLC & GEX Level Chart",
        xaxis_title="Date",
        yaxis_title="Price",
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
    )
    return fig, has_ohlc


def figure_to_html(fig: go.Figure) -> str:
    """Standalone HTML ready for QWebEngineView.setHtml(...) (uses CDN)."""
    return fig.to_html(include_plotlyjs="cdn", full_html=True)
