"""Streamed HTML run-log writer for the TradingView auto-paste module.

One file per run, e.g. ``data/tradingview/logs/run_20260515_164213.html``.
Each event is flushed immediately so a partial file is still readable if the
process is killed mid-batch.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import IO


SEVERITY_CSS = {
    "info":    "#9ca3af",
    "done":    "#22c55e",
    "skip":    "#94a3b8",
    "warn":    "#eab308",
    "error":   "#ef4444",
    "preview": "#3b82f6",
    "stop":    "#a855f7",
    "summary": "#06b6d4",
}

_VALID_SEVERITIES = set(SEVERITY_CSS.keys())


_STYLE = """
:root { color-scheme: dark; }
body {
  background: #0d1117;
  color: #e6edf3;
  font-family: -apple-system, "SF Mono", "Menlo", monospace;
  font-size: 13px;
  margin: 0;
  padding: 24px 32px;
  line-height: 1.55;
}
h1 { font-size: 18px; margin: 0 0 4px; color: #f0f6fc; }
.meta { color: #8b949e; font-size: 12px; margin-bottom: 8px; }
.title-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 13px;
  margin-bottom: 20px;
  padding: 8px 12px;
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
}
.title-summary strong { color: #f0f6fc; }
.title-summary .dim { color: #6e7681; }
.layout-badge {
  margin-left: 12px;
  font-weight: 400;
  font-size: 12px;
  display: inline-flex;
  gap: 10px;
}
.layout {
  border: 1px solid #30363d;
  border-radius: 6px;
  margin-bottom: 12px;
  background: #161b22;
}
.layout > summary {
  cursor: pointer;
  padding: 10px 14px;
  font-weight: 600;
  color: #f0f6fc;
  list-style: none;
}
.layout > summary::-webkit-details-marker { display: none; }
.layout > summary::before {
  content: "▸ ";
  display: inline-block;
  width: 1em;
  transition: transform 0.15s;
}
.layout[open] > summary::before { content: "▾ "; }
.layout .layout-meta {
  color: #8b949e;
  font-weight: 400;
  font-size: 12px;
  margin-left: 8px;
}
.events { padding: 6px 14px 12px; }
.event {
  display: grid;
  grid-template-columns: 90px 180px 140px 1fr;
  gap: 10px;
  align-items: baseline;
  padding: 3px 0;
  word-break: break-word;
}
.ts {
  color: #6e7681;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.tag { font-weight: 600; white-space: nowrap; }
.sub { color: #8b949e; font-variant-numeric: tabular-nums; white-space: nowrap; }
.msg { color: #e6edf3; }
.event:hover { background: #1a2230; }
details.why { margin: 0 0 6px 80px; }
details.why summary {
  cursor: pointer;
  color: #6e7681;
  font-size: 12px;
  list-style: none;
}
details.why summary::-webkit-details-marker { display: none; }
details.why summary::before { content: "+ 詳細"; }
details.why[open] summary::before { content: "- 收合"; }
details.why pre {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 4px;
  color: #c9d1d9;
  font-size: 12px;
  white-space: pre-wrap;
}
.footer {
  margin-top: 24px;
  padding-top: 12px;
  border-top: 1px solid #30363d;
  color: #8b949e;
  font-size: 12px;
}
.footer .totals { font-size: 14px; color: #e6edf3; margin-bottom: 4px; }
""".strip()


_AGGREGATE_SCRIPT = """
<script>
(function() {
  var COLORS = {
    done:'#22c55e', skip:'#94a3b8', warn:'#eab308', error:'#ef4444',
    preview:'#3b82f6', stop:'#a855f7', summary:'#06b6d4', info:'#9ca3af'
  };
  var LABEL = {
    done:'完成', skip:'略過', warn:'警告', error:'失敗',
    preview:'預覽', stop:'中止', summary:'摘要', info:'資訊'
  };
  function fmt(counts) {
    var bits = [];
    Object.keys(COLORS).forEach(function(k) {
      if (counts[k]) {
        bits.push('<span style="color:' + COLORS[k] + '">' + LABEL[k] + '=' + counts[k] + '</span>');
      }
    });
    return bits.join(' ');
  }
  function countIn(root) {
    var c = {};
    root.querySelectorAll('.event').forEach(function(e) {
      var s = e.dataset.severity || 'info';
      c[s] = (c[s] || 0) + 1;
    });
    return c;
  }
  // Per-layout badges
  document.querySelectorAll('details.layout').forEach(function(d) {
    var badge = d.querySelector('.layout-badge');
    if (!badge) return;
    var c = countIn(d);
    badge.innerHTML = fmt(c);
  });
  // Top summary
  var slot = document.getElementById('total-summary');
  if (slot) {
    var c = countIn(document);
    var total = 0;
    Object.keys(c).forEach(function(k){ total += c[k]; });
    var ran = document.querySelectorAll('details.layout').length;
    var totalLayoutsEl = document.getElementById('layout-total');
    var scanned = totalLayoutsEl ? parseInt(totalLayoutsEl.dataset.total, 10) : ran;
    var html = '<strong>版面 ' + ran + ' / ' + scanned + '</strong>'
             + ' <span class="dim">(實際跑 / 掃描到)</span>'
             + ' &nbsp; <strong>共 ' + total + ' 事件</strong>';
    var rest = fmt(c);
    if (rest) html += ' &nbsp; ' + rest;
    slot.innerHTML = html;
  }
})();
</script>
""".strip()


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _esc(s: str | None) -> str:
    return html.escape("" if s is None else str(s), quote=False)


class TVRunLogWriter:
    """Streamed HTML writer. Call ``open()`` once, then events, then ``close()``."""

    def __init__(self, path: Path, title: str) -> None:
        self.path = Path(path)
        self.title = title
        self._fh: IO[str] | None = None
        self._layout_open = False
        self._event_count = 0
        self._layout_total: int | None = None
        self._layout_total_written = False

    # ---------- lifecycle ----------
    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write(
            "<!doctype html>\n<html lang=\"zh-Hant\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            f"<title>{_esc(self.title)}</title>\n"
            f"<style>{_STYLE}</style>\n"
            "</head>\n<body>\n"
            f"<h1>{_esc(self.title)}</h1>\n"
            f"<div class=\"meta\">開始時間：{started} ｜ {_esc(str(self.path))}</div>\n"
            "<div class=\"title-summary\" id=\"total-summary\">"
            "<span class=\"dim\">執行中…</span></div>\n"
        )

    def close(self, summary: dict | None = None) -> None:
        if self._fh is None:
            return
        if self._layout_open:
            self.end_layout()
        ended = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        totals_html = ""
        if summary:
            parts = []
            for key in ("total", "done", "skipped", "failed"):
                if key in summary:
                    parts.append(f"{_esc(key)} = {_esc(summary[key])}")
            if parts:
                totals_html = f"<div class=\"totals\">{' ｜ '.join(parts)}</div>"
            extra = summary.get("note")
            if extra:
                totals_html += f"<div>{_esc(extra)}</div>"
        self._write(
            f"<div class=\"footer\">{totals_html}結束時間：{ended} ｜ 共 {self._event_count} 筆事件</div>\n"
        )
        self._write(_AGGREGATE_SCRIPT)
        self._write("</body>\n</html>\n")
        self._fh.close()
        self._fh = None

    # ---------- structural ----------
    def set_layout_total(self, total: int) -> None:
        """Record the total number of layouts intended to be scanned.

        Written once into the stream; JS reads it to show "scanned X / ran Y".
        """
        if self._fh is None or self._layout_total_written:
            return
        self._layout_total = int(total)
        self._layout_total_written = True
        self._write(
            f"<div id=\"layout-total\" data-total=\"{int(total)}\" hidden></div>\n"
        )

    def begin_layout(
        self,
        name: str,
        mode: str | None = None,
        url: str | None = None,
        subchart_count: int | None = None,
    ) -> None:
        if self._layout_open:
            self.end_layout()
        meta_bits = []
        if mode:
            # Callers may pass either a bare mode ("cleanup") or a pre-formatted
            # annotation that already starts with "模式…" ("模式：equity（預設）",
            # "模式（依子圖序）：index, equity"). Only add the prefix when it's
            # missing, so we don't render the doubled "模式：模式：".
            mode_str = _esc(mode)
            meta_bits.append(mode_str if mode_str.startswith("模式") else f"模式：{mode_str}")
        if subchart_count is not None:
            meta_bits.append(f"子圖：{int(subchart_count)}")
        if url:
            meta_bits.append(f"URL：{_esc(url)}")
        meta_html = (
            f"<span class=\"layout-meta\">{' ｜ '.join(meta_bits)}</span>" if meta_bits else ""
        )
        self._write(
            f"<details class=\"layout\">\n"
            f"  <summary>{_esc(name)}{meta_html}"
            f"<span class=\"layout-badge\"></span></summary>\n"
            f"  <div class=\"events\">\n"
        )
        self._layout_open = True

    def end_layout(self) -> None:
        if not self._layout_open:
            return
        self._write("  </div>\n</details>\n")
        self._layout_open = False

    # ---------- events ----------
    def event(
        self,
        severity: str,
        tag: str,
        text: str,
        *,
        subchart: int | None = None,
        ticker: str | None = None,
        detail: str | None = None,
    ) -> None:
        if self._fh is None:
            return
        if severity not in _VALID_SEVERITIES:
            severity = "info"
        color = SEVERITY_CSS[severity]
        ts = _now()
        sub_bits = []
        if subchart is not None:
            sub_bits.append(f"#{subchart}")
        if ticker:
            sub_bits.append(_esc(ticker))
        sub_html = " ".join(sub_bits) if sub_bits else "&nbsp;"
        line = (
            f"    <span class=\"event\" data-severity=\"{severity}\">"
            f"<span class=\"ts\">[{ts}]</span>"
            f"<span class=\"tag\" style=\"color:{color}\">【{_esc(tag)}】</span>"
            f"<span class=\"sub\">{sub_html}</span>"
            f"<span class=\"msg\">{_esc(text)}</span>"
            f"</span>\n"
        )
        self._write(line)
        if detail:
            self._write(
                f"    <details class=\"why\"><summary></summary>"
                f"<pre>{_esc(detail)}</pre></details>\n"
            )
        self._event_count += 1

    # ---------- internal ----------
    def _write(self, s: str) -> None:
        if self._fh is None:
            return
        self._fh.write(s)
        self._fh.flush()


def latest_log_path(log_dir: Path) -> Path | None:
    """Return the most recent ``run_*.html`` file under ``log_dir`` if any."""
    if not log_dir.exists():
        return None
    candidates = sorted(log_dir.glob("run_*.html"), reverse=True)
    return candidates[0] if candidates else None


def new_log_path(log_dir: Path, kind: str) -> Path:
    """Generate a fresh timestamped log path (caller ensures dir exists)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{kind}" if kind else ""
    return log_dir / f"run_{stamp}{suffix}.html"
