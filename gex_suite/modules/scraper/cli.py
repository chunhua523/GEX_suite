"""GEX Scraper CLI — headless mode for cron / n8n.

Examples::

    python -m gex_suite.modules.scraper.cli
    python -m gex_suite.modules.scraper.cli --models "TV Code"
    python -m gex_suite.modules.scraper.cli --groups "Index,科技股"
    python -m gex_suite.modules.scraper.cli --headless
    python -m gex_suite.modules.scraper.cli --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

from gex_suite.shared.paths import (
    SCRAPER_DATA_DIR,
    SCRAPER_LOG_DIR,
    SCRAPER_SETTINGS_PATH,
    SCRAPER_STATE_PATH,
    ensure_dirs,
)

from .runner import LietaScraper
from .utils import load_tickers_with_groups

DEFAULT_SETTINGS = {
    "ticker_filepath": "",
    "cme_ticker_filepath": "",
    "download_folder": "",
    "selected_models": ["Gamma", "Term", "Smile", "TV Code"],
    "selected_cme_models": ["Gamma", "Smile", "Term", "TV Code"],
    "parallel": True,
    "browser": "chrome",
}


def setup_logging() -> logging.Logger:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = SCRAPER_LOG_DIR / f"cli_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("GEX_CLI")


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SCRAPER_SETTINGS_PATH.exists():
        try:
            with SCRAPER_SETTINGS_PATH.open("r", encoding="utf-8") as f:
                saved = json.load(f)
            settings.update(saved or {})
        except Exception as exc:
            print(f"⚠️  讀取 settings.json 失敗，使用預設值：{exc}")
    return settings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GEX Scraper CLI")
    p.add_argument("--models", default="", help='Comma list, e.g. "TV Code,Gamma"')
    p.add_argument("--cme-models", default="", help="CME models, comma list")
    p.add_argument("--groups", default="", help="Only run given ticker groups")
    p.add_argument("--tv-code-only", action="store_true", help="Only run TV Code (fast)")
    p.add_argument("--headless", action="store_true", help="Headless browser")
    p.add_argument("--no-cme", action="store_true", help="Skip CME platform")
    p.add_argument("--parallel", action="store_true", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--result-json", default="")
    p.add_argument("--retry-failed-file", default="")
    return p.parse_args()


def get_tickers_for_groups(filepath: str, groups: list[str]) -> list[str]:
    all_groups = load_tickers_with_groups(filepath)
    if not groups:
        out: list[str] = []
        for v in all_groups.values():
            out.extend(v)
        return list(dict.fromkeys(out))
    out = []
    for g in groups:
        g = g.strip()
        if g in all_groups:
            out.extend(all_groups[g])
        else:
            print(f"⚠️  group not found: {g}; available: {list(all_groups.keys())}")
    return list(dict.fromkeys(out))


async def run_scraper(tickers, models, cme_tickers, cme_models, download_folder,
                     parallel, headless, logger):
    def log_func(msg: str) -> None:
        logger.info(msg)

    scraper = LietaScraper(logger_func=log_func, browser_type="brave")
    await scraper.start_browser(headless=headless)
    if headless:
        logger.info("✅ Headless launched.")
    else:
        logger.info("✅ Browser launched (with window).")

    result = {
        "initial_failed_tasks": [],
        "retry_failed_tasks": [],
        "retried": False,
        "total_processed": 0,
        "success_count": 0,
        "failed_count": 0,
    }
    try:
        failed = await scraper.run_scraping_job(
            tickers=tickers,
            models=models,
            cme_tickers=cme_tickers,
            cme_models=cme_models,
            download_folder=download_folder,
            parallel_mode=parallel,
        )
        result["initial_failed_tasks"] = failed or []
        result["success_count"] = int(getattr(scraper, "success_count", 0))
        result["failed_count"] = len(result["initial_failed_tasks"])
        result["total_processed"] = result["success_count"] + result["failed_count"]
        if failed:
            logger.warning(f"⚠️  {len(failed)} failed (no auto-retry).")
    finally:
        await scraper.close()
    return result


async def run_retry_only(failed_tasks, download_folder, parallel, headless, logger):
    def log_func(msg: str) -> None:
        logger.info(msg)
    scraper = LietaScraper(logger_func=log_func, browser_type="brave")
    await scraper.start_browser(headless=headless)
    try:
        remaining = await scraper.retry_scraping_job(
            failed_tasks=failed_tasks,
            download_folder=download_folder,
            parallel_mode=parallel,
        )
        success = int(getattr(scraper, "success_count", 0))
        failed_count = len(remaining or [])
        return {
            "initial_failed_tasks": failed_tasks,
            "retry_failed_tasks": remaining or [],
            "retried": True,
            "retry_only": True,
            "total_processed": success + failed_count,
            "success_count": success,
            "failed_count": failed_count,
        }
    finally:
        await scraper.close()


def main() -> int:
    args = parse_args()
    logger = setup_logging()
    settings = load_settings()

    logger.info("=" * 50)
    logger.info(f"🚀 GEX Scraper CLI starting {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 50)

    if args.tv_code_only:
        models = ["TV Code"]
        cme_models = ["TV Code"]
    else:
        models = [m.strip() for m in args.models.split(",")] if args.models else settings["selected_models"]
        cme_models = [m.strip() for m in args.cme_models.split(",")] if args.cme_models else settings["selected_cme_models"]

    ticker_fp = settings.get("ticker_filepath", "")
    if not ticker_fp or not os.path.exists(ticker_fp):
        logger.error(f"❌ ticker_filepath invalid: {ticker_fp}")
        return 1
    groups = [g.strip() for g in args.groups.split(",")] if args.groups else []
    tickers = get_tickers_for_groups(ticker_fp, groups)

    if args.no_cme:
        cme_tickers: list[str] = []
    else:
        cme_fp = settings.get("cme_ticker_filepath", "")
        cme_tickers = get_tickers_for_groups(cme_fp, []) if cme_fp and os.path.exists(cme_fp) else []

    parallel = settings.get("parallel", True)
    if args.parallel is not None:
        parallel = args.parallel
    headless = args.headless

    download_folder = settings.get("download_folder", "")
    logger.info(f"📂 download: {download_folder}")
    logger.info(f"📋 std tickers: {len(tickers)}")
    logger.info(f"📋 std models: {models}")
    logger.info(f"📋 cme tickers: {len(cme_tickers)}")
    logger.info(f"📋 cme models: {cme_models}")
    logger.info(f"⚙️  parallel: {parallel}, headless: {headless}")

    if args.dry_run:
        logger.info("ℹ️  Dry run, exiting.")
        return 0

    if not download_folder or not os.path.isdir(download_folder):
        logger.error(f"❌ download folder invalid: {download_folder}")
        return 1
    if not SCRAPER_STATE_PATH.exists():
        logger.error("❌ state.json not found; run 'Log in via Browser' first.")
        return 1

    started = datetime.now()
    if args.retry_failed_file:
        with open(args.retry_failed_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        failed_tasks = payload if isinstance(payload, list) else payload.get("failed_tasks", [])
        if not isinstance(failed_tasks, list):
            logger.error("❌ retry-failed-file format error")
            return 1
        result = asyncio.run(run_retry_only(failed_tasks, download_folder, parallel, headless, logger))
    else:
        result = asyncio.run(
            run_scraper(tickers, models, cme_tickers, cme_models, download_folder, parallel, headless, logger)
        )
    elapsed = (datetime.now() - started).total_seconds()
    final_failed = result.get("retry_failed_tasks") or result.get("initial_failed_tasks", [])
    summary = {
        "success": len(final_failed) == 0,
        "elapsed_seconds": round(elapsed, 2),
        "initial_failed_count": len(result.get("initial_failed_tasks", [])),
        "retry_failed_count": len(result.get("retry_failed_tasks", [])),
        "failed_tasks": final_failed,
        "retried": bool(result.get("retried")),
        "retry_only": bool(result.get("retry_only")),
        "finished_at": datetime.now().isoformat(),
    }
    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info(f"✅ done in {elapsed:.0f}s")
    logger.info("=" * 50)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
