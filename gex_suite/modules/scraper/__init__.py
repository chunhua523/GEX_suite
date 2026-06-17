"""Scraper module: PySide6 widget + Playwright backend (LietaScraper).

Standalone usage::

    python -m gex_suite.modules.scraper
"""
from .runner import LietaScraper, LoginRequiredError, BASE_URL  # noqa: F401
from .widget import ScraperPage  # noqa: F401
