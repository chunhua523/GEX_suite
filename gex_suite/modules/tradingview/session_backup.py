"""TradingView CDP session cookie backup / restore.

Paste relies on ``sessionid`` living in the dedicated Chrome-CDP profile.
That cookie is often wiped by TradingView server-side (device / connection
limits, "Log out everywhere") — not by local TTL. A local backup cannot
revive a revoked session, but it **does** recover from local profile wipe /
Cookies-DB loss without a manual re-login.

Flow (see ``Automator._assert_logged_in``):
  - logged in  → overwrite backup
  - missing    → try restore → soft-validate via chart navigation →
                 re-check ``sessionid`` (TV clears dead cookies on load)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from gex_suite.shared.paths import TRADINGVIEW_SESSION_COOKIES_PATH

_TV_COOKIE_URL = "https://www.tradingview.com/"
_TV_VALIDATE_URL = "https://www.tradingview.com/chart/"

# Analytics / consent noise — skip so the backup stays auth-focused.
_SKIP_COOKIE_NAMES = frozenset({
    "_ga",
    "_gid",
    "_gat",
    "_gcl_au",
    "__gads",
    "__gpi",
    "__eoi",
    "_sp_id.cf1a",
    "_sp_ses.cf1a",
    "sp",
    "cookiePrivacyPreferenceBannerProduction",
    "cookiesSettings",
    "g_state",
})


def backup_path() -> Path:
    return TRADINGVIEW_SESSION_COOKIES_PATH


def has_backup(path: Path | None = None) -> bool:
    p = path or backup_path()
    return p.is_file() and p.stat().st_size > 0


def _is_tv_host(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return d == "tradingview.com" or d.endswith(".tradingview.com")


def _should_backup_cookie(cookie: dict[str, Any]) -> bool:
    name = str(cookie.get("name") or "")
    if not name or name in _SKIP_COOKIE_NAMES:
        return False
    if name.startswith("_ga") or name.startswith("_sp_"):
        return False
    value = cookie.get("value")
    if value is None or value == "":
        return False
    return _is_tv_host(str(cookie.get("domain") or ""))


def _serialize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie.get("path") or "/",
        "httpOnly": bool(cookie.get("httpOnly")),
        "secure": bool(cookie.get("secure")),
    }
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        out["expires"] = float(expires)
    same_site = cookie.get("sameSite")
    if same_site in ("Strict", "Lax", "None"):
        out["sameSite"] = same_site
    return out


def save_cookies(cookies: list[dict[str, Any]], path: Path | None = None) -> int:
    """Persist auth-ish TradingView cookies. Returns count written (0 = skip)."""
    selected = [_serialize_cookie(c) for c in cookies if _should_backup_cookie(c)]
    if not any(c["name"] == "sessionid" for c in selected):
        return 0
    target = path or backup_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "cookies": selected,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return len(selected)


def load_cookies(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or backup_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not name or value is None or value == "" or not domain:
            continue
        out.append(_serialize_cookie(item))
    return out


async def backup_from_context(
    context: BrowserContext,
    path: Path | None = None,
) -> int:
    cookies = await context.cookies(_TV_COOKIE_URL)
    return save_cookies(cookies, path=path)


async def restore_into_context(
    context: BrowserContext,
    path: Path | None = None,
) -> int:
    """Inject backed-up cookies. Returns number added (0 = nothing to restore)."""
    cookies = load_cookies(path=path)
    if not cookies:
        return 0
    try:
        await context.add_cookies(cookies)
    except Exception:
        return 0
    return len(cookies)


async def validate_session_after_restore(
    context: BrowserContext,
    page: Page | None = None,
) -> bool:
    """After restore, hit a chart URL so TradingView can drop revoked cookies.

    Returns True only if ``sessionid`` is still present afterwards.
    """
    probe = page
    created = False
    try:
        if probe is None:
            probe = await context.new_page()
            created = True
        await probe.goto(
            _TV_VALIDATE_URL,
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            await probe.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        cookies = await context.cookies(_TV_COOKIE_URL)
        return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
    except Exception:
        return False
    finally:
        if created and probe is not None:
            try:
                await probe.close()
            except Exception:
                pass
