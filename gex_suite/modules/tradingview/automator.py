"""TradingView automator implementations.

This module exposes an abstract contract plus a Playwright/CDP-backed
implementation that can attach to a user-launched Chrome/Brave instance
(``--remote-debugging-port=9222``), then drive TradingView UI actions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import secrets
import re

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from gex_suite.shared import db
from gex_suite.shared.paths import DATA_DIR


@dataclass(frozen=True)
class IndicatorInfo:
    title: str
    start_date: date | None = None


@dataclass(frozen=True)
class LayoutInfo:
    id: str
    name: str
    subtitle: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class SubChartInfo:
    index: int
    symbol: str | None = None


@dataclass
class WeeklyGexRowSnapshot:
    """One legend row worth of state collected in a single settings open."""

    row_signature: str
    start_iso: str
    levels: dict[str, str | None]


@dataclass
class WeeklyGexSubchartCache:
    """Per-subchart scan: each matched row opened once (date + Mon..Fri levels).

    Optional ``keep_mondays`` window removes expired rows during the same pass
    so batch flows do not run a separate cleanup sweep before each week.
    """

    rows: list[WeeklyGexRowSnapshot]
    probe_complete: bool
    removed_expired: int = 0

    @property
    def signature_set(self) -> frozenset[str]:
        return frozenset(r.row_signature for r in self.rows if r.row_signature)


class FavoriteNotFoundError(RuntimeError):
    """Raised when the expected TradingView favorite indicator is missing."""


class IndicatorQuotaExceededError(RuntimeError):
    """Raised when TradingView refuses a new indicator (plan / max indicators)."""


class TVAutomator(ABC):
    """Contract every TradingView automator must implement."""

    @abstractmethod
    async def connect(self) -> None:
        """Attach to the running browser (e.g. via CDP)."""

    @abstractmethod
    async def open_ticker(self, symbol: str) -> None:
        """Switch the active chart to ``symbol``."""

    @abstractmethod
    async def open_pine_editor(self) -> None:
        """Open the Pine Script editor pane."""

    @abstractmethod
    async def paste_code(self, code: str) -> None:
        """Replace editor contents with ``code``."""

    @abstractmethod
    async def save_indicator(self, name: str) -> None:
        """Save the currently-edited indicator with ``name``."""

    @abstractmethod
    async def close(self) -> None:
        """Detach (without closing the user's browser)."""


class NotImplementedAutomator(TVAutomator):
    """Placeholder automator that raises ``NotImplementedError``.

    Replace at a later iteration with a Playwright/CDP-backed implementation.
    """

    async def connect(self) -> None:
        raise NotImplementedError("TradingView automator not implemented yet")

    async def open_ticker(self, symbol: str) -> None:
        raise NotImplementedError

    async def open_pine_editor(self) -> None:
        raise NotImplementedError

    async def paste_code(self, code: str) -> None:
        raise NotImplementedError

    async def save_indicator(self, name: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return


class PlaywrightCDPAutomator(TVAutomator):
    """TradingView automator implemented with Playwright CDP attach."""

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        chart_url: str = "https://www.tradingview.com/chart/",
        layout_settle_ms: int = 1200,
    ) -> None:
        self.cdp_url = cdp_url
        self.chart_url = chart_url
        self.layout_settle_ms = max(300, int(layout_settle_ms))
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._debug_dir = DATA_DIR / "tradingview" / "debug"
        self.apply_visibility_preset = True
        self._logger = None  # type: ignore[assignment]
        self._scoped_subchart_index: int | None = None
        self._scope_attr = "data-gex-scope-target"
        self._chart_settings_misopen_count = 0
        self._chart_settings_misopen_limit = 3

    def set_logger(self, logger) -> None:
        """Inject an optional callable(str) used for verbose runtime logs."""
        self._logger = logger

    def set_indicator_scope_subchart(self, idx: int | None) -> None:
        """Optionally pin indicator-locator scope to a specific subchart index."""
        if idx is None:
            self._scoped_subchart_index = None
            return
        self._scoped_subchart_index = max(0, int(idx))

    async def pin_indicator_scope_to_subchart(self, idx: int) -> bool:
        """Mark a visible chart-widget as the exclusive indicator search scope."""
        page = self._require_page()
        self._scoped_subchart_index = max(0, int(idx))
        try:
            pinned = await page.evaluate(
                """
                ({targetIdx, scopeAttr}) => {
                  const nodes = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  ).filter((el) => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  });
                  nodes.forEach((el) => el.removeAttribute(scopeAttr));
                  if (targetIdx < 0 || targetIdx >= nodes.length) return false;
                  nodes[targetIdx].setAttribute(scopeAttr, "1");
                  return true;
                }
                """,
                {"targetIdx": self._scoped_subchart_index, "scopeAttr": self._scope_attr},
            )
            return bool(pinned)
        except Exception:
            return False

    async def clear_indicator_scope_marker(self) -> None:
        """Clear any temporary chart-widget scope marker."""
        page = self._require_page()
        try:
            await page.evaluate(
                """
                (scopeAttr) => {
                  document.querySelectorAll(`[${scopeAttr}]`).forEach((el) => {
                    el.removeAttribute(scopeAttr);
                  });
                }
                """,
                self._scope_attr,
            )
        except Exception:
            pass

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:
            pass

    async def connect(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        self._context = self._pick_context(self._browser)
        self._page = await self._pick_or_open_page(self._context)

    async def open_ticker(self, symbol: str) -> None:
        page = self._require_page()
        symbol = symbol.strip().upper()
        if not symbol:
            return
        await page.bring_to_front()
        await page.keyboard.press("Alt+S")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Control+A")
        await page.keyboard.type(symbol, delay=20)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(800)

    async def open_pine_editor(self) -> None:
        page = self._require_page()
        await page.bring_to_front()
        if await page.locator("text=Pine Editor").count():
            await page.locator("text=Pine Editor").first.click()
            await page.wait_for_timeout(250)
            return
        await page.keyboard.press("Alt+E")
        await page.wait_for_timeout(250)

    async def paste_code(self, code: str) -> None:
        page = self._require_page()
        cleaned_code = code.replace("\r\n", "\n").replace("\r", "\n")

        await page.bring_to_front()
        await page.locator("div.view-lines").first.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.type(cleaned_code, delay=0)
        await page.wait_for_timeout(250)

    async def save_indicator(self, name: str) -> None:
        page = self._require_page()
        indicator_name = name.strip()
        await page.bring_to_front()
        await page.keyboard.press("Control+S")
        await page.wait_for_timeout(500)
        if indicator_name:
            if await page.locator("input[type='text']").count():
                await page.locator("input[type='text']").last.fill(indicator_name)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._browser = None
            self._context = None
            self._page = None
            self._pw = None

    async def open_chart_urls_via_cdp(self, urls: list[str]) -> None:
        """Open chart URLs in the browser attached to ``self.cdp_url`` (e.g. 9222).

        Reuses the current tab for the first URL, then opens one new tab per
        additional URL in the same CDP context.
        """
        cleaned = [str(u).strip() for u in urls if str(u).strip()]
        if not cleaned:
            return
        await self.connect()
        ctx = self._context
        if ctx is None:
            raise RuntimeError("CDP context missing after connect")
        page = self._page
        if page is None:
            page = await self._pick_or_open_page(ctx)
            self._page = page
        for i, url in enumerate(cleaned):
            low = url.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                continue
            if i > 0:
                page = await ctx.new_page()
                self._page = page
            await page.bring_to_front()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    def set_apply_visibility_preset(self, enabled: bool) -> None:
        self.apply_visibility_preset = bool(enabled)

    # ---------- Phase B helpers (layouts / sub-charts) ----------
    async def list_layouts(self) -> list[LayoutInfo]:
        """List available layouts from '.' Layouts dialog.

        Also tries to harvest each row's destination URL so that
        ``load_layout`` can navigate via ``page.goto`` instead of relying on
        UI clicks (which TV sometimes swallows).
        """
        page = self._require_page()
        await page.bring_to_front()
        opened = await self._open_layout_dialog()
        if not opened:
            self._log("[list_layouts] fallback=Current reason=dialog_not_opened")
            return [LayoutInfo(id="current", name="Current")]
        dialog = self._layout_dialog_locator()
        await self._ensure_layouts_all_tab(dialog)
        # Clear any sticky search/filter text; otherwise TV may show only one row.
        search_input = dialog.locator(
            "input[placeholder*='Search'], input[placeholder*='search'], "
            "input[type='search'], input[aria-label*='Search'], input[aria-label*='搜尋']"
        ).first
        if await search_input.count():
            try:
                await search_input.click(timeout=800)
                await search_input.fill("")
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(220)
            except Exception:
                pass
        out: list[LayoutInfo] = []
        seen_keys: set[str] = set()
        first_row_dumped = False
        rows = self._layout_rows_locator(dialog)
        if await rows.count() == 0:
            # TradingView variants may not expose strict role attributes.
            rows = dialog.locator(
                "[data-name*='layout-item'], "
                "[class*='item'], "
                "[class*='row'], "
                "[role='button']"
            )

        stagnant_rounds = 0
        last_seen_total = -1
        for _ in range(14):
            count = await rows.count()
            for idx in range(count):
                row = rows.nth(idx)
                try:
                    text = (await row.inner_text(timeout=1200)).strip()
                except Exception:
                    continue
                if not text:
                    continue
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if not lines:
                    continue
                key = lines[0]
                if len(lines) > 1:
                    key = f"{key}|{lines[1]}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                url = await self._extract_layout_row_url(row)
                if not first_row_dumped and url is None:
                    # One-time DOM dump so we can inspect TV's row markup when
                    # URL extraction fails outright.
                    await self._dump_row_outer_html(row, label="layout_row_first")
                    first_row_dumped = True
                out.append(
                    LayoutInfo(
                        id=f"layout::{key}",
                        name=lines[0],
                        subtitle=lines[1] if len(lines) > 1 else None,
                        url=url,
                    )
                )

            if len(seen_keys) == last_seen_total:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                last_seen_total = len(seen_keys)
            if stagnant_rounds >= 2:
                break

            # Scroll list container to discover off-screen rows.
            try:
                await rows.last.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            try:
                await dialog.hover(timeout=600)
            except Exception:
                pass
            try:
                await page.mouse.wheel(0, 700)
            except Exception:
                pass
            await page.wait_for_timeout(140)

        if not out:
            await self._dump_dom("layout_dialog_rows_empty")
            self._log("[list_layouts] fallback=Current reason=rows_empty")
        with_url = sum(1 for layout in out if layout.url)
        self._log(f"[list_layouts] total={len(out)} with_url={with_url}")
        await self._close_layout_dialog_gently()
        return out or [LayoutInfo(id="current", name="Current")]

    async def _extract_layout_row_url(self, row) -> str | None:
        """Best-effort extract layout destination URL from a Layouts row."""
        anchor_selectors = ("a[href*='/chart/']", "a[href]")
        for sel in anchor_selectors:
            anchor = row.locator(sel).first
            if await anchor.count():
                try:
                    href = await anchor.get_attribute("href")
                except Exception:
                    href = None
                if href:
                    normalized = self._normalize_layout_url(href)
                    if normalized:
                        return normalized

        for attr in ("data-href", "href", "data-url", "data-target-url"):
            try:
                val = await row.get_attribute(attr)
            except Exception:
                val = None
            if val:
                normalized = self._normalize_layout_url(val)
                if normalized:
                    return normalized

        try:
            attrs = await row.evaluate(
                """
                (el) => {
                  const out = {};
                  for (const a of Array.from(el.attributes || [])) out[a.name] = a.value;
                  return out;
                }
                """
            )
        except Exception:
            attrs = {}
        for key, val in (attrs or {}).items():
            if not isinstance(val, str):
                continue
            key_l = str(key).lower()
            if "url" in key_l or "href" in key_l or "link" in key_l:
                normalized = self._normalize_layout_url(val)
                if normalized:
                    return normalized

        for attr in ("data-layout-id", "data-id", "data-saved-chart-id", "data-name"):
            try:
                val = await row.get_attribute(attr)
            except Exception:
                val = None
            if val and re.fullmatch(r"[A-Za-z0-9_-]{4,}", val):
                return f"https://www.tradingview.com/chart/{val}/"

        try:
            html = await row.evaluate("el => el.outerHTML")
        except Exception:
            html = ""
        if html:
            for pat in (
                r"https?://(?:www\.)?tradingview\.com/chart/([A-Za-z0-9_-]{4,})/?",
                r"['\"](?:https?:)?//(?:www\.)?tradingview\.com/chart/([A-Za-z0-9_-]{4,})/?['\"]",
                r"['\"]/chart/([A-Za-z0-9_-]{4,})/?['\"]",
                r"(?:layoutId|chartId|savedChartId|data-layout-id|data-saved-chart-id)[\"'=:\s>]+([A-Za-z0-9_-]{4,})",
            ):
                m = re.search(pat, html, flags=re.IGNORECASE)
                if m:
                    return f"https://www.tradingview.com/chart/{m.group(1)}/"
        return None

    @staticmethod
    def _normalize_layout_url(url: str) -> str | None:
        url = (url or "").strip()
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"https://www.tradingview.com{url}"
        return None

    async def _dump_row_outer_html(self, row, label: str) -> None:
        try:
            html = await row.evaluate("el => el.outerHTML")
        except Exception:
            return
        if not html:
            return
        out_dir = self._debug_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "row"
        path = out_dir / f"{safe}.html"
        try:
            path.write_text(html, encoding="utf-8")
        except Exception:
            pass

    async def load_layout(self, layout: LayoutInfo) -> bool:
        """Load a layout. Prefer ``page.goto`` to the layout's chart URL.

        The Layouts dialog click flow is unreliable across TV builds
        (clicks sometimes close the dialog without loading the layout). When
        ``layout.url`` is known we just navigate directly -- this is the
        same effect as a successful row-click but bypasses the UI handler.
        """
        page = self._require_page()
        await page.bring_to_front()
        if layout.id == "current":
            return True

        before_url = page.url
        self._log(
            f"[load_layout] BEGIN target='{layout.name}' before_url={before_url} "
            f"layout_url={layout.url or '-'}"
        )

        if layout.url:
            return await self._load_layout_via_goto(layout, before_url)

        # Fallback: open dialog and click the row.
        return await self._load_layout_via_click(layout, before_url)

    async def save_current_layout(self) -> None:
        """Best-effort persist current chart layout changes."""
        page = self._require_page()
        await page.bring_to_front()
        try:
            await page.keyboard.press("Control+S")
            await page.wait_for_timeout(260)
        except Exception:
            # Keep non-fatal: save failures should not crash whole batch.
            return

    async def _load_layout_via_goto(self, layout: LayoutInfo, before_url: str) -> bool:
        page = self._require_page()
        target_url = layout.url or ""
        if not target_url:
            return False
        if target_url.rstrip("/") == before_url.rstrip("/"):
            self._log(f"[load_layout] same URL, no switch needed: {target_url}")
            return True
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            self._log(f"[load_layout] goto FAIL target='{layout.name}' err={exc!r}")
            return False
        # Give TradingView time to hydrate widgets/legend after navigation.
        try:
            await page.wait_for_selector(
                "[data-name='chart-widget'], [class*='chart-widget']",
                state="visible",
                timeout=12000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_selector(
                "button#header-toolbar-symbol-search",
                state="visible",
                timeout=8000,
            )
        except Exception:
            pass
        # Extra settle loop for TV hydration lag after navigation.
        await self._wait_after_layout_goto()
        switched = page.url.rstrip("/") != before_url.rstrip("/")
        self._log(
            f"[load_layout] post-goto target='{layout.name}' final_url={page.url} switched={switched}"
        )
        return switched

    async def _wait_after_layout_goto(self) -> None:
        """Best-effort settle wait after ``page.goto`` layout switch."""
        page = self._require_page()
        waited = 0
        step = 160
        while waited < self.layout_settle_ms:
            try:
                widgets = await self._count_visible_chart_widgets()
            except Exception:
                widgets = 0
            if widgets > 0:
                # One extra beat even after widgets appear to avoid racing subchart scan.
                await page.wait_for_timeout(step)
                return
            await page.wait_for_timeout(step)
            waited += step
        # Final fixed buffer for stubborn hydration cases.
        await page.wait_for_timeout(180)

    async def _load_layout_via_click(self, layout: LayoutInfo, before_url: str) -> bool:
        page = self._require_page()
        opened = await self._open_layout_dialog()
        if not opened:
            self._log(f"[load_layout] FAIL open dialog target='{layout.name}'")
            return False
        dialog = self._layout_dialog_locator()

        search_input = dialog.locator("input[placeholder*='Search'], input[type='search']").first
        if await search_input.count():
            try:
                await search_input.click()
                await search_input.fill(layout.name)
                await page.wait_for_timeout(220)
            except Exception:
                pass

        target = await self._resolve_layout_row(dialog, layout)
        if target is None:
            self._log(f"[load_layout] FAIL resolve row target='{layout.name}'")
            await self._close_layout_dialog_gently()
            return False

        try:
            target_text = (await target.inner_text(timeout=1000)).strip().replace("\n", " | ")
        except Exception:
            target_text = "<no-text>"
        self._log(f"[load_layout] resolved row text='{target_text[:120]}'")

        try:
            await target.scroll_into_view_if_needed()
        except Exception:
            pass

        # Try Playwright click first.
        try:
            await target.click(timeout=2000)
        except Exception as exc:
            self._log(f"[load_layout] click error target='{layout.name}' err={exc!r}")

        if await self._wait_layout_committed(before_url=before_url, max_ms=3000):
            return True

        # Fallback A: native DOM click (bypasses Playwright mouse synthesis).
        try:
            clicked = await self._dom_click_layout_row_by_name(layout)
            if not clicked:
                raise RuntimeError("row not found by dom lookup")
        except Exception as exc:
            self._log(f"[load_layout] dom-click error target='{layout.name}' err={exc!r}")

        if await self._wait_layout_committed(before_url=before_url, max_ms=3000):
            return True

        # Fallback B: dblclick on the row (some TV variants need it).
        try:
            await target.dblclick(timeout=1500)
        except Exception:
            pass

        if await self._wait_layout_committed(before_url=before_url, max_ms=3000):
            return True

        # Fallback C: Enter on focused row.
        if await self._is_layout_dialog_open():
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass
            if await self._wait_layout_committed(before_url=before_url, max_ms=2500):
                return True

        await self._dump_row_outer_html(target, label=f"layout_row_failed_{layout.name}")
        await self._close_layout_dialog_gently()
        self._log(f"[load_layout] retry-open target='{layout.name}'")
        if await self._retry_load_layout_click(layout, before_url):
            return True
        self._log(f"[load_layout] END target='{layout.name}' final_url={page.url} switched=False")
        return False

    async def _retry_load_layout_click(self, layout: LayoutInfo, before_url: str) -> bool:
        """One-shot retry: reopen dialog, clear search, resolve row, click."""
        page = self._require_page()
        opened = await self._open_layout_dialog()
        if not opened:
            return False
        dialog = self._layout_dialog_locator()
        await self._ensure_layouts_all_tab(dialog)
        search_input = dialog.locator(
            "input[placeholder*='Search'], input[placeholder*='search'], input[type='search']"
        ).first
        if await search_input.count():
            try:
                await search_input.click(timeout=900)
                await search_input.fill("")
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(200)
                await search_input.fill(layout.name)
                await page.wait_for_timeout(220)
            except Exception:
                pass
        target = await self._resolve_layout_row(dialog, layout)
        if target is None:
            await self._close_layout_dialog_gently()
            return False
        try:
            await target.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await target.click(timeout=2200)
        except Exception:
            pass
        if await self._wait_layout_committed(before_url=before_url, max_ms=3500):
            return True
        await self._close_layout_dialog_gently()
        return False

    async def _dom_click_layout_row_by_name(self, layout: LayoutInfo) -> bool:
        """DOM-level row click by text to avoid stale locator handles."""
        page = self._require_page()
        try:
            return bool(
                await page.evaluate(
                    """
                    ({ name, subtitle }) => {
                      const dialogs = Array.from(document.querySelectorAll("[role='dialog']"));
                      const dialog = dialogs.find((d) => /layouts|版面|佈局/i.test((d.innerText || "").slice(0, 200)));
                      if (!dialog) return false;
                      const rows = Array.from(
                        dialog.querySelectorAll("[role='option'], [role='listitem'], [data-name*='layout-item'], [role='button']")
                      );
                      const wanted = (name || "").trim();
                      const sub = (subtitle || "").trim();
                      for (const row of rows) {
                        const txt = (row.innerText || "").trim();
                        if (!txt) continue;
                        if (wanted && !txt.includes(wanted)) continue;
                        if (sub && !txt.includes(sub)) continue;
                        row.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                        row.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                        row.click();
                        return true;
                      }
                      return false;
                    }
                    """,
                    {"name": layout.name, "subtitle": layout.subtitle or ""},
                )
            )
        except Exception:
            return False

    async def _resolve_layout_row(self, dialog, layout: LayoutInfo):
        rows = self._layout_rows_locator(dialog)
        count = await rows.count()
        for idx in range(count):
            try:
                txt = (await rows.nth(idx).inner_text(timeout=1500)).strip()
            except Exception:
                continue
            if not txt:
                continue
            if layout.name not in txt:
                continue
            if layout.subtitle and layout.subtitle not in txt:
                continue
            return rows.nth(idx)
        fallback = dialog.locator(f":text('{layout.name}')").first
        if await fallback.count():
            return fallback
        return None

    async def _wait_layout_committed(self, *, before_url: str, max_ms: int) -> bool:
        """Wait until layout switch is committed.

        TRUE only when ``page.url`` changes -- TradingView always rewrites
        URL to ``/chart/<id>/`` when a saved layout is loaded. Dialog auto
        close alone is NOT a success signal because clicks outside the
        layout row (or Cancel) also dismiss the dialog without switching.

        If the dialog closes but URL stays the same we keep polling for a
        short grace window to absorb pushState lag, then give up.
        """
        page = self._require_page()
        elapsed = 0
        step = 120
        grace_after_close = 0
        grace_max = 700
        while elapsed < max_ms:
            if page.url != before_url:
                return True
            dialog_open = await self._is_layout_dialog_open()
            if not dialog_open:
                grace_after_close += step
                if grace_after_close >= grace_max:
                    return False
            await page.wait_for_timeout(step)
            elapsed += step
        return False

    async def _close_layout_dialog_gently(self) -> None:
        """Close Layouts dialog WITHOUT pressing Escape.

        Pressing Escape makes TradingView revert layout in some flows.
        """
        page = self._require_page()
        dialog = self._layout_dialog_locator()
        if await dialog.count() == 0:
            return
        close_btn = dialog.locator(
            "button[data-qa-id='close'], button[aria-label*='Close'], button[aria-label*='關閉']"
        ).first
        if await close_btn.count():
            try:
                await close_btn.click(timeout=1000)
                await page.wait_for_timeout(150)
                if await self._is_layout_dialog_open() == 0:
                    return
            except Exception:
                pass
        # Click outside the dialog area to dismiss without revert.
        try:
            await page.mouse.click(8, 8)
            await page.wait_for_timeout(150)
        except Exception:
            pass

    async def enumerate_subcharts(self) -> list[SubChartInfo]:
        """Enumerate visible sub-charts and read per-chart symbol best-effort."""
        page = self._require_page()
        await page.bring_to_front()
        widget_count = await self._count_visible_chart_widgets()
        if widget_count == 0:
            # Fallback: single active chart
            return [SubChartInfo(index=0, symbol=await self.get_active_symbol())]

        out: list[SubChartInfo] = []
        for idx in range(widget_count):
            await self.activate_subchart(idx)
            out.append(SubChartInfo(index=idx, symbol=await self.get_symbol_search_value()))
        return out

    async def activate_subchart(self, idx: int) -> None:
        page = self._require_page()
        await page.bring_to_front()
        if idx < 0:
            return
        clicked = await page.evaluate(
            """
            (targetIdx) => {
              const nodes = Array.from(
                document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
              ).filter((el) => {
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
              });
              if (targetIdx < 0 || targetIdx >= nodes.length) {
                return false;
              }
              const el = nodes[targetIdx];
              el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
              el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
              el.click();
              return true;
            }
            """,
            idx,
        )
        if clicked:
            await page.wait_for_timeout(220)
        else:
            # Fallback path for single-chart / selector drift.
            widgets = page.locator("[data-name='chart-widget'], [class*='chart-widget']")
            if await widgets.count() > idx:
                await widgets.nth(idx).click()
                await page.wait_for_timeout(220)
        await self.expand_collapsed_indicator_rows(idx)

    async def expand_collapsed_indicator_rows(self, chart_widget_index: int) -> None:
        """Expand collapsed indicator stacks without opening per-indicator *Settings* dialogs.

        Only targets:

        - ``button[data-qa-id="legend-toggler"]`` (TV pane control; may sit outside ``[data-name='legend']``).
        - Small **digit-only** chips inside the legend container (not toolbar / not settings).

        A previous broad ``button[aria-label*='indicator']`` pass matched gear/settings controls and
        caused repeated *Settings* popups; that logic was removed.
        """
        page = self._require_page()
        await page.bring_to_front()
        idx = max(0, int(chart_widget_index))
        try:
            summary = await page.evaluate(
                """
                (chartIdx) => {
                  const nodes = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  ).filter((el) => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  });
                  const root = (chartIdx >= 0 && chartIdx < nodes.length) ? nodes[chartIdx] : document.body;

                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return r.width > 2 && r.height > 2 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                  };

                  const visibleRowCount = () =>
                    Array.from(
                      root.querySelectorAll(
                        "[data-name='legend-source-item'], [class*='sourceItem'], [class*='item'][class*='study']"
                      )
                    ).filter(isVisible).length;

                  const clickToggle = (el) => {
                    try {
                      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                      el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                      return true;
                    } catch (_) {
                      return false;
                    }
                  };

                  const isSettingsLike = (el) => {
                    const a = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.getAttribute("data-name") || ""}`.toLowerCase();
                    return /settings|設定|gear|options|編輯|edit indicator|indicator settings/.test(a);
                  };

                  const isMoreCountChip = (el) => {
                    if (isSettingsLike(el)) return false;
                    if (el.closest("button[data-qa-id='legend-toggler']")) return false;
                    const t = (el.textContent || "").replace(/\\s+/g, " ").trim();
                    if (!t || t.length > 14) return false;
                    if (/^\\d{1,3}\\s*\\+$/.test(t)) return true;
                    if (/^\\d{1,3}$/.test(t)) return true;
                    return /^[\\u02c5\\u2304\\u2228\\u25bc\\u25be\\u22c1▼⌄˅∨v]+\\s*\\d{1,3}\\s*$/i.test(t);
                  };

                  let totalClicks = 0;
                  let togglerUsed = false;

                  const pickTogglers = () => {
                    let list = Array.from(
                      root.querySelectorAll(
                        "button[data-qa-id='legend-toggler'], [data-qa-id='legend-toggler'][role='button']"
                      )
                    ).filter(isVisible);
                    if (!list.length && root !== document.body) {
                      const rr = root.getBoundingClientRect();
                      list = Array.from(
                        document.querySelectorAll("button[data-qa-id='legend-toggler']")
                      ).filter(isVisible).filter((el) => {
                        const r = el.getBoundingClientRect();
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        return cx >= rr.left && cx <= rr.right && cy >= rr.top && cy <= rr.bottom;
                      });
                    }
                    return list;
                  };

                  for (let round = 0; round < 4; round++) {
                    const before = visibleRowCount();
                    let clickedThisRound = false;

                    if (!togglerUsed) {
                      for (const el of pickTogglers()) {
                        const aria = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""}`;
                        const ariaLower = aria.toLowerCase();
                        if (/隱藏.*圖例|hide.*legend/i.test(ariaLower) && !/顯示|show/i.test(aria)) {
                          togglerUsed = true;
                          break;
                        }
                        const counterEl = el.querySelector("[class*='counter']");
                        const ctext = (counterEl && counterEl.textContent) ? counterEl.textContent.replace(/\\s+/g, "").trim() : "";
                        const n = parseInt(ctext, 10);
                        const hasCountChip = /^\\d{1,3}$/.test(ctext);
                        const showZh = aria.includes("顯示指標圖例");
                        const showEn = /show\\s+.*legend/i.test(aria) && /indicator|object/i.test(ariaLower);
                        if (hasCountChip && Number.isFinite(n) && n > 0) {
                          clickToggle(el);
                          clickedThisRound = true;
                          totalClicks += 1;
                          togglerUsed = true;
                          break;
                        }
                        if (showZh || showEn) {
                          clickToggle(el);
                          clickedThisRound = true;
                          totalClicks += 1;
                          togglerUsed = true;
                          break;
                        }
                      }
                    }

                    const legendHosts = [];
                    root.querySelectorAll("[data-name='legend'], [class*='pane-legend']").forEach((h) => legendHosts.push(h));
                    for (const host of legendHosts) {
                      const btns = Array.from(host.querySelectorAll("button, [role='button']")).filter(isVisible);
                      for (const el of btns) {
                        if (isSettingsLike(el)) continue;
                        if (!isMoreCountChip(el)) continue;
                        if (clickToggle(el)) {
                          clickedThisRound = true;
                          totalClicks += 1;
                        }
                        if (totalClicks > 12) break;
                      }
                    }

                    const after = visibleRowCount();
                    if (!clickedThisRound && after <= before) break;
                  }
                  return { totalClicks, finalRows: visibleRowCount(), chartIdx };
                }
                """,
                idx,
            )
        except Exception:
            summary = {"totalClicks": 0, "finalRows": 0, "chartIdx": idx, "error": True}
        await page.wait_for_timeout(120)
        # Playwright path: real click often works when synthetic DOM events do not.
        try:
            pane = page.locator("[data-name='chart-widget'], [class*='chart-widget']").nth(idx)
            lt = pane.locator("button[data-qa-id='legend-toggler']").first
            if await lt.count():
                aria = (await lt.get_attribute("aria-label") or "").strip()
                counter = lt.locator("[class*='counter']").first
                ctext = ""
                if await counter.count():
                    ctext = (await counter.inner_text()).strip()
                digits = bool(ctext) and ctext.isdigit() and 1 <= int(ctext) <= 999
                show_legend = "顯示指標圖例" in aria or ("show" in aria.lower() and "legend" in aria.lower())
                hide_only = ("隱藏" in aria or "hide" in aria.lower()) and not show_legend
                if not hide_only and (digits or show_legend):
                    await lt.scroll_into_view_if_needed()
                    await lt.click(timeout=2500)
                    await page.wait_for_timeout(180)
        except Exception:
            pass
        self._log(
            "[legend_expand_rows] "
            f"chart_widget={idx} clicks={summary.get('totalClicks')} final_rows={summary.get('finalRows')}"
        )

    async def get_active_symbol(self) -> str | None:
        """Best-effort read active chart symbol from header."""
        by_search = await self.get_symbol_search_value()
        if by_search:
            return by_search
        cands = await self.get_active_symbol_candidates()
        return cands[0] if cands else None

    async def get_symbol_search_value(self) -> str | None:
        """Read symbol from top-left symbol search/input control."""
        page = self._require_page()
        await page.bring_to_front()
        selectors = [
            "button#header-toolbar-symbol-search span[class^='value-']",
            "button#header-toolbar-symbol-search",
            "span[class^='value-']",
            "button[aria-label*='Symbol Search'] span[class^='value-']",
            "button[data-name*='symbol-search'] span[class^='value-']",
            "input[data-role='symbol-search-input']",
            "input[placeholder*='Symbol']",
            "input[aria-label*='Symbol']",
            "input[aria-label*='商品']",
            "input[aria-label*='Ticker']",
            "button[aria-label*='Symbol Search']",
            "button[data-name*='symbol-search']",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                # input-like controls
                value = (await loc.input_value()).strip()
            except Exception:
                value = (await loc.inner_text()).strip()
            if value:
                token = self._extract_symbol_token(value)
                if token:
                    return token
        return None

    async def get_active_symbol_candidates(self) -> list[str]:
        """Collect multiple symbol/name candidates from active chart UI."""
        page = self._require_page()
        await page.bring_to_front()
        search_value = await self.get_symbol_search_value()
        selectors = [
            "[data-name='legend-source-item-main-series']",
            "[data-name='symbol-header']",
            "[class*='legendMainSource']",
            "[data-name='legend']",
            "[class*='pane-legend']",
        ]
        out: list[str] = []
        seen: set[str] = set()
        if search_value:
            out.append(search_value)
            seen.add(search_value)
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count():
                text = (await loc.inner_text()).strip()
                if text:
                    top = text.split("\n", 1)[0].strip()
                    if top and top not in seen:
                        out.append(top)
                        seen.add(top)
                    if text not in seen:
                        out.append(text)
                        seen.add(text)
        return out

    @staticmethod
    def _extract_symbol_token(raw: str) -> str | None:
        text = raw.strip().upper()
        if not text:
            return None
        lines = [ln.strip().upper() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        # Prefer concise symbol-like lines.
        for ln in lines:
            if re.fullmatch(r"[A-Z0-9._:-]{1,20}", ln):
                return ln
        # Fallback: regex search inside noisy text blocks.
        m = re.search(r"\b[A-Z]{1,8}(?::[A-Z0-9._-]{1,16})?\b", text)
        return m.group(0) if m else None

    # ---------- Phase A helpers (active chart only) ----------
    async def list_chart_indicators(self) -> list[IndicatorInfo]:
        """Read current chart indicator titles from the chart legend area."""
        page = self._require_page()
        await page.bring_to_front()

        selectors = [
            "div[data-name='legend'] [data-name='legend-source-item']",
            "div[class*='legend'] [class*='sourceItem']",
        ]
        names: list[str] = []
        for selector in selectors:
            nodes = page.locator(selector)
            count = await nodes.count()
            if count == 0:
                continue
            for idx in range(count):
                text = (await nodes.nth(idx).inner_text()).strip()
                if text:
                    names.append(text)
            if names:
                break
        # de-dup but keep order
        seen: set[str] = set()
        out: list[IndicatorInfo] = []
        for raw_name in names:
            title = raw_name.split("\n", 1)[0].strip()
            if title in seen:
                continue
            seen.add(title)
            out.append(IndicatorInfo(title=title))
        return out

    async def _tv_shows_indicator_quota_block(self, page: Page) -> bool:
        """Detect free-plan / max-indicators notices (wording varies by locale and TV build)."""
        try:
            blob = await page.evaluate(
                """() => {
                    const chunks = [];
                    const bodyText = (document.body && document.body.innerText) || "";
                    chunks.push(bodyText);
                    document.querySelectorAll('[role="alertdialog"], [role="dialog"], [class*="toast"]').forEach((el) => {
                        chunks.push(el.innerText || "");
                    });
                    return chunks.join("\\n").slice(0, 120000);
                }"""
            )
        except Exception:
            return False
        low = str(blob).lower()
        needles = (
            "maximum number of indicators",
            "max number of indicators",
            "max indicators",
            "too many indicators",
            "indicator limit",
            "reached the limit",
            "upgrade your plan",
            "upgrade to add",
            "指標數量已達上限",
            "指標已達上限",
            "已達指標上限",
        )
        return any(n in low for n in needles)

    async def _finish_add_favorite_indicator_attempt(self, page: Page) -> None:
        """After choosing a favorite row: detect quota toast/modal, else dismiss picker."""
        await page.wait_for_timeout(450)
        if await self._tv_shows_indicator_quota_block(page):
            await self._dump_dom("indicator_quota_exceeded")
            for _ in range(4):
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(120)
            raise IndicatorQuotaExceededError(
                "TradingView 拒絕新增指標（常見原因：免費版指標數量已滿）。"
                "請刪除圖上其他指標或升級方案後再執行批次。"
            )
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)

    async def add_favorite_indicator(self, name: str) -> None:
        """Add indicator from favorites panel by indicator title."""
        page = self._require_page()
        await page.bring_to_front()
        target_tokens = ("daily", "weekly", "gex")

        # Path A: open indicators dialog via shortcut.
        await page.keyboard.press("/")
        await page.wait_for_timeout(350)
        if not await self._is_indicator_dialog_open():
            # Path B fallback: click toolbar "Indicators / FX" button.
            fx_btn = page.locator(
                "[data-name='legend-indicators-button'], "
                "[data-name='header-toolbar-indicators'], "
                "button[aria-label*='Indicators'], "
                "button[aria-label*='指標'], "
                "button:has-text('Indicators')"
            ).first
            if await fx_btn.count():
                await fx_btn.click()
                await page.wait_for_timeout(350)

        await self._open_favorites_tab_if_present()

        # Best-effort search if input exists; if not, still try direct row match.
        search_box = page.locator(
            "[role='dialog'] input[placeholder*='Search'], "
            "[role='dialog'] input[placeholder*='search'], "
            "[role='dialog'] input[aria-label*='Search'], "
            "[role='dialog'] input[aria-label*='搜尋'], "
            "[role='dialog'] input[type='search'], "
            "[role='dialog'] input[type='text']"
        ).first
        if await search_box.count():
            await search_box.click()
            # User-verified query: this exact phrase reliably surfaces the target.
            await search_box.fill("daily & weekly GEX")
            await page.wait_for_timeout(400)
            dialog = page.locator("[role='dialog']").last
            text_hit = dialog.get_by_text("Daily & Weekly GEX", exact=False).first
            if await text_hit.count():
                await text_hit.click()
                await page.wait_for_timeout(500)
                await self._finish_add_favorite_indicator_attempt(page)
                return

            # Fallback: keyboard confirm current search result.
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            await self._finish_add_favorite_indicator_attempt(page)
            return

        row_selectors = (
            "[role='dialog'] [role='row'], "
            "[role='dialog'] [class*='item'], "
            "[role='dialog'] [data-name*='list-item']"
        )
        rows = page.locator(row_selectors)
        match_index: int | None = None
        row_count = await rows.count()
        for idx in range(row_count):
            text = (await rows.nth(idx).inner_text()).strip().lower()
            if all(tok in text for tok in target_tokens):
                match_index = idx
                break

        if match_index is None:
            # Last fallback: try exact text selector with caller-provided name.
            exact_row = page.locator(
                f"[role='dialog'] [role='row']:has-text('{name}'), "
                f"[role='dialog'] [class*='item']:has-text('{name}')"
            ).first
            if await exact_row.count():
                await exact_row.click()
                await page.wait_for_timeout(350)
                await self._finish_add_favorite_indicator_attempt(page)
                return
            await self._dump_dom("favorite_not_found")
            visible = await self._collect_indicator_dialog_rows(limit=8)
            await page.keyboard.press("Escape")
            raise FavoriteNotFoundError(
                f"Favorite indicator not found: {name}. "
                "Please add it to TradingView favorites first. "
                f"Visible rows: {visible}"
            )
        await rows.nth(match_index).click()
        await page.wait_for_timeout(350)
        await self._finish_add_favorite_indicator_attempt(page)

    async def open_indicator_settings(self, title_keyword: str = "Daily & Weekly GEX") -> None:
        """Open indicator settings from chart legend/context menu."""
        page = self._require_page()
        await page.bring_to_front()

        # If settings dialog is already open for this indicator, continue directly.
        if await self._is_target_settings_dialog_open(title_keyword):
            await self._initialize_indicator_settings_inputs()
            return

        indicator = await self._resolve_visible_indicator_locator(title_keyword)
        if indicator is None:
            hidden_indicator = await self._resolve_any_indicator_locator(title_keyword)
            if hidden_indicator is None:
                await self._dump_dom("open_settings_indicator_missing")
                raise RuntimeError(f"Indicator not found on active chart: {title_keyword}")
            forced = await self._force_open_indicator_settings_via_dom(hidden_indicator)
            if forced:
                await page.wait_for_timeout(260)
                if await self._is_target_settings_dialog_open(title_keyword):
                    await self._initialize_indicator_settings_inputs()
                    return
            # Keep a locator for downstream context-menu fallback.
            indicator = hidden_indicator

        # Path A: force/detached-safe open sequence.
        if await self._try_open_settings_via_indicator(indicator, title_keyword):
            return

        # Path B: click inline settings/gear button if present.
        gear_btn = indicator.locator(
            "button[aria-label*='Settings'], "
            "button[aria-label*='設定'], "
            "[data-name*='legend-source-item-settings'], "
            "[class*='settingsButton']"
        ).first
        if await gear_btn.count():
            await gear_btn.click()
            await page.wait_for_timeout(250)
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return

        # Path C: context menu.
        try:
            await indicator.click(button="right", force=True, timeout=1200)
        except Exception:
            try:
                await indicator.evaluate(
                    """
                    (el) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return false;
                      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
                      return true;
                    }
                    """
                )
            except Exception:
                pass
        await page.wait_for_timeout(150)
        settings_item = page.locator(
            "[role='menuitem']:has-text('Settings'), "
            "[role='menuitem']:has-text('設定'), "
            "#overlap-manager-root [class*='item']:has-text('Settings'), "
            "#overlap-manager-root [class*='item']:has-text('設定')"
        ).first
        if await settings_item.count():
            await settings_item.click()
            await page.wait_for_timeout(300)
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return

        # Last fallback: search settings text globally in overlap root.
        generic_settings = page.locator(
            "#overlap-manager-root :text('Settings'), "
            "#overlap-manager-root :text('設定')"
        ).first
        if await generic_settings.count():
            await generic_settings.click()
            await page.wait_for_timeout(300)
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return

        if not await self._is_target_settings_dialog_open(title_keyword):
            await self._dump_dom("open_settings_menu_missing")
            raise RuntimeError("Could not find 'Settings' in indicator context menu.")

    async def _resolve_visible_indicator_locator(self, title_keyword: str):
        """Return the first visible legend indicator locator for keyword."""
        scoped = await self._collect_visible_indicator_locators(title_keyword, active_only=True)
        if scoped:
            return scoped[0]
        global_matches = await self._collect_visible_indicator_locators(title_keyword, active_only=False)
        if global_matches:
            return global_matches[0]
        return None

    async def _resolve_any_indicator_locator(self, title_keyword: str):
        """Return first matched legend indicator locator, visible or not."""
        scoped = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if scoped:
            return scoped[0]
        global_matches = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        if global_matches:
            return global_matches[0]
        return None

    async def _force_open_indicator_settings_via_dom(self, locator) -> bool:
        """Try opening settings by dispatching DOM events even when hidden."""
        page = self._require_page()
        try:
            opened = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return false;
                  row.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                  row.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                  row.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
                  return true;
                }
                """
            )
            if opened:
                await page.wait_for_timeout(180)
                return True
        except Exception:
            return False
        return False

    async def _try_open_settings_via_indicator(self, indicator, title_keyword: str) -> bool:
        """Try resilient open sequence that does not depend on visibility.

        TradingView legend rows often span the full chart-pane width. The
        actual hit area (title text + hover icons) is on the left ~200px.
        Playwright's default click target (bbox center) lands on the chart
        canvas instead, which TV interprets as a chart click and may open
        the wrong indicator's settings (whichever overlay is at that
        canvas position). We always target the title text node first.
        """
        page = self._require_page()
        try:
            await indicator.scroll_into_view_if_needed()
        except Exception:
            pass

        if await self._dblclick_indicator_legend_target(indicator):
            await page.wait_for_timeout(220)
            if await self._is_chart_settings_dialog_open():
                self._chart_settings_misopen_count += 1
                self._log("[open_settings] chart-settings-opened-by-mistake; closing and retrying")
                await page.keyboard.press("Escape")
                await self._ensure_indicator_dialog_closed()
                return False
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return True

        # Fallback: synthetic dblclick dispatched directly on the row element
        # is precise (no bbox center math) but may not always reach TV's React
        # listener -- keep as last-ditch attempt.
        forced = await self._force_open_indicator_settings_via_dom(indicator)
        if forced:
            await page.wait_for_timeout(220)
            if await self._is_chart_settings_dialog_open():
                self._chart_settings_misopen_count += 1
                self._log("[open_settings] chart-settings-opened-by-mistake; closing and retrying")
                await page.keyboard.press("Escape")
                await self._ensure_indicator_dialog_closed()
                return False
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return True
        return False

    async def _dblclick_indicator_legend_target(self, locator) -> bool:
        """Double-click on the indicator title node (or row left edge).

        Returns True when a real mouse dblclick was issued on a sane target;
        callers must still verify the resulting dialog matches the row.
        """
        page = self._require_page()
        try:
            target = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  const titleNode = row.querySelector(
                    "[data-name='legend-source-item-title'], " +
                    "[data-name*='legend-source-item-title'], " +
                    "[class*='sourceTitle'], [class*='studyTitle']"
                  ) || row.querySelector("[class*='title']");
                  const rowRect = row.getBoundingClientRect();
                  if (titleNode) {
                    const r = titleNode.getBoundingClientRect();
                    if (r.width > 1 && r.height > 1 && r.width < rowRect.width * 0.9) {
                      return {
                        kind: "title",
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        rowLeft: Math.round(rowRect.left),
                        rowWidth: Math.round(rowRect.width)
                      };
                    }
                  }
                  if (rowRect.width <= 0 || rowRect.height <= 0) return null;
                  // Fallback: pin the click to the legend's left edge where
                  // the indicator label/icon area lives in TV's UI.
                  const offsetX = Math.min(80, Math.max(20, rowRect.width * 0.05));
                  return {
                    kind: "row-left",
                    x: rowRect.left + offsetX,
                    y: rowRect.top + rowRect.height / 2,
                    rowLeft: Math.round(rowRect.left),
                    rowWidth: Math.round(rowRect.width)
                  };
                }
                """
            )
        except Exception:
            return False
        if not isinstance(target, dict):
            return False
        x = target.get("x")
        y = target.get("y")
        if x is None or y is None:
            return False
        try:
            await page.mouse.move(float(x), float(y))
            await page.wait_for_timeout(40)
            await page.mouse.dblclick(float(x), float(y))
        except Exception:
            return False
        self._log(
            f"[open_settings] dblclick kind={target.get('kind')} "
            f"x={int(float(x))} y={int(float(y))} "
            f"row_left={target.get('rowLeft')} row_width={target.get('rowWidth')}"
        )
        return True

    async def open_indicator_settings_at(self, title_keyword: str, match_index: int) -> None:
        """Open settings for a specific matched legend indicator (0-based)."""
        page = self._require_page()
        await page.bring_to_front()

        all_candidates = await self._collect_visible_indicator_locators(title_keyword)
        if len(all_candidates) <= match_index:
            # Fallback for compact viewports: index from non-visible matches.
            all_any = await self._collect_any_indicator_locators(title_keyword)
            if len(all_any) <= match_index:
                raise RuntimeError(f"Indicator index out of range for '{title_keyword}': {match_index}")
            indicator = all_any[match_index]
            forced = await self._force_open_indicator_settings_via_dom(indicator)
            if forced:
                await page.wait_for_timeout(220)
                if await self._is_target_settings_dialog_open(title_keyword):
                    await self._initialize_indicator_settings_inputs()
                    return
        else:
            indicator = all_candidates[match_index]

        if await self._try_open_settings_via_indicator(indicator, title_keyword):
            return

        try:
            await indicator.click(button="right", force=True, timeout=1200)
        except Exception:
            try:
                await indicator.evaluate(
                    """
                    (el) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return false;
                      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
                      return true;
                    }
                    """
                )
            except Exception:
                pass
        await page.wait_for_timeout(150)
        settings_item = page.locator(
            "[role='menuitem']:has-text('Settings'), "
            "[role='menuitem']:has-text('設定'), "
            "#overlap-manager-root [class*='item']:has-text('Settings'), "
            "#overlap-manager-root [class*='item']:has-text('設定')"
        ).first
        if await settings_item.count():
            await settings_item.click()
            await page.wait_for_timeout(260)
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return
        raise RuntimeError("Could not open settings for indexed indicator.")

    async def _collect_any_indicator_locators(self, title_keyword: str, *, active_only: bool = True) -> list:
        """Collect matched legend indicator locators, including hidden ones."""
        return await self._collect_indicator_locators(
            title_keyword=title_keyword,
            active_only=active_only,
            visible_only=False,
        )

    async def _collect_visible_indicator_locators(self, title_keyword: str, *, active_only: bool = True) -> list:
        """Collect visible legend indicator locators in stable visual order."""
        return await self._collect_indicator_locators(
            title_keyword=title_keyword,
            active_only=active_only,
            visible_only=True,
        )

    @staticmethod
    def _title_keyword_tokens(title_keyword: str) -> list[str]:
        tokens = [tok for tok in re.findall(r"[a-z0-9]+", (title_keyword or "").lower()) if tok]
        return tokens if tokens else ["daily", "weekly", "gex"]

    @staticmethod
    def _normalize_keyword_text(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    @staticmethod
    def _normalize_start_date_text(raw: str | None) -> str | None:
        """Normalize TradingView date text to ISO date (YYYY-MM-DD)."""
        text = (raw or "").strip()
        if not text:
            return None
        m = re.search(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", text)
        if not m:
            return None
        try:
            parsed = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return parsed.isoformat()

    @staticmethod
    def _tokens_match_in_order(text: str, tokens: list[str]) -> bool:
        if not text or not tokens:
            return False
        cursor = 0
        for tok in tokens:
            pos = text.find(tok, cursor)
            if pos < 0:
                return False
            cursor = pos + len(tok)
        return True

    @staticmethod
    def _is_weekly_gex_keyword(title_keyword: str) -> bool:
        tokens = set(PlaywrightCDPAutomator._title_keyword_tokens(title_keyword))
        return {"daily", "weekly", "gex"}.issubset(tokens)

    def _indicator_title_matches_keyword(self, title: str, title_keyword: str) -> bool:
        norm_title = self._normalize_keyword_text(title)
        if not norm_title:
            return False
        norm_keyword = self._normalize_keyword_text(title_keyword)
        tokens = self._title_keyword_tokens(title_keyword)
        # For the target indicator family, keep matching strict to prevent
        # opening unrelated rows that happen to contain overlapping tokens.
        if self._is_weekly_gex_keyword(title_keyword):
            return bool(re.search(r"\bdaily\b.*\bweekly\b.*\bgex\b", norm_title))
        if norm_keyword and norm_keyword in norm_title:
            return True
        return self._tokens_match_in_order(norm_title, tokens)

    async def _indicator_row_title(self, locator) -> str | None:
        """Extract indicator row title text (first line / title node)."""
        try:
            title = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  const pickText = (raw) => {
                    if (!raw) return "";
                    return String(raw).replace(/\\s+/g, " ").trim();
                  };
                  const pickNode = (node) => {
                    if (!node) return "";
                    return pickText(node.textContent || "");
                  };
                  const attrs = [
                    row.getAttribute("aria-label"),
                    row.getAttribute("title"),
                    row.getAttribute("data-source-title"),
                    row.getAttribute("data-study-title")
                  ];
                  for (const raw of attrs) {
                    const txt = pickText(raw);
                    if (txt) return txt;
                  }
                  const named = [
                    row.querySelector("[data-name='legend-source-item-title']"),
                    row.querySelector("[data-name*='legend-source-item-title']"),
                    row.querySelector("[class*='sourceTitle']"),
                    row.querySelector("[class*='studyTitle']"),
                    row.querySelector("[class*='title']")
                  ];
                  for (const node of named) {
                    const txt = pickNode(node);
                    if (txt) return txt;
                  }
                  const raw = (row.innerText || row.textContent || "").trim();
                  if (!raw) return null;
                  const firstLine = raw.split(/\\n+/)[0].trim();
                  if (firstLine) return firstLine;
                  return raw.replace(/\\s+/g, " ").trim() || null;
                }
                """
            )
        except Exception:
            return None
        cleaned = (str(title).strip() if title else "")
        return cleaned or None

    async def _indicator_row_sort_key(self, locator) -> tuple[int, int, int, int]:
        """Compute a stable visual ordering key for legend rows."""
        try:
            data = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return [999999, 999999, 999999, 999999];
                  const legend = row.closest("[data-name='legend'], [class*='legend']");
                  const rect = row.getBoundingClientRect();
                  const legendRect = legend ? legend.getBoundingClientRect() : null;
                  const q = (v) => Number.isFinite(v) ? Math.round(v / 4) : 999999;
                  const y = legendRect ? q(rect.top - legendRect.top) : q(rect.top);
                  const x = legendRect ? q(rect.left - legendRect.left) : q(rect.left);
                  let rowOrder = 999999;
                  if (row.parentElement && row.parentElement.children) {
                    rowOrder = Array.prototype.indexOf.call(row.parentElement.children, row);
                    if (!Number.isFinite(rowOrder) || rowOrder < 0) rowOrder = 999999;
                  }
                  const isVisible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const st = window.getComputedStyle(node);
                    return r.width > 2 && r.height > 2 && st.display !== "none" && st.visibility !== "hidden";
                  };
                  const legends = Array.from(
                    document.querySelectorAll("[data-name='legend'], [class*='legend']")
                  ).filter(isVisible);
                  legends.sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    if (Math.abs(ar.top - br.top) > 2) return ar.top - br.top;
                    return ar.left - br.left;
                  });
                  let legendOrder = legends.indexOf(legend);
                  if (!Number.isFinite(legendOrder) || legendOrder < 0) legendOrder = 999999;
                  return [legendOrder, rowOrder, y, x];
                }
                """
            )
            if isinstance(data, (list, tuple)) and len(data) >= 4:
                return (int(data[0]), int(data[1]), int(data[2]), int(data[3]))
        except Exception:
            pass
        return (999999, 999999, 999999, 999999)

    async def _indicator_row_dom_key(self, locator) -> str | None:
        """Best-effort DOM path key for de-duplicating selector overlaps."""
        try:
            key = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  const seg = (node) => {
                    const parent = node.parentElement;
                    let idx = -1;
                    if (parent && parent.children) {
                      idx = Array.prototype.indexOf.call(parent.children, node);
                    }
                    const tag = (node.tagName || "").toLowerCase();
                    const dn = node.getAttribute("data-name") || "";
                    const sid = node.getAttribute("data-source-id") || "";
                    const iid = node.getAttribute("data-instance-id") || "";
                    const id = node.id || "";
                    return `${tag}#${dn}#${sid}#${iid}#${id}#${idx}`;
                  };
                  const parts = [];
                  let cur = row;
                  let depth = 0;
                  while (cur && cur.nodeType === Node.ELEMENT_NODE && depth < 8) {
                    parts.push(seg(cur));
                    if (cur.matches("[data-name='legend'], [class*='legend']")) break;
                    cur = cur.parentElement;
                    depth += 1;
                  }
                  return parts.join(">");
                }
                """
            )
        except Exception:
            return None
        return str(key).strip() if key else None

    async def _collect_indicator_locators(
        self,
        *,
        title_keyword: str,
        active_only: bool,
        visible_only: bool,
    ) -> list:
        """Collect target indicator rows in deterministic DOM order.

        Tags every matched row with ``data-gex-scan-row=<idx>`` inside a single
        JS evaluate pass. This avoids:
          - Selector overlap producing duplicate Python locators.
          - Unstable signatures based on live legend text (price tickers).
          - Lazy ``locator.nth(idx)`` resolution that drifts when the legend
            re-renders between operations.
        """
        page = self._require_page()
        scan_attr = "data-gex-scan-row"
        strict_family = self._is_weekly_gex_keyword(title_keyword)
        tokens = list(self._title_keyword_tokens(title_keyword))
        norm_keyword = self._normalize_keyword_text(title_keyword)
        use_scope_root = active_only and self._scoped_subchart_index is not None
        try:
            result = await page.evaluate(
                """
                ({scopeAttr, scanAttr, useScopeRoot, visibleOnly, strictFamily, tokens, normKeyword}) => {
                  document.querySelectorAll(`[${scanAttr}]`).forEach((el) => el.removeAttribute(scanAttr));
                  const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 2 && r.height > 2
                      && st.display !== "none" && st.visibility !== "hidden";
                  };
                  const normalize = (s) => (s || "")
                    .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                  const scopeRoot = useScopeRoot
                    ? document.querySelector(`[${scopeAttr}='1']`)
                    : null;
                  const root = scopeRoot || document;
                  const raw = root.querySelectorAll(
                    "[data-name='legend-source-item'], " +
                    "[class*='sourceItem'], " +
                    "[class*='item'][class*='study']"
                  );
                  const seen = new Set();
                  const rows = [];
                  for (const el of raw) {
                    const row = el.closest("[data-name='legend-source-item']")
                      || el.closest("[class*='sourceItem']")
                      || el.closest("[class*='item'][class*='study']")
                      || el;
                    if (!row || seen.has(row)) continue;
                    seen.add(row);
                    rows.push(row);
                  }
                  const legendOf = (row) =>
                    row.closest("[data-name='legend'], [class*='legend']");
                  rows.sort((a, b) => {
                    const la = legendOf(a);
                    const lb = legendOf(b);
                    if (la !== lb) {
                      const ar = (la || a).getBoundingClientRect();
                      const br = (lb || b).getBoundingClientRect();
                      if (Math.abs(ar.top - br.top) > 2) return ar.top - br.top;
                      if (Math.abs(ar.left - br.left) > 2) return ar.left - br.left;
                    }
                    const pos = a.compareDocumentPosition(b);
                    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
                    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
                    return 0;
                  });
                  const titlesOut = [];
                  let next = 0;
                  for (const row of rows) {
                    if (visibleOnly && !isVisible(row)) continue;
                    let title = "";
                    const titleNode = row.querySelector(
                      "[data-name='legend-source-item-title'], " +
                      "[data-name*='legend-source-item-title'], " +
                      "[class*='sourceTitle'], [class*='studyTitle']"
                    );
                    if (titleNode) title = (titleNode.textContent || "").trim();
                    if (!title) {
                      const stableAttrs = [
                        row.getAttribute("data-source-title"),
                        row.getAttribute("data-study-title"),
                        row.getAttribute("aria-label"),
                        row.getAttribute("title"),
                      ];
                      for (const v of stableAttrs) {
                        if (v && !title) title = v;
                      }
                    }
                    if (!title) {
                      title = ((row.innerText || row.textContent) || "")
                        .split("\\n")[0].trim();
                    }
                    const ntitle = normalize(title);
                    if (!ntitle) continue;
                    let ok;
                    if (strictFamily) {
                      ok = /\\bdaily\\b.*\\bweekly\\b.*\\bgex\\b/.test(ntitle);
                    } else if (normKeyword && ntitle.indexOf(normKeyword) >= 0) {
                      ok = true;
                    } else {
                      let cursor = 0;
                      ok = true;
                      for (const tok of tokens) {
                        const pos = ntitle.indexOf(tok, cursor);
                        if (pos < 0) { ok = false; break; }
                        cursor = pos + tok.length;
                      }
                    }
                    if (!ok) continue;
                    row.setAttribute(scanAttr, String(next));
                    titlesOut.push(title.replace(/\\s+/g, " ").slice(0, 160));
                    next += 1;
                  }
                  return { count: next, titles: titlesOut };
                }
                """,
                {
                    "scopeAttr": self._scope_attr,
                    "scanAttr": scan_attr,
                    "useScopeRoot": use_scope_root,
                    "visibleOnly": visible_only,
                    "strictFamily": strict_family,
                    "tokens": tokens,
                    "normKeyword": norm_keyword,
                },
            )
        except Exception:
            return []
        if not isinstance(result, dict):
            return []
        count = int(result.get("count") or 0)
        if count <= 0:
            return []
        titles = list(result.get("titles") or [])
        if titles:
            preview = " | ".join(titles[:8])
            self._log(
                "[collect] tagged="
                f"{count} active={int(active_only)} visible={int(visible_only)} "
                f"scope={'pinned' if use_scope_root else 'document'} "
                f"titles=[{preview}]"
            )
        locators: list = []
        for idx in range(count):
            loc = page.locator(f"[{scan_attr}='{idx}']").first
            if active_only and self._scoped_subchart_index is not None:
                try:
                    if not await self._locator_belongs_to_scoped_widget(loc):
                        continue
                except Exception:
                    continue
            locators.append(loc)
        return locators

    async def _locator_belongs_to_scoped_widget(self, locator) -> bool:
        """Best-effort bind a legend row to the currently scoped subchart."""
        idx = self._scoped_subchart_index
        if idx is None:
            return True
        try:
            ok = await locator.evaluate(
                """
                (el, targetIdx) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return false;
                  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
                  const distanceToRect = (rect, target) => {
                    const cx = (rect.left + rect.right) / 2;
                    const cy = (rect.top + rect.bottom) / 2;
                    const nx = clamp(cx, target.left, target.right);
                    const ny = clamp(cy, target.top, target.bottom);
                    const dx = cx - nx;
                    const dy = cy - ny;
                    return Math.sqrt(dx * dx + dy * dy);
                  };
                  const overlaps = (a, b) =>
                    a.right >= b.left && a.left <= b.right && a.bottom >= b.top && a.top <= b.bottom;
                  const widgets = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  ).filter((node) => {
                    const r = node.getBoundingClientRect();
                    const st = window.getComputedStyle(node);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  });
                  if (targetIdx < 0 || targetIdx >= widgets.length) return true;
                  const widgetRects = widgets.map((node) => node.getBoundingClientRect());
                  const rowRect = row.getBoundingClientRect();
                  const legendRoot = row.closest("[data-name='legend'], [class*='legend']");
                  const legendRect = legendRoot ? legendRoot.getBoundingClientRect() : null;
                  const nearestWidgetIndex = (rect) => {
                    if (!rect) return -1;
                    let bestIdx = -1;
                    let bestDist = Number.POSITIVE_INFINITY;
                    for (let i = 0; i < widgetRects.length; i += 1) {
                      const wr = widgetRects[i];
                      const dist = distanceToRect(rect, wr);
                      if (dist < bestDist) {
                        bestDist = dist;
                        bestIdx = i;
                        continue;
                      }
                      if (Math.abs(dist - bestDist) < 1) {
                        // Tie-break: prefer the widget that actually overlaps.
                        if (bestIdx >= 0 && !overlaps(rect, widgetRects[bestIdx]) && overlaps(rect, wr)) {
                          bestIdx = i;
                          bestDist = dist;
                        }
                      }
                    }
                    return bestIdx;
                  };
                  const targetRect = widgetRects[targetIdx];
                  if (overlaps(rowRect, targetRect)) return true;
                  const rowOwner = nearestWidgetIndex(rowRect);
                  if (rowOwner >= 0) return rowOwner === targetIdx;
                  const legendOwner = nearestWidgetIndex(legendRect);
                  if (legendOwner >= 0) {
                    if (legendOwner !== targetIdx) return false;
                    const dist = distanceToRect(rowRect, targetRect);
                    return dist <= 120;
                  }
                  return false;
                }
                """,
                idx,
            )
            return bool(ok)
        except Exception:
            return True

    async def _indicator_search_locator(self, selector: str, *, active_only: bool):
        page = self._require_page()
        if not active_only:
            return page.locator(selector)
        marked_scope = page.locator(f"[{self._scope_attr}='1']").first
        if await marked_scope.count():
            scoped = marked_scope.locator(selector)
            if await scoped.count():
                return scoped
        active_idx = self._scoped_subchart_index
        if active_idx is None:
            active_idx = await self._detect_active_chart_widget_index()
        if active_idx is not None:
            widgets = page.locator("[data-name='chart-widget'], [class*='chart-widget']")
            if await widgets.count() > active_idx:
                scoped = widgets.nth(active_idx).locator(selector)
                if await scoped.count():
                    return scoped
        # Important: in some TV builds, legend rows are rendered outside
        # the widget subtree. Fall back to global query and rely on
        # _locator_belongs_to_scoped_widget() for ownership filtering.
        return page.locator(selector)

    async def _detect_active_chart_widget_index(self) -> int | None:
        page = self._require_page()
        try:
            idx = await page.evaluate(
                """
                () => {
                  const nodes = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  ).filter((el) => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  });
                  if (!nodes.length) return -1;
                  let bestIdx = 0;
                  let bestScore = -1;
                  for (let i = 0; i < nodes.length; i += 1) {
                    const el = nodes[i];
                    const cls = (el.className || "").toString().toLowerCase();
                    let score = 0;
                    if (cls.includes("active") || cls.includes("selected") || cls.includes("focused")) score += 4;
                    if (el.querySelector("[class*='active'], [class*='selected'], [class*='focused']")) score += 2;
                    if (el.contains(document.activeElement)) score += 3;
                    if (score > bestScore) {
                      bestScore = score;
                      bestIdx = i;
                    }
                  }
                  return bestIdx;
                }
                """
            )
        except Exception:
            return None
        idx = int(idx)
        return None if idx < 0 else idx

    async def count_indicators(self, title_keyword: str = "Daily & Weekly GEX") -> int:
        page = self._require_page()
        await page.bring_to_front()
        candidates = page.locator(
            f"[data-name='legend-source-item']:has-text('{title_keyword}'), "
            f"[class*='sourceItem']:has-text('{title_keyword}'), "
            f"[class*='item'][class*='study']:has-text('{title_keyword}'), "
            f"[data-name='legend'] [class*='item'][class*='study']:has-text('{title_keyword}')"
        )
        return await candidates.count()

    async def _remove_study_from_open_indicator_properties_dialog(self) -> bool:
        """Click Remove/Delete in the already-open indicator properties dialog."""
        page = self._require_page()
        remove_btn = page.locator(
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('Remove'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('Delete'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('移除'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('刪除')"
        ).first
        if not await remove_btn.count():
            return False
        try:
            await remove_btn.click()
            await page.wait_for_timeout(180)
            await self._confirm_delete_dialog_if_present()
            await self._close_chart_settings_if_open()
            return True
        except Exception:
            try:
                await self.close_settings(save=False)
            except Exception:
                pass
            return False

    async def build_weekly_gex_subchart_cache(
        self,
        *,
        keep_mondays: list[date] | None = None,
        title_keyword: str = "Daily & Weekly GEX",
    ) -> WeeklyGexSubchartCache:
        """One pass per stable DOM: read each row's start date + weekday levels.

        When ``keep_mondays`` is set, rows whose Monday is **strictly before**
        ``min(keep_mondays)`` are removed during this pass (same rule as
        ``remove_expired_weekly_gex_indicators``), avoiding a second sweep.
        """
        page = self._require_page()
        await page.bring_to_front()
        await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
        allow_global_fallback = self._allow_global_indicator_fallback()
        keep_sorted = sorted(set(keep_mondays or []))
        cutoff = keep_sorted[0] if keep_sorted else None
        final_rows: list[WeeklyGexRowSnapshot] = []
        probe_complete = True
        removed_expired = 0
        for _round in range(96):
            candidates = await self._collect_any_indicator_locators(
                title_keyword, active_only=True
            )
            if not candidates and allow_global_fallback:
                candidates = await self._collect_any_indicator_locators(
                    title_keyword, active_only=False
                )
            if not candidates:
                return WeeklyGexSubchartCache(
                    rows=[], probe_complete=True, removed_expired=removed_expired
                )
            pass_rows: list[WeeklyGexRowSnapshot] = []
            removed = False
            for cand in list(candidates):
                opened = await self._open_settings_for_locator(cand, title_keyword)
                if not opened:
                    probe_complete = False
                    continue
                try:
                    sig = await self._indicator_row_signature(cand)
                except Exception:
                    sig = None
                date_val, _time_val = await self.read_weekly_start_datetime()
                levels = await self.read_weekly_levels()
                normalized = self._normalize_start_date_text(date_val)
                row_monday: date | None = None
                if normalized:
                    try:
                        row_monday = date.fromisoformat(normalized)
                    except ValueError:
                        row_monday = None
                if cutoff is not None and row_monday is not None and row_monday < cutoff:
                    if await self._remove_study_from_open_indicator_properties_dialog():
                        removed_expired += 1
                        removed = True
                        break
                    await self.close_settings(save=False)
                    probe_complete = False
                    continue
                await self.close_settings(save=False)
                if not normalized or not sig:
                    probe_complete = False
                    continue
                pass_rows.append(
                    WeeklyGexRowSnapshot(
                        row_signature=sig,
                        start_iso=normalized,
                        levels=dict(levels),
                    )
                )
            if removed:
                continue
            if len(pass_rows) != len(candidates):
                probe_complete = False
            final_rows = pass_rows
            break
        self._log(
            "[subchart_cache] built rows=%s probe_complete=%s removed_expired=%s cutoff=%s"
            % (
                len(final_rows),
                int(probe_complete),
                removed_expired,
                cutoff.isoformat() if cutoff else "-",
            )
        )
        return WeeklyGexSubchartCache(
            rows=final_rows,
            probe_complete=probe_complete,
            removed_expired=removed_expired,
        )

    async def _append_created_row_to_subchart_cache(
        self,
        cache: WeeklyGexSubchartCache,
        *,
        before_signatures: frozenset[str],
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> None:
        """After a successful add, append the new row to ``cache`` (dialog still open)."""
        after = await self._collect_indicator_signatures(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )
        new_only = after - before_signatures
        if not new_only:
            return
        candidates = await self._collect_any_indicator_locators(
            title_keyword, active_only=True
        )
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(
                title_keyword, active_only=False
            )
        for cand in candidates:
            try:
                sig = await self._indicator_row_signature(cand)
            except Exception:
                continue
            if not sig or sig not in new_only:
                continue
            date_val, _ = await self.read_weekly_start_datetime()
            levels = await self.read_weekly_levels()
            norm = self._normalize_start_date_text(date_val)
            if norm:
                cache.rows.append(
                    WeeklyGexRowSnapshot(
                        row_signature=sig,
                        start_iso=norm,
                        levels=dict(levels),
                    )
                )
                self._log("[subchart_cache] appended created row sig=%s start=%s" % (sig[:24], norm))
            break

    async def _append_created_row_from_open_dialog(
        self,
        cache: WeeklyGexSubchartCache,
        *,
        new_row_signature: str,
    ) -> None:
        """Update ``cache`` using the already-open indicator settings (no legend re-scan)."""
        date_val, _ = await self.read_weekly_start_datetime()
        levels = await self.read_weekly_levels()
        norm = self._normalize_start_date_text(date_val)
        if norm and new_row_signature:
            cache.rows.append(
                WeeklyGexRowSnapshot(
                    row_signature=new_row_signature,
                    start_iso=norm,
                    levels=dict(levels),
                )
            )
            self._log(
                "[subchart_cache] appended from_dialog sig=%s start=%s"
                % (new_row_signature[:24], norm)
            )

    async def open_or_create_indicator_for_week(
        self,
        monday: date,
        *,
        title_keyword: str = "Daily & Weekly GEX",
        favorite_name: str = "Daily & Weekly GEX by daniel56_trade",
        subchart_cache: WeeklyGexSubchartCache | None = None,
        dry_run: bool = False,
    ) -> str:
        """Open settings for matching week indicator, or create a new one.

        Returns:
            "existing" when matched indicator found
            "created" when new indicator added for this week
            "would_create" when ``dry_run`` and the week would be added (no favorite/add)

        When ``subchart_cache`` is supplied (from :meth:`build_weekly_gex_subchart_cache`),
        a full per-row probe is skipped for **add** if the live legend still matches
        the cache; **existing** still performs one targeted open.
        """
        target = monday.isoformat()
        page = self._require_page()
        self._chart_settings_misopen_count = 0
        await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
        allow_global_fallback = self._allow_global_indicator_fallback()
        scoped_before = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not scoped_before and allow_global_fallback:
            scoped_before = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        existing_start_dates: set[str] = set()
        opened_existing_rows = 0
        unreadable_existing_rows = 0
        probe_complete = False
        before_count = len(scoped_before)
        probe_candidates = list(scoped_before)
        before_signatures: frozenset[str] = frozenset()

        use_cache_eligible = (
            subchart_cache is not None
            and subchart_cache.probe_complete
            and len(scoped_before) == len(subchart_cache.rows)
        )
        use_cache = False
        if use_cache_eligible:
            cache_dates = {r.start_iso for r in subchart_cache.rows if r.start_iso}
            before_signatures = subchart_cache.signature_set
            if target not in cache_dates:
                # New week: cache already scanned every row; do not re-run
                # _collect_indicator_signatures (N Playwright passes) before add_favorite.
                use_cache = True
                existing_start_dates = set(cache_dates)
                opened_existing_rows = len(subchart_cache.rows)
                unreadable_existing_rows = 0
                probe_complete = True
                self._log(
                    "[open_or_create] subchart_cache_trust_add "
                    "skip_pre_favorite_live_signature_scan=1"
                )
            else:
                live_sigs = await self._collect_indicator_signatures(
                    title_keyword=title_keyword,
                    allow_global_fallback=allow_global_fallback,
                )
                if live_sigs != subchart_cache.signature_set:
                    self._log("[open_or_create] subchart_cache signature mismatch; full probe")
                else:
                    use_cache = True
                    existing_start_dates = set(cache_dates)
                    opened_existing_rows = len(subchart_cache.rows)
                    unreadable_existing_rows = 0
                    probe_complete = True
                    matches = [r for r in subchart_cache.rows if r.start_iso == target]
                    for snap in matches:
                        for cand in probe_candidates:
                            try:
                                sig = await self._indicator_row_signature(cand)
                            except Exception:
                                continue
                            if sig != snap.row_signature:
                                continue
                            if await self._open_settings_for_locator(cand, title_keyword):
                                self._log(
                                    "[open_or_create] subchart_cache_hit_existing "
                                    f"week={target}"
                                )
                                return "existing"
                    self._log("[open_or_create] subchart_cache reopen failed; full probe")
                    use_cache = False

        if not use_cache:
            before_signatures = frozenset(
                await self._collect_indicator_signatures(
                    title_keyword=title_keyword,
                    allow_global_fallback=allow_global_fallback,
                )
            )
            before_count = len(scoped_before)
            probe_candidates = list(scoped_before)
            if before_count > 1:
                self._log(
                    "[open_or_create] full existing probe "
                    f"total={before_count}"
                )
            existing_start_dates = set()
            opened_existing_rows = 0
            unreadable_existing_rows = 0
            for indicator in probe_candidates:
                opened = await self._open_settings_for_locator(indicator, title_keyword)
                if not opened:
                    continue
                opened_existing_rows += 1
                date_val, _ = await self.read_weekly_start_datetime()
                normalized = self._normalize_start_date_text(date_val)
                if normalized:
                    existing_start_dates.add(normalized)
                else:
                    unreadable_existing_rows += 1
                if normalized == target:
                    return "existing"
                await self.close_settings(save=False)
            probe_complete = (
                before_count > 0
                and opened_existing_rows == before_count
                and unreadable_existing_rows == 0
            )
        else:
            self._log(
                "[open_or_create] subchart_cache_fast_add "
                f"rows={before_count} target={target}"
            )
        if existing_start_dates:
            self._log(
                "[open_or_create] existing start_dates="
                f"{','.join(sorted(existing_start_dates))}"
            )

        # Secondary snapshot pass before create:
        # establish a deterministic set of existing Mondays and avoid
        # duplicate add when pre-scan couldn't open all rows.
        if before_count > 0 and target not in existing_start_dates:
            if probe_complete:
                # First probe already opened every row once; snapshot would repeat
                # the same opens only to re-read start_date + levels.
                self._log("[open_or_create] pre-add skip_snapshot probe_complete=1")
            else:
                snapshots = await self._snapshot_weekly_gex_rows(
                    title_keyword=title_keyword,
                    allow_global_fallback=allow_global_fallback,
                )
                snapshot_start_dates = sorted({d.isoformat() for d in snapshots.keys()})
                if snapshot_start_dates:
                    existing_start_dates.update(snapshot_start_dates)
                    self._log(
                        "[open_or_create] pre-add snapshot start_dates="
                        f"{','.join(snapshot_start_dates)}"
                    )
            if target in existing_start_dates:
                recovered_existing = await self._open_settings_for_target_start_date(
                    title_keyword=title_keyword,
                    target_start_date=target,
                    allow_global_fallback=allow_global_fallback,
                )
                if recovered_existing:
                    self._log("[open_or_create] pre-add snapshot recovered existing target start_date")
                    return "existing"
                raise RuntimeError(
                    "Target week already exists but could not reopen it deterministically; "
                    "aborted to avoid duplicate add."
                )
            # At this point ``target`` is confirmed not in ``existing_start_dates``.
            # Adding a brand-new week is safe even if other weeks happen to have
            # duplicate rows (a pre-existing chart state), so long as the probe
            # itself was deterministic for the target week. We only block when
            # the probe is incomplete or there are unreadable rows that could
            # plausibly mask the target week.
            if unreadable_existing_rows > 0:
                raise RuntimeError(
                    "Existing indicator start_date scan has unreadable rows; "
                    f"unreadable={unreadable_existing_rows}, total={before_count}; "
                    "aborted to avoid duplicate add."
                )
            if opened_existing_rows < before_count:
                raise RuntimeError(
                    "Existing indicator scan incomplete before add; "
                    f"opened={opened_existing_rows}, total={before_count}, "
                    "aborted to avoid duplicate add."
                )
            if opened_existing_rows == before_count and len(existing_start_dates) < before_count:
                # Other weeks duplicate; target unambiguously absent. Keep going.
                self._log(
                    "[open_or_create] pre-add probe duplicates_in_other_weeks "
                    f"unique_start_dates={len(existing_start_dates)} total={before_count} "
                    f"target={target}"
                )

        # Safety re-scan before create: only when the first probe did not open every
        # row (transient selector drift / virtualization may hide a matching week).
        if before_count > 0 and not probe_complete:
            recovered_existing = await self._open_settings_for_target_start_date(
                title_keyword=title_keyword,
                target_start_date=target,
                allow_global_fallback=allow_global_fallback,
            )
            if not recovered_existing:
                await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
                for _ in range(2):
                    await page.wait_for_timeout(180)
                    recovered_existing = await self._open_settings_for_target_start_date(
                        title_keyword=title_keyword,
                        target_start_date=target,
                        allow_global_fallback=allow_global_fallback,
                    )
                    if recovered_existing:
                        break
            if recovered_existing:
                self._log("[open_or_create] pre-add recovered existing target start_date")
                return "existing"
        elif before_count > 0 and probe_complete:
            self._log("[open_or_create] pre-add skip_target_rescan probe_complete=1")

        if dry_run:
            self._log("[open_or_create] dry_run: would add indicator for week; skipping add_favorite")
            try:
                await self.close_settings(save=False)
            except Exception:
                pass
            return "would_create"

        marker_attr = f"data-gex-existing-{secrets.token_hex(4)}"
        if not use_cache:
            await self._mark_existing_indicator_rows(
                title_keyword=title_keyword,
                marker_attr=marker_attr,
                allow_global_fallback=allow_global_fallback,
            )
        await self.add_favorite_indicator(favorite_name)
        try:
            opened = False
            opened_new_sig: str | None = None
            if use_cache:
                opened, opened_new_sig = await self._open_new_row_after_favorite_subchart_cache(
                    title_keyword=title_keyword,
                    before_signatures=before_signatures,
                    before_count=before_count,
                    allow_global_fallback=allow_global_fallback,
                )
            if not opened:
                if use_cache:
                    await self._mark_indicator_rows_matching_signatures(
                        title_keyword=title_keyword,
                        marker_attr=marker_attr,
                        signatures=before_signatures,
                        allow_global_fallback=allow_global_fallback,
                    )
                opened = await self._open_settings_for_newly_added_by_delta(
                    title_keyword=title_keyword,
                    before_signatures=set(before_signatures),
                    marker_attr=marker_attr,
                    allow_global_fallback=allow_global_fallback,
                )
            if not opened:
                # Strict created-path: avoid non-delta guesses that may open old rows.
                await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
                for _ in range(4):
                    await page.wait_for_timeout(220)
                    opened = await self._open_settings_for_newly_added_by_delta(
                        title_keyword=title_keyword,
                        before_signatures=set(before_signatures),
                        marker_attr=marker_attr,
                        allow_global_fallback=allow_global_fallback,
                    )
                    if opened:
                        self._log("[open_or_create] recovered via strict delta polling")
                        break
            if not opened:
                diag = await self._collect_indicator_targeting_diag(
                    title_keyword=title_keyword,
                    marker_attr=marker_attr,
                    before_signatures=set(before_signatures),
                    allow_global_fallback=allow_global_fallback,
                )
                await self._dump_dom("open_or_create_new_row_delta_missing")
                self._log(f"[open_or_create] targeting_fail {diag}")
                raise RuntimeError(
                    "新增未生效於當前子圖：strict-delta 未偵測到可確認的新 row；"
                    "Could not deterministically open newly added indicator settings (strict delta mode); "
                    f"aborted to avoid editing the wrong indicator. ({diag})"
                )
            date_val, _ = await self.read_weekly_start_datetime()
            normalized = (date_val or "").strip()
            if date_val is None:
                await self._dump_dom("open_or_create_created_missing_start_field")
                raise RuntimeError(
                    "Opened created indicator settings but Start date field is missing; "
                    "aborted to avoid editing the wrong dialog."
                )
            if normalized and normalized != target and normalized in existing_start_dates:
                created_levels = await self.read_weekly_levels()
                non_empty_days = [
                    day for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
                    if (created_levels.get(day) or "").strip()
                ]
                after_candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
                if not after_candidates and allow_global_fallback:
                    after_candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
                after_count = len(after_candidates)
                if after_count > before_count:
                    self._log(
                        "[open_or_create] created-row duplicate start_date accepted "
                        f"start_date={normalized} before_count={before_count} after_count={after_count} "
                        f"non_empty_days={','.join(non_empty_days) if non_empty_days else '-'}"
                    )
                else:
                    await self.close_settings(save=False)
                    recovered = await self._open_settings_for_target_start_date(
                        title_keyword=title_keyword,
                        target_start_date=target,
                        allow_global_fallback=allow_global_fallback,
                    )
                    if recovered:
                        date_val, _ = await self.read_weekly_start_datetime()
                        normalized = (date_val or "").strip()
                        self._log("[open_or_create] recovered by target start_date scan")
                    if normalized and normalized != target and normalized in existing_start_dates:
                        raise RuntimeError(
                            "Opened a pre-existing indicator after add; "
                            f"expected new week={target}, got existing start_date={normalized}; "
                            f"non_empty_days={','.join(non_empty_days) if non_empty_days else '-'} "
                            f"before_count={before_count} after_count={after_count}"
                        )
            if subchart_cache is not None:
                if opened_new_sig:
                    await self._append_created_row_from_open_dialog(
                        subchart_cache,
                        new_row_signature=opened_new_sig,
                    )
                else:
                    await self._append_created_row_to_subchart_cache(
                        subchart_cache,
                        before_signatures=frozenset(before_signatures),
                        title_keyword=title_keyword,
                        allow_global_fallback=allow_global_fallback,
                    )
            self._log(
                f"[open_or_create] created picked_by_marker attr={marker_attr} "
                f"start_date={normalized or '-'} before_count={before_count}"
            )
            return "created"
        finally:
            await self._clear_indicator_row_marker(marker_attr)

    async def cleanup_and_sort_weekly_gex_indicators(
        self,
        *,
        keep_mondays: list[date],
        ticker: str | None,
        time_str: str = "04:00",
        title_keyword: str = "Daily & Weekly GEX",
        favorite_name: str = "Daily & Weekly GEX by daniel56_trade",
    ) -> dict[str, int]:
        """Delete old rows and rebuild kept weeks in chronological order."""
        page = self._require_page()
        await page.bring_to_front()
        await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
        allow_global_fallback = self._allow_global_indicator_fallback()
        keep_sorted = sorted(set(keep_mondays))
        keep_set = set(keep_sorted)
        cutoff = keep_sorted[0] if keep_sorted else None
        before_rows = await self._count_target_indicators(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )

        snapshots = await self._snapshot_weekly_gex_rows(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )
        before_count = len(snapshots)
        retained_mondays = sorted([m for m in snapshots.keys() if m in keep_set])
        if len(keep_sorted) > 0 and len(retained_mondays) > len(keep_sorted):
            retained_mondays = retained_mondays[-len(keep_sorted):]

        stale_exists = bool(cutoff and any(m < cutoff for m in snapshots.keys()))
        duplicate_rows = before_rows > len(snapshots)
        needs_reorder = before_rows > 1
        needs_cleanup = stale_exists or duplicate_rows or needs_reorder
        if not needs_cleanup:
            self._log(
                "[cleanup] no-op "
                f"before_rows={before_rows} unique_weeks={before_count} "
                f"keep_target={len(keep_sorted)} ticker={str(ticker or '').strip().upper() or '-'}"
            )
            return {
                "before": before_count,
                "before_rows": before_rows,
                "keep_target": len(keep_sorted),
                "kept_existing": len(retained_mondays),
                "removed": 0,
                "recreated": 0,
                "remaining_after_remove": before_rows,
            }

        plans: list[tuple[date, dict[str, str | None]]] = []
        token = str(ticker or "").strip().upper()
        for monday in keep_sorted:
            snap_levels = snapshots.get(monday) or {}
            merged: dict[str, str | None] = {
                day: ((snap_levels.get(day) or "").strip() or None)
                for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
            }
            has_snapshot = monday in snapshots
            has_db_data = False
            if token:
                db_codes = db.fetch_tv_codes_for_week(ticker=token, monday=monday)
                for day, code in db_codes.items():
                    if code:
                        merged[day] = code
                        has_db_data = True
            if not has_snapshot and not has_db_data:
                continue
            plans.append((monday, merged))

        if before_rows > 0 and not plans:
            raise RuntimeError(
                "Cleanup 無可重建計畫（無快照且 DB 無對應週資料），已中止以避免誤刪。"
            )

        removed, remaining_after_remove = await self._remove_all_target_indicators(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )
        if before_rows > 0 and remaining_after_remove > 0:
            diag = await self._collect_indicator_targeting_diag(
                title_keyword=title_keyword,
                marker_attr=f"cleanup-diag-{secrets.token_hex(3)}",
                before_signatures=None,
                allow_global_fallback=allow_global_fallback,
            )
            await self._dump_dom("cleanup_remove_incomplete")
            raise RuntimeError(
                "Cleanup 刪除未完成，已中止以避免重寫既有 indicator；"
                f"before_rows={before_rows}, removed={removed}, remaining={remaining_after_remove}, diag={diag}"
            )

        recreated = 0
        for monday, codes in plans:
            await self.open_or_create_indicator_for_week(
                monday=monday,
                title_keyword=title_keyword,
                favorite_name=favorite_name,
            )
            await self.set_weekly_start_date(monday=monday, time_str=time_str)
            await self.fill_weekly_levels(codes)
            await self.close_settings(save=True)
            recreated += 1
        self._log(
            "[cleanup] "
            f"before={before_count} before_rows={before_rows} keep_target={len(keep_sorted)} "
            f"kept_existing={len(retained_mondays)} removed={removed} recreated={recreated} "
            f"remaining={remaining_after_remove} ticker={token or '-'}"
        )
        return {
            "before": before_count,
            "before_rows": before_rows,
            "keep_target": len(keep_sorted),
            "kept_existing": len(retained_mondays),
            "removed": removed,
            "recreated": recreated,
            "remaining_after_remove": remaining_after_remove,
        }

    async def remove_expired_weekly_gex_indicators(
        self,
        *,
        keep_mondays: list[date],
        title_keyword: str = "Daily & Weekly GEX",
    ) -> dict[str, int]:
        """Delete only rows whose week Monday is strictly before the rolling keep window.

        Does not add indicators, reorder, or refresh kept weeks — only removes stale rows.
        """
        page = self._require_page()
        await page.bring_to_front()
        await self._ensure_indicator_legend_expanded(title_keyword=title_keyword)
        allow_global_fallback = self._allow_global_indicator_fallback()
        keep_sorted = sorted(set(keep_mondays))
        if not keep_sorted:
            return {
                "before": 0,
                "before_rows": 0,
                "removed": 0,
                "recreated": 0,
            }
        cutoff = keep_sorted[0]
        before_rows = await self._count_target_indicators(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )
        removed_total = 0
        for _round in range(96):
            candidates = await self._collect_any_indicator_locators(
                title_keyword, active_only=True
            )
            if not candidates and allow_global_fallback:
                candidates = await self._collect_any_indicator_locators(
                    title_keyword, active_only=False
                )
            if not candidates:
                break
            removed_this_round = False
            for cand in candidates:
                opened = await self._open_settings_for_locator(
                    cand, title_keyword, allow_context_menu=False
                )
                if not opened:
                    continue
                date_val, _time_val = await self.read_weekly_start_datetime()
                await self.close_settings(save=False)
                normalized = self._normalize_start_date_text(date_val)
                if not normalized:
                    continue
                try:
                    row_monday = date.fromisoformat(normalized)
                except ValueError:
                    continue
                if row_monday >= cutoff:
                    continue
                deleted = await self._remove_indicator_by_locator(cand, title_keyword)
                if deleted:
                    removed_total += 1
                    removed_this_round = True
                    await page.wait_for_timeout(200)
                    break
            if not removed_this_round:
                break
        self._log(
            "[remove_expired] "
            f"before_rows={before_rows} removed={removed_total} "
            f"keep_since={cutoff.isoformat()} ticker_window={len(keep_sorted)}w"
        )
        return {
            "before": before_rows,
            "before_rows": before_rows,
            "removed": removed_total,
            "recreated": 0,
        }

    async def _snapshot_weekly_gex_rows(
        self,
        *,
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> dict[date, dict[str, str | None]]:
        """Capture start date + Mon~Fri values for matched indicators."""
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        out: dict[date, dict[str, str | None]] = {}
        for cand in list(candidates):
            opened = await self._open_settings_for_locator(cand, title_keyword)
            if not opened:
                continue
            date_val, _time_val = await self.read_weekly_start_datetime()
            normalized = self._normalize_start_date_text(date_val)
            levels = await self.read_weekly_levels()
            await self.close_settings(save=False)
            if not normalized:
                continue
            monday = date.fromisoformat(normalized)
            prev = out.get(monday)
            if prev is None or self._count_non_empty_levels(levels) >= self._count_non_empty_levels(prev):
                out[monday] = levels
        return out

    @staticmethod
    def _count_non_empty_levels(levels: dict[str, str | None]) -> int:
        return sum(
            1
            for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
            if (levels.get(day) or "").strip()
        )

    async def _remove_all_target_indicators(
        self,
        *,
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> tuple[int, int]:
        removed = 0
        remaining = await self._count_target_indicators(
            title_keyword=title_keyword,
            allow_global_fallback=allow_global_fallback,
        )
        for _ in range(96):
            if remaining <= 0:
                break
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
            if not candidates and allow_global_fallback:
                candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
            if not candidates:
                break
            removed_this_round = False
            for cand in candidates:
                before = remaining
                _attempted = await self._remove_indicator_by_locator(cand, title_keyword)
                await self._require_page().wait_for_timeout(180)
                remaining = await self._count_target_indicators(
                    title_keyword=title_keyword,
                    allow_global_fallback=allow_global_fallback,
                )
                if remaining < before:
                    removed += (before - remaining)
                    removed_this_round = True
                    break
            if not removed_this_round:
                break
        return removed, remaining

    async def _count_target_indicators(
        self,
        *,
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> int:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        return len(candidates)

    async def _remove_indicator_by_locator(self, indicator, title_keyword: str) -> bool:
        """Best-effort delete one indicator row via context menu/settings."""
        page = self._require_page()
        await page.bring_to_front()
        await self._close_chart_settings_if_open()

        row_remove_btn = indicator.locator(
            "button[aria-label*='Remove'], "
            "button[aria-label*='Delete'], "
            "button[aria-label*='移除'], "
            "button[aria-label*='刪除'], "
            "button[title*='Remove'], "
            "button[title*='Delete'], "
            "button[title*='移除'], "
            "button[title*='刪除']"
        ).first
        if await row_remove_btn.count():
            try:
                await row_remove_btn.click(force=True, timeout=1200)
                await page.wait_for_timeout(180)
                await self._confirm_delete_dialog_if_present()
                await self._close_chart_settings_if_open()
                return True
            except Exception:
                pass

        direct_clicked = await self._try_click_row_remove_control(indicator)
        if direct_clicked:
            await page.wait_for_timeout(180)
            await self._confirm_delete_dialog_if_present()
            await self._close_chart_settings_if_open()
            return True

        try:
            # Focus row first so context menu targets the indicator row.
            await indicator.click(force=True, timeout=900)
        except Exception:
            pass
        try:
            await indicator.click(button="right", force=True, timeout=1200)
        except Exception:
            try:
                await indicator.evaluate(
                    """
                    (el) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return false;
                      row.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                      row.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
                      return true;
                    }
                    """
                )
            except Exception:
                pass
        await page.wait_for_timeout(160)
        remove_menu = page.locator(
            "[role='menuitem']:has-text('Remove'), "
            "[role='menuitem']:has-text('Delete'), "
            "[role='menuitem']:has-text('移除'), "
            "[role='menuitem']:has-text('刪除'), "
            "[role='menuitem']:has-text('移掉'), "
            "[role='menuitem']:has-text('移除研究'), "
            "[role='menuitem']:has-text('移除指標'), "
            "#overlap-manager-root [class*='item']:has-text('Remove'), "
            "#overlap-manager-root [class*='item']:has-text('Delete'), "
            "#overlap-manager-root [class*='item']:has-text('移除'), "
            "#overlap-manager-root [class*='item']:has-text('刪除'), "
            "#overlap-manager-root [class*='item']:has-text('移掉'), "
            "#overlap-manager-root [class*='item']:has-text('移除研究'), "
            "#overlap-manager-root [class*='item']:has-text('移除指標')"
        ).first
        if await remove_menu.count():
            try:
                await remove_menu.click()
                await page.wait_for_timeout(200)
                await self._confirm_delete_dialog_if_present()
                await self._close_chart_settings_if_open()
                return True
            except Exception:
                pass

        delete_icon = indicator.locator(
            "button[aria-label*='Remove'], "
            "button[aria-label*='Delete'], "
            "button[aria-label*='移除'], "
            "button[aria-label*='刪除'], "
            "button[data-name*='remove'], "
            "button[data-name*='delete']"
        ).first
        if await delete_icon.count():
            try:
                await delete_icon.click(timeout=1000)
                await page.wait_for_timeout(160)
                await self._confirm_delete_dialog_if_present()
                await self._close_chart_settings_if_open()
                return True
            except Exception:
                pass

        opened = await self._open_settings_for_locator(
            indicator,
            title_keyword,
            allow_context_menu=True,
        )
        if not opened:
            return False
        remove_btn = page.locator(
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('Remove'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('Delete'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('移除'), "
            "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('刪除')"
        ).first
        if await remove_btn.count() == 0:
            await self.close_settings(save=False)
            return False
        try:
            await remove_btn.click()
            await page.wait_for_timeout(180)
            await self._confirm_delete_dialog_if_present()
            await self._close_chart_settings_if_open()
            return True
        except Exception:
            await self.close_settings(save=False)
            return False

    async def _confirm_delete_dialog_if_present(self) -> None:
        page = self._require_page()
        confirm = page.locator(
            "[role='dialog'] button:has-text('Remove'), "
            "[role='dialog'] button:has-text('Delete'), "
            "[role='dialog'] button:has-text('Yes'), "
            "[role='dialog'] button:has-text('OK'), "
            "[role='dialog'] button:has-text('移除'), "
            "[role='dialog'] button:has-text('刪除'), "
            "[role='dialog'] button:has-text('確認'), "
            "[role='dialog'] button:has-text('確定'), "
            "[role='dialog'] button:has-text('是')"
        ).first
        if await confirm.count():
            try:
                await confirm.click(timeout=1000)
                await page.wait_for_timeout(180)
            except Exception:
                pass

    async def _close_chart_settings_if_open(self) -> None:
        page = self._require_page()
        try:
            if await self._is_chart_settings_dialog_open():
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(120)
        except Exception:
            return

    async def _try_click_row_remove_control(self, indicator) -> bool:
        """Try row-local remove/delete controls without opening settings."""
        try:
            clicked = await indicator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return false;
                  row.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
                  row.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
                  const controls = Array.from(
                    row.querySelectorAll("button, [role='button'], [data-name], [class*='button']")
                  );
                  const score = (node) => {
                    const attrs = [
                      node.getAttribute("aria-label") || "",
                      node.getAttribute("title") || "",
                      node.getAttribute("data-name") || "",
                      (node.className || "").toString(),
                      node.textContent || "",
                    ].join(" ").toLowerCase();
                    let s = 0;
                    if (/remove|delete|trash|close|dismiss|remove study/.test(attrs)) s += 6;
                    if (/移除|刪除|移掉|關閉/.test(attrs)) s += 6;
                    if (/settings|設定|齒輪/.test(attrs)) s -= 5;
                    return s;
                  };
                  controls.sort((a, b) => score(b) - score(a));
                  for (const node of controls.slice(0, 8)) {
                    if (score(node) <= 0) continue;
                    try {
                      node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                      node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                      return true;
                    } catch (_) {
                      continue;
                    }
                  }
                  return false;
                }
                """
            )
        except Exception:
            return False
        return bool(clicked)

    async def _try_click_row_settings_control(self, indicator) -> bool:
        """Try row-local settings/gear controls before using context menu."""
        try:
            clicked = await indicator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return false;
                  row.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
                  row.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
                  const controls = Array.from(
                    row.querySelectorAll("button, [role='button'], [data-name], [class*='button']")
                  );
                  const score = (node) => {
                    const attrs = [
                      node.getAttribute("aria-label") || "",
                      node.getAttribute("title") || "",
                      node.getAttribute("data-name") || "",
                      (node.className || "").toString(),
                      node.textContent || "",
                    ].join(" ").toLowerCase();
                    let s = 0;
                    if (/settings|gear|cog|format/.test(attrs)) s += 7;
                    if (/設定|齒輪|格式/.test(attrs)) s += 7;
                    if (/remove|delete|trash|close|dismiss/.test(attrs)) s -= 7;
                    if (/移除|刪除|移掉|關閉/.test(attrs)) s -= 7;
                    return s;
                  };
                  controls.sort((a, b) => score(b) - score(a));
                  for (const node of controls.slice(0, 8)) {
                    if (score(node) <= 0) continue;
                    try {
                      node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
                      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
                      node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                      return true;
                    } catch (_) {
                      continue;
                    }
                  }
                  return false;
                }
                """
            )
        except Exception:
            return False
        return bool(clicked)

    async def _scroll_indicator_row_into_view(self, indicator) -> bool:
        """Try scrolling an indicator row into the legend viewport."""
        page = self._require_page()
        try:
            scrolled = await indicator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row || !row.scrollIntoView) return false;
                  row.scrollIntoView({ block: "center", inline: "nearest" });
                  return true;
                }
                """
            )
            if not scrolled:
                return False
            await page.wait_for_timeout(120)
            return bool(await indicator.is_visible())
        except Exception:
            return False

    async def _mark_existing_indicator_rows(
        self,
        *,
        title_keyword: str,
        marker_attr: str,
        allow_global_fallback: bool,
    ) -> None:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        for cand in candidates:
            try:
                await cand.evaluate(
                    """
                    (el, markerAttr) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return;
                      row.setAttribute(markerAttr, "1");
                    }
                    """,
                    marker_attr,
                )
            except Exception:
                continue

    async def _mark_indicator_rows_matching_signatures(
        self,
        *,
        title_keyword: str,
        marker_attr: str,
        signatures: frozenset[str],
        allow_global_fallback: bool,
    ) -> None:
        """Tag only rows whose legend signature is in ``signatures`` (pre-add rows)."""
        if not signatures:
            return
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        for cand in candidates:
            try:
                sig = await self._indicator_row_signature(cand)
            except Exception:
                continue
            if not sig or sig not in signatures:
                continue
            try:
                await cand.evaluate(
                    """
                    (el, markerAttr) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return;
                      row.setAttribute(markerAttr, "1");
                    }
                    """,
                    marker_attr,
                )
            except Exception:
                continue

    async def _open_new_row_after_favorite_subchart_cache(
        self,
        *,
        title_keyword: str,
        before_signatures: frozenset[str],
        before_count: int,
        allow_global_fallback: bool,
        settle_rounds: int = 8,
    ) -> tuple[bool, str | None]:
        """Open settings on the newly added row without scanning every legend signature first.

        TradingView usually appends a favorite study as the last legend row for the pane.
        When ``len(rows) == before_count + 1``, we only read **one** signature (last row) and
        open it if that signature is new. Falls back to a full singleton-diff scan only when
        the count heuristic does not match.
        """
        page = self._require_page()
        for attempt in range(settle_rounds):
            if attempt:
                await page.wait_for_timeout(260)
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
            if not candidates and allow_global_fallback:
                candidates = await self._collect_any_indicator_locators(
                    title_keyword, active_only=False
                )
            n = len(candidates)
            if n == before_count + 1:
                last = candidates[-1]
                try:
                    sig = await self._indicator_row_signature(last)
                except Exception:
                    sig = None
                if sig and sig not in before_signatures:
                    ok = await self._open_settings_for_locator(last, title_keyword)
                    if ok:
                        self._log(
                            "[open_or_create] new_row_by_count_plus_one "
                            f"attempt={attempt + 1}/{settle_rounds}"
                        )
                        return True, sig
                self._log(
                    "[open_or_create] new_row_count_plus_one_bad_tail "
                    f"attempt={attempt + 1} n={n}"
                )
            elif n > before_count + 1:
                self._log(
                    "[open_or_create] new_row_count_overshoot n=%s before=%s"
                    % (n, before_count)
                )
                break
        for attempt in range(3):
            if attempt:
                await page.wait_for_timeout(240)
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
            if not candidates and allow_global_fallback:
                candidates = await self._collect_any_indicator_locators(
                    title_keyword, active_only=False
                )
            fresh: list = []
            for cand in candidates:
                try:
                    sig = await self._indicator_row_signature(cand)
                except Exception:
                    continue
                if sig and sig not in before_signatures:
                    fresh.append((cand, sig))
            if len(fresh) == 1:
                ok = await self._open_settings_for_locator(fresh[0][0], title_keyword)
                if ok:
                    self._log("[open_or_create] new_row_singleton_sig_scan")
                    return True, fresh[0][1]
                return False, None
            if len(fresh) > 1:
                self._log(
                    "[open_or_create] new_row_sig_ambiguous n=%s; marker+delta"
                    % len(fresh)
                )
                break
        self._log("[open_or_create] new_row_open_failed_prescan")
        return False, None

    async def _open_settings_for_active_or_unmarked_row(
        self,
        *,
        title_keyword: str,
        marker_attr: str,
        allow_global_fallback: bool,
    ) -> bool:
        page = self._require_page()
        candidates = await self._collect_visible_indicator_locators(title_keyword, active_only=True)
        if not candidates:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_visible_indicator_locators(title_keyword, active_only=False)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)

        active: list = []
        unmarked: list = []
        for cand in candidates:
            try:
                status = await cand.evaluate(
                    """
                    (el, markerAttr) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return { active: false, marked: true };
                      const cls = (row.className || "").toString().toLowerCase();
                      const isActive =
                        cls.includes("active") ||
                        cls.includes("selected") ||
                        cls.includes("focused") ||
                        (row.getAttribute("aria-selected") || "").toLowerCase() === "true" ||
                        row.contains(document.activeElement);
                      const isMarked = row.hasAttribute(markerAttr);
                      return { active: !!isActive, marked: !!isMarked };
                    }
                    """,
                    marker_attr,
                )
            except Exception:
                continue
            if isinstance(status, dict) and bool(status.get("active")):
                active.append(cand)
            if isinstance(status, dict) and not bool(status.get("marked")):
                unmarked.append(cand)

        picks = active or unmarked
        # Check all picks; do not truncate to avoid missing correct row.
        for cand in picks:
            if await self._open_settings_for_locator(cand, title_keyword):
                return True
            if self._chart_settings_misopen_count >= self._chart_settings_misopen_limit:
                self._log(
                    "[open_settings] abort-after-misopen-limit "
                    f"count={self._chart_settings_misopen_count}"
                )
                return False
            await page.wait_for_timeout(120)
        if not picks and len(candidates) == 1:
            # Safe downgrade: only one candidate exists in scoped search.
            # Try opening it directly instead of failing on missing active/unmarked signal.
            self._log("[open_settings] single-candidate fallback")
            return await self._open_settings_for_locator(candidates[0], title_keyword)
        return False

    async def _open_settings_for_newly_added_by_delta(
        self,
        *,
        title_keyword: str,
        before_signatures: set[str],
        marker_attr: str,
        allow_global_fallback: bool,
    ) -> bool:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)

        unmarked_rows: list = []
        unmarked_visible_rows: list = []
        unmarked_total = 0
        sig_delta_total = 0
        visible_total = 0
        for cand in candidates:
            marked = await self._indicator_row_marked_status(cand, marker_attr=marker_attr)
            sig = await self._indicator_row_signature(cand)
            try:
                is_visible = await cand.is_visible()
            except Exception:
                is_visible = False
            is_unmarked = marked is False
            is_sig_delta = bool(sig) and sig not in before_signatures
            if is_visible:
                visible_total += 1
            if is_unmarked:
                unmarked_total += 1
                unmarked_rows.append(cand)
                if is_visible:
                    unmarked_visible_rows.append(cand)
            if is_sig_delta:
                sig_delta_total += 1

        self._log(
            "[open_settings] delta-scan "
            f"total={len(candidates)} unmarked={unmarked_total} sig_delta={sig_delta_total} "
            f"visible={visible_total} strict={len(unmarked_visible_rows)}"
        )

        if len(unmarked_visible_rows) == 1:
            self._log("[open_settings] delta-target hit=1")
            return await self._open_settings_for_locator(unmarked_visible_rows[0], title_keyword)
        if len(unmarked_visible_rows) > 1:
            self._log(f"[open_settings] delta-target ambiguous={len(unmarked_visible_rows)}")
            return False
        if len(unmarked_rows) == 1:
            self._log("[open_settings] delta-target hidden-only; try reveal")
            if await self._scroll_indicator_row_into_view(unmarked_rows[0]):
                return await self._open_settings_for_locator(unmarked_rows[0], title_keyword)
            return False
        return False

    async def _open_settings_for_target_start_date(
        self,
        *,
        title_keyword: str,
        target_start_date: str,
        allow_global_fallback: bool,
    ) -> bool:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        skipped: list = []
        for cand in candidates:
            opened = await self._open_settings_for_locator(cand, title_keyword)
            if not opened:
                skipped.append(cand)
                continue
            date_val, _ = await self.read_weekly_start_datetime()
            normalized = self._normalize_start_date_text(date_val)
            if normalized == target_start_date:
                return True
            await self.close_settings(save=False)
        # Retry previously skipped rows once after a short settle delay.
        for cand in skipped:
            await self._require_page().wait_for_timeout(120)
            opened = await self._open_settings_for_locator(cand, title_keyword)
            if not opened:
                continue
            date_val, _ = await self.read_weekly_start_datetime()
            normalized = self._normalize_start_date_text(date_val)
            if normalized == target_start_date:
                return True
            await self.close_settings(save=False)
        return False

    async def _open_settings_for_latest_row_fallback(
        self,
        *,
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> bool:
        candidates = await self._collect_visible_indicator_locators(title_keyword, active_only=True)
        if not candidates:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_visible_indicator_locators(title_keyword, active_only=False)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        if not candidates:
            return False
        dated_candidates: list[tuple[date, object]] = []
        for cand in candidates:
            opened = await self._open_settings_for_locator(cand, title_keyword)
            if not opened:
                continue
            date_val, _ = await self.read_weekly_start_datetime()
            normalized = self._normalize_start_date_text(date_val)
            await self.close_settings(save=False)
            if not normalized:
                continue
            d = date.fromisoformat(normalized)
            dated_candidates.append((d, cand))
        if not dated_candidates:
            return False
        dated_candidates.sort(key=lambda it: it[0])
        newest = dated_candidates[-1][1]
        return await self._open_settings_for_locator(newest, title_keyword)

    async def _collect_indicator_signatures(
        self,
        *,
        title_keyword: str,
        allow_global_fallback: bool,
    ) -> set[str]:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)

        out: set[str] = set()
        for cand in candidates:
            sig = await self._indicator_row_signature(cand)
            if sig:
                out.add(sig)
        return out

    async def _indicator_row_marked_status(self, locator, *, marker_attr: str) -> bool | None:
        try:
            marked = await locator.evaluate(
                """
                (el, markerAttr) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  return row.hasAttribute(markerAttr);
                }
                """,
                marker_attr,
            )
        except Exception:
            return None
        if marked is None:
            return None
        return bool(marked)

    async def _indicator_row_signature(self, locator) -> str | None:
        try:
            sig = await locator.evaluate(
                """
                (el) => {
                  const row = el.closest("[data-name='legend-source-item']")
                    || el.closest("[class*='sourceItem']")
                    || el.closest("[class*='item'][class*='study']")
                    || el;
                  if (!row) return null;
                  const q = (n) => Number.isFinite(n) ? Math.round(n / 6) : -1;
                  const txt = (row.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 220);
                  const rowRect = row.getBoundingClientRect();
                  const legend = row.closest("[data-name='legend'], [class*='legend']");
                  const legendRect = legend ? legend.getBoundingClientRect() : null;
                  const widgets = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  ).filter((node) => {
                    const r = node.getBoundingClientRect();
                    const st = window.getComputedStyle(node);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  });
                  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
                  const nearestWidgetIndex = (rect) => {
                    if (!rect || !widgets.length) return -1;
                    let bestIdx = -1;
                    let bestDist = Number.POSITIVE_INFINITY;
                    for (let i = 0; i < widgets.length; i += 1) {
                      const wr = widgets[i].getBoundingClientRect();
                      const cx = (rect.left + rect.right) / 2;
                      const cy = (rect.top + rect.bottom) / 2;
                      const nx = clamp(cx, wr.left, wr.right);
                      const ny = clamp(cy, wr.top, wr.bottom);
                      const dx = cx - nx;
                      const dy = cy - ny;
                      const dist = Math.sqrt(dx * dx + dy * dy);
                      if (dist < bestDist) {
                        bestDist = dist;
                        bestIdx = i;
                      }
                    }
                    return bestIdx;
                  };
                  const rowOwner = nearestWidgetIndex(rowRect);
                  const legendOwner = nearestWidgetIndex(legendRect);
                  const rowGeom = legendRect
                    ? `${q(rowRect.left - legendRect.left)}:${q(rowRect.top - legendRect.top)}:${q(rowRect.width)}:${q(rowRect.height)}`
                    : `${q(rowRect.left)}:${q(rowRect.top)}:${q(rowRect.width)}:${q(rowRect.height)}`;
                  const legendGeom = legendRect
                    ? `${q(legendRect.left)}:${q(legendRect.top)}:${q(legendRect.width)}:${q(legendRect.height)}`
                    : "-:-:-:-";
                  const attrs = [
                    row.getAttribute("id") || "",
                    row.getAttribute("data-name") || "",
                    row.getAttribute("data-qa-id") || "",
                    row.getAttribute("data-source-id") || "",
                    row.getAttribute("data-study-id") || "",
                    row.getAttribute("data-instance-id") || "",
                    row.getAttribute("data-slot") || "",
                    row.getAttribute("aria-label") || "",
                    row.getAttribute("role") || "",
                  ];
                  const cls = (row.className || "").toString().replace(/\\s+/g, " ").trim().slice(0, 80);
                  return `${attrs.join("|")}::w=${rowOwner}|lw=${legendOwner}|rg=${rowGeom}|lg=${legendGeom}|cls=${cls}|txt=${txt}`;
                }
                """
            )
        except Exception:
            return None
        return str(sig).strip() if sig else None

    async def _collect_indicator_targeting_diag(
        self,
        *,
        title_keyword: str,
        marker_attr: str,
        before_signatures: set[str] | None = None,
        allow_global_fallback: bool,
    ) -> str:
        candidates = await self._collect_any_indicator_locators(title_keyword, active_only=True)
        if not candidates and allow_global_fallback:
            candidates = await self._collect_any_indicator_locators(title_keyword, active_only=False)
        matched = len(candidates)
        unmarked = 0
        active = 0
        sig_delta = 0
        sig_missing = 0
        for cand in candidates:
            try:
                status = await cand.evaluate(
                    """
                    (el, markerAttr) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return { active: false, marked: true };
                      const cls = (row.className || "").toString().toLowerCase();
                      const isActive =
                        cls.includes("active") ||
                        cls.includes("selected") ||
                        cls.includes("focused") ||
                        (row.getAttribute("aria-selected") || "").toLowerCase() === "true" ||
                        row.contains(document.activeElement);
                      const isMarked = row.hasAttribute(markerAttr);
                      return { active: !!isActive, marked: !!isMarked };
                    }
                    """,
                    marker_attr,
                )
            except Exception:
                continue
            if isinstance(status, dict):
                if bool(status.get("active")):
                    active += 1
                if not bool(status.get("marked")):
                    unmarked += 1
            sig = await self._indicator_row_signature(cand)
            if not sig:
                sig_missing += 1
            elif before_signatures is not None and sig not in before_signatures:
                sig_delta += 1
        scope_mode = "scoped_only" if not allow_global_fallback else "scoped_plus_global"
        return (
            f"matched={matched} unmarked={unmarked} active={active} "
            f"sig_delta={sig_delta if before_signatures is not None else '-'} "
            f"sig_missing={sig_missing} scope={scope_mode}"
        )

    def _allow_global_indicator_fallback(self) -> bool:
        """When subchart is pinned, never expand to global legend matching."""
        return self._scoped_subchart_index is None

    async def _ensure_indicator_legend_expanded(self, *, title_keyword: str) -> None:
        """Force-expand indicator legend in current scoped chart when collapsed.

        Delegates to :meth:`expand_collapsed_indicator_rows` (per-pane, multi-round,
        including ``⌄ N`` style chips). The legacy early-exit path that skipped expansion
        whenever *any* legend row was visible has been removed.
        """
        page = self._require_page()
        widx = int(self._scoped_subchart_index) if self._scoped_subchart_index is not None else 0
        await self.expand_collapsed_indicator_rows(widx)
        await page.wait_for_timeout(120)
        scoped_visible_after = await self._collect_visible_indicator_locators(title_keyword, active_only=True)
        self._log(
            "[legend_expand] "
            f"scoped_visible_after={len(scoped_visible_after)} "
            f"scope_idx={self._scoped_subchart_index if self._scoped_subchart_index is not None else '-'}"
        )

    async def _clear_indicator_row_marker(self, marker_attr: str) -> None:
        page = self._require_page()
        try:
            await page.evaluate(
                """
                (markerAttr) => {
                  document.querySelectorAll(`[${markerAttr}]`).forEach((el) => el.removeAttribute(markerAttr));
                }
                """,
                marker_attr,
            )
        except Exception:
            return

    async def _open_settings_for_locator(
        self,
        indicator,
        title_keyword: str,
        *,
        allow_context_menu: bool = False,
    ) -> bool:
        """Open settings from a specific matched indicator locator."""
        page = self._require_page()
        await page.bring_to_front()
        await self._close_chart_settings_if_open()
        # Guarantee no stale indicator-properties-dialog leaks across rows.
        # Without this, a previous row's still-rendered dialog can be falsely
        # accepted as "this row opened" and the same start_date is read twice.
        await self._ensure_indicator_dialog_closed()
        row_title = await self._indicator_row_title(indicator)
        if not row_title:
            self._log("[open_settings] skip-row-without-title")
            return False
        if not self._indicator_title_matches_keyword(row_title, title_keyword):
            preview = row_title if len(row_title) <= 120 else (row_title[:117] + "...")
            self._log(f"[open_settings] skip-row-title-mismatch title={preview!r}")
            return False
        is_visible = False
        try:
            is_visible = await indicator.is_visible()
        except Exception:
            is_visible = False
        if not is_visible:
            is_visible = await self._scroll_indicator_row_into_view(indicator)
        if not is_visible:
            forced = await self._force_open_indicator_settings_via_dom(indicator)
            if forced:
                await page.wait_for_timeout(220)
                if await self._is_chart_settings_dialog_open():
                    self._chart_settings_misopen_count += 1
                    self._log("[open_settings] chart-settings-opened-by-mistake; closing and retrying")
                    await page.keyboard.press("Escape")
                    await self._ensure_indicator_dialog_closed()
                    return False
                if await self._is_target_settings_dialog_open(title_keyword):
                    await self._initialize_indicator_settings_inputs()
                    return True
            self._log("[open_settings] skip-invisible-row-candidate")
            return False
        if await self._try_open_settings_via_indicator(indicator, title_keyword):
            return True
        direct_settings_clicked = await self._try_click_row_settings_control(indicator)
        if direct_settings_clicked:
            await page.wait_for_timeout(170)
            if await self._is_chart_settings_dialog_open():
                self._chart_settings_misopen_count += 1
                self._log("[open_settings] chart-settings-opened-by-mistake; closing and retrying")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(140)
                return False
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return True
        if not allow_context_menu:
            return False
        try:
            await indicator.click(force=True, timeout=900)
        except Exception:
            pass
        try:
            await indicator.click(button="right", force=True, timeout=1200)
        except Exception:
            try:
                await indicator.evaluate(
                    """
                    (el) => {
                      const row = el.closest("[data-name='legend-source-item']")
                        || el.closest("[class*='sourceItem']")
                        || el.closest("[class*='item'][class*='study']")
                        || el;
                      if (!row) return false;
                      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
                      return true;
                    }
                    """
                )
            except Exception:
                pass
        await page.wait_for_timeout(150)
        settings_item = page.locator(
            "#overlap-manager-root [role='menu']"
            ":has([role='menuitem']:has-text('Remove')) "
            ":has([role='menuitem']:has-text('Settings')) [role='menuitem']:has-text('Settings'), "
            "#overlap-manager-root [role='menu']"
            ":has([role='menuitem']:has-text('刪除')) "
            ":has([role='menuitem']:has-text('設定')) [role='menuitem']:has-text('設定')"
        ).first
        if await settings_item.count() == 0:
            settings_item = page.locator(
                "[role='menuitem']:has-text('Settings'), "
                "[role='menuitem']:has-text('設定'), "
                "#overlap-manager-root [class*='item']:has-text('Settings'), "
                "#overlap-manager-root [class*='item']:has-text('設定')"
            ).first
        if await settings_item.count():
            await settings_item.click()
            await page.wait_for_timeout(260)
            # Guard: TradingView sometimes opens Chart Settings (Status line tab)
            # instead of indicator properties when context target drifts.
            if await self._is_chart_settings_dialog_open():
                self._chart_settings_misopen_count += 1
                self._log("[open_settings] chart-settings-opened-by-mistake; closing and retrying")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(140)
                return False
            if await self._is_target_settings_dialog_open(title_keyword):
                await self._initialize_indicator_settings_inputs()
                return True
        return False

    async def _is_chart_settings_dialog_open(self) -> bool:
        """Detect generic chart settings dialog (not indicator properties)."""
        page = self._require_page()
        indicator_props = page.locator("[role='dialog'][data-name='indicator-properties-dialog']")
        if await indicator_props.count():
            return False
        chart_settings = page.locator(
            "[role='dialog']:has-text('Status line'), "
            "[role='dialog']:has-text('狀態列'), "
            "[role='dialog']:has-text('Scales and lines'), "
            "[role='dialog']:has-text('刻度與線'), "
            "[role='dialog']:has-text('Canvas')"
        ).first
        return await chart_settings.count() > 0

    async def set_weekly_start_date(self, monday: date, time_str: str = "04:00") -> None:
        """Set weekly start date in indicator settings dialog."""
        page = self._require_page()
        await page.bring_to_front()
        await self._select_indicator_tab("Inputs")

        start_date = monday.strftime("%Y-%m-%d")
        weekly_section = await self._get_weekly_gex_section()
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            await self._dump_dom("indicator_properties_dialog_missing")
            raise RuntimeError("Indicator settings dialog is not open.")
        if weekly_section:
            date_input = weekly_section.locator(
                "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]/following::input[1]"
            ).first
        else:
            date_input = dialog.locator(
                "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]/following::input[1]"
            ).first
        if await date_input.count() == 0:
            date_anchor_fallback = dialog.locator(
                "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]/following::input[1]"
            ).first
            if await date_anchor_fallback.count():
                date_input = date_anchor_fallback
            else:
            # Last fallback: among all date inputs in dialog, pick the second one
            # (first is Daily Start Time date, second is Weekly Start date).
                date_fallback = page.locator(
                    "[role='dialog'][data-name='indicator-properties-dialog'] input[placeholder='YYYY-MM-DD']"
                )
                date_count = await date_fallback.count()
                if date_count >= 2:
                    date_input = date_fallback.nth(1)
                elif date_count == 1:
                    date_input = date_fallback.first
                else:
                    await self._dump_dom("start_date_input_missing")
                    raise RuntimeError("Could not find Start date input in indicator settings.")
        await self._fill_input_value(date_input, start_date)

        if time_str:
            await self._set_weekly_start_time_fast(time_str)
        # Read-back verification: retry once when UI swallows update.
        date_val, time_val = await self.read_weekly_start_datetime()
        date_ok = (date_val or "").strip() == start_date
        time_ok = not time_str or (time_val or "").strip() == time_str
        if not (date_ok and time_ok):
            if await date_input.count():
                await self._fill_input_value(date_input, start_date)
            if time_str:
                await self._set_weekly_start_time_fast(time_str)
        await page.wait_for_timeout(180)

    async def _set_weekly_start_time_fast(self, time_str: str) -> None:
        """Set weekly start time with bounded, non-blocking DOM update."""
        page = self._require_page()
        try:
            changed = await page.evaluate(
                """
                (val) => {
                  const dialog = document.querySelector("[role='dialog'][data-name='indicator-properties-dialog']");
                  if (!dialog) return false;
                  const timeInputs = Array.from(
                    dialog.querySelectorAll("input[data-qa-id='ui-lib-Input-input time-input-input']")
                  );
                  if (!timeInputs.length) return false;
                  const valueSetter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype,
                    "value"
                  )?.set;
                  if (!valueSetter) return false;
                  // TV variants may reorder rows; set all visible time fields
                  // to keep weekly slot from being missed.
                  for (const target of timeInputs) {
                    target.focus();
                    valueSetter.call(target, val);
                    target.dispatchEvent(new Event("input", { bubbles: true }));
                    target.dispatchEvent(new Event("change", { bubbles: true }));
                    target.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
                    target.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", bubbles: true }));
                    target.dispatchEvent(new Event("blur", { bubbles: true }));
                  }
                  return true;
                }
                """,
                time_str,
            )
            if changed:
                await page.wait_for_timeout(120)
                return
        except Exception:
            pass
        # Last fallback to previous interaction method, but keep it best-effort.
        try:
            time_inputs = page.locator(
                "[role='dialog'][data-name='indicator-properties-dialog'] "
                "input[data-qa-id='ui-lib-Input-input time-input-input']"
            )
            target = time_inputs.nth(1) if await time_inputs.count() >= 2 else time_inputs.first
            if await target.count():
                await self._fill_input_value(target, time_str)
        except Exception:
            pass

    async def fill_weekly_levels(
        self,
        codes: dict[str, str | None],
        *,
        clear_missing: bool = False,
    ) -> list[str]:
        """Fill Mon~Fri fields with available codes only.

        Returns the day names that were actually updated.
        """
        page = self._require_page()
        await page.bring_to_front()
        await self._select_indicator_tab("Inputs")

        await self._ensure_weekly_compare_checked()
        levels_section = await self._get_weekly_levels_section()
        if not levels_section:
            await self._dump_dom("weekly_levels_section_missing")
            raise RuntimeError("Could not find WEEKLY GEX LEVELS section.")

        filled: list[str] = []
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            code = codes.get(day)
            if code is None and not clear_missing:
                continue
            input_box = await self._find_weekly_day_input(levels_section, day)
            if input_box is None:
                await self._dump_dom(f"day_input_missing_{day.lower()}")
                raise RuntimeError(f"Could not find {day} input in WEEKLY GEX LEVELS.")
            await self._fill_input_value(input_box, code or "")
            await page.wait_for_timeout(80)
            if code is not None:
                filled.append(day)
        return filled

    async def close_settings(self, save: bool = True) -> None:
        page = self._require_page()
        await page.bring_to_front()
        if save:
            await self._ensure_status_line_input_unchecked()
            if self.apply_visibility_preset:
                await self._ensure_visibility_days_weeks_months_unchecked()
            # Normalize to Inputs tab before save so subsequent checks read
            # predictable fields on the expected tab.
            await self._select_indicator_tab("Inputs")
            submit_btn = page.locator(
                "[role='dialog'][data-name='indicator-properties-dialog'] button[data-name='submit-button'], "
                "[role='dialog'][data-name='indicator-properties-dialog'] button[name='submit']"
            ).first
            if await submit_btn.count():
                await submit_btn.click()
                await self._wait_for_indicator_dialog_closed()
                return
            save_btn = page.locator(
                "[role='dialog'] button:has-text('OK'), "
                "[role='dialog'] button:has-text('Save'), "
                "[role='dialog'] button:has-text('確定'), "
                "[role='dialog'] button:has-text('儲存')"
            ).first
            if await save_btn.count():
                await save_btn.click()
                await self._wait_for_indicator_dialog_closed()
                return
        await page.keyboard.press("Escape")
        await self._wait_for_indicator_dialog_closed()

    async def _wait_for_indicator_dialog_closed(self, timeout_ms: int = 1500) -> bool:
        """Wait until indicator-properties-dialog is fully removed.

        Avoids the next ``_open_settings_for_locator`` falsely reading a stale
        previous dialog's fields (which would silently produce duplicate reads).
        """
        page = self._require_page()
        elapsed = 0
        step = 60
        while elapsed < timeout_ms:
            if not await self._is_indicator_properties_dialog_present():
                return True
            await page.wait_for_timeout(step)
            elapsed += step
        return not await self._is_indicator_properties_dialog_present()

    async def _ensure_indicator_dialog_closed(self, timeout_ms: int = 1500) -> bool:
        """Force-close any lingering indicator-properties-dialog before reuse."""
        page = self._require_page()
        if not await self._is_indicator_properties_dialog_present():
            return True
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        if await self._wait_for_indicator_dialog_closed(timeout_ms=timeout_ms):
            return True
        # Try clicking generic Cancel as last resort.
        try:
            cancel_btn = page.locator(
                "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('Cancel'), "
                "[role='dialog'][data-name='indicator-properties-dialog'] button:has-text('取消')"
            ).first
            if await cancel_btn.count():
                await cancel_btn.click()
        except Exception:
            pass
        return await self._wait_for_indicator_dialog_closed(timeout_ms=timeout_ms)

    async def _is_indicator_properties_dialog_present(self) -> bool:
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return False
        try:
            return await dialog.count() > 0
        except Exception:
            return False

    async def read_weekly_start_datetime(self) -> tuple[str | None, str | None]:
        """Read Start date (Monday) date/time from currently opened settings dialog."""
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return None, None
        date_input = dialog.locator(
            "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]/following::input[1]"
        ).first
        time_input = dialog.locator(
            "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]/following::input[2]"
        ).first
        if await time_input.count() == 0:
            time_input = dialog.locator(
                "xpath=.//*[contains(normalize-space(.), 'Start date (Monday)')]"
                "/following::input[@data-qa-id='ui-lib-Input-input time-input-input'][1]"
            ).first
        raw_date = await date_input.input_value() if await date_input.count() else None
        time_val = await time_input.input_value() if await time_input.count() else None
        normalized_date = self._normalize_start_date_text(raw_date)
        date_val = normalized_date or ((raw_date or "").strip() or None)
        return date_val, time_val

    async def read_weekly_levels(self) -> dict[str, str | None]:
        """Read Monday~Friday input values under WEEKLY GEX LEVELS."""
        page = self._require_page()
        await page.bring_to_front()
        await self._select_indicator_tab("Inputs")
        levels_section = await self._get_weekly_levels_section()
        if not levels_section:
            return {
                "Monday": None,
                "Tuesday": None,
                "Wednesday": None,
                "Thursday": None,
                "Friday": None,
            }

        out: dict[str, str | None] = {}
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            input_box = await self._find_weekly_day_input(levels_section, day)
            if input_box is None:
                out[day] = None
                continue
            try:
                val = await input_box.input_value()
            except Exception:
                val = None
            out[day] = val
        return out

    def _pick_context(self, browser: Browser) -> BrowserContext:
        if browser.contexts:
            return browser.contexts[0]
        raise RuntimeError("No browser context found from CDP connection.")

    async def _pick_or_open_page(self, context: BrowserContext) -> Page:
        for page in context.pages:
            url = page.url or ""
            if "tradingview.com/chart" in url:
                return page
        page = await context.new_page()
        await page.goto(self.chart_url)
        await page.wait_for_load_state("domcontentloaded")
        return page

    def _require_page(self) -> Page:
        if not self._page:
            raise RuntimeError("Automator is not connected. Call connect() first.")
        return self._page

    async def _dump_dom(self, label: str) -> None:
        """Best-effort debug dump for selector drift troubleshooting."""
        page = self._require_page()
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "debug"
        out_dir = self._debug_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / f"{safe_label}.html"
        png_path = out_dir / f"{safe_label}.png"
        try:
            content = await page.content()
            html_path.write_text(content, encoding="utf-8")
        except Exception:
            pass
        try:
            await page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            pass

    async def _is_indicator_dialog_open(self) -> bool:
        page = self._require_page()
        dialog = page.locator(
            "[role='dialog']:has(input), "
            "[data-name='indicators-dialog'], "
            "[class*='indicatorsDialog']"
        ).first
        return await dialog.count() > 0

    async def _get_indicator_properties_dialog(self):
        """Return indicator settings dialog locator across TV DOM variants."""
        page = self._require_page()
        dialog = page.locator("[role='dialog'][data-name='indicator-properties-dialog']").last
        if await dialog.count():
            return dialog
        fallback = page.locator(
            "[role='dialog']:has([role='tab']:has-text('Inputs')), "
            "[role='dialog']:has([role='tab']:has-text('輸入'))"
        ).last
        if await fallback.count():
            return fallback
        return None

    async def _dialog_looks_like_target_indicator(self, dialog, title_keyword: str) -> bool:
        """Check whether opened settings dialog belongs to target indicator."""
        # Prefer structural anchors that are specific to Daily & Weekly GEX.
        weekly_section = dialog.locator(
            "[data-qa-id='property-dialog-item Weekly GEX'], "
            "[data-qa-id='property-dialog-item Weekly GEX Levels']"
        )
        if await weekly_section.count():
            return True

        # Fallback to title text matching in dialog body/header.
        for text in (title_keyword, "Daily & Weekly GEX"):
            needle = (text or "").strip()
            if not needle:
                continue
            hit = dialog.locator(f":text('{needle}')").first
            if await hit.count():
                return True
        return False

    async def _is_target_settings_dialog_open(self, title_keyword: str) -> bool:
        if await self._is_chart_settings_dialog_open():
            return False
        props = await self._get_indicator_properties_dialog()
        if props is None:
            return False
        return await self._dialog_looks_like_target_indicator(props, title_keyword)

    async def _initialize_indicator_settings_inputs(self) -> None:
        """Normalize an opened indicator dialog to the Inputs tab."""
        page = self._require_page()
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return
        try:
            await self._select_indicator_tab("Inputs")
            await page.wait_for_timeout(60)
        except Exception:
            return

    async def _open_favorites_tab_if_present(self) -> None:
        page = self._require_page()
        fav_tab = page.locator(
            "[role='dialog'] [role='tab']:has-text('Favorites'), "
            "[role='dialog'] [role='tab']:has-text('我的最愛'), "
            "[role='dialog'] [role='tab']:has-text('最愛'), "
            "[role='dialog'] button:has-text('Favorites'), "
            "[role='dialog'] button:has-text('我的最愛'), "
            "[role='dialog'] button:has-text('最愛')"
        ).first
        if await fav_tab.count():
            await fav_tab.click()
            await page.wait_for_timeout(250)

    async def _collect_indicator_dialog_rows(self, limit: int = 8) -> str:
        page = self._require_page()
        rows = page.locator(
            "[role='dialog'] [role='row'], "
            "[role='dialog'] [class*='item'], "
            "[role='dialog'] [data-name*='list-item']"
        )
        count = await rows.count()
        texts: list[str] = []
        for idx in range(min(limit, count)):
            txt = (await rows.nth(idx).inner_text()).strip().replace("\n", " / ")
            if txt:
                texts.append(txt[:120])
        return "; ".join(texts) if texts else "<no rows captured>"

    async def _fill_dialog_field(self, label: str, value: str, index: int = 0) -> bool:
        """Fill input field near a label in settings dialog.

        index is used when a row has multiple inputs (e.g. date/time).
        """
        page = self._require_page()
        dialog = page.locator("[role='dialog']").last

        # Strategy A: row-ish container with the label text and descendant inputs.
        candidates = dialog.locator(
            f"div:has-text('{label}') input, "
            f"label:has-text('{label}') ~ div input, "
            f"*:has-text('{label}') input"
        )
        if await candidates.count() > index:
            target = candidates.nth(index)
            await target.scroll_into_view_if_needed()
            await target.click()
            await target.fill(value)
            await page.keyboard.press("Enter")
            return True

        # Strategy B: accessible label binding.
        labeled = dialog.get_by_label(label, exact=False)
        if await labeled.count() > index:
            target = labeled.nth(index)
            await target.scroll_into_view_if_needed()
            await target.click()
            await target.fill(value)
            await page.keyboard.press("Enter")
            return True

        return False

    async def _fill_dialog_field_by_label(self, label: str, value: str, field_index: int = 0) -> bool:
        """Fill an input/textbox that belongs to a label text in dialog."""
        page = self._require_page()
        dialog = page.locator("[role='dialog']").last

        # Look for nearby editable elements after a label anchor.
        label_xpath = self._xpath_literal(label)
        editable = dialog.locator(
            "xpath=.//*[contains(normalize-space(.), "
            f"{label_xpath})]/following::*"
            "[self::input or self::textarea or @role='textbox' or @contenteditable='true']"
        )
        if await editable.count() > field_index:
            target = editable.nth(field_index)
            await target.scroll_into_view_if_needed()
            await target.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.type(value, delay=0)
            await page.keyboard.press("Enter")
            return True

        # Fallback to existing generic method.
        return await self._fill_dialog_field(label, value, index=field_index)

    async def _fill_weekly_day_with_scroll(self, day: str, value: str) -> bool:
        page = self._require_page()
        dialog = page.locator("[role='dialog']").last
        # Some TradingView dialogs lazy-render lower rows; scroll and retry.
        for _ in range(7):
            if await self._fill_dialog_field_by_label(day, value, field_index=0):
                return True
            await dialog.hover()
            await page.mouse.wheel(0, 260)
            await page.wait_for_timeout(120)
        return False

    @staticmethod
    def _xpath_literal(text: str) -> str:
        if "'" not in text:
            return f"'{text}'"
        if '"' not in text:
            return f'"{text}"'
        parts = text.split("'")
        return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"

    async def _ensure_weekly_compare_checked(self) -> None:
        page = self._require_page()
        levels_section = await self._get_weekly_levels_section()
        if not levels_section:
            return
        checkbox = levels_section.locator(
            "xpath=.//*[contains(normalize-space(.), 'Compare with Previous Day Levels')]/ancestor::label[1]//input[@type='checkbox'][1]"
        ).first
        if await checkbox.count() == 0:
            dialog = page.locator("[role='dialog'][data-name='indicator-properties-dialog']").last
            checkbox = dialog.locator(
                "xpath=.//*[contains(normalize-space(.), 'Compare with Previous Day Levels')]/ancestor::label[1]//input[@type='checkbox'][1]"
            ).first
        if await checkbox.count() == 0:
            return
        try:
            checked = await checkbox.is_checked()
        except Exception:
            checked = False
        if not checked:
            await checkbox.scroll_into_view_if_needed()
            await checkbox.click()
            try:
                checked = await checkbox.is_checked()
            except Exception:
                checked = False
            if not checked:
                # Fallback: click the label text when direct checkbox click is swallowed.
                label = page.locator(
                    "[role='dialog'][data-name='indicator-properties-dialog'] "
                    ":text('Compare with Previous Day Levels')"
                ).first
                if await label.count():
                    await label.click()
            await page.wait_for_timeout(80)

    async def _ensure_status_line_input_unchecked(self) -> None:
        page = self._require_page()
        dialog = page.locator("[role='dialog'][data-name='indicator-properties-dialog']").last
        await self._select_indicator_tab("Style")
        label = dialog.locator(
            "xpath=.//*[contains(normalize-space(.), '狀態行輸入') or "
            "contains(normalize-space(.), 'Inputs in status line') or "
            "contains(normalize-space(.), 'Status line input')]/ancestor::label[1]"
        ).first
        if await label.count() == 0:
            return

        for _ in range(3):
            checked = await self._is_label_checkbox_checked(label)
            if not checked:
                return

            await label.scroll_into_view_if_needed()
            # Try clicking the checkbox input first.
            input_box = label.locator("input[type='checkbox']").first
            if await input_box.count():
                await input_box.click()
                await page.wait_for_timeout(80)
                if not await self._is_label_checkbox_checked(label):
                    return

            # Fallback: click visual checkbox square.
            box = label.locator("[class*='box-'], [data-qa-id*='checkbox-view']").first
            if await box.count():
                await box.click()
                await page.wait_for_timeout(80)
                if not await self._is_label_checkbox_checked(label):
                    return

            # Last fallback: click entire label row text.
            await label.click()
            await page.wait_for_timeout(100)

    async def _ensure_visibility_days_weeks_months_unchecked(self) -> None:
        page = self._require_page()
        dialog = page.locator("[role='dialog'][data-name='indicator-properties-dialog']").last
        await self._select_indicator_tab("Visibility")
        for names in (("Days", "天"), ("Weeks", "週"), ("Months", "月")):
            label = dialog.locator(
                "xpath=.//label[.//*[contains(normalize-space(.), "
                f"{self._xpath_literal(names[0])}) or contains(normalize-space(.), {self._xpath_literal(names[1])})]]"
            ).first
            if await label.count() == 0:
                continue
            for _ in range(2):
                if not await self._is_label_checkbox_checked(label):
                    break
                checkbox = label.locator("input[type='checkbox']").first
                if await checkbox.count():
                    await checkbox.click()
                else:
                    await label.click()
                await page.wait_for_timeout(80)

    async def _is_label_checkbox_checked(self, label) -> bool:
        """Check checkbox state from label subtree (robust across TV UI variants)."""
        input_box = label.locator("input[type='checkbox']").first
        if await input_box.count():
            aria_checked = (await input_box.get_attribute("aria-checked")) or ""
            if aria_checked.lower() in {"true", "false"}:
                return aria_checked.lower() == "true"
            try:
                return await input_box.is_checked()
            except Exception:
                pass
        # Visual class fallback used by TradingView checkbox skin.
        checked_view = label.locator("[class*='checked-'], [data-qa-id*='checkbox-view-checked']").first
        return await checked_view.count() > 0

    async def _select_indicator_tab(self, tab_name: str) -> None:
        """Select indicator properties tab by name (i18n tolerant)."""
        page = self._require_page()
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return
        tab_map = {
            "inputs": ("Inputs", "輸入"),
            "style": ("Style", "樣式"),
            "visibility": ("Visibility", "可見性"),
        }
        keys = tab_map.get(tab_name.strip().lower(), (tab_name,))
        for key in keys:
            tab = dialog.locator(f"[role='tab']:has-text('{key}')").first
            if await tab.count():
                selected = (await tab.get_attribute("aria-selected")) == "true"
                if not selected:
                    await tab.click()
                    await page.wait_for_timeout(140)
                return

    async def _get_weekly_gex_section(self):
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return None
        section = dialog.locator(
            "xpath=.//*[@data-qa-id='property-dialog-item Weekly GEX']/ancestor::div[contains(@class,'titleWrap')][1]"
        ).first
        return section if await section.count() else None

    async def _get_weekly_levels_section(self):
        dialog = await self._get_indicator_properties_dialog()
        if dialog is None:
            return None
        section = dialog.locator(
            "xpath=.//*[@data-qa-id='property-dialog-item Weekly GEX Levels']/ancestor::div[contains(@class,'titleWrap')][1]"
        ).first
        return section if await section.count() else None

    async def _find_weekly_day_input(self, levels_section, day: str):
        # Scroll within dialog and try multiple times because lower rows are lazy-visible.
        page = self._require_page()
        dialog = page.locator("[role='dialog'][data-name='indicator-properties-dialog']").last
        day_xpath = self._xpath_literal(day)
        for _ in range(8):
            candidate = levels_section.locator(
                "xpath=.//following::div[contains(@class,'cell-RLntasnw')][.//div[contains(@class,'inner-RLntasnw') and normalize-space()="
                f"{day_xpath}]]"
                "[1]/following::input[@data-qa-id='ui-lib-Input-input'][1]"
            ).first
            if await candidate.count():
                return candidate
            await dialog.hover()
            await page.mouse.wheel(0, 240)
            await page.wait_for_timeout(120)
        return None

    async def _fill_input_value(self, target, value: str) -> None:
        page = self._require_page()
        await target.scroll_into_view_if_needed()
        await target.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.type(value, delay=0)
        await page.keyboard.press("Enter")

    async def _try_click_open_layout_menuitem(self) -> bool:
        """In the manage-layout dropdown, click the row that opens saved layouts (Dot)."""
        page = self._require_page()
        roots = (
            page.locator("[data-qa-id='popup-menu-container'] [role='menuitem']"),
            page.locator("#overlap-manager-root [role='menuitem']"),
        )
        label_rx = re.compile(
            r"(open\s*layout|開啟版面|打开布局|開啟版面配置|開啟版面…)",
            re.I,
        )
        for root in roots:
            try:
                n = await root.count()
            except Exception:
                n = 0
            for i in range(min(n, 48)):
                item = root.nth(i)
                try:
                    txt = ((await item.inner_text(timeout=500)) or "").strip()
                except Exception:
                    continue
                if not txt or not label_rx.search(txt):
                    continue
                try:
                    await item.click(timeout=1400)
                    self._log(f"[open_layout_dialog] clicked Open layout menuitem: {txt[:72]!r}")
                    return True
                except Exception as exc:
                    self._log(f"[open_layout_dialog] Open layout menuitem click err: {exc!r}")
        return False

    async def _open_layout_dialog_via_header_layout_dropdown(self) -> bool:
        """Header layout name / #header-toolbar-layouts → 'Open layout...' (not chart properties)."""
        page = self._require_page()
        await page.bring_to_front()
        triggers = page.locator("#header-toolbar-layouts, [data-name='header-toolbar-layouts']")
        try:
            cnt = await triggers.count()
        except Exception:
            cnt = 0
        self._log(f"[open_layout_dialog] header-layout-dropdown candidates={cnt}")
        for i in range(cnt):
            el = triggers.nth(i)
            try:
                if not await el.is_visible():
                    continue
                await el.click(timeout=1600)
            except Exception as exc:
                self._log(f"[open_layout_dialog] header-layout-dropdown click err i={i} {exc!r}")
                continue
            await page.wait_for_timeout(280)
            if await self._is_layout_dialog_open():
                self._log("[open_layout_dialog] header toolbar layouts opened dialog directly")
                return True
            if await self._try_click_open_layout_menuitem():
                await page.wait_for_timeout(360)
                if await self._is_layout_dialog_open():
                    self._log("[open_layout_dialog] opened via header dropdown → Open layout...")
                    return True
            await self._focus_chart_for_shortcuts()
            await page.wait_for_timeout(100)
        return False

    async def _open_layout_dialog(self) -> bool:
        """Open saved-layouts list: '.' first; if only manage menu appears, use Open layout…

        Do NOT press Escape here -- in some flows it cancels the prior layout switch.
        """
        page = self._require_page()
        await page.bring_to_front()
        if await self._is_layout_dialog_open():
            return True
        # Menu already open (e.g. user left manage-layout dropdown): try Open layout…
        if await self._is_any_popup_menu_open():
            if await self._try_click_open_layout_menuitem():
                await page.wait_for_timeout(340)
                if await self._is_layout_dialog_open():
                    self._log("[open_layout_dialog] opened via Open layout... (pre-existing menu)")
                    return True
            self._log("[open_layout_dialog] popup already open; skip '.' shortcut typing")
            return False
        await page.wait_for_timeout(50)

        await self._focus_chart_for_shortcuts()
        for key in (".", "Period", "NumpadDecimal"):
            try:
                await page.keyboard.press(key)
            except Exception:
                continue
            for _ in range(12):
                await page.wait_for_timeout(130)
                if await self._is_layout_dialog_open():
                    self._log(f"[open_layout_dialog] opened via key={key}")
                    return True
                if await self._is_any_popup_menu_open():
                    # '.' often opens the manage menu first; that menu is still [role=menu],
                    # so we must click Open layout… instead of aborting as non-target.
                    if await self._try_click_open_layout_menuitem():
                        await page.wait_for_timeout(300)
                        if await self._is_layout_dialog_open():
                            self._log(f"[open_layout_dialog] opened via Open layout... after key={key}")
                            return True
                    self._log(
                        f"[open_layout_dialog] popup menu after key={key} but not Layouts dialog; "
                        "stopping further keys (avoid typing into search)"
                    )
                    break

        if await self._open_layout_dialog_via_header_layout_dropdown():
            return True
        if await self._open_layout_dialog_via_current_layout_menu():
            return True
        if await self._open_layout_dialog_via_save_load_menu():
            return True
        await self._log_layout_open_candidates()
        await self._dump_dom("layout_dialog_open_failed")
        return False

    async def _focus_chart_for_shortcuts(self) -> None:
        """Best-effort focus transfer back to chart so '.' shortcut works."""
        page = self._require_page()
        selectors = (
            "[data-name='legend-source-item']",
            "[data-name='pane']",
            "[data-name='chart-widget']",
            ".chart-widget",
            "canvas",
        )
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=900)
                await page.wait_for_timeout(80)
                return
            except Exception:
                continue

    async def _is_any_popup_menu_open(self) -> bool:
        """Detect any TradingView popup menu currently visible."""
        page = self._require_page()
        popup = page.locator(
            "[data-qa-id='popup-menu-container'] [role='menu'], "
            "#overlap-manager-root [role='menu']"
        ).first
        try:
            return await popup.count() > 0
        except Exception:
            return False

    async def _open_layout_dialog_via_current_layout_menu(self) -> bool:
        page = self._require_page()
        current_btn = page.locator(
            "button[aria-label*='當前版面'], button[aria-label*='当前版面'], "
            "button[data-tooltip*='當前版面'], button[data-tooltip*='当前版面'], "
            "button[aria-label*='Current layout'], button[aria-label*='Current Layout'], "
            "button[data-tooltip*='Current layout'], button[data-tooltip*='Current Layout']"
        ).first
        if await current_btn.count() == 0:
            return False
        try:
            await current_btn.click(timeout=1200)
            await page.wait_for_timeout(220)
        except Exception:
            return False

        if await self._try_click_open_layout_menuitem():
            await page.wait_for_timeout(300)
            if await self._is_layout_dialog_open():
                self._log("[open_layout_dialog] via-current-layout-btn → Open layout...")
                return True

        # Try likely menu actions that open saved-layout list.
        items = page.locator(
            "[role='menuitem']:has-text('版面'), [role='menuitem']:has-text('佈局'), "
            "[role='menuitem']:has-text('Layout'), [role='menuitem']:has-text('載入'), "
            "[role='menuitem']:has-text('開啟'), [role='menuitem']:has-text('Open')"
        )
        try:
            count = await items.count()
        except Exception:
            count = 0
        for i in range(min(count, 12)):
            item = items.nth(i)
            try:
                txt = ((await item.inner_text(timeout=600)) or "").strip()
            except Exception:
                txt = ""
            low = txt.lower()
            if not txt:
                continue
            # Avoid re-clicking split-layout setup entries.
            if any(k in low for k in ("同步", "sync", "crosshair", "商品", "週期", "symbol")):
                continue
            try:
                await item.click(timeout=1000)
                await page.wait_for_timeout(260)
            except Exception:
                continue
            if await self._is_layout_dialog_open():
                self._log(f"[open_layout_dialog] via-current-layout-menu item='{txt[:60]}'")
                return True
        return False

    async def _open_layout_dialog_via_save_load_menu(self) -> bool:
        page = self._require_page()
        trigger_clicked = False
        primary = page.locator("[data-name='save-load-menu'], button[data-name='save-load-menu']")
        try:
            p_count = await primary.count()
        except Exception:
            p_count = 0
        self._log(f"[open_layout_dialog] via-save-load-menu candidates={p_count}")
        for i in range(p_count):
            t = primary.nth(i)
            try:
                if not await t.is_visible():
                    self._log(f"[open_layout_dialog] via-save-load-menu idx={i} visible=False")
                    continue
                await t.click(timeout=1200)
                self._log(f"[open_layout_dialog] via-save-load-menu idx={i} click=ok")
                trigger_clicked = True
                break
            except Exception as exc:
                self._log(f"[open_layout_dialog] via-save-load-menu idx={i} click=err {exc!r}")
                continue
        if not trigger_clicked:
            # DOM-level fallback for hidden/overlay quirks.
            try:
                clicked = await page.evaluate(
                    """
                    () => {
                      const nodes = Array.from(document.querySelectorAll("[data-name='save-load-menu']"));
                      const vis = nodes.find((el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width > 2 && r.height > 2 && st.display !== "none" && st.visibility !== "hidden";
                      });
                      if (!vis) return false;
                      vis.click();
                      return true;
                    }
                    """
                )
                trigger_clicked = bool(clicked)
                self._log(f"[open_layout_dialog] via-save-load-menu dom-click={trigger_clicked}")
            except Exception:
                trigger_clicked = False
                self._log("[open_layout_dialog] via-save-load-menu dom-click=err")
        if not trigger_clicked:
            return False
        await page.wait_for_timeout(240)

        if await self._is_layout_dialog_open():
            self._log("[open_layout_dialog] via-save-load-menu direct-open")
            return True

        # Snapshot menuitem candidates once, then probe each by index.
        items = page.locator("[role='menuitem']")
        try:
            count = await items.count()
        except Exception:
            count = 0
        for i in range(min(count, 12)):
            # Re-open menu each probe because click usually closes it.
            try:
                await page.locator("[data-name='save-load-menu']").first.click(timeout=1000)
                await page.wait_for_timeout(160)
            except Exception:
                pass
            probe_items = page.locator("[role='menuitem']")
            try:
                if await probe_items.count() <= i:
                    continue
                it = probe_items.nth(i)
            except Exception:
                continue
            try:
                txt = ((await it.inner_text(timeout=500)) or "").strip()
            except Exception:
                txt = ""
            try:
                dn = ((await it.get_attribute("data-name")) or "").strip().lower()
            except Exception:
                dn = ""
            low = txt.lower()
            if any(k in low for k in ("save", "儲存", "rename", "重新命名", "delete", "刪除")):
                continue
            if any(k in dn for k in ("save", "rename", "delete", "remove")):
                continue
            try:
                await it.click(timeout=1000)
                await page.wait_for_timeout(260)
            except Exception:
                continue
            if await self._is_layout_dialog_open():
                self._log(f"[open_layout_dialog] via-save-load-menu item#{i} txt='{txt[:50]}' dn='{dn[:40]}'")
                return True
        return False

    async def _log_layout_open_candidates(self) -> None:
        """Log top candidate controls when layout dialog cannot be opened."""
        page = self._require_page()
        try:
            candidates = await page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll("button, [role='button'], [role='menuitem']"));
                  const out = [];
                  for (const el of nodes) {
                    const txt = (el.textContent || "").replace(/\\s+/g, " ").trim();
                    const aria = (el.getAttribute("aria-label") || "").trim();
                    const title = (el.getAttribute("title") || "").trim();
                    const dn = (el.getAttribute("data-name") || "").trim();
                    const dt = (el.getAttribute("data-tooltip") || "").trim();
                    const all = `${txt} ${aria} ${title} ${dn} ${dt}`.toLowerCase();
                    if (!/(layout|版面|佈局|當前版面|当前版面|open|載入|開啟)/.test(all)) continue;
                    out.push({ txt, aria, title, data_name: dn, data_tooltip: dt });
                    if (out.length >= 20) break;
                  }
                  return out;
                }
                """
            )
        except Exception:
            candidates = []
        if not candidates:
            self._log("[open_layout_dialog] candidates: none")
            return
        for idx, c in enumerate(candidates, start=1):
            self._log(
                "[open_layout_dialog] candidate "
                f"{idx}: txt='{(c.get('txt') or '')[:40]}' "
                f"aria='{(c.get('aria') or '')[:50]}' "
                f"title='{(c.get('title') or '')[:30]}' "
                f"data-name='{(c.get('data_name') or '')[:40]}' "
                f"data-tooltip='{(c.get('data_tooltip') or '')[:40]}'"
            )

    def _layout_dialog_locator(self):
        page = self._require_page()
        return page.locator(
            "[role='dialog']:has-text('Layouts'), "
            "[role='dialog']:has-text('LAYOUT NAME'), "
            "[role='dialog']:has-text('版面配置'), "
            "[role='dialog']:has-text('佈局配置'), "
            "[role='dialog'][aria-label*='Layouts'], "
            "[role='dialog'][aria-label*='版面'], "
            "[role='dialog'][aria-label*='佈局'], "
            # Some TV builds render the layouts UI as popup menu instead of dialog.
            "[data-qa-id='popup-menu-container'] [role='menu'][data-name='layouts-list'], "
            "#overlap-manager-root [role='menu'][data-name='layouts-list']"
        ).first

    async def _is_layout_dialog_open(self) -> bool:
        dialog = self._layout_dialog_locator()
        return await dialog.count() > 0

    async def _read_current_layout_name(self) -> str | None:
        """Best-effort read current layout name from header toolbar control."""
        page = self._require_page()
        selectors = [
            "[data-name='header-toolbar-layouts']",
            "#header-toolbar-layouts",
            "button[aria-label*='Layout setup']",
            "button[aria-label*='Layout']",
            "button[aria-label*='版面']",
            "button[aria-label*='佈局']",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                text = (await loc.inner_text()).strip()
            except Exception:
                text = ""
            cleaned = self._clean_layout_name_text(text)
            if cleaned:
                return cleaned
            try:
                aria = (await loc.get_attribute("aria-label") or "").strip()
            except Exception:
                aria = ""
            cleaned_aria = self._clean_layout_name_text(aria)
            if cleaned_aria:
                return cleaned_aria
        return None

    @staticmethod
    def _clean_layout_name_text(raw: str | None) -> str | None:
        if not raw:
            return None
        text = " ".join(raw.replace("\n", " ").split()).strip()
        if not text:
            return None
        upper = text.upper()
        generic = {
            "LAYOUT",
            "LAYOUTS",
            "LAYOUT SETUP",
            "版面",
            "版面設定",
            "佈局",
            "佈局設定",
        }
        if upper in generic:
            return None
        parts = [p.strip() for p in re.split(r"[|/]", text) if p.strip()]
        if parts:
            tail = parts[-1]
            if tail and tail.upper() not in generic:
                return tail
        return text

    async def get_current_layout_name(self) -> str | None:
        """Public helper for runtime guard in batch/preview loops."""
        return await self._read_current_layout_name()

    async def get_runtime_snapshot(self) -> dict[str, str | int | None]:
        """Collect lightweight runtime debug info for logs."""
        page = self._require_page()
        return {
            "layout_name": await self._read_current_layout_name(),
            "symbol": await self.get_symbol_search_value(),
            "visible_widgets": await self._count_visible_chart_widgets(),
            "url": page.url,
        }

    async def _count_visible_chart_widgets(self) -> int:
        page = self._require_page()
        return int(
            await page.evaluate(
                """
                () => {
                  const nodes = Array.from(
                    document.querySelectorAll("[data-name='chart-widget'], [class*='chart-widget']")
                  );
                  return nodes.filter((el) => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 20 && st.display !== "none" && st.visibility !== "hidden";
                  }).length;
                }
                """
            )
        )

    @staticmethod
    def _layout_rows_locator(dialog):
        # Keep this strict to avoid matching the dialog itself, header
        # elements or unrelated toolbar buttons via [data-name*='layout'].
        return dialog.locator(
            "[role='listbox'] [role='option'], "
            "[role='option'], "
            "[role='list'] [role='listitem'], "
            "[data-name*='layout-item']"
        )

    async def _ensure_layouts_all_tab(self, dialog) -> None:
        """Best-effort switch Layouts dialog to 'All layouts' tab."""
        page = self._require_page()
        tab_labels = (
            "All layouts",
            "All Layouts",
            "All",
            "全部",
            "所有",
            "所有版面",
            "全部版面",
        )
        for label in tab_labels:
            tab = dialog.locator(f"[role='tab']:has-text('{label}')").first
            if await tab.count() == 0:
                continue
            try:
                selected = (await tab.get_attribute("aria-selected")) == "true"
            except Exception:
                selected = False
            if selected:
                return
            try:
                await tab.click(timeout=1200)
                await page.wait_for_timeout(220)
                return
            except Exception:
                continue


