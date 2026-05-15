"""FastAPI server entry point.

Run via:

    python -m gex_suite.api.server
    # or, after `pip install -e .`:
    gex-api
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI

from gex_suite.shared.paths import ensure_dirs

from .routes import scraper as scraper_routes

# Load DISCORD_NOTIFY_WEBHOOK from Jeff-Agent .env if not already in env.
# Both Macs keep this in ~/Jeff-Agent/.env or ~/Documents/Jeff-Agent/.env.
def _load_env_fallback() -> None:
    if os.environ.get("DISCORD_NOTIFY_WEBHOOK"):
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    candidates = [
        Path.home() / "Jeff-Agent" / ".env",
        Path.home() / "Documents" / "Jeff-Agent" / ".env",
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
            return


_load_env_fallback()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="GEX Suite API",
    description="Remote control for the GEX Scraper (Discord bot backend).",
    version="1.0.0",
)
app.include_router(scraper_routes.router)


@app.on_event("startup")
def _on_startup() -> None:
    ensure_dirs()
    scraper_routes.start_schedule_worker()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    import uvicorn
    port = int(os.environ.get("GEX_API_PORT", "8765"))
    host = os.environ.get("GEX_API_HOST", "127.0.0.1")
    uvicorn.run(
        "gex_suite.api.server:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
