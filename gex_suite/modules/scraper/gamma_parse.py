"""Parse scraped Gamma ``.html`` snapshots and compare per-expiration GEX over days.

A scraped Gamma file is itself a Plotly figure exported to HTML: it calls
``Plotly.newPlot("<id>", [ ...traces... ], {layout}, {config})`` with all data inlined
as JSON. Each **bar** trace is one expiration date; its ``name`` carries the headline
figure, e.g.::

    "2026-06-18 (w|…)   0 dte   GEX: 3.61 M (4.627%)   [51.9%+ / 48.1%- ]   P/C: 0.65 (29.5%)"

The per-expiration "Gamma" metric is the ``GEX:`` value parsed from each bar trace's
``name`` (``sum(x)`` over per-strike values is used as a fallback). The non-bar scatter
traces (per-strike connectors, profile overlays, "Flip Points") are ignored — some
duplicate the GEX text and would double-count.

This module is UI-free and unit-testable apart from :class:`GammaCompareDialog`.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import webbrowser
from dataclasses import dataclass, field

from gex_suite.modules.chart import plot

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_GEX_RE = re.compile(r"GEX:\s*(-?[\d.]+)\s*([kMB]?)", re.IGNORECASE)
_EXPIRY_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_FILE_DT_RE = re.compile(r"(\d{8})_(\d{6})")
_SUFFIX = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9}


def extract_plotly_traces(html: str) -> list[dict]:
    """Return the trace list from the ``Plotly.newPlot(...)`` call in *html*.

    Finds the first ``[`` after ``Plotly.newPlot(`` and bracket-matches its closing
    ``]`` (respecting strings + escapes), then ``json.loads`` it. Returns ``[]`` if no
    Plotly call is present.
    """
    start = html.find("Plotly.newPlot(")
    if start < 0:
        return []
    lb = html.find("[", start)
    if lb < 0:
        return []
    depth = 0
    in_str = False
    esc = False
    for j in range(lb, len(html)):
        c = html[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[lb : j + 1])
                except json.JSONDecodeError:
                    return []
    return []


def parse_gex_value(name: str) -> float | None:
    """Parse the headline ``GEX: 3.61 M`` figure from a trace name into a float."""
    m = _GEX_RE.search(name or "")
    if not m:
        return None
    try:
        return float(m.group(1)) * _SUFFIX[m.group(2).lower()]
    except (ValueError, KeyError):
        return None


def parse_expiry(name: str) -> str | None:
    """Parse the leading ``YYYY-MM-DD`` expiration date from a trace name."""
    m = _EXPIRY_RE.search(name or "")
    return m.group(1) if m else None


@dataclass
class GammaSnapshot:
    """One scraped Gamma file: per-expiry GEX (and per-strike gamma) at a scrape date."""

    snapshot_date: str  # "YYYY-MM-DD" from the filename
    ticker: str
    path: str
    by_expiry: dict[str, float] = field(default_factory=dict)
    # expiry -> {strike: signed gamma at that strike}
    strikes_by_expiry: dict[str, dict[float, float]] = field(default_factory=dict)


def _snapshot_date_from_filename(path: str) -> str | None:
    m = _FILE_DT_RE.search(os.path.basename(path))
    if not m:
        return None
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"


def parse_gamma_file(path: str) -> GammaSnapshot:
    """Parse a single Gamma ``.html`` into a :class:`GammaSnapshot`.

    Raises ``ValueError`` if the file has no parseable Plotly bar traces or no
    snapshot date in its filename.
    """
    snapshot_date = _snapshot_date_from_filename(path)
    if not snapshot_date:
        raise ValueError(f"no YYYYMMDD_HHMMSS timestamp in filename: {path}")

    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    by_expiry: dict[str, float] = {}
    strikes_by_expiry: dict[str, dict[float, float]] = {}
    for tr in extract_plotly_traces(html):
        if tr.get("type") != "bar":
            continue
        name = tr.get("name") or ""
        expiry = parse_expiry(name)
        if not expiry:
            continue
        # Per-strike gamma: y = strikes, x = signed gamma at each strike.
        strikes: dict[float, float] = {}
        for strike, gamma in zip(tr.get("y", []), tr.get("x", [])):
            if isinstance(strike, (int, float)) and isinstance(gamma, (int, float)):
                strikes[float(strike)] = float(gamma)
        strikes_by_expiry[expiry] = strikes

        val = parse_gex_value(name)
        if val is None:  # fallback: sum signed per-strike gamma
            val = sum(strikes.values())
        by_expiry[expiry] = val

    if not by_expiry:
        raise ValueError(f"no Gamma bar traces found in {path}")

    ticker = os.path.basename(path).split("_")[0] or "?"
    return GammaSnapshot(
        snapshot_date=snapshot_date,
        ticker=ticker,
        path=path,
        by_expiry=by_expiry,
        strikes_by_expiry=strikes_by_expiry,
    )


def load_snapshots(paths: list[str]) -> list[GammaSnapshot]:
    """Parse *paths* into snapshots sorted by scrape date, skipping unparseable files.

    If two files share a snapshot date (multiple scrapes that day), the later one wins.
    """
    by_date: dict[str, GammaSnapshot] = {}
    for p in paths:
        try:
            snap = parse_gamma_file(p)
        except (OSError, ValueError) as exc:
            print(f"[gamma_parse] skipping {p}: {exc}")
            continue
        by_date[snap.snapshot_date] = snap  # later path for the same date overwrites
    return [by_date[d] for d in sorted(by_date)]


# ---------------------------------------------------------------------------
# Figures (Plotly)
# ---------------------------------------------------------------------------

def _all_expiries(snapshots: list["GammaSnapshot"]) -> list[str]:
    seen: set[str] = set()
    for s in snapshots:
        seen.update(s.by_expiry)
    return sorted(seen)


def build_delta_figure(prev: "GammaSnapshot | None", latest: "GammaSnapshot | None"):
    """Diverging horizontal bar of GEX change (latest − previous) per expiry."""
    import plotly.graph_objects as go

    fig = go.Figure()
    if prev is None or latest is None:
        fig.update_layout(
            title="Select two days to compare",
            template="plotly_dark",
        )
        return fig

    expiries = [e for e in _all_expiries([prev, latest]) if e in prev.by_expiry and e in latest.by_expiry]
    deltas = [latest.by_expiry[e] - prev.by_expiry[e] for e in expiries]
    colors = ["#2CC985" if d >= 0 else "#DC143C" for d in deltas]

    fig.add_trace(go.Bar(
        x=deltas,
        y=expiries,
        orientation="h",
        marker_color=colors,
        text=[f"{d:+,.0f}" for d in deltas],
        textposition="auto",
        hovertemplate="Expiry %{y}<br>ΔGEX %{x:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{latest.ticker} Gamma daily change per expiration<br>"
              f"<sub>{prev.snapshot_date} → {latest.snapshot_date}</sub>",
        xaxis_title="ΔGEX (latest − previous)",
        yaxis_title="Expiration",
        yaxis=dict(autorange="reversed"),
        template="plotly_dark",
        showlegend=False,
    )
    return fig


def build_timeseries_figure(snapshots: list["GammaSnapshot"]):
    """One line+markers trace per expiry; x = scrape dates, y = that expiry's GEX."""
    import plotly.graph_objects as go

    fig = go.Figure()
    dates = [s.snapshot_date for s in snapshots]
    for expiry in _all_expiries(snapshots):
        # None for snapshots missing this expiry → broken line (no backfill).
        ys = [s.by_expiry.get(expiry) for s in snapshots]
        fig.add_trace(go.Scatter(
            x=dates,
            y=ys,
            mode="lines+markers",
            name=expiry,
            connectgaps=False,
            hovertemplate=f"Expiry {expiry}<br>%{{x}}<br>GEX %{{y:,.0f}}<extra></extra>",
        ))
    ticker = snapshots[0].ticker if snapshots else ""
    fig.update_layout(
        title=f"{ticker} Gamma per expiration over time",
        xaxis_title="Scrape date",
        yaxis_title="GEX",
        template="plotly_dark",
        legend_title="Expiration",
    )
    return fig


def expiries_in(snapshots: list["GammaSnapshot"]) -> list[str]:
    """Union of all expiration dates across *snapshots*, sorted ascending."""
    return _all_expiries(snapshots)


def build_strike_delta_figure(prev: "GammaSnapshot | None", latest: "GammaSnapshot | None", expiry: str):
    """Diverging horizontal bar of per-strike gamma change (latest − previous).

    Strikes absent on a day are treated as 0 gamma (unlisted strike ≈ no net gamma),
    so newly-appearing strikes still surface as a change.
    """
    import plotly.graph_objects as go

    fig = go.Figure()
    if prev is None or latest is None or not expiry:
        fig.update_layout(
            title="Select two days and an expiration to compare",
            template="plotly_dark",
        )
        return fig

    prev_s = prev.strikes_by_expiry.get(expiry, {})
    latest_s = latest.strikes_by_expiry.get(expiry, {})
    strikes = sorted(set(prev_s) | set(latest_s))
    deltas = [latest_s.get(k, 0.0) - prev_s.get(k, 0.0) for k in strikes]
    colors = ["#2CC985" if d >= 0 else "#DC143C" for d in deltas]

    fig.add_trace(go.Bar(
        x=deltas,
        y=strikes,
        orientation="h",
        marker_color=colors,
        hovertemplate="Strike %{y}<br>Δγ %{x:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{latest.ticker} {expiry} per-strike Gamma change<br>"
              f"<sub>{prev.snapshot_date} → {latest.snapshot_date}</sub>",
        xaxis_title="Δ gamma (latest − previous)",
        yaxis_title="Strike",
        template="plotly_dark",
        showlegend=False,
        height=max(400, 18 * len(strikes)),
    )
    return fig


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:
    QWebEngineView = None  # type: ignore[assignment]
    HAS_WEBENGINE = False


class _PlotView(QWidget):
    """Hosts a Plotly figure: embedded QWebEngineView, or external-browser fallback."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        if HAS_WEBENGINE:
            self._web = QWebEngineView()
            v.addWidget(self._web, 1)
        else:
            self._web = None
            v.addWidget(QLabel(
                "QtWebEngine 未安裝，圖表將於外部瀏覽器開啟。\n"
                "可執行：pip install PySide6-Addons"
            ))
            v.addStretch(1)

    def show_figure(self, fig) -> None:
        html = plot.figure_to_html(fig)
        if self._web is not None:
            self._web.setHtml(html, QUrl("about:blank"))
        else:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".html", delete=False, encoding="utf-8"
            ) as tf:
                tf.write(html)
                tmp_path = tf.name
            webbrowser.open(QUrl.fromLocalFile(tmp_path).toString())


class GammaCompareDialog(QDialog):
    """Daily-change, time-series, and per-strike-change Plotly views for Gamma."""

    def __init__(self, snapshots: list[GammaSnapshot], parent=None) -> None:
        super().__init__(parent)
        self._snapshots = snapshots
        self._by_date = {s.snapshot_date: s for s in snapshots}
        self._dates = [s.snapshot_date for s in snapshots]  # already sorted ascending
        ticker = snapshots[0].ticker if snapshots else ""
        self.setWindowTitle(f"Gamma Compare — {ticker} ({len(snapshots)} snapshot(s))")
        self.resize(1100, 800)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        tabs.addTab(self._build_delta_tab(), "Daily Change")

        ts_view = _PlotView()
        ts_view.show_figure(build_timeseries_figure(snapshots))
        tabs.addTab(ts_view, "Time Series")

        tabs.addTab(self._build_strike_tab(), "Per-Strike Δ")

    # -- shared helpers -------------------------------------------------------
    def _make_pair_combos(self) -> "tuple[QComboBox, QComboBox]":
        """Two date pickers defaulting to the two most recent days."""
        from_combo, to_combo = QComboBox(), QComboBox()
        for d in self._dates:
            from_combo.addItem(d)
            to_combo.addItem(d)
        # default: From = second-latest, To = latest
        from_combo.setCurrentIndex(max(0, len(self._dates) - 2))
        to_combo.setCurrentIndex(max(0, len(self._dates) - 1))
        return from_combo, to_combo

    def _pair(self, from_combo: QComboBox, to_combo: QComboBox):
        return self._by_date.get(from_combo.currentText()), self._by_date.get(to_combo.currentText())

    # -- tabs -----------------------------------------------------------------
    def _build_delta_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        from_combo, to_combo = self._make_pair_combos()
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Compare:"))
        hdr.addWidget(from_combo)
        hdr.addWidget(QLabel("→"))
        hdr.addWidget(to_combo)
        hdr.addStretch(1)
        v.addLayout(hdr)

        view = _PlotView()
        v.addWidget(view, 1)

        def refresh(*_a) -> None:
            prev, latest = self._pair(from_combo, to_combo)
            view.show_figure(build_delta_figure(prev, latest))

        from_combo.currentTextChanged.connect(refresh)
        to_combo.currentTextChanged.connect(refresh)
        refresh()
        return page

    def _build_strike_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        from_combo, to_combo = self._make_pair_combos()
        expiry_combo = QComboBox()
        for e in expiries_in(self._snapshots):
            expiry_combo.addItem(e)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Compare:"))
        hdr.addWidget(from_combo)
        hdr.addWidget(QLabel("→"))
        hdr.addWidget(to_combo)
        hdr.addSpacing(16)
        hdr.addWidget(QLabel("Expiration:"))
        hdr.addWidget(expiry_combo)
        hdr.addStretch(1)
        v.addLayout(hdr)

        view = _PlotView()
        v.addWidget(view, 1)

        def refresh(*_a) -> None:
            prev, latest = self._pair(from_combo, to_combo)
            view.show_figure(build_strike_delta_figure(prev, latest, expiry_combo.currentText()))

        from_combo.currentTextChanged.connect(refresh)
        to_combo.currentTextChanged.connect(refresh)
        expiry_combo.currentTextChanged.connect(refresh)
        refresh()
        return page
