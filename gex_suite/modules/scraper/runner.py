import asyncio
import os
import time
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from datetime import datetime

from . import utils
from gex_suite.shared.paths import SCRAPER_DATA_DIR, SCRAPER_STATE_PATH, ensure_dirs

# URL
BASE_URL = "https://www.lietaresearch.com"
ensure_dirs()
STOP_FLAG_PATH = str(SCRAPER_DATA_DIR / ".stop_requested")


class LoginRequiredError(Exception):
    """Raised when the page indicates session expired / not logged in."""
    pass


LOGIN_REQUIRED_REASON = "Login required (session expired)"

class LietaScraper:
    def __init__(self, logger_func=print, browser_type="chrome"):
        self.log = logger_func
        self.playwright = None
        self.browser = None
        self.storage_state_path = str(SCRAPER_STATE_PATH)
        self.browser_type = browser_type
        
        self.stop_requested = False # Flag to control stopping

    def _refresh_stop_requested(self):
        """Allow external process (API) to request graceful stop via file flag."""
        if not self.stop_requested and os.path.exists(STOP_FLAG_PATH):
            self.stop_requested = True
            self.log("External stop requested. Finishing current step and stopping...")

    def _get_brave_path(self):
        """Attempts to find Brave Browser executable path."""
        import platform
        system = platform.system()
        
        if system == "Windows":
            paths = [
                os.path.join(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware\\Brave-Browser\\Application\\brave.exe")
            ]
            for p in paths:
                if os.path.exists(p):
                    return p
        elif system == "Darwin": # macOS
            path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
            if os.path.exists(path):
                return path
        
        return None

    async def start_browser(self, headless=False):
        self.playwright = await async_playwright().start()
        
        launch_args = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"]
        }

        if self.browser_type == "brave":
            brave_path = self._get_brave_path()
            if brave_path:
                launch_args["executable_path"] = brave_path
                self.log(f"Verified Brave path: {brave_path}")
            else:
                self.log("Brave not found, falling back to System Chrome...")
                launch_args["channel"] = "chrome"
        else:
            # Default to Chrome
            launch_args["channel"] = "chrome"

        self.browser = await self.playwright.chromium.launch(**launch_args)
        self.log(f"Browser launched ({self.browser_type}).")

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.log("Browser closed.")

    async def ensure_login(self):
        """
        Opens browser, checks if logged in. If not, waits for user to log in.
        Saves state to storage_state_path.
        """
        # Load existing state if available
        context_args = {}
        if os.path.exists(self.storage_state_path):
            context_args["storage_state"] = self.storage_state_path
            self.log("Found existing session state.")

        context = await self.browser.new_context(**context_args)
        page = await context.new_page()
        
        try:
            self.log(f"Navigating to {BASE_URL}...")
            await page.goto(BASE_URL)
            
            # Check for login indicator. 
            # Looking at screenshot: There is a Profile Icon (green circle with person).
            # We assume if that exists, we are logged in.
            # If we see a "Login" or "Sign In" button, we are not.
            
            # Simple check: wait for profile icon or login form
            # Adjust selector based on actual site content. 
            # Assuming profile icon has some class or ID.
            # If we can't be sure, we just pause and ask user.
            
            # For now, we will perform a heuristic check. 
            # If user hasn't logged in before, they likely need to.
            
            # We'll just wait for the user to confirm in the UI (or we can automate the wait).
            # Better approach: Open the page, tell user "Please login in the browser window if not already",
            # then wait for a signal or poll for a specific element.
            
            self.log("Waiting for you to log in... (Close the browser window when done)")
            
            # Wait loop: Check if page/browser is closed every 1 second
            while not page.is_closed() and self.browser.is_connected():
                await asyncio.sleep(1)

            # NOTE: If user closed the page, we can't save state easily if context is gone?
            # Actually, if page is closed, context might still be open. 
            # But standard behavior: User closes window -> Page closes.
            # We need to save state JUST BEFORE close or implies user finished.
            
            # Ideally, user should click a button in our GUI "I finished Login"? 
            # Or we autosave periodically?
            
            # Let's save logic: We will save state every few seconds WHILE user is logging in, 
            # so that when they close, we have the latest state.
            
        except Exception as e:
            self.log(f"Login check failed (or window closed): {e}")
        finally:
            # Try to save one last time if context is still valid
            try:
                await context.storage_state(path=self.storage_state_path)
                self.log("Session saved to disk.")
            except:
                pass
            await context.close()

    async def perform_login_flow(self):
        """
        Runs the full login lifecycle in a single event loop.
        """
        await self.start_browser(headless=False)
        await self.ensure_login()
        await self.close()

    async def perform_full_job(self, tickers, models, cme_tickers, cme_models, download_folder, parallel):
        """
        Runs the full job lifecycle (Start -> Run -> Close) in a single loop.
        Returns list of failed tasks.
        """
        try:
            await self.start_browser(headless=False)
            return await self.run_scraping_job(tickers, models, cme_tickers, cme_models, download_folder, parallel)
        finally:
            await self.close()

    async def perform_retry_job(self, failed_tasks, download_folder, parallel):
        """
        Runs a retry job for specific failed tasks.
        """
        try:
            await self.start_browser(headless=False)
            return await self.retry_scraping_job(failed_tasks, download_folder, parallel)
        finally:
            await self.close()

    async def _assert_logged_in_for_platform(self, page, target_url):
        """
        Fail fast when platform page is unavailable due to not-logged-in state.
        """
        # Wait shortly for either: platform UI ready OR login-required hints.
        # This avoids waiting full 60s Playwright timeout on each model page.
        for _ in range(20):  # up to ~10s
            if await page.get_by_text("Select model", exact=False).count() > 0:
                return
            if await page.get_by_text("選擇模型", exact=False).count() > 0:
                return

            page_text = await page.evaluate("() => document.body.innerText")
            lowered = page_text.lower()
            has_login_marker = (
                ("login" in lowered)
                or ("log in" in lowered)
                or ("sign in" in lowered)
                or ("登入" in page_text)
            )
            has_home_hero = ("掌握數據" in page_text) or ("跟隨市場" in page_text)

            # If page looks like landing/login page, fail immediately.
            if has_login_marker or has_home_hero:
                raise LoginRequiredError(
                    f"Not logged in or session expired while opening {target_url}. "
                    "Please run 'Log in via Browser' first."
                )
            await asyncio.sleep(0.5)

        raise Exception(
            f"Platform UI not ready within 10s for {target_url} "
            "(missing 'Select model')."
        )

    async def _preflight_platform_access(self, context, target_url, label):
        """
        Single-shot platform readiness check before launching model queues.
        """
        page = await context.new_page()
        try:
            page.set_default_timeout(15000)
            await page.goto(target_url)
            await page.wait_for_load_state("networkidle")
            await self._assert_logged_in_for_platform(page, target_url)
            self.log(f"[Preflight-{label}] Platform ready.")
        finally:
            await page.close()

    def record_failure(self, platform, model, ticker, reason=""):
        """Records a failure in both log string format and structured format."""
        # String format for log
        prefix = f"[CME-{model}]" if platform == "cme" else f"[{model}]"
        log_msg = f"{prefix} {ticker}"
        if reason:
             log_msg += f" ({reason})"
        
        # Avoid duplicates if possible (though retry logic might cause them if not careful, but list append is simple)
        self.failed_items.append(log_msg)
        
        # Structured format for retry
        item = {
            "platform": platform,
            "model": model,
            "ticker": ticker
        }
        if reason:
            item["reason"] = reason
        self.failed_tasks_structured.append(item)

    async def run_scraping_job(self, tickers: list, models: list, cme_tickers: list, cme_models: list, download_folder: str, parallel_mode: bool = False):
        """
        Main scrapping logic. Returns structured failed tasks.
        """
        self.stop_requested = False
        if os.path.exists(STOP_FLAG_PATH):
            try:
                os.remove(STOP_FLAG_PATH)
            except Exception:
                pass
        if not os.path.exists(self.storage_state_path):
             self.log("No session file found. Please use 'Log in via Browser' first.")
             return []

        self.success_count = 0
        self.failed_items = []
        self.failed_tasks_structured = [] # List of {'platform': 'std'|'cme', 'model': str, 'ticker': str}

        self.log(f"Starting job. Std: {len(models)} models, CME: {len(cme_models)} models.")
        
        tv_codes_std = []
        tv_codes_cme = []
        
        if not self.browser:
            await self.start_browser(headless=False)

        context = await self.browser.new_context(storage_state=self.storage_state_path, accept_downloads=True)

        need_std = bool(tickers and models)
        need_cme = bool(cme_tickers and cme_models)
        try:
            if need_std:
                await self._preflight_platform_access(context, f"{BASE_URL}/platform", "STD")
            if need_cme:
                await self._preflight_platform_access(context, f"{BASE_URL}/platform/cme", "CME")
        except Exception as e:
            self.log(f"Preflight failed: {e}")
            reason = LOGIN_REQUIRED_REASON if isinstance(e, LoginRequiredError) else "Platform precheck failed"
            if need_std:
                for m in models:
                    for t in tickers:
                        self.record_failure("std", m, t, reason)
            if need_cme:
                for m in cme_models:
                    for t in cme_tickers:
                        self.record_failure("cme", m, t, reason)
            self.log_summary()
            return self.failed_tasks_structured
        
        tasks = []
        
        # 1. Standard Platform Tasks
        if tickers and models:
            for i, model in enumerate(models):
                self._refresh_stop_requested()
                if self.stop_requested:
                    # In sequential mode, this catches future models
                    for skipped_model in models[i:]:
                        for t in tickers:
                            self.record_failure("std", skipped_model, t, "Stopped")
                    break
                    
                # Standard URL, No prefix
                coro = self.process_model_queue(context, model, tickers, download_folder, tv_codes_std, target_url=f"{BASE_URL}/platform", subfolder_prefix="")
                if parallel_mode:
                    tasks.append(coro)
                else:
                    await coro

        # 2. CME Platform Tasks
        if cme_tickers and cme_models:
            CME_URL = f"{BASE_URL}/platform/cme"
            for i, model in enumerate(cme_models):
                self._refresh_stop_requested()
                if self.stop_requested:
                    # In sequential mode, this catches future models
                    for skipped_model in cme_models[i:]:
                        for t in cme_tickers:
                            self.record_failure("cme", skipped_model, t, "Stopped")
                    break
                    
                # CME URL, "CME" prefix
                coro = self.process_model_queue(context, model, cme_tickers, download_folder, tv_codes_cme, target_url=CME_URL, subfolder_prefix="CME")
                if parallel_mode:
                    tasks.append(coro)
                else:
                    await coro

        if parallel_mode and tasks:
            await asyncio.gather(*tasks)
        
        # Save TV codes
        if tv_codes_std:
            self.save_tv_codes(tv_codes_std, download_folder, subfolder="")
        if tv_codes_cme:
            self.save_tv_codes(tv_codes_cme, download_folder, subfolder="CME")
            
        self.log_summary()
        return self.failed_tasks_structured

    async def retry_scraping_job(self, failed_tasks, download_folder, parallel_mode):
        """
        Retries specifically the failed tasks.
        failed_tasks: list of dicts {'platform': 'std'|'cme', 'model': ..., 'ticker': ...}
        """
        self.stop_requested = False
        if os.path.exists(STOP_FLAG_PATH):
            try:
                os.remove(STOP_FLAG_PATH)
            except Exception:
                pass
        if not os.path.exists(self.storage_state_path):
             self.log("No session file found. Please use 'Log in via Browser' first.")
             return failed_tasks 

        self.success_count = 0
        self.failed_items = []
        self.failed_tasks_structured = [] # New failures during retry

        self.log(f"Starting RETRY job. {len(failed_tasks)} items.")
        
        # Group tasks by (platform, model) to utilize batch processing
        grouped = {} # text_key -> {'platform': p, 'model': m, 'tickers': [], 'url': ...}
        
        for item in failed_tasks:
            platform = item['platform']
            model = item['model']
            ticker = item['ticker']
            
            key = (platform, model)
            if key not in grouped:
                if platform == 'cme':
                    url = f"{BASE_URL}/platform/cme"
                    sub = "CME"
                else:
                    url = f"{BASE_URL}/platform"
                    sub = ""
                grouped[key] = {
                    'platform': platform,
                    'model': model,
                    'tickers': [],
                    'url': url,
                    'sub': sub
                }
            grouped[key]['tickers'].append(ticker)

        tv_codes_std = []
        tv_codes_cme = []

        if not self.browser:
            await self.start_browser(headless=False)

        context = await self.browser.new_context(storage_state=self.storage_state_path, accept_downloads=True)

        platforms = {task_info["platform"] for task_info in grouped.values()}
        try:
            if "std" in platforms:
                await self._preflight_platform_access(context, f"{BASE_URL}/platform", "STD")
            if "cme" in platforms:
                await self._preflight_platform_access(context, f"{BASE_URL}/platform/cme", "CME")
        except Exception as e:
            self.log(f"Preflight failed: {e}")
            reason = LOGIN_REQUIRED_REASON if isinstance(e, LoginRequiredError) else "Platform precheck failed"
            for task_info in grouped.values():
                for t in task_info["tickers"]:
                    self.record_failure(task_info["platform"], task_info["model"], t, reason)
            self.log_summary()
            return self.failed_tasks_structured
        
        tasks = []
        
        for k, task_info in grouped.items():
            self._refresh_stop_requested()
            if self.stop_requested:
                # Mark remaining as failed
                for t in task_info['tickers']:
                    self.record_failure(task_info['platform'], task_info['model'], t, "Stopped")
                continue

            # Identify if it maps to tv_codes lists
            # We pass the list to collect results.
            if task_info['platform'] == 'cme':
                codes_list = tv_codes_cme
            else:
                codes_list = tv_codes_std
            
            coro = self.process_model_queue(
                context, 
                task_info['model'], 
                task_info['tickers'], 
                download_folder, 
                codes_list, 
                target_url=task_info['url'], 
                subfolder_prefix=task_info['sub']
            )
            
            if parallel_mode:
                tasks.append(coro)
            else:
                await coro
                
        if parallel_mode and tasks:
            await asyncio.gather(*tasks)

        # Save TV codes
        if tv_codes_std:
            self.save_tv_codes(tv_codes_std, download_folder, subfolder="")
        if tv_codes_cme:
            self.save_tv_codes(tv_codes_cme, download_folder, subfolder="CME")
            
        self.log_summary()
        return self.failed_tasks_structured

    def log_summary(self):
        total = self.success_count + len(self.failed_items)
        self.log("\n" + "="*30)
        self.log(f"JOB SUMMARY")
        self.log(f"Total Processed: {total}")
        self.log(f"Success: {self.success_count}")
        self.log(f"Failed: {len(self.failed_items)}")
        if self.failed_items:
            self.log("Failed Items:")
            for item in self.failed_items:
                self.log(f" - {item}")
        self.log("="*30 + "\n")
            
        # await context.close() # Done in caller wrapper

    async def process_model_queue(self, context, model, tickers, download_folder, tv_codes_list, target_url, subfolder_prefix=""):
        """
        Processes all tickers for a single model in one page.
        """
        page = await context.new_page()
        try:
            page.set_default_timeout(60000) # Set timeout to 60s
            prefix_log = f"[CME-{model}]" if subfolder_prefix else f"[{model}]"
            short_plat = "cme" if subfolder_prefix == "CME" else "std"
            
            self.log(f"{prefix_log} Page initialized.")
            await page.goto(target_url)
            await page.wait_for_load_state("networkidle")
            await self._assert_logged_in_for_platform(page, target_url)
            
            # Select Model
            await page.get_by_text("Select model", exact=False).first.click(timeout=5000)
            await asyncio.sleep(0.5)
            await page.get_by_text(model, exact=True).first.click(timeout=5000)
            self.log(f"{prefix_log} Model selected.")

            for i, ticker in enumerate(tickers):
                self._refresh_stop_requested()
                if self.stop_requested:
                    self.log(f"{prefix_log} Stopped. Skipping remaining tickers.")
                    for skipped_ticker in tickers[i:]:
                         self.record_failure(short_plat, model, skipped_ticker, "Stopped")
                    break
                
                await self.process_single_ticker(page, model, ticker, download_folder, tv_codes_list, subfolder_prefix)
                
        except Exception as e:
            self.log(f"{prefix_log} Error: {e}")
            reason = "Setup error"
            if isinstance(e, LoginRequiredError):
                reason = LOGIN_REQUIRED_REASON
                self.stop_requested = True
            for t in tickers:
                self.record_failure(short_plat, model, t, reason)
        finally:
            await page.close()

    async def process_single_ticker(self, page, model, ticker, download_folder, tv_codes_list, subfolder_prefix):
        max_retries = 15
        short_plat = "cme" if subfolder_prefix == "CME" else "std"
        
        for attempt in range(max_retries):
            self._refresh_stop_requested()
            if self.stop_requested: 
                self.record_failure(short_plat, model, ticker, "Stopped")
                return
            try:
                # 2. Input Ticker
                # Placeholder "Ticker"
                await page.get_by_placeholder("Ticker").fill(ticker)
                
                # 3. Enter
                await page.get_by_role("button", name="Enter").click()
                
                # --- Early Failure Detection (User Request) ---
                # "如果按下 Enter 後等兩秒沒有出現這個畫面，也要直接 retry"
                await asyncio.sleep(2)
                
                # Check 1: Is the specific loading text present?
                loading_text_present = await page.get_by_text("有些模型需要較長的時間計算").count() > 0
                
                # Check 2: Is the data already valid (Fast load)?
                # If data loaded instantly, we shouldn't fail even if loading text is gone.
                fast_check_content = await page.evaluate("() => document.body.innerText")
                data_already_loaded = False
                if f"{ticker} " in fast_check_content or f"{ticker}:" in fast_check_content or \
                   f"{ticker}\n" in fast_check_content or f" {ticker}" in fast_check_content:
                    data_already_loaded = True
                
                if not loading_text_present and not data_already_loaded:
                    raise Exception("Action failed: No loading screen or data update detected after 2s (Click might have been ignored).")
                
                # 4. Wait for processing
                # Detection: "Download" button becomes enabled? Or data appears?
                # User said "wait for data load out".
                # We can wait for a spinner to disappear or "Download" to trigger.
                # Let's wait for the "Download" button to be clickable/enabled.
                
                # Also handle "System Busy" or failure texts here if they exist.
                
                download_btn = page.get_by_role("button", name="下載") # Chinese "Download"
                # Or English "Download" depending on lang. Screenshot shows "下載".
                
                # Wait for response
                # We'll wait up to 30s
                
                # 4. Wait for processing & Validate Data Load
                # Validate that the page has actually loaded the data for the requested TICKER
                # This prevents downloading stale data from the previous search
                
                data_validated = False
                for _ in range(120): # Wait up to 60s
                    self._refresh_stop_requested()
                    if self.stop_requested: return

                    try:
                        # 4.1 Check for Server Errors (Toast)
                        # 4.1 Check for Server Errors (Toast)
                            # Check CN Toast
                        t_cn = page.get_by_text("獲取數據失敗")
                        if await t_cn.count() > 0:
                            msg = await t_cn.first.text_content()
                            raise Exception(f"Server indicated failure: {msg}")

                        # Check EN Toast
                        t_en = page.get_by_text("Please Try Again")
                        if await t_en.count() > 0:
                            msg = await t_en.first.text_content()
                            raise Exception(f"Server indicated failure: {msg}")

                        # 4.2 Check for Ticker Presence in Content
                        # We get the full text to ensure the new Ticker is mentioned in the charts/header
                        content_text = await page.evaluate("() => document.body.innerText")
                        
                        # Heuristic: The Ticker should appear in the body text (Chart Title, etc.)
                        # We look for the ticker string. To avoid matching the input box only, 
                        # we can try to look for "{Ticker} Dealers" or just assume if it appears 
                        # multiple times or in specific context it's good.
                        # Simple check: If content contains Ticker. 
                        # Problem: Input box contains Ticker.
                        # Refined Check: The screenshot shows "SPX Dealers Gamma Hedging".
                        # So we check for Ticker + " " (space) or Ticker + ":" or Ticker + " Dealers"
                        
                        # If we just switched, the OLD ticker might still be there for a split second?
                        # No, usually innerText updates. 
                        # We want to ensure at least ONE instance of the Ticker exists that is NOT the input?
                        # Actually, looking for the specific header pattern from screenshot is best.
                        # But we need to be general.
                        
                        # Let's count occurrences of the Ticker string.
                        # If > 1 (Input + Header), likely loaded.
                        # Or check if "Dealers" is present?
                        
                        # Let's trust that if the text contains "{Ticker} ", it's likely the header or content.
                        if f"{ticker} " in content_text or f"{ticker}:" in content_text or \
                           f"{ticker}\n" in content_text or f" {ticker}" in content_text:
                            data_validated = True
                            break
                            
                    except Exception as e:
                        if "Server indicated" in str(e): raise e
                        # Ignore other parsing errors while waiting
                        pass
                        
                    await asyncio.sleep(0.5)
                
                if not data_validated:
                     # This usually means the Spinner didn't stop, or the page never updated from the previous ticker
                     raise Exception(f"Validation failed: Ticker '{ticker}' not found in loaded content (Stale data?).")
                
                # Additional small buffer for rendering
                await asyncio.sleep(1)
                
                # If model is TV Code, we scrape text
                # If model is TV Code, we scrape text
                if model == "TV Code":
                    # Polling for data update (up to 20s)
                    found_code_line = None
                    for _ in range(120): # 60 seconds total
                        self._refresh_stop_requested()
                        if self.stop_requested: return
                        
                        # Wait for ANY Put Wall to be present logic (fast check)
                        try:
                            # We verify "Put Wall" exists first to avoid reading empty body
                            if await page.get_by_text("Put Wall").count() > 0:
                                content = await page.evaluate("() => document.body.innerText")
                                found_current_ticker = False
                                for line in content.split('\n'):
                                    # Relaxed check: Just ticker and Put Wall in same line.
                                    if ticker in line and "Put Wall" in line:
                                        found_code_line = line
                                        found_current_ticker = True
                                        break
                                
                                if found_code_line:
                                    break
                                
                                # If we found "Put Wall" but NOT the current ticker, this is likely stale data or server busy
                                # Check for specific error message toast if possible, or just fail fast
                                if not found_current_ticker:
                                    # Check CN Toast
                                    t_cn = page.get_by_text("獲取數據失敗")
                                    if await t_cn.count() > 0:
                                        msg = await t_cn.first.text_content()
                                        raise Exception(f"Server indicated failure: {msg}")
                                    
                                    # Check EN Toast
                                    t_en = page.get_by_text("Please Try Again")
                                    if await t_en.count() > 0:
                                        msg = await t_en.first.text_content()
                                        raise Exception(f"Server indicated failure: {msg}")
                                    
                                    # Stale data detection
                                    raise Exception(f"Stale data detected: Found 'Put Wall' but not for {ticker}.")

                            # Check CN Toast
                            t_cn = page.get_by_text("獲取數據失敗")
                            if await t_cn.count() > 0:
                                msg = await t_cn.first.text_content()
                                raise Exception(f"Server indicated failure: {msg}")
                            
                            # Check EN Toast
                            t_en = page.get_by_text("Please Try Again")
                            if await t_en.count() > 0:
                                msg = await t_en.first.text_content()
                                raise Exception(f"Server indicated failure: {msg}")

                        except Exception as e:
                            # specific retry exceptions should propagate
                            if "Server indicated" in str(e) or "Stale data" in str(e):
                                raise e
                            pass
                        
                        if found_code_line:
                            break
                        await asyncio.sleep(0.5)
                    
                    if found_code_line:
                        tv_codes_list.append(found_code_line.strip('" '))
                        self.log(f"[{model}] {ticker} - Code extracted.")
                        self.success_count += 1
                        # Wait a bit to ensure we don't spam too fast
                        await asyncio.sleep(1) 
                    else:
                        raise Exception(f"Validation failed: No data found for ticker {ticker} (Stale data from previous search?)")
                else:
                    # Standard Download
                    # We need to monitor for Error Toast WHILE waiting for download
                    # Create a task for the download event
                    try:
                        async with page.expect_download(timeout=60000) as download_info:
                            await download_btn.click()
                            
                            # Polling for error while waiting for download
                            # Since expect_download is a context manager that waits on __exit__, 
                            # we can't easily run a parallel loop *inside* the with-block efficiently 
                            # because flow blocks at __exit__.
                            # HOWEVER, Playwright's expect_download returns a Download object when yielded? 
                            # No, it yields an EventInfo that you await .value on.
                            
                            # Hack: We can start a background check loop, but we need to stop it when download starts.
                            # Better approach: Use asyncio.wait between download_info.value and error check?
                            
                            # Let's try a custom wait loop instead of simple await download_info.value
                            
                            # Create a task for Getting the download
                            download_task = asyncio.create_task(download_info.value)
                            
                            # Create a polling loop for error visibility
                            # Create a polling loop for error visibility
                            async def check_error():
                                for _ in range(120): # 60s
                                    # Check CN Toast
                                    t_cn = page.get_by_text("獲取數據失敗")
                                    if await t_cn.count() > 0:
                                        msg = await t_cn.first.text_content()
                                        return msg
                                    
                                    # Check EN Toast
                                    t_en = page.get_by_text("Please Try Again")
                                    if await t_en.count() > 0:
                                        msg = await t_en.first.text_content()
                                        return msg
                                        
                                    if download_task.done():
                                        return None
                                    await asyncio.sleep(0.5)
                                return None

                            error_task = asyncio.create_task(check_error())
                            
                            done, pending = await asyncio.wait([download_task, error_task], return_when=asyncio.FIRST_COMPLETED)
                            
                            if error_task in done:
                                error_msg = error_task.result()
                                if error_msg:
                                    # Error detected
                                    raise Exception(f"Server indicated failure (Toast detected): {error_msg}")
                            
                            # If we are here, either download is done OR timeout (handled by expect_download internal timeout usually? No, we need to await download_task)
                            if not download_task.done():
                                # This means error_task finished with False (unlikely if loop matches timeout) or we timed out logic?
                                # Let's await download_task to get the download object
                                # If it timed out, it will raise here.
                                await download_task
                            
                            download = await download_task

                    except Exception as e:
                         # Re-raise to trigger retry
                         raise e
                    
                    # Structure: 
                    # Standard: download_folder/Model/Ticker/Ticker_date.HTML
                    # CME: download_folder/CME/Model/Ticker/Ticker_date.HTML
                    
                    if subfolder_prefix:
                        # e.g. "CME"
                        model_dir = os.path.join(download_folder, subfolder_prefix, utils.clean_filename(model), utils.clean_filename(ticker))
                    else:
                        model_dir = os.path.join(download_folder, utils.clean_filename(model), utils.clean_filename(ticker))
                        
                    os.makedirs(model_dir, exist_ok=True)
                    
                    save_path = os.path.join(model_dir, f"{ticker}_{utils.get_timestamp_filename(prefix='', extension='.html')}")
                    await download.save_as(save_path)
                    self.log(f"[{model}] {ticker} - Downloaded.")
                    self.success_count += 1

                break # Success, break retry loop

            except Exception as e:
                err_msg = str(e)
                if "Target page, context or browser has been closed" in err_msg or "TargetClosedError" in type(e).__name__:
                    self.log(f"[{model}] {ticker} - Browser/context closed, skipping (no retry).")
                    self.record_failure(short_plat, model, ticker, "Browser/context closed")
                    return
                self.log(f"[{model}] {ticker} - Attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    self.log(f"[{model}] {ticker} - Skipped after retries.")
                    self.record_failure(short_plat, model, ticker)
                await asyncio.sleep(2)

    def save_tv_codes(self, codes, download_folder, subfolder=""):
        if not codes:
            return
        
        # Structure: 
        # Standard: download_folder/TV Code/TV_Codes_date.txt
        # CME: download_folder/CME/TV Code/TV_Codes_date.txt
        
        if subfolder:
            tv_dir = os.path.join(download_folder, subfolder, "TV Code")
        else:
            tv_dir = os.path.join(download_folder, "TV Code")
            
        os.makedirs(tv_dir, exist_ok=True)
        
        filename = utils.get_timestamp_filename(prefix="TV_Codes", extension=".txt")
        info_lines = [f"{code}" for code in codes]
        content = "\n".join(info_lines)
        
        path = os.path.join(tv_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.log(f"Saved aggregated TV codes to {path}")
