import json
import os
import time
import random
import logging
import hashlib
import re
from datetime import datetime, timedelta
from selenium import webdriver

# Cross-bot statistics tracking (SQLite)
try:
    import sys as _csys
    _csys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from stats_tracker import get_tracker
    _cr_tracker = get_tracker("CommentsReply")
    del _csys
except Exception:
    class _Null:
        def log_event(self, *a, **kw): pass
        def log_login_failure(self, *a, **kw): pass
        def log_login_success(self, *a, **kw): pass
    _cr_tracker = _Null()
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from urllib.parse import urlparse, parse_qs, unquote
import csv

# Optional: requests for CSV fetching
try:
    import requests
except Exception:
    requests = None

# Optional: Google Sheets support
try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

class FacebookCommentBot:
    def __init__(self):
        self.config = self.load_config()
        self.setup_logging()
        self.driver = None
        self.processed_posts = self.load_processed_data('processed_posts_file')
        self.processed_comments = self.load_processed_data('processed_comments_file')
        self.reply_messages = self.load_reply_messages()
        self.groups = self.load_groups()
        self.posts_list = self.load_posts()
        # Auto-reload timestamps for Sheets CSV hot-reload
        now = time.time()
        self.last_groups_reload = now
        self.last_posts_reload = now
        self.last_accounts_reload = now
        self.last_logout_check = now
        self.last_telegram_stats = now
        self.telegram_notices = {}  # Legacy per-account notices (kept for compatibility)
        # Aggregated failed-login alert state
        self.failed_login_accounts = set()
        self.failed_login_message_id = None
        self.telegram_stats_message_id = None
        self.telegram_live_replies_message_id = None  # Live reply status message
        self.recent_replies = []  # Store last 10 replies for live status
        self.current_group_url = None  # Current group being processed
        self.current_group_name = None  # Current group name
        # Per-session counters (start at 0 each run) — these drive the dashboard
        # so a reset truly zeroes counts even though processed_comments persists
        # locally for de-duplication.
        self.session_replies = 0
        self.session_failures = 0

    def _jitter_sleep(self, base_seconds, jitter_key="reply_jitter"):
        """Sleep for base_seconds plus optional jitter from config. jitter_key in anti_block."""
        try:
            ab = self.config.get('anti_block', {})
            jitter = ab.get(jitter_key, [0, 0])
            if isinstance(jitter, (list, tuple)) and len(jitter) == 2:
                extra = random.uniform(float(jitter[0]), float(jitter[1]))
            else:
                extra = 0
            time.sleep(max(0, float(base_seconds)) + max(0.0, extra))
        except Exception:
            time.sleep(base_seconds)
        
    def load_config(self):
        """Load configuration from config.json"""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.error("config.json not found!")
            return {}

    def _save_telemetry(self, status=None, failed_logins=None, stats=None, recent_events=None, account=None):
        try:
            import time
            import json
            import os
            telemetry_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "telemetry"))
            os.makedirs(telemetry_dir, exist_ok=True)
            
            if account is None:
                account = getattr(self, 'account_number', None)
            if account is None and isinstance(failed_logins, dict) and failed_logins:
                account = list(failed_logins.keys())[0]
            if account is None:
                account = 'unknown'

            # Log to shared database (login state only). Reply/message counts
            # are logged separately at the real reply moment (see reply_to_comment)
            # so periodic status pings don't inflate the dashboard counters.
            try:
                acc_name = str(account).replace("_cookies.json", "").replace("_cookies", "").replace(".json", "").strip()
                if failed_logins is not None and isinstance(failed_logins, dict):
                    if failed_logins:
                        _cr_tracker.log_login_failure(acc_name, reason=list(failed_logins.values())[0])
                    else:
                        _cr_tracker.log_login_success(acc_name)
            except Exception:
                pass

            filename = f"CommentsReplyBot_{account}.json"
            filepath = os.path.join(telemetry_dir, filename)
            
            data = {}
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    pass
            
            data["bot_name"] = "CommentsReplyBot"
            data["account"] = str(account)
            data["last_update"] = time.time()
            
            if status is not None:
                data["status"] = status
            if failed_logins is not None:
                existing_failed = data.get("failed_logins", {})
                if isinstance(failed_logins, dict):
                    if not failed_logins:
                        data["failed_logins"] = {}
                    else:
                        existing_failed.update(failed_logins)
                        data["failed_logins"] = existing_failed
                else:
                    data["failed_logins"] = failed_logins
            if stats is not None:
                data["stats"] = stats
            if recent_events is not None:
                existing_events = data.get("recent_events", [])
                for event in recent_events:
                    if event not in existing_events:
                        existing_events.append(event)
                data["recent_events"] = existing_events[-10:]
                
            temp_path = filepath + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, filepath)
        except Exception as e:
            self.logger.debug(f"Telemetry save failed: {e}")

    def send_telegram_message(self, message, message_id=None):
        return 999999

    def delete_telegram_message(self, message_id: int) -> bool:
        return True

    def _build_failed_login_alert_message(self) -> str:
        return ""

    def _update_aggregate_failed_login_alert(self):
        pass

    def send_or_update_failed_login_notice(self, account_fname, message):
        account = account_fname.replace("_cookies.json", "")
        self._save_telemetry(
            status="Logged Out",
            failed_logins={account: message}
        )

    def clear_failed_login_notice(self, account_fname):
        account = account_fname.replace("_cookies.json", "")
        self._save_telemetry(
            status="Running",
            failed_logins={}
        )

    def load_cookies_from_sheets(self, account_number):
        """Load cookies from Google Sheets CSV"""
        try:
            accounts_config = self.config.get('accounts_sheet', {})
            if not accounts_config.get('enabled') or not requests:
                return None
                
            url = accounts_config.get('url')
            if not url:
                return None
            # Cache-bust to always get latest content
            try:
                ts = int(time.time())
                url_with_ts = url + (('&' if '?' in url else '?') + f"_ts={ts}")
            except Exception:
                url_with_ts = url

            # Force network fetch (avoid CDN cache)
            headers = {
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
            }
            response = requests.get(url_with_ts, timeout=15, headers=headers)
            response.raise_for_status()
            
            reader = csv.reader(response.text.splitlines())
            account_file_col = int(accounts_config.get('account_file_col', 0))
            cookies_json_col = int(accounts_config.get('cookies_json_col', 1))
            
            target_filename = f"{account_number}_cookies.json"
            
            for row_idx, row in enumerate(reader):
                if row_idx == 0:  # Skip header
                    continue
                    
                if len(row) > max(account_file_col, cookies_json_col):
                    account_file = row[account_file_col].strip()
                    cookies_json = row[cookies_json_col].strip()
                    
                    if account_file == target_filename and cookies_json:
                        try:
                            cookies = json.loads(cookies_json)
                            # Quick validation: ensure critical cookies exist
                            names = {c.get('name') for c in cookies if isinstance(c, dict)}
                            if 'c_user' not in names or 'xs' not in names:
                                self.logger.warning(f"Sheets cookies for {account_file} missing critical keys (have: {sorted(list(names))[:6]}...)")
                            self.logger.info(f"Loaded cookies for {account_file} from Google Sheets")
                            return cookies
                        except json.JSONDecodeError as e:
                            self.logger.error(f"Invalid JSON in sheets for {account_file}: {e}")
                            continue
                            
            self.logger.warning(f"Account {target_filename} not found in Google Sheets")
            return None
            
        except Exception as e:
            self.logger.error(f"Failed to load cookies from Google Sheets: {e}")
            return None

    def refresh_account_cookies(self, account_fname):
        """Refresh cookies from Google Sheets"""
        try:
            account_number = account_fname.replace('_cookies.json', '')
            cookies = self.load_cookies_from_sheets(account_number)
            
            if cookies:
                # Save to local file as backup
                os.makedirs('accounts', exist_ok=True)
                with open(f'accounts/{account_fname}', 'w', encoding='utf-8') as f:
                    json.dump(cookies, f, indent=2)
                self.logger.info(f"Refreshed cookies for {account_fname}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"Failed to refresh cookies for {account_fname}: {e}")
            return False

    def is_logged_out(self):
        """Check if current session is logged out"""
        try:
            current_url = self.driver.current_url.lower()
            
            # Check for login page indicators
            logout_indicators = [
                'login.php',
                'checkpoint',
                'security',
                'verify',
                'confirm',
                '/login'
            ]
            
            for indicator in logout_indicators:
                if indicator in current_url:
                    return True
                    
            # Check for login form elements
            try:
                login_elements = self.driver.find_elements(By.CSS_SELECTOR, 
                    'input[name="email"], input[name="pass"], input[type="password"]')
                if login_elements:
                    return True
            except Exception:
                pass
                
            # Check for "Log In" text/buttons
            try:
                login_buttons = self.driver.find_elements(By.XPATH, 
                    "//*[contains(text(), 'Log In') or contains(text(), 'Sign In')]")
                if login_buttons:
                    return True
            except Exception:
                pass
                
            return False
        except Exception:
            return True  # Assume logged out on error

    def reload_cookies_into_driver(self, cookies):
        """Reload cookies into current driver session with normalization and proper domain context"""
        try:
            # Navigate to base domain so cookie domains match
            try:
                self.driver.get("https://www.facebook.com/")
                time.sleep(1.5)
            except Exception:
                pass

            # Clear existing cookies and storage
            try:
                self.driver.delete_all_cookies()
            except Exception:
                pass
            try:
                self.driver.execute_script("localStorage.clear(); sessionStorage.clear();")
            except Exception:
                pass

            def normalize_cookie(raw):
                c = {k: v for k, v in raw.items() if v is not None}
                # Selenium expects 'expiry' int, not 'expirationDate'
                if 'expirationDate' in c and 'expiry' not in c:
                    try:
                        c['expiry'] = int(float(c.pop('expirationDate')))
                    except Exception:
                        c.pop('expirationDate', None)
                # Normalize sameSite values
                ss = c.get('sameSite')
                if isinstance(ss, str):
                    m = ss.lower().replace('_', '-').strip()
                    if 'no' in m:
                        c['sameSite'] = 'None'
                    elif 'lax' in m:
                        c['sameSite'] = 'Lax'
                    elif 'strict' in m:
                        c['sameSite'] = 'Strict'
                    else:
                        c.pop('sameSite', None)
                # Domain: strip leading dot for add_cookie; keep path default
                if 'domain' in c and isinstance(c['domain'], str):
                    c['domain'] = c['domain'].lstrip('.')
                if 'path' not in c:
                    c['path'] = '/'
                # Only keep supported keys
                allowed = {'name','value','path','domain','secure','httpOnly','expiry','sameSite'}
                return {k: v for k, v in c.items() if k in allowed}

            # Group cookies by domain for context-aware adding
            by_domain = {}
            for rc in cookies or []:
                try:
                    d = (rc.get('domain') or '').lstrip('.') if isinstance(rc.get('domain'), str) else 'facebook.com'
                    by_domain.setdefault(d, []).append(rc)
                except Exception:
                    continue

            domains_to_try = list(by_domain.keys()) or ['facebook.com']
            # Ensure primary domains first
            domains_to_try.sort(key=lambda d: 0 if d.endswith('facebook.com') else 1)

            total_added = 0
            for dom in domains_to_try:
                try:
                    # Navigate to a matching domain before adding
                    url = f"https://{dom}/"
                    self.driver.get(url)
                    time.sleep(1.0)
                except Exception:
                    pass
                for rc in by_domain.get(dom, []):
                    c = normalize_cookie(rc)
                    # Selenium requires name and value
                    if not c.get('name') or c.get('value') is None:
                        continue
                    try:
                        self.driver.add_cookie(c)
                        total_added += 1
                    except Exception as e:
                        self.logger.debug(f"Failed to add cookie for {dom}: {e}")

            # Finally go to main site and refresh
            try:
                self.driver.get("https://www.facebook.com/")
                time.sleep(1.0)
                self.driver.refresh()
            except Exception:
                pass
            time.sleep(2.0)
            self.logger.info(f"Successfully added {total_added}/{len(cookies) if cookies else 0} cookies")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reload cookies: {e}")
            return False

    def handle_logout_and_refresh(self, account_fname):
        """Handle logout detection and attempt cookie refresh - keeps retrying until fixed"""
        retry_count = 0
        max_retries = 999999  # Essentially infinite retries
        retry_delay = 30  # 30 seconds between retries
        
        while retry_count < max_retries:
            try:
                retry_count += 1
                self.logger.warning(f"Logout detected for {account_fname} (attempt {retry_count})")
                
                # Send persistent notice with retry count
                self.send_or_update_failed_login_notice(account_fname, 
                    f"🔴 Account {account_fname} logged out — attempting cookie refresh… (attempt {retry_count})")
                
                # Try to refresh cookies from Google Sheets first
                self.refresh_account_cookies(account_fname)
                    
                # Try to reload cookies - prioritize Google Sheets, fallback to local
                account_number = account_fname.replace('_cookies.json', '')
                
                # Try Google Sheets first
                cookies = self.load_cookies_from_sheets(account_number)
                if cookies:
                    self.logger.info("Loaded cookies from Google Sheets")
                else:
                    # Fallback to local cookies
                    try:
                        with open(f'accounts/{account_fname}', 'r', encoding='utf-8') as f:
                            cookies = json.load(f)
                        self.logger.info("Loaded cookies from local file (fallback)")
                    except Exception as e:
                        self.logger.debug(f"Failed to load local cookies: {e}")
                
                if cookies and self.reload_cookies_into_driver(cookies):
                    time.sleep(self.config.get('bot_settings', {}).get('delay_retry_login', 4))
                    
                    # Navigate to Facebook to test login
                    try:
                        self.driver.get("https://www.facebook.com")
                        time.sleep(3)
                    except Exception:
                        pass
                    
                    if not self.is_logged_out():
                        self.logger.info(f"Auto re-login successful after cookie refresh (attempt {retry_count})")
                        self.clear_failed_login_notice(account_fname)
                        return True
                    else:
                        self.logger.error("Re-login after cookie refresh did not succeed")
                        self.send_or_update_failed_login_notice(account_fname,
                            f"🔴 Still logged out for {account_fname} after cookie refresh (attempt {retry_count})")
                else:
                    self.logger.error("Failed to reload cookies after refresh")
                    self.send_or_update_failed_login_notice(account_fname,
                        f"🔴 Failed to reload cookies for {account_fname} (attempt {retry_count})")
                
                # Wait before next retry
                self.logger.info(f"Waiting {retry_delay} seconds before retry...")
                time.sleep(retry_delay)
                
            except Exception as e:
                self.logger.error(f"Error during logout handling (attempt {retry_count}): {e}")
                self.send_or_update_failed_login_notice(account_fname,
                    f"🔴 Error handling logout for {account_fname}: {str(e)[:100]} (attempt {retry_count})")
                time.sleep(retry_delay)
                continue
                
        # This should never be reached due to infinite retries
        return False
    
    def setup_logging(self):
        """Setup logging configuration"""
        os.makedirs('logs', exist_ok=True)
        
        # Configure logging
        handlers = [
            logging.FileHandler(self.config.get('logging', {}).get('log_file', 'logs/bot.log'), encoding='utf-8')
        ]
        if self.config.get('logging', {}).get('log_to_console', False):
            handlers.append(logging.StreamHandler())
        logging.basicConfig(
            level=getattr(logging, self.config.get('logging', {}).get('log_level', 'INFO')),
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=handlers,
        )
        self.logger = logging.getLogger(__name__)

    # ----- Scrolling helpers -----
    def slow_scroll_to(self, element, steps=8, pause=0.25):
        """Smoothly scroll so that element is fully visible in viewport."""
        try:
            rect = self.driver.execute_script(
                "const r=arguments[0].getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height};",
                element,
            ) or {"top": 0, "bottom": 0, "height": 0}
            # Ensure top comes slightly above center
            viewport_h = self.driver.execute_script("return window.innerHeight || document.documentElement.clientHeight;") or 900
            target_y = self.driver.execute_script("return window.pageYOffset;") + rect.get("top", 0) - int(viewport_h*0.2)
            current = self.driver.execute_script("return window.pageYOffset;") or 0
            distance = target_y - current
            if steps < 1:
                steps = 1
            step = distance / steps
            for _ in range(steps):
                current += step
                self.driver.execute_script("window.scrollTo(0, arguments[0]);", int(current))
                time.sleep(pause)
            # Final align and small pause
            self.driver.execute_script("arguments[0].scrollIntoView({block:'start'});", element)
            time.sleep(0.4)
        except Exception as e:
            self.logger.debug(f"slow_scroll_to failed: {e}")

    def ensure_post_fully_visible(self, element):
        """Scroll until the post's bottom is visible (slow)."""
        try:
            for _ in range(4):
                rect = self.driver.execute_script(
                    "const r=arguments[0].getBoundingClientRect();return {bottom:r.bottom, top:r.top};",
                    element,
                )
                viewport_h = self.driver.execute_script("return window.innerHeight || document.documentElement.clientHeight;")
                if rect and rect.get("bottom", 0) <= viewport_h - 20 and rect.get("top", 0) >= 0:
                    break
                self.driver.execute_script("arguments[0].scrollIntoView({block:'end'});", element)
                time.sleep(0.6)
        except Exception as e:
            self.logger.debug(f"ensure_post_fully_visible failed: {e}")

    def wait_for_feed_ready(self, timeout: int = 12):
        """Wait until at least one feed post has loaded beyond the skeleton state.
        This mitigates cases where DOM debug shows only loading placeholders and no anchors.
        """
        try:
            # Wait for any article cards to be present
            WebDriverWait(self.driver, min(max(timeout, 2), 30)).until(
                EC.presence_of_element_located((By.XPATH, "//*[@role='article']"))
            )
            # Give a short grace to allow inner content to populate
            time.sleep(1.0)
            return True
        except Exception as e:
            self.logger.debug(f"wait_for_feed_ready timed out: {e}")
            return False

    # ----- URL collection helpers -----
    def collect_post_permalinks_on_page(self):
        """Collect clean post permalinks currently present in DOM.
        Returns a set of cleaned hrefs like /groups/.../posts/... with query removed."""
        urls = set()
        try:
            # Ensure feed has rendered beyond skeletons
            self.wait_for_feed_ready(timeout=8)
            # Determine current group id (if on group page)
            try:
                cur = self.driver.current_url
                m_gid = re.search(r"/groups/([^/?#]+)", cur)
                current_group_id = m_gid.group(1) if m_gid else None
            except Exception:
                current_group_id = None

            def is_valid_group_post_url(href: str) -> bool:
                if not href:
                    return False
                # Reject obvious non-post types
                lower = href.lower()
                if any(x in lower for x in [
                    '/events/', '/members/', '/chats/', '/marketplace/', '/watch/', '/gaming/', '/notifications/'
                ]):
                    return False
                if any(x in lower for x in ['comment_id=', 'reply_comment_id=', '/comment/', '/comments/', '/replies/']):
                    return False
                # Explicitly reject photo links with set=g.<groupId> (right-rail recent media)
                if ('/photo/' in lower or '/photos/' in lower) and 'set=g.' in lower:
                    return False
                # Accept any link that carries set=pcb.<postId> (parent post indicator), regardless of path
                if 'set=pcb.' in lower:
                    return True
                # Accept canonical group post
                if '/groups/' in href and '/posts/' in href:
                    return True
                # Accept /groups/{id}/permalink/{postId}
                if '/groups/' in href and '/permalink/' in href:
                    return True
                # Accept /groups/{id}/?post_id=...
                if '/groups/' in href and 'post_id=' in href:
                    return True
                # Accept /groups/{id}/?multi_permalinks=...
                if '/groups/' in href and 'multi_permalinks=' in href:
                    return True
                # Accept pfbid-style posts
                if '/groups/' in href and '/posts/pfbid' in href:
                    return True
                # Accept photo permalinks that are part of a post album/thread: set=gm.{threadId}
                if '/photo/' in href or '/photo/?' in href or '/photos/' in href:
                    if 'set=gm.' in href:  # album/thread media set
                        return True
                    # Do NOT accept set=g.<groupId> anymore
                    # if current_group_id and (f"idorvanity={current_group_id}" in href or f"/groups/{current_group_id}" in href):
                    #     return True
                # Accept story.php?story_fbid=... (gid may be resolved in canonicalize via current_group_id)
                if 'story.php' in href and 'story_fbid=' in href:
                    return True
                return False

            # Primary: anchors with clean group post permalinks
            xpath_variants = [
                "//a[contains(@href,'/groups/') and contains(@href,'/posts/')]",
                "//*[@role='link' and contains(@href,'/groups/') and contains(@href,'/posts/')]",
                # Timestamp anchors often wrap a <time> element
                "//a[descendant::time]",
                "//*[@role='link' and descendant::time]",
                # Also pick up photo links that indicate parent post via set=pcb.<pid>
                "//a[contains(@href,'set=pcb.')]",
                "//*[@role='link' and contains(@href,'set=pcb.')]",
                # story.php permalink style
                "//a[contains(@href,'story.php') and contains(@href,'story_fbid=')]",
                "//*[@role='link' and contains(@href,'story.php') and contains(@href,'story_fbid=')]",
            ]

            elements = []
            for xp in xpath_variants:
                try:
                    found = self.driver.find_elements(By.XPATH, xp)
                    if found:
                        self.logger.debug(f"URL collect: selector {xp} -> {len(found)} elements")
                        elements.extend(found)
                except Exception:
                    continue

            # Right-rail filtering disabled per user request; rely on URL-level rejection (set=g.*)

            def pick_href(el):
                try:
                    # Prefer direct href
                    href = (el.get_attribute('href') or '').strip()
                    # Fallbacks that FB sometimes uses
                    if not href:
                        href = (el.get_attribute('data-lynx-uri') or '').strip()
                    if not href:
                        href = (el.get_attribute('ajaxify') or '').strip()
                    return href
                except Exception:
                    return ''

            def normalize(href):
                try:
                    if not href:
                        return ''
                    # Decode Facebook redirector links
                    if 'l.php' in href and 'u=' in href:
                        try:
                            q = urlparse(href)
                            u = parse_qs(q.query).get('u', [''])[0]
                            if u:
                                href = unquote(u)
                        except Exception:
                            pass
                    # Some links are relative like /groups/.../posts/...
                    if href.startswith('/'):
                        href = "https://web.facebook.com" + href
                    # Preserve query for post_id parsing; drop only fragments for stability
                    href = href.split('#')[0]
                    return href
                except Exception:
                    return href

            def canonicalize(href: str, current_group_id: str | None) -> str | None:
                """Return a canonical group post URL if href looks like a group post in any accepted format.
                Examples normalized to: https://web.facebook.com/groups/{gid}/posts/{pid}
                If we cannot derive pid, return a cleaned accepted href (e.g., photo URL) or None.
                """
                try:
                    if not href:
                        return None
                    u = urlparse(href)
                    qs = parse_qs(u.query or '')
                    base_no_q = f"{u.scheme}://{u.netloc}{u.path}"
                    # 1) Already canonical /groups/{gid}/posts/{pid}
                    m = re.search(r"/groups/([^/]+)/posts/([A-Za-z0-9]+)", u.path)
                    if m:
                        return base_no_q
                    # 2) /groups/{gid}/permalink/{pid} -> convert to /posts/{pid}
                    m = re.search(r"/groups/([^/]+)/permalink/([A-Za-z0-9]+)", u.path)
                    if m:
                        gid, pid = m.group(1), m.group(2)
                        return f"https://{u.netloc}/groups/{gid}/posts/{pid}"
                    # 3) /groups/{gid}/?...post_id=PID -> convert
                    if '/groups/' in u.path:
                        pid = (qs.get('post_id') or [''])[0]
                        if pid:
                            gid_match = re.search(r"/groups/([^/]+)/", u.path)
                            gid = gid_match.group(1) if gid_match else current_group_id
                            if gid and pid:
                                return f"https://{u.netloc}/groups/{gid}/posts/{pid}"
                        # 3a) /groups/{gid}/?...multi_permalinks=PID -> convert
                        mplist = (qs.get('multi_permalinks') or [''])[0]
                        if mplist:
                            gid_match = re.search(r"/groups/([^/]+)/", u.path)
                            gid = gid_match.group(1) if gid_match else current_group_id
                            if gid:
                                return f"https://{u.netloc}/groups/{gid}/posts/{mplist}"
                    # 3b) photo set=pcb.PID indicates the parent post id (works even without '/groups/' in path)
                    set_param = (qs.get('set') or [''])[0]
                    if set_param and set_param.startswith('pcb.'):
                        pid = set_param.split('pcb.', 1)[1]
                        # Prefer group id from path if present, else fallback to current_group_id
                        gid_match = re.search(r"/groups/([^/]+)/", u.path)
                        gid = gid_match.group(1) if gid_match else current_group_id
                        if gid and pid:
                            return f"https://{u.netloc}/groups/{gid}/posts/{pid}"
                    # 4) story.php?story_fbid=PID&id=GID -> convert to /permalink/{PID}
                    if 'story.php' in u.path:
                        qs = parse_qs(u.query or '')
                        pid = (qs.get('story_fbid') or [''])[0]
                        gid = (qs.get('id') or [''])[0] or (qs.get('idorvanity') or [''])[0] or (current_group_id or '')
                        if pid and gid:
                            return f"https://{u.netloc}/groups/{gid}/posts/{pid}"
                    # 5) Photo URLs: keep as-is if tied to group (set=g.{gid} or set=gm.)
                    if is_valid_group_post_url(href):
                        # Reject set=g.<gid> photos at canonicalization time too
                        if ('/photo/' in u.path or '/photos/' in u.path) and 'set=g.' in (u.query or ''):
                            return None
                        # Prefer removing query noise for storage, but keep essential params on photos
                        if '/photo/' in u.path or '/photos/' in u.path:
                            # Strip tracking params but keep fbid and set (when set=gm.|pcb.)
                            qs = parse_qs(u.query or '')
                            keep = {}
                            for k in ['fbid', 'set']:
                                if k in qs and qs[k]:
                                    # only keep set when it's not set=g.
                                    if k == 'set' and isinstance(qs[k], list) and qs[k] and str(qs[k][0]).startswith('g.'):
                                        continue
                                    keep[k] = qs[k][0]
                            if keep:
                                new_q = '&'.join(f"{k}={keep[k]}" for k in keep)
                                return f"https://{u.netloc}{u.path}?{new_q}"
                            return base_no_q
                        return base_no_q
                except Exception:
                    pass
                return None

            count_seen = 0
            for el in elements:
                try:
                    raw = pick_href(el)
                    href = normalize(raw)
                    if not href:
                        continue
                    count_seen += 1
                    if not is_valid_group_post_url(href):
                        continue
                    canon = canonicalize(href, current_group_id)
                    if canon:
                        urls.add(canon)
                except Exception:
                    continue

            # Post-scoped fallback: iterate visible post containers and resolve timestamp from each
            try:
                posts = []
                try:
                    posts.extend(self.driver.find_elements(By.XPATH, "//*[@role='article']"))
                except Exception:
                    pass
                try:
                    posts.extend(self.driver.find_elements(By.XPATH, "//div[starts-with(@data-pagelet,'FeedUnit') or contains(@data-pagelet,'FeedUnit')]") )
                except Exception:
                    pass
                # Right-rail post container filtering disabled; rely on URL-level rejection (set=g.*)
                # If nothing collected yet, dump DOM of the first post for diagnostics
                try:
                    if not urls and posts:
                        self.dump_post_dom_debug(posts[0], note="initial-collect")
                except Exception:
                    pass
                resolved = 0
                constructed = 0
                # Parse group id from current URL, if available
                try:
                    cur = self.driver.current_url
                    m_gid = re.search(r"/groups/([^/?#]+)", cur)
                    group_id = m_gid.group(1) if m_gid else None
                except Exception:
                    group_id = None
                for p in posts:
                    try:
                        ts_el, ts_href = self.find_best_timestamp_link(p)
                        if not ts_href:
                            ts_el, ts_href = self.find_best_timestamp_link_global(p)
                        if ts_href:
                            h = normalize(ts_href)
                            if h and is_valid_group_post_url(h):
                                canon = canonicalize(h, current_group_id)
                                if canon and canon not in urls:
                                    urls.add(canon)
                                    resolved += 1
                            continue
                        # 2nd fallback: look for photo anchors with set=pcb.{postId} within this post
                        try:
                            # Look for any anchors that carry set=pcb.<pid>, not only those with 'photo' in path
                            photo_links = p.find_elements(By.XPATH, ".//a[contains(@href,'set=pcb.')]")
                        except Exception:
                            photo_links = []
                        got_from_pcb = False
                        for a in photo_links[:3]:  # sample a few
                            try:
                                raw = a.get_attribute('href') or a.get_attribute('data-lynx-uri') or a.get_attribute('ajaxify') or ''
                                if not raw:
                                    continue
                                h = normalize(raw)
                                canon = canonicalize(h, current_group_id)
                                if canon and canon not in urls:
                                    urls.add(canon)
                                    resolved += 1
                                    got_from_pcb = True
                                    break
                            except Exception:
                                continue
                        if got_from_pcb:
                            continue
                        # If we couldn't get a timestamp href, try constructing from post_id in attributes
                        post_id = None
                        try:
                            cand_attrs = [
                                p.get_attribute('data-ft') or '',
                                p.get_attribute('data-store') or '',
                                p.get_attribute('data-gt') or '',
                                p.get_attribute('data-serialized') or '',
                            ]
                            txt = ' '.join(cand_attrs)
                            # Common keys: top_level_post_id, story_fbid, feedback_target_id, post_id
                            m = re.search(r"(top_level_post_id|story_fbid|feedback_target_id|post_id)\"?[:=]\"?(\d{8,})", txt)
                            if m:
                                post_id = m.group(2)
                            if not post_id:
                                # As a last resort, pick a long numeric token
                                m2 = re.search(r"\b(\d{12,})\b", txt)
                                if m2:
                                    post_id = m2.group(1)
                        except Exception:
                            post_id = None
                        if post_id and group_id:
                            built = f"https://web.facebook.com/groups/{group_id}/posts/{post_id}"
                            if built not in urls:
                                urls.add(built)
                                constructed += 1
                    except Exception:
                        continue
                self.logger.debug(f"URL collect: per-post resolution added {resolved} permalinks; constructed {constructed} from {len(posts)} post containers")
            except Exception:
                pass

            self.logger.debug(f"URL collect: unique permalinks found this pass: {len(urls)} (from {count_seen} raw)")
            try:
                if urls:
                    sample = list(sorted(urls))[:10]
                    for i, u in enumerate(sample, 1):
                        self.logger.debug(f"URL collect sample [{i}]: {u}")
            except Exception:
                pass
        except Exception as e:
            self.logger.debug(f"collect_post_permalinks_on_page failed: {e}")
        return urls

    def extract_post_id_from_url(self, url):
        """Try to extract a stable post id or pfbid from a group post URL."""
        try:
            m = re.search(r"/posts/([^/?#]+)", url)
            if m:
                return m.group(1)
        except Exception:
            pass
        return hashlib.md5(url.encode('utf-8')).hexdigest()[:16]

    def canonicalize_post_url(self, href: str, current_group_id: str | None = None) -> str | None:
        """Canonicalize to https://web.facebook.com/groups/{gid}/posts/{pid} when possible.
        Reject photo links like .../photo/?fbid=...&set=g.<gid> by returning None.
        Supports conversions from permalink, post_id query, pcb.<pid>, and story.php formats.
        """
        try:
            if not href:
                return None
            # Make sure relative paths are absolute to web.facebook.com
            if href.startswith('/'):
                href = "https://web.facebook.com" + href
            u = urlparse(href)
            qs = parse_qs(u.query or '')
            netloc = u.netloc or 'web.facebook.com'
            base_no_q = f"https://{netloc}{u.path}"
            # Already canonical
            if re.search(r"/groups/([^/]+)/posts/([A-Za-z0-9]+)", u.path):
                return base_no_q
            # permalink -> posts
            m = re.search(r"/groups/([^/]+)/permalink/([A-Za-z0-9]+)", u.path)
            if m:
                gid, pid = m.group(1), m.group(2)
                return f"https://{netloc}/groups/{gid}/posts/{pid}"
            # /groups/{gid}/?...post_id=PID
            if '/groups/' in u.path and 'post_id' in qs:
                pid = (qs.get('post_id') or [''])[0]
                gid_match = re.search(r"/groups/([^/]+)/", u.path)
                gid = gid_match.group(1) if gid_match else current_group_id
                if gid and pid:
                    return f"https://{netloc}/groups/{gid}/posts/{pid}"
            # pcb.POSTID on photo set indicates parent post
            set_param = (qs.get('set') or [''])[0]
            if set_param and set_param.startswith('pcb.'):
                pid = set_param.split('pcb.', 1)[1]
                gid_match = re.search(r"/groups/([^/]+)/", u.path)
                gid = gid_match.group(1) if gid_match else current_group_id
                if gid and pid:
                    return f"https://{netloc}/groups/{gid}/posts/{pid}"
            # story.php?story_fbid=PID&id=GID
            if 'story.php' in u.path:
                qs = parse_qs(u.query or '')
                pid = (qs.get('story_fbid') or [''])[0]
                gid = (qs.get('id') or [''])[0] or (qs.get('idorvanity') or [''])[0] or (current_group_id or '')
                if pid and gid:
                    return f"https://{netloc}/groups/{gid}/posts/{pid}"
            # Hard reject: group photo gallery links set=g.<gid>
            if ('/photo/' in u.path or '/photos/' in u.path) and 'set=g.' in (u.query or ''):
                return None
            return None
        except Exception:
            return None

    def process_comments_on_current_post_page(self, post_id):
        """Process comments assuming we are already on a post page in the current tab."""
        try:
            # Aggressively load comments: click "View more" buttons and scroll until stagnation
            load_conf = self.config.get('bot_settings', {})
            max_load_rounds = load_conf.get('max_comment_load_rounds', 8)
            stagnation_threshold = load_conf.get('stagnation_threshold', 3)
            scroll_pause = load_conf.get('scroll_pause_seconds', 1.5)

            def _click_view_more_buttons():
                labels = [
                    'View more comments',
                    'View more replies',
                    'See more',
                    'View previous comments',
                    'Most relevant',  # sometimes opens a menu; we avoid clicking if it has a menuindicator
                ]
                clicked = 0
                try:
                    # Prefer explicit buttons/links within the comment area
                    btns = self.driver.find_elements(
                        By.XPATH,
                        "//div[@role='main']//*/self::div|self::span|self::a|self::button"
                        "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view more comments')"
                        " or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view more replies')"
                        " or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'see more')"
                        " or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view previous comments')]"
                    )
                except Exception:
                    btns = []
                for b in btns[:12]:  # cap per pass
                    try:
                        text = (b.text or '').strip().lower()
                        if not any(lbl in text for lbl in ['view more comments','view more replies','see more','view previous comments']):
                            continue
                        if not b.is_displayed():
                            continue
                        try:
                            b.click()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", b)
                        clicked += 1
                        time.sleep(0.3)
                    except Exception:
                        continue
                return clicked

            def _count_comments_now():
                try:
                    return len(self.driver.find_elements(By.CSS_SELECTOR, 'div[data-testid="comment"], div[aria-label*="Comment by"], [data-testid="UFI2Comment/root_depth_0"], li[role="article"]'))
                except Exception:
                    return 0

            last_height = None
            stagnation = 0
            prev_count = -1
            for r in range(max_load_rounds):
                # Click any visible expanders
                clicked = _click_view_more_buttons()
                # Scroll down a page
                try:
                    self.driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.9));")
                except Exception:
                    pass
                time.sleep(scroll_pause)
                # Scroll up a bit to trigger lazy-load in both directions
                try:
                    self.driver.execute_script("window.scrollBy(0, -Math.floor(window.innerHeight*0.3));")
                except Exception:
                    pass
                time.sleep(0.4)

                # Check growth by height and by comment count
                try:
                    height = self.driver.execute_script("return document.body.scrollHeight || document.documentElement.scrollHeight;")
                except Exception:
                    height = None
                cur_count = _count_comments_now()
                if height is not None and last_height is not None and height <= last_height and cur_count <= prev_count and clicked == 0:
                    stagnation += 1
                else:
                    stagnation = 0
                last_height = height
                prev_count = max(prev_count, cur_count)
                self.logger.debug(f"Comment load round {r+1}/{max_load_rounds}: clicked={clicked}, comments_seen={cur_count}, stagnation={stagnation}")
                if stagnation >= stagnation_threshold:
                    self.logger.debug("Stopping comment load due to stagnation.")
                    break

            comment_selectors = [
                'div[role="article"] div[data-testid="comment"]',
                '[data-testid="UFI2Comment/root_depth_0"]',
                'div[aria-label*="Comment by"]',
                'div[data-testid="comment"]',
                'li[role="article"]'
            ]
            comments = []
            for selector in comment_selectors:
                try:
                    found_comments = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if found_comments:
                        actual = []
                        for c in found_comments:
                            try:
                                if c.text and len(c.text.strip()) > 5 and self.is_top_level_comment(c):
                                    actual.append(c)
                            except Exception:
                                continue
                        if actual:
                            self.logger.info(f"Found {len(actual)} comments on post page using selector: {selector}")
                            comments = actual
                            break
                except Exception as e:
                    self.logger.warning(f"Post page comment selector {selector} failed: {e}")
                    continue

            if not comments:
                self.logger.info(f"No comments found on post page for {post_id}")
                return True

            unique_comments = []
            seen_keys = set()
            for c in comments:
                try:
                    if not self.is_top_level_comment(c):
                        continue
                    txt = self.get_comment_text(c)
                    usr = self.get_comment_username(c)
                    dom_id = self.get_comment_id(c)
                    key = dom_id or self.make_comment_key(post_id, usr, txt)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    unique_comments.append((c, key, usr, txt))
                except Exception:
                    continue

            # Build set of users we've already replied to for this post (persisted)
            reply_once = self.config.get('bot_settings', {}).get('reply_only_once_per_user_per_post', True)
            replied_users_persisted = set()
            if reply_once:
                try:
                    for rec in self.processed_comments.values():
                        try:
                            if rec.get('action') == 'replied' and rec.get('post_id') == post_id and rec.get('username'):
                                replied_users_persisted.add(rec.get('username'))
                        except Exception:
                            continue
                except Exception:
                    pass

            max_comments = self.config.get('bot_settings', {}).get('max_comments_per_post', 1000)
            if not max_comments or max_comments <= 0:
                max_comments = len(unique_comments)
            self.logger.info(f"Processing {min(len(unique_comments), max_comments)} unique root comments from post page (found={len(unique_comments)})")

            session_processed = set()
            session_replied_users = set()
            replied_count = 0
            for i, (comment, comment_key, username, comment_text) in enumerate(unique_comments[:max_comments]):
                try:
                    if self.is_in_cooldown():
                        self.logger.warning("Cooldown became active during post processing; stopping comments for this post.")
                        break
                    if (comment_key not in self.processed_comments) and (comment_key not in session_processed):
                        # Skip if we've already replied to this user on this post (persisted or in-session)
                        if reply_once and username:
                            if (username in replied_users_persisted) or (username in session_replied_users):
                                reason = f"already replied to user {username} on this post"
                                self.logger.info(f"Skipping comment from {username}: {reason}")
                                self.processed_comments[comment_key] = {
                                    'processed_at': datetime.now().isoformat(),
                                    'action': 'skipped',
                                    'reason': 'user_already_replied',
                                    'username': username,
                                    'post_id': post_id
                                }
                                session_processed.add(comment_key)
                                self.save_processed_data(self.processed_comments, 'processed_comments_file')
                                continue
                        if comment_text in ["No text found", ""] or "Error extracting" in comment_text or len(comment_text.strip()) < 3:
                            self.logger.info(f"Skipping comment {i+1}: No meaningful text")
                            continue
                        should_skip, skip_reason = self.should_skip_comment(comment_text, username)
                        if should_skip:
                            self.logger.info(f"Skipping comment {comment_key}: {skip_reason}")
                            self.processed_comments[comment_key] = {
                                'processed_at': datetime.now().isoformat(),
                                'action': 'skipped',
                                'reason': skip_reason,
                                'username': username
                            }
                        else:
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", comment)
                                time.sleep(0.8)
                            except Exception:
                                pass
                            success = self.reply_to_comment(comment, comment_key, username, comment_text)
                            if success:
                                replied_count += 1
                                self.processed_comments[comment_key] = {
                                    'processed_at': datetime.now().isoformat(),
                                    'action': 'replied',
                                    'username': username,
                                    'text': comment_text,
                                    'post_id': post_id
                                }
                                if reply_once and username:
                                    session_replied_users.add(username)
                                session_processed.add(comment_key)
                            else:
                                self.processed_comments[comment_key] = {
                                    'processed_at': datetime.now().isoformat(),
                                    'action': 'failed',
                                    'username': username,
                                    'text': comment_text,
                                    'post_id': post_id
                                }
                                session_processed.add(comment_key)
                            self._jitter_sleep(self.config.get('bot_settings', {}).get('delay_between_comments', 3), jitter_key="reply_jitter")
                        self.save_processed_data(self.processed_comments, 'processed_comments_file')
                except Exception as e:
                    self.logger.error(f"Error processing post page comment {i}: {e}")
                    continue
            self.logger.info(f"Replied to {replied_count} comments for post {post_id} (current tab)")
            return True
        except Exception as e:
            self.logger.error(f"Error processing comments on current post page: {e}")
            return False

    def make_navigation_variants(self, canonical_url: str):
        """Given a canonical /groups/{gid}/posts/{pid}, return [posts_url, permalink_url].
        If parsing fails, return [canonical_url]."""
        try:
            if not canonical_url:
                return []
            m = re.search(r"https?://[^/]+/groups/([^/]+)/posts/([^/?#]+)", canonical_url)
            if not m:
                return [canonical_url]
            gid, pid = m.group(1), m.group(2)
            base = re.match(r"https?://[^/]+", canonical_url).group(0)
            posts_url = f"{base}/groups/{gid}/posts/{pid}"
            permalink_url = f"{base}/groups/{gid}/permalink/{pid}"
            return [posts_url, permalink_url]
        except Exception:
            return [canonical_url]

    def process_posts_multitab(self):
        """Open each post URL from posts.txt in its own tab and process comments in a loop.
        Periodically refresh each tab and rescan for new comments."""
        try:
            posts = self.posts_list or self.load_posts()
            if not posts:
                self.logger.error("No posts in posts.txt. Add post URLs (one per line).")
                return False

            # Normalize to canonical and prepare targets
            targets = []  # list of dicts: {url, pid}
            for raw in posts:
                try:
                    cur = 'https://web.facebook.com/'
                    m_gid = None
                    gid_ctx = None
                    canon = self.canonicalize_post_url(raw, gid_ctx)
                    if not canon:
                        self.logger.warning(f"Skipping non-canonical/forbidden post URL from posts.txt: {raw}")
                        continue
                    m = re.search(r"/groups/[^/]+/posts/([^/?#]+)", canon)
                    pid = m.group(1) if m else self.extract_post_id_from_url(canon)
                    if not pid:
                        self.logger.warning(f"Could not extract post id from {canon}")
                        continue
                    targets.append({"url": canon, "pid": pid})
                except Exception as e:
                    self.logger.warning(f"Error preparing post URL {raw}: {e}")
                    continue

            if not targets:
                self.logger.error("No valid post URLs to open.")
                return False

            # Open tabs up to capacity and navigate
            handles = []
            settings = self.config.get('posts_multitab', {})
            max_tabs = int(settings.get('max_tabs', 0))
            if max_tabs <= 0:
                max_tabs = len(targets)
            # Keep a master list so we can restart cycles over all posts
            all_targets = list(targets)
            pending_targets = list(all_targets)
            initial_batch = pending_targets[:max_tabs]
            pending_targets = pending_targets[max_tabs:]
            for t in initial_batch:
                try:
                    # Selenium 4 API for a new tab
                    self.driver.switch_to.new_window('tab')
                    h = self.driver.current_window_handle
                    handles.append({"handle": h, "pid": t["pid"], "url": t["url"]})
                    variants = self.make_navigation_variants(t["url"])
                    nav_url = random.choice(variants) if len(variants) > 1 else t["url"]
                    self.logger.info(f"Opening post in new tab via: {nav_url}")
                    self.driver.get(nav_url)
                    try:
                        pid = t["pid"]
                        def _ok(d):
                            cur = d.current_url or ''
                            return ('/posts/' in cur or '/permalink/' in cur or (pid and pid in cur))
                        WebDriverWait(self.driver, 12).until(_ok)
                    except Exception:
                        time.sleep(2)
                except Exception as e:
                    self.logger.warning(f"Failed to open tab for {t['url']}: {e}")
                    continue

            if not handles:
                self.logger.error("Failed to open tabs for posts.")
                return False

            # Round-robin refresh and process
            refresh_seconds = settings.get('refresh_seconds', 60)
            max_rounds = settings.get('max_rounds', 0)  # 0 => infinite
            per_tab_pause = settings.get('per_tab_pause_seconds', 3)
            # Reload control for posts source
            if getattr(self, '_posts_source', 'default') == 'per_account_sheet':
                src_cfg = self.config.get('posts_per_account_sheet', {})
                reload_seconds = int(src_cfg.get('reload_seconds', 300))
                reload_enabled = bool(src_cfg.get('enabled')) and bool(src_cfg.get('url'))
            else:
                sheets_csv_cfg = self.config.get('sheets_csv', {})
                reload_seconds = int(sheets_csv_cfg.get('reload_seconds', 300))
                reload_enabled = bool(sheets_csv_cfg.get('enabled')) and bool(sheets_csv_cfg.get('url'))

            rounds = 0
            self.logger.info(f"Starting multi-tab loop for {len(handles)} posts; refresh {refresh_seconds}s, rounds={'infinite' if max_rounds==0 else max_rounds}")
            while True:
                if self.is_in_cooldown():
                    self.logger.warning("Cooldown active; pausing multi-tab scanning.")
                    time.sleep(min(600, refresh_seconds))
                    continue
                # Ensure we have exactly up to max_tabs active; if less (shouldn't happen often), open new windows
                while len(handles) < min(max_tabs, len(all_targets)) and pending_targets:
                    t = pending_targets.pop(0)
                    try:
                        self.driver.switch_to.new_window('tab')
                        h = self.driver.current_window_handle
                        handles.append({"handle": h, "pid": t["pid"], "url": t["url"]})
                        variants = self.make_navigation_variants(t["url"])
                        nav_url = random.choice(variants) if len(variants) > 1 else t["url"]
                        self.logger.info(f"[Init-Topup] Opening post in new tab via: {nav_url}")
                        self.driver.get(nav_url)
                        try:
                            pid = t["pid"]
                            def _ok(d):
                                cur = d.current_url or ''
                                return ('/posts/' in cur or '/permalink/' in cur or (pid and pid in cur))
                            WebDriverWait(self.driver, 12).until(_ok)
                        except Exception:
                            time.sleep(2)
                    except Exception as e:
                        self.logger.warning(f"[Init-Topup] Failed to open tab for {t['url']}: {e}")
                        continue

                for info in list(handles):
                    try:
                        self.driver.switch_to.window(info["handle"])
                        try:
                            self.driver.refresh()
                        except Exception:
                            pass
                        time.sleep(per_tab_pause)
                        self.process_comments_on_current_post_page(info["pid"])
                        # After processing this tab, immediately rotate to next queued post if available
                        if pending_targets:
                            next_t = pending_targets.pop(0)
                            variants = self.make_navigation_variants(next_t["url"])
                            nav_url = random.choice(variants) if len(variants) > 1 else next_t["url"]
                            self.logger.info(f"[Rotate] Switching tab to next post via: {nav_url}")
                            try:
                                self.driver.get(nav_url)
                                try:
                                    pid = next_t["pid"]
                                    def _ok(d):
                                        cur = d.current_url or ''
                                        return ('/posts/' in cur or '/permalink/' in cur or (pid and pid in cur))
                                    WebDriverWait(self.driver, 12).until(_ok)
                                except Exception:
                                    time.sleep(2)
                                # Update handle info to reflect the new assignment
                                info["pid"], info["url"] = next_t["pid"], next_t["url"]
                            except Exception as e:
                                self.logger.warning(f"[Rotate] Navigation failed for {next_t['url']}: {e}")
                    except Exception as e:
                        self.logger.warning(f"Error in tab for PID {info.get('pid')}: {e}")
                        continue
                rounds += 1
                # Hot-reload posts list from configured source and enqueue newly added posts
                try:
                    if reload_enabled and (time.time() - self.last_posts_reload >= reload_seconds):
                        self.logger.info("Reload interval reached for posts. Fetching latest posts from configured source...")
                        if getattr(self, '_posts_source', 'default') == 'per_account_sheet':
                            acct = getattr(self, '_posts_account_number', None)
                            new_posts = self.load_posts_for_account_from_sheet(acct) or []
                        else:
                            new_posts = self.load_posts() or []
                        # Canonicalize and derive pids as done initially
                        new_targets = []
                        for raw in new_posts:
                            try:
                                canon = self.canonicalize_post_url(raw, None)
                                if not canon:
                                    continue
                                m = re.search(r"/groups/[^/]+/posts/([^/?#]+)", canon)
                                pid = m.group(1) if m else self.extract_post_id_from_url(canon)
                                if not pid:
                                    continue
                                new_targets.append({"url": canon, "pid": pid})
                            except Exception:
                                continue
                        if new_targets:
                            # Build a set of all known pids (active and pending) to avoid dup enqueues
                            existing_pids = set(h.get('pid') for h in handles)
                            existing_pids.update(t['pid'] for t in pending_targets)
                            existing_pids.update(t['pid'] for t in all_targets)
                            additions = [t for t in new_targets if t['pid'] not in existing_pids]
                            if additions:
                                # Append to both the master list and the pending queue so they'll be rotated in next
                                all_targets.extend(additions)
                                pending_targets.extend(additions)
                        self.last_posts_reload = time.time()
                except Exception as e:
                    self.logger.warning(f"[Auto-Reload] Error while reloading posts: {e}")

                # If we've exhausted the pending queue, start a new cycle over all targets to check for new comments
                if not pending_targets:
                    pending_targets = list(all_targets)
                    self.logger.info(f"[Cycle] Completed a full pass over {len(all_targets)} posts. Restarting from the first post to check for new comments.")
                # Periodic Telegram stats check
                self.check_and_send_telegram_stats()

                if max_rounds and rounds >= max_rounds:
                    break
                time.sleep(refresh_seconds)

            self.logger.info("Multi-tab posts processing finished.")
            return True
        except Exception as e:
            self.logger.error(f"process_posts_multitab failed: {e}")
            return False

    def run_posts_multitab(self, account_number, use_account_posts_sheet: bool = False):
        """Setup driver, login via cookies, and run multi-tab posts processing."""
        try:
            self.account_number = str(account_number)
            print("\n🚀 Starting Multi-Tab Posts Mode")
            if not self.setup_driver():
                print("❌ Failed to setup browser driver")
                return False
            if not self.load_cookies(account_number):
                print("❌ Failed to login with cookies")
                return False
            # Prepare posts source based on flag/config
            posts_source = "posts.txt"
            if use_account_posts_sheet:
                sheet_posts = self.load_posts_for_account_from_sheet(account_number)
                if isinstance(sheet_posts, list) and sheet_posts:
                    # Temporarily override posts_list for this run
                    posts_source = "Google Sheet (per-account)"
                    old_posts = self.posts_list
                    old_src = getattr(self, '_posts_source', 'default')
                    old_acct = getattr(self, '_posts_account_number', None)
                    self.posts_list = sheet_posts
                    # Mark current posts source so refresh logic can re-fetch from sheet
                    self._posts_source = 'per_account_sheet'
                    self._posts_account_number = account_number
                else:
                    print("⚠️ Could not load posts from sheet for this account. Falling back to posts.txt")
                    old_posts = None
                    old_src = getattr(self, '_posts_source', 'default')
                    old_acct = getattr(self, '_posts_account_number', None)
            else:
                old_posts = None
                old_src = getattr(self, '_posts_source', 'default')
                old_acct = getattr(self, '_posts_account_number', None)

            print(f"✅ Logged in; opening posts from {posts_source} in tabs...")
            ok = self.process_posts_multitab()
            print("✅ Multi-Tab mode completed" if ok else "❌ Multi-Tab mode ended with errors")
            return ok
        except Exception as e:
            self.logger.error(f"run_posts_multitab failed: {e}")
            return False
        finally:
            # Restore posts_list if overridden
            try:
                if 'old_posts' in locals() and old_posts is not None:
                    self.posts_list = old_posts
                # Restore posts source markers
                if 'old_src' in locals():
                    self._posts_source = old_src
                if 'old_acct' in locals():
                    self._posts_account_number = old_acct
            except Exception:
                pass
            if self.driver:
                self.driver.quit()

    # ----- Timestamp detection -----
    def find_post_timestamp_href(self, post_element):
        """Find the permalink href of this post (timestamp link)."""
        try:
            # First: within post
            anchors = post_element.find_elements(By.TAG_NAME, 'a')
            hrefs = []
            for a in anchors:
                href = a.get_attribute('href') or ''
                if ('/groups/' in href and '/posts/' in href) or ('permalink' in href) or ('/posts/pfbid' in href):
                    hrefs.append(href)
            if hrefs:
                return hrefs[0]

            # Second: choose nearest matching anchor on page by geometry
            post_rect = self.driver.execute_script(
                "const r=arguments[0].getBoundingClientRect();return {cy:(r.top+r.bottom)/2};",
                post_element,
            ) or {"cy": 0}
            all_links = self.driver.find_elements(By.XPATH, "//a[contains(@href,'/groups/') and contains(@href,'/posts/')]")
            best_href, best_dy = None, None
            for a in all_links:
                try:
                    rect = self.driver.execute_script(
                        "const r=arguments[0].getBoundingClientRect();return {cy:(r.top+r.bottom)/2};",
                        a,
                    )
                    dy = abs((rect or {}).get('cy', 0) - post_rect.get('cy', 0))
                    if best_href is None or dy < best_dy:
                        best_href, best_dy = a.get_attribute('href'), dy
                except Exception:
                    continue
            return best_href
        except Exception as e:
            self.logger.debug(f"find_post_timestamp_href failed: {e}")
            return None

    def find_best_timestamp_link(self, post_element):
        """Return (element, href) for the best timestamp link near the post header.
        Prefers clean post permalinks and avoids comment links (comment_id, reply_comment_id)."""
        try:
            candidates = []

            # 1) Heuristic: prefer anchors with a <time> descendant inside this post
            try:
                time_links = post_element.find_elements(By.XPATH, ".//a[descendant::time]")
            except Exception:
                time_links = []
            # Include role='link' variants with <time>
            try:
                time_links_role = post_element.find_elements(By.XPATH, ".//*[@role='link' and descendant::time]")
            except Exception:
                time_links_role = []
            for a in time_links + time_links_role:
                try:
                    href = (a.get_attribute('href') or a.get_attribute('data-lynx-uri') or a.get_attribute('ajaxify') or '').strip()
                    if not href:
                        continue
                    clean_href = href.split('?')[0]
                    if '/groups/' in clean_href and '/posts/' in clean_href and not any(b in clean_href for b in ['comment_id=','/comment/','/comments/','/replies/']):
                        score = 100
                        label = (a.get_attribute('aria-label') or a.text or '').lower()
                        if any(tok in label for tok in ['m','h','d','ago']):
                            score += 20
                        candidates.append((score, a, clean_href))
                except Exception:
                    continue

            # 2) Fallback: spans with time-like text (e.g., "5 m", "1 h", "2 d"). Climb to nearest ancestor anchor
            try:
                time_like_spans = post_element.find_elements(
                    By.XPATH,
                    ".//span[normalize-space()='m' or normalize-space()='h' or contains(normalize-space(), 'ago') or matches(., '^[0-9]+\\s*(m|h|d|w)$')]"
                )
            except Exception:
                time_like_spans = []
            # XPath 1.0 in Selenium may not support matches(); include simpler fallbacks
            if not time_like_spans:
                try:
                    time_like_spans = post_element.find_elements(
                        By.XPATH,
                        ".//span[contains(translate(., 'MHDAGO', 'mhdago'), 'm') or contains(translate(., 'MHDAGO', 'mhdago'), 'h') or contains(translate(., 'MHDAGO', 'mhdago'), 'd') or contains(translate(., 'MHDAGO', 'mhdago'), 'ago')]"
                    )
                except Exception:
                    time_like_spans = []
            for s in time_like_spans:
                try:
                    anc = s.find_element(By.XPATH, "./ancestor::a[1]")
                except Exception:
                    anc = None
                if not anc:
                    continue
                try:
                    href = (anc.get_attribute('href') or anc.get_attribute('data-lynx-uri') or anc.get_attribute('ajaxify') or '').strip()
                    if not href:
                        continue
                    clean_href = href.split('?')[0]
                    if '/groups/' in clean_href and '/posts/' in clean_href and not any(b in clean_href for b in ['comment_id=','/comment/','/comments/','/replies/']):
                        # score lower than explicit <time> but still valid
                        score = 80
                        candidates.append((score, anc, clean_href))
                except Exception:
                    continue

            # 3) Also consider general anchors under post header containing /groups/.../posts/...
            try:
                generic_links = post_element.find_elements(By.XPATH, ".//a[contains(@href,'/groups/') and contains(@href,'/posts/')]")
            except Exception:
                generic_links = []
            for a in generic_links:
                try:
                    href = (a.get_attribute('href') or '').strip()
                    if not href:
                        continue
                    if ('/groups/' in href and '/posts/' in href) or ('permalink' in href) or ('/posts/pfbid' in href):
                        clean_href = href.split('?')[0]
                        has_comment = ('comment_id=' in href) or ('reply_comment_id=' in href) or ('comment' in href and 'posts' not in href.split('comment')[0])
                        score = 1000
                        # Shorter URL better
                        score -= len(clean_href)
                        # Penalty for query params
                        if '?' in href:
                            score -= 50
                        # Heavy penalty if comment related
                        if has_comment:
                            score -= 400
                        # Boost if label text resembles time
                        label = (a.get_attribute('aria-label') or a.text or '').lower()
                        if any(tok in label for tok in ['m', 'h', 'd', 'ago']) or re.search(r"\b\d+\s*(m|h|d|w)\b", label or ''):
                            score += 50
                        candidates.append((score, a, clean_href))
                except Exception:
                    continue

            if not candidates:
                return None, None
            # Pick best scored
            candidates.sort(key=lambda x: x[0], reverse=True)
            _, el, href = candidates[0]
            return el, href
        except Exception as e:
            self.logger.debug(f"find_best_timestamp_link failed: {e}")
            return None, None

    def find_best_timestamp_link_global(self, post_element):
        """Search the whole page for the nearest clean /groups/.../posts/... link to this post.
        Returns (element, href). Avoids comment links."""
        try:
            post_cy = self.driver.execute_script(
                "const r=arguments[0].getBoundingClientRect();return (r.top+r.bottom)/2;",
                post_element,
            )
            # Include anchors and role='link' elements with href
            links = []
            try:
                links.extend(self.driver.find_elements(By.XPATH, "//a[contains(@href,'/groups/') and contains(@href,'/posts/')]") )
            except Exception:
                pass
            try:
                links.extend(self.driver.find_elements(By.XPATH, "//*[@role='link' and contains(@href,'/groups/') and contains(@href,'/posts/')]") )
            except Exception:
                pass
            best = None
            for a in links:
                href = (a.get_attribute('href') or '').strip()
                if not href:
                    continue
                # Exclude comment/reply links even when encoded in path or query
                if ('comment_id=' in href) or ('reply_comment_id=' in href) or ('/comment/' in href) or ('/comments/' in href) or ('/replies/' in href):
                    continue
                clean = href.split('?')[0]
                cy = self.driver.execute_script(
                    "const r=arguments[0].getBoundingClientRect();return (r.top+r.bottom)/2;",
                    a,
                )
                dy = abs((cy or 0) - (post_cy or 0))
                score = -dy - len(clean)*0.1
                if best is None or score > best[0]:
                    best = (score, a, clean)
            if best:
                return best[1], best[2]
            return None, None
        except Exception as e:
            self.logger.debug(f"find_best_timestamp_link_global failed: {e}")
            return None, None

    def dump_post_dom_debug(self, post_element, note=""):
        """Dump HTML diagnostics for a post container to logs/dom_debug_*.html.
        Captures outerHTML for the post, header/timestamp candidates, and some anchor samples.
        """
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            os.makedirs('logs', exist_ok=True)
            path = os.path.join('logs', f'dom_debug_{ts}.html')
            cur = ''
            try:
                cur = self.driver.current_url
            except Exception:
                pass
            # Collect snippets
            outer = ''
            try:
                outer = self.driver.execute_script("return arguments[0].outerHTML;", post_element) or ''
            except Exception:
                pass
            # Timestamp/link candidates inside post
            candidates_html = []
            xpaths = [
                ".//a[descendant::time]",
                ".//*[@role='link' and descendant::time]",
                ".//span[contains(translate(., 'MHDAGO', 'mhdago'), 'm') or contains(translate(., 'MHDAGO', 'mhdago'), 'h') or contains(translate(., 'MHDAGO', 'mhdago'), 'd') or contains(translate(., 'MHDAGO', 'mhdago'), 'ago')]/ancestor::a[1]",
                ".//a[contains(@href,'/groups/') and contains(@href,'/posts/')]",
                ".//a[contains(@href,'permalink') or contains(@href,'story.php') or contains(@href,'photo') or contains(@href,'post_id=')]",
            ]
            for xp in xpaths:
                try:
                    els = post_element.find_elements(By.XPATH, xp)
                    for i, el in enumerate(els[:5]):
                        try:
                            href = el.get_attribute('href') or el.get_attribute('data-lynx-uri') or el.get_attribute('ajaxify') or ''
                        except Exception:
                            href = ''
                        try:
                            html = self.driver.execute_script("return arguments[0].outerHTML;", el) or ''
                        except Exception:
                            html = ''
                        candidates_html.append(f"<!-- XP:{xp} idx:{i} href:{href} -->\n{html}")
                except Exception:
                    continue
            # Write file
            with open(path, 'w', encoding='utf-8') as f:
                f.write("<!doctype html><meta charset='utf-8'>\n")
                f.write(f"<pre>URL: {cur}\nNote: {note}</pre>\n<hr>\n")
                f.write("<h3>Post outerHTML</h3>\n")
                f.write(f"<div>{outer}</div>\n<hr>\n")
                f.write("<h3>Timestamp/link candidates (first few per XPath)</h3>\n")
                for block in candidates_html:
                    f.write(block + "\n<hr>\n")
            self.logger.debug(f"DOM debug written to {path}")
        except Exception as e:
            self.logger.debug(f"dump_post_dom_debug failed: {e}")
    
    def load_processed_data(self, file_key):
        """Load processed posts/comments data"""
        file_path = self.config.get('database', {}).get(file_key, f'logs/{file_key}.json')
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def save_processed_data(self, data, file_key):
        """Save processed posts/comments data"""
        file_path = self.config.get('database', {}).get(file_key, f'logs/{file_key}.json')
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def load_reply_messages(self):
        """Load reply messages from reply_text.txt"""
        try:
            with open('reply_text.txt', 'r', encoding='utf-8') as f:
                # Ignore empty lines and lines starting with '#'
                messages = [line.strip() for line in f.readlines() if line.strip() and not line.strip().startswith('#')]
                return messages
        except FileNotFoundError:
            self.logger.error("reply_text.txt not found!")
            return ["Thanks for sharing!"]
    
    def load_groups(self):
        """Load Facebook group URLs from Google Sheets (auth or public CSV) or groups.txt.
        Sheets config example in config.json:
        {
          "sheets": {
            "enabled": true,
            "creds_file": "service_account.json",
            "spreadsheet_id": "<sheet-id>",
            "groups_range": "Groups!A:A",
            "posts_range": "Posts!A:A"
          }
        }
        """
        # 0) Public CSV mode (no auth)
        sheets_csv = self.config.get('sheets_csv', {}) or {}
        if sheets_csv.get('enabled') and sheets_csv.get('url') and requests:
            try:
                resp = requests.get(sheets_csv['url'], timeout=15)
                resp.raise_for_status()
                groups = []
                reader = csv.reader(resp.text.splitlines())
                # default to column A (index 0)
                col = int(sheets_csv.get('groups_col', 0))
                # Skip header heuristically if it contains 'group'
                first = True
                for row in reader:
                    if not row or len(row) <= col:
                        continue
                    val = (row[col] or '').strip()
                    if not val:
                        continue
                    if first and val.lower().startswith('group'):
                        first = False
                        continue
                    first = False
                    if not val.startswith('#'):
                        groups.append(val)
                self.logger.info(f"Loaded {len(groups)} groups from public CSV")
                if groups:
                    return groups
            except Exception as e:
                self.logger.error(f"Failed to load groups from public CSV: {e}")

        sheets_cfg = self.config.get('sheets', {}) or {}
        if sheets_cfg.get('enabled') and gspread and Credentials:
            try:
                scope = ['https://www.googleapis.com/auth/spreadsheets.readonly']
                creds = Credentials.from_service_account_file(sheets_cfg['creds_file'], scopes=scope)
                client = gspread.authorize(creds)
                sh = client.open_by_key(sheets_cfg['spreadsheet_id'])
                rng = sheets_cfg.get('groups_range', 'Groups!A:A')
                ws_name, col = rng.split('!')[0], rng.split('!')[1]
                ws = sh.worksheet(ws_name)
                values = ws.get(col)
                groups = [v[0].strip() for v in values if v and v[0].strip() and not v[0].strip().startswith('#')]
                self.logger.info(f"Loaded {len(groups)} groups from Google Sheets")
                return groups
            except Exception as e:
                self.logger.error(f"Failed to load groups from Google Sheets: {e}. Falling back to groups.txt")
                # fallthrough to file
        # Fallback to local file
        try:
            with open('groups.txt', 'r', encoding='utf-8') as f:
                groups = [line.strip() for line in f.readlines() if line.strip() and not line.startswith('#')]
            return groups
        except FileNotFoundError:
            self.logger.error("groups.txt not found!")
            return []

    def load_posts(self):
        """Load post URLs from Google Sheets (auth or public CSV) or posts.txt (one per line)."""
        # 0) Public CSV mode (no auth)
        sheets_csv = self.config.get('sheets_csv', {}) or {}
        if sheets_csv.get('enabled') and sheets_csv.get('url') and requests:
            try:
                resp = requests.get(sheets_csv['url'], timeout=15)
                resp.raise_for_status()
                posts = []
                reader = csv.reader(resp.text.splitlines())
                # default to column B (index 1)
                col = int(sheets_csv.get('posts_col', 1))
                # Skip header heuristically if it contains 'post'
                first = True
                for row in reader:
                    if not row or len(row) <= col:
                        continue
                    val = (row[col] or '').strip()
                    if not val:
                        continue
                    if first and val.lower().startswith('post'):
                        first = False
                        continue
                    first = False
                    if not val.startswith('#'):
                        posts.append(val)
                self.logger.info(f"Loaded {len(posts)} posts from public CSV")
                if posts:
                    return posts
            except Exception as e:
                self.logger.error(f"Failed to load posts from public CSV: {e}")

        sheets_cfg = self.config.get('sheets', {}) or {}
        if sheets_cfg.get('enabled') and gspread and Credentials:
            try:
                scope = ['https://www.googleapis.com/auth/spreadsheets.readonly']
                creds = Credentials.from_service_account_file(sheets_cfg['creds_file'], scopes=scope)
                client = gspread.authorize(creds)
                sh = client.open_by_key(sheets_cfg['spreadsheet_id'])
                rng = sheets_cfg.get('posts_range', 'Posts!A:A')
                ws_name, col = rng.split('!')[0], rng.split('!')[1]
                ws = sh.worksheet(ws_name)
                values = ws.get(col)
                posts = [v[0].strip() for v in values if v and v[0].strip() and not v[0].strip().startswith('#')]
                self.logger.info(f"Loaded {len(posts)} posts from Google Sheets")
                return posts
            except Exception as e:
                self.logger.error(f"Failed to load posts from Google Sheets: {e}. Falling back to posts.txt")
                # fallthrough to file
        try:
            with open('posts.txt', 'r', encoding='utf-8') as f:
                posts = [line.strip() for line in f.readlines() if line.strip() and not line.startswith('#')]
                return posts
        except FileNotFoundError:
            self.logger.warning("posts.txt not found; multi-tab mode will have no posts unless you create it.")
            return []
    
    def load_posts_for_account_from_sheet(self, account_number: str):
        """Load posts for a specific account from a public CSV Google Sheet.
        It searches the header row for a column named like 'Posts {account_number}' (case-insensitive).
        Fallbacks to self.load_posts() if not available.
        """
        try:
            cfg = self.config.get('posts_per_account_sheet', {}) or {}
            url = cfg.get('url')
            if not (cfg.get('enabled') and url and requests):
                return None

            # Cache-bust to avoid stale content
            try:
                ts = int(time.time())
                url_with_ts = url + (('&' if '?' in url else '?') + f"_ts={ts}")
            except Exception:
                url_with_ts = url

            resp = requests.get(url_with_ts, timeout=15)
            resp.raise_for_status()
            lines = list(csv.reader(resp.text.splitlines()))
            if not lines:
                return []
            header = [str(h or '').strip() for h in lines[0]]
            target_name = f"posts {str(account_number).strip()}".lower().replace('_', ' ')
            target_idx = None
            for idx, name in enumerate(header):
                if str(name).strip().lower().replace('_', ' ') == target_name:
                    target_idx = idx
                    break
            if target_idx is None:
                # Try looser match: header contains both 'posts' and account_number
                for idx, name in enumerate(header):
                    low = str(name).strip().lower()
                    if 'posts' in low and str(account_number).strip().lower() in low:
                        target_idx = idx
                        break
            if target_idx is None:
                self.logger.warning(f"Posts column for account {account_number} not found in sheet header: {header}")
                return None

            urls = []
            for row in lines[1:]:
                if target_idx < len(row):
                    val = (row[target_idx] or '').strip()
                    if val and val.lower().startswith('http'):
                        urls.append(val)
            self.logger.info(f"Loaded {len(urls)} posts for account {account_number} from per-account sheet")
            return urls
        except Exception as e:
            self.logger.error(f"Failed to load posts for account {account_number} from sheet: {e}")
            return None
    
    def get_chromedriver_path(self):
        """Get the path to local ChromeDriver executable"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        chromedriver_dir = os.path.join(script_dir, 'chromedriver')
        
        # Check for different possible ChromeDriver names
        possible_names = ['chromedriver.exe', 'chromedriver']
        
        for name in possible_names:
            chromedriver_path = os.path.join(chromedriver_dir, name)
            if os.path.exists(chromedriver_path):
                self.logger.info(f"Found ChromeDriver at: {chromedriver_path}")
                return chromedriver_path
        
        self.logger.error(f"ChromeDriver not found in {chromedriver_dir}")
        return None

    def setup_driver(self):
        """Setup Chrome WebDriver with options using local ChromeDriver"""
        chrome_options = Options()
        
        if self.config.get('bot_settings', {}).get('headless_mode', False):
            chrome_options.add_argument('--headless')
        
        window_size = self.config.get('bot_settings', {}).get('window_size', '1920,1080')
        chrome_options.add_argument(f'--window-size={window_size}')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--allow-running-insecure-content')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        # Allow actions to work while Chrome window is in background/occluded
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-backgrounding-occluded-windows')
        chrome_options.add_argument('--disable-renderer-backgrounding')
        chrome_options.add_argument('--disable-features=RendererCodeIntegrity,CalculateNativeWinOcclusion')
        
        try:
            # Auto-download compatible ChromeDriver via webdriver-manager
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
                self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                self.logger.info("ChromeDriver setup successful (webdriver-manager)")
                return True
            except WebDriverException as we:
                self.logger.warning(f"webdriver-manager failed, attempting Selenium Manager fallback: {str(we)[:160]}")
            except Exception as e:
                self.logger.warning(f"webdriver-manager init error, trying Selenium Manager: {str(e)[:160]}")

            # Fallback: Selenium Manager (Selenium 4.6+) auto-downloads a compatible driver for installed Chrome
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
                self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                self.logger.info("ChromeDriver setup successful (Selenium Manager)")
                return True
            except Exception as e2:
                self.logger.error(f"Selenium Manager fallback failed: {str(e2)[:200]}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to setup driver: {e}")
            return False
    
    def load_cookies(self, account_number):
        """Load Facebook cookies for authentication - try Google Sheets first, then local file"""
        account_fname = f'{account_number}_cookies.json'
        
        # Try Google Sheets first
        cookies = self.load_cookies_from_sheets(account_number)
        
        # Fallback to local file
        if not cookies:
            cookie_file = f'accounts/{account_fname}'
            try:
                with open(cookie_file, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                self.logger.info(f"Loaded cookies from local file: {cookie_file}")
            except FileNotFoundError:
                self.logger.error(f"Cookie file {cookie_file} not found!")
                return False
            except json.JSONDecodeError as e:
                self.logger.error(f"Invalid JSON in cookie file {cookie_file}: {e}")
                return False
        
        if not cookies:
            self.logger.error(f"No cookies available for account {account_number}")
            return False
        
        try:
            # Navigate to Facebook first
            self.logger.info("Navigating to Facebook...")
            self.driver.get('https://facebook.com')
            time.sleep(3)
        
            # Add cookies one by one with better error handling
            successful_cookies = 0
            for i, cookie in enumerate(cookies):
                try:
                    # Ensure required fields are present
                    if 'name' in cookie and 'value' in cookie:
                        # Remove problematic fields that might cause issues
                        clean_cookie = {
                            'name': cookie['name'],
                            'value': cookie['value'],
                            'domain': cookie.get('domain', '.facebook.com'),
                            'path': cookie.get('path', '/'),
                        }
                        
                        # Add optional fields if they exist and are valid
                        if 'secure' in cookie:
                            clean_cookie['secure'] = cookie['secure']
                        if 'httpOnly' in cookie:
                            clean_cookie['httpOnly'] = cookie['httpOnly']
                        
                        self.driver.add_cookie(clean_cookie)
                        successful_cookies += 1
                        
                except Exception as e:
                    self.logger.warning(f"Failed to add cookie {i}: {str(e)[:100]}")
                    continue
            
            self.logger.info(f"Successfully added {successful_cookies}/{len(cookies)} cookies")
            
            # Navigate directly to first group instead of homepage
            if self.groups:
                first_group = self.groups[0]
                self.logger.info(f"Navigating directly to first group: {first_group}")
                self.driver.get(first_group)
                time.sleep(5)
            else:
                # Fallback to refresh if no groups
                self.driver.refresh()
                time.sleep(5)
            
            # Check if login was successful
            time.sleep(5)
            current_url = self.driver.current_url
            if 'facebook.com' in current_url and 'login' not in current_url.lower():
                self.logger.info(f"Successfully logged in with {successful_cookies} cookies")
                self.clear_failed_login_notice(account_fname)  # Clear any existing notices
                return True
            else:
                self.logger.error(f"Failed to login with account {account_number}. Current URL: {current_url}")
                self.send_or_update_failed_login_notice(account_fname, 
                    f"🔴 Failed to login with {account_fname} — please check cookies in Google Sheets")
                return False
            
        except Exception as e:
            self.logger.error(f"Error loading cookies: {str(e)[:200]}")
            self.send_or_update_failed_login_notice(account_fname,
                f"🔴 Error loading cookies for {account_fname}: {str(e)[:100]}")
            return False
    
    def get_random_reply(self):
        """Get a random reply message"""
        if self.config.get('reply_settings', {}).get('randomize_replies', True):
            tmpl = random.choice(self.reply_messages) if self.reply_messages else "Thanks for sharing!"
            return self.render_reply_template(tmpl)
        # Deterministic path still renders templates so placeholders are expanded
        base = self.reply_messages[0] if self.reply_messages else "Thanks for sharing!"
        return self.render_reply_template(base)

    def render_reply_template(self, template: str) -> str:
        """Render a reply template that may contain placeholders like {{RAN(A|B|C)}}.
        Example: "{{RAN(Hi|Hello)}} {{RAN(❤️ |🤩 |🥳 |If you need help|if you want help)}} send what you need in {{RAN(Dm|DM|D.M|message)}}"
        """
        try:
            if not template or '{{RAN(' not in template:
                return template
            # Pattern to find {{RAN(option1|option2|...)}}
            pat = re.compile(r"\{\{RAN\((.*?)\)\}\}")

            def _pick(m):
                body = m.group(1)
                # Split by | and choose one; keep user-provided spacing/case
                opts = [o for o in body.split('|')]
                if not opts:
                    return ''
                choice = random.choice(opts)
                # As a tiny variation to reduce duplication, collapse multiple spaces randomly (no hidden chars)
                try:
                    choice = re.sub(r"\s{2,}", " ", choice)
                except Exception:
                    pass
                return choice

            # Replace all occurrences iteratively until none left (handles nested cases safely if ever used)
            prev = None
            out = template
            # Safety cap on iterations
            for _ in range(10):
                prev = out
                out = pat.sub(_pick, out)
                if out == prev:
                    break
            return out.strip()
        except Exception as e:
            self.logger.debug(f"render_reply_template error: {e}")
            return template
    
    def is_post_recent(self, post_element):
        """Check if post is recent based on configuration"""
        try:
            max_hours = self.config.get('filters', {}).get('only_reply_to_posts_newer_than_hours', 24)
            # This is a simplified check - you might need to implement more sophisticated time parsing
            return True  # For now, assume all posts are recent
        except Exception:
            return True
    
    def should_skip_comment(self, comment_text, username):
        """Check if comment should be skipped based on filters"""
        filters = self.config.get('filters', {})
        
        # Check minimum length (skip if min_comment_length is not set or is empty)
        min_length = filters.get('min_comment_length')
        if min_length and len(comment_text) < min_length:
            return True, "Comment too short"
        
        # Check skip keywords
        skip_keywords = filters.get('skip_keywords', [])
        for keyword in skip_keywords:
            if keyword.lower() in comment_text.lower():
                return True, f"Contains skip keyword: {keyword}"
        
        # Check skip users
        skip_users = filters.get('skip_users', [])
        if username in skip_users:
            return True, f"User in skip list: {username}"
        
        return False, ""

    # ----- Anti-block and cooldown helpers -----
    def _now(self):
        try:
            return datetime.now()
        except Exception:
            return datetime.utcnow()

    def is_in_cooldown(self):
        """Return remaining seconds if in cooldown, else 0."""
        until = getattr(self, 'cooldown_until', None)
        if not until:
            return 0
        remaining = (until - self._now()).total_seconds()
        return max(0, int(remaining))

    def start_cooldown(self, reason="rate_limit"):
        conf = self.config.get('anti_block', {})
        minutes = conf.get('cooldown_minutes_on_block', 30)
        self.cooldown_until = self._now() + timedelta(minutes=minutes)
        self.logger.warning(f"Entering cooldown for {minutes}m due to {reason}")

    def detect_and_handle_rate_limit_dialog(self):
        """Detect the Facebook rate limit dialog and start cooldown if found. Returns True if detected."""
        try:
            # Look for common dialog container
            dialogs = self.driver.find_elements(By.CSS_SELECTOR, 'div[role="dialog"]')
            for d in dialogs:
                try:
                    text = (d.text or '').lower()
                    if (
                        "you can't use this feature" in text
                        or "we limit how often you can" in text
                        or "try again later" in text
                    ):
                        # Click OK if present to dismiss
                        try:
                            btns = d.find_elements(By.XPATH, ".//*[self::div or self::button][normalize-space()='OK']")
                            if btns:
                                try:
                                    btns[0].click()
                                except Exception:
                                    self.driver.execute_script("arguments[0].click();", btns[0])
                        except Exception:
                            pass
                        self.logger.error("Rate-limit dialog detected. Starting cooldown.")
                        self.start_cooldown("rate_limit_dialog")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False
    
    def process_group_posts(self, group_url):
        """Process all posts in a Facebook group"""
        try:
            self.logger.info(f"Processing group: {group_url}")
            # Store current group info for Telegram notifications
            self.current_group_url = group_url
            self.driver.get(group_url)
            time.sleep(5)

            # Get actual group name from page title
            self.current_group_name = self._scrape_group_name_from_page()
            self.logger.info(f"Group name: {self.current_group_name}")

            # Config limits
            max_posts = self.config.get('bot_settings', {}).get('max_posts_per_group', 10)
            max_scrolls = self.config.get('bot_settings', {}).get('max_scrolls_collect', 30)
            stagnation_threshold = self.config.get('bot_settings', {}).get('stagnation_threshold', 3)

            # Step 1: Collect post permalinks while scrolling
            collected = []
            seen_urls = set()
            last_height = 0
            stagnation = 0
            self.logger.info("Collecting post permalinks by scrolling the feed...")
            for s in range(max_scrolls):
                urls = self.collect_post_permalinks_on_page()
                new = 0
                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        collected.append(u)
                        new += 1
                self.logger.info(f"Scroll {s+1}/{max_scrolls}: collected +{new}, total={len(collected)}")
                if len(collected) >= max_posts:
                    break
                # Scroll down
                try:
                    self.driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.9));")
                except Exception:
                    pass
                time.sleep(self.config.get('bot_settings', {}).get('scroll_pause_seconds', 2))
                # Stagnation detection
                try:
                    height = self.driver.execute_script("return document.body.scrollHeight || document.documentElement.scrollHeight;")
                except Exception:
                    height = None
                if height is not None:
                    if height <= last_height:
                        stagnation += 1
                    else:
                        stagnation = 0
                        last_height = height
                if stagnation >= stagnation_threshold:
                    self.logger.info("No further growth in feed height, stopping collection.")
                    break

            # Final extra pass after small wait to catch late-loaded anchors
            time.sleep(2)
            prev_count = len(collected)
            final_urls = self.collect_post_permalinks_on_page()
            for u in final_urls:
                if u not in seen_urls:
                    seen_urls.add(u)
                    collected.append(u)
            if collected:
                self.logger.info(f"Final pass added {len(collected) - prev_count}; total unique URLs: {len(collected)}")

            # Rescue collection: if still nothing, try a few aggressive passes and per-post resolution
            if not collected:
                self.logger.warning("No post URLs collected after normal passes. Attempting rescue collection...")
                try:
                    # Try a few more deep scrolls with brief pauses
                    for r in range(3):
                        try:
                            self.driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*1.2));")
                        except Exception:
                            pass
                        time.sleep(self.config.get('bot_settings', {}).get('scroll_pause_seconds', 3))
                        extra = self.collect_post_permalinks_on_page()
                        added = 0
                        for u in extra:
                            if u not in seen_urls:
                                seen_urls.add(u)
                                collected.append(u)
                                added += 1
                        self.logger.info(f"Rescue scroll {r+1}/3: collected +{added}, total={len(collected)}")
                        if collected:
                            break
                except Exception:
                    pass

            if not collected:
                # Last resort: directly inspect visible post containers to construct/resolve permalinks
                try:
                    posts = []
                    try:
                        posts.extend(self.driver.find_elements(By.XPATH, "//*[@role='article']"))
                    except Exception:
                        pass
                    try:
                        posts.extend(self.driver.find_elements(By.XPATH, "//div[starts-with(@data-pagelet,'FeedUnit') or contains(@data-pagelet,'FeedUnit')]") )
                    except Exception:
                        pass
                    # Increase readiness wait a bit for this pass
                    self.wait_for_feed_ready(timeout=12)
                    resolved_here = 0
                    for p in posts[:8]:
                        try:
                            ts_el, ts_href = self.find_best_timestamp_link(p)
                            if not ts_href:
                                ts_el, ts_href = self.find_best_timestamp_link_global(p)
                            # Also inspect pcb anchors within the post
                            pcb_href = None
                            if not ts_href:
                                try:
                                    a_links = p.find_elements(By.XPATH, ".//a[contains(@href,'set=pcb.')]")
                                except Exception:
                                    a_links = []
                                for a in a_links[:3]:
                                    h = a.get_attribute('href') or a.get_attribute('data-lynx-uri') or a.get_attribute('ajaxify') or ''
                                    if h and 'set=pcb.' in h:
                                        pcb_href = h
                                        break
                            href = ts_href or pcb_href
                            if href:
                                # Normalize + canonicalize with current group context
                                try:
                                    cur = self.driver.current_url
                                    m_gid = re.search(r"/groups/([^/?#]+)", cur)
                                    gid_ctx = m_gid.group(1) if m_gid else None
                                except Exception:
                                    gid_ctx = None
                                canon = self.canonicalize_post_url(href, gid_ctx)
                                if canon and canon not in seen_urls:
                                    seen_urls.add(canon)
                                    collected.append(canon)
                                    resolved_here += 1
                        except Exception:
                            continue
                    self.logger.info(f"Rescue per-post resolution added {resolved_here} URLs; total={len(collected)}")
                except Exception as e:
                    self.logger.debug(f"Rescue per-post resolution failed: {e}")

            if not collected:
                self.logger.warning("No post URLs collected after all rescue attempts; skipping this group for now.")
                return

            # Step 2: Process each collected URL
            processed_count = 0
            session_seen_post_ids = set()
            for idx, url in enumerate(collected[:max_posts]):
                try:
                    # Enforce cooldown between posts
                    remaining = self.is_in_cooldown()
                    if remaining:
                        self.logger.warning(f"Cooldown active ({remaining}s). Pausing before processing next post.")
                        time.sleep(min(remaining, 600))
                    post_id = self.extract_post_id_from_url(url)
                    if post_id in self.processed_posts:
                        self.logger.info(f"Post {post_id} already processed, skipping")
                        continue
                    if post_id in session_seen_post_ids:
                        self.logger.info(f"Post {post_id} already queued/processed in this session, skipping duplicate")
                        continue
                    session_seen_post_ids.add(post_id)
                    self.logger.info(f"[{idx+1}/{min(len(collected), max_posts)}] Processing post URL: {url}")
                    success = self.process_post_by_url(url, post_id)
                    if success:
                        processed_count += 1
                        self.processed_posts[post_id] = {
                            'processed_at': datetime.now().isoformat(),
                            'group_url': group_url,
                            'post_url': url,
                        }
                        self.save_processed_data(self.processed_posts, 'processed_posts_file')
                    self._jitter_sleep(self.config.get('bot_settings', {}).get('delay_between_posts', 2), jitter_key="post_jitter")
                except Exception as e:
                    self.logger.error(f"Error processing collected URL {url}: {e}")
                    continue

            self.logger.info(f"Processed {processed_count} posts from group via collected URLs")
            
        except Exception as e:
            self.logger.error(f"Error processing group {group_url}: {e}")
        
    def process_post_by_url(self, url, post_id, return_to_feed: bool = True):
        """Open a post by URL and process its comments (root-level). Returns True on success."""
        try:
            self.current_post_url = url
            # Guard against photo set=g.*; canonicalize before navigating
            cur = self.driver.current_url if self.driver else ''
            m_gid = re.search(r"/groups/([^/?#]+)", cur) if cur else None
            gid_ctx = m_gid.group(1) if m_gid else None
            canon = self.canonicalize_post_url(url, gid_ctx)
            if not canon:
                self.logger.warning(f"Skipping navigation: non-canonical or forbidden URL {url}")
                return False
            # Randomize between /posts/ and /permalink/ to reduce repetitive patterns
            variants = self.make_navigation_variants(canon)
            nav_url = random.choice(variants) if len(variants) > 1 else canon
            self.logger.debug(f"Navigating to post via: {nav_url}")
            self.driver.get(nav_url)
            try:
                target_pid = post_id
                def _ok(d):
                    cur = d.current_url or ''
                    if '/posts/' in cur or '/permalink/' in cur:
                        return True
                    if target_pid and target_pid in cur:
                        return True
                    return False
                WebDriverWait(self.driver, 10).until(_ok)
            except Exception:
                time.sleep(2)
            # Delegate comment expansion and processing to the robust shared routine
            ok = self.process_comments_on_current_post_page(post_id)
            # Navigate back to group page unless called from multi-tab mode
            if return_to_feed:
                try:
                    self.driver.back()
                    time.sleep(2)
                except Exception:
                    pass
            return ok
        except Exception as e:
            self.logger.error(f"Error processing post by URL {url}: {e}")
            return False

    def get_post_id(self, post_element):
        """Extract post ID from post element"""
        try:
            # Try to find post ID from various attributes
            post_id = post_element.get_attribute('data-ft')
            if post_id:
                return post_id
            
            # Alternative method
            links = post_element.find_elements(By.TAG_NAME, 'a')
            for link in links:
                href = link.get_attribute('href')
                if href and '/posts/' in href:
                    return href.split('/posts/')[-1].split('?')[0]
            
            return str(hash(post_element.get_attribute('outerHTML')[:100]))
        except Exception:
            return None
    
    def process_post_comments(self, post_element, post_id):
        """Process comments for a specific post by opening it in a popup modal"""
        try:
            self.logger.info(f"Processing comments for post {post_id}")
            
            # Smoothly bring post into full view before interacting
            self.slow_scroll_to(post_element, steps=10, pause=0.2)
            self.ensure_post_fully_visible(post_element)

            # Prefer clicking the visible timestamp link next to the author's name
            ts_el, ts_href = self.find_best_timestamp_link(post_element)
            if ts_el and ts_href:
                try:
                    # Try to click the hyperlink time
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ts_el)
                    time.sleep(0.4)
                    try:
                        ts_el.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", ts_el)
                    self.logger.info(f"Clicked timestamp hyperlink -> {ts_href}")
                    # Wait until URL reflects post page
                    try:
                        WebDriverWait(self.driver, 6).until(lambda d: ('/groups/' in d.current_url and '/posts/' in d.current_url) or d.current_url.startswith(ts_href.split('?')[0]))
                    except Exception:
                        time.sleep(2.0)
                except Exception as e:
                    # Fallback: navigate directly to href
                    self.logger.warning(f"Click timestamp failed, navigating instead: {e}")
                    self.logger.info(f"Opening post via timestamp href: {ts_href}")
                    cur = self.driver.current_url
                    m_gid = re.search(r"/groups/(\d+)", cur) if cur else None
                    gid_ctx = m_gid.group(1) if m_gid else None
                    canon = self.canonicalize_post_url(ts_href, gid_ctx)
                    if not canon:
                        self.logger.warning("Timestamp href resolved to forbidden/non-canonical URL; skipping post")
                        return False
                    self.driver.get(canon)
                    try:
                        WebDriverWait(self.driver, 6).until(lambda d: ('/groups/' in d.current_url and '/posts/' in d.current_url))
                    except Exception:
                        time.sleep(2.0)
            
            # At this point we're navigated to the post page via href
            opened_in_modal = False
            opened_on_page = True
            time.sleep(1)
            
            # Now we're on the post page or in the modal - look for comments
            comment_selectors = [
                'div[role="article"] div[data-testid="comment"]',
                '[data-testid="UFI2Comment/root_depth_0"]',
                'div[aria-label*="Comment by"]',
                'div[data-testid="comment"]',
                'li[role="article"]'
            ]
            
            # Scroll within modal to load more comments
            modal_container = None
            try:
                modal_container = self.driver.find_element(By.CSS_SELECTOR, 'div[role="dialog"]')
                self.logger.info("Found modal container")
            except:
                self.logger.info("Modal container not found, using body for scrolling")
            
            # Scroll slowly within modal to load comments
            for scroll_attempt in range(3):
                if modal_container:
                    self.driver.execute_script("arguments[0].scrollTop += 300;", modal_container)
                else:
                    self.driver.execute_script("window.scrollBy(0, 300);")
                time.sleep(2)
            
            # Find comments in modal/page and keep only top-level (root) comments
            comments = []
            for selector in comment_selectors:
                try:
                    found_comments = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if found_comments:
                        # Filter for actual comments with text and keep only root depth
                        actual_comments = []
                        for comment in found_comments:
                            try:
                                if comment.text and len(comment.text.strip()) > 5 and self.is_top_level_comment(comment):
                                    actual_comments.append(comment)
                            except:
                                continue
                        
                        if actual_comments:
                            self.logger.info(f"Found {len(actual_comments)} comments in modal using selector: {selector}")
                            comments = actual_comments
                            break
                except Exception as e:
                    self.logger.warning(f"Modal comment selector {selector} failed: {e}")
                    continue
            
            if not comments:
                self.logger.info(f"No comments found in modal for post {post_id}")
                self.close_post_modal()
                return True
            
            # Build a unique list of root comments using a stable key
            unique_comments = []  # list of tuples: (element, key, username, text)
            seen_keys = set()
            for c in comments:
                try:
                    # Only main/root comments
                    if not self.is_top_level_comment(c):
                        continue
                    txt = self.get_comment_text(c)
                    usr = self.get_comment_username(c)
                    dom_id = self.get_comment_id(c)
                    key = dom_id or self.make_comment_key(post_id, usr, txt)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    unique_comments.append((c, key, usr, txt))
                except Exception:
                    continue

            replied_count = 0
            session_processed = set()
            max_comments = self.config.get('bot_settings', {}).get('max_comments_per_post', 1000)
            
            self.logger.info(f"Processing {min(len(unique_comments), max_comments)} unique root comments")
            
            for i, (comment, comment_id, username, comment_text) in enumerate(unique_comments[:max_comments]):
                try:
                    if (comment_id not in self.processed_comments) and (comment_id not in session_processed):
                        
                        self.logger.info(f"Processing modal comment {i+1}: {username} - {comment_text[:50]}...")
                        
                        # Skip if no meaningful text found
                        if comment_text in ["No text found", ""] or "Error extracting" in comment_text or len(comment_text.strip()) < 3:
                            self.logger.info(f"Skipping comment {i+1}: No meaningful text")
                            continue
                        
                        # Check if should skip
                        should_skip, skip_reason = self.should_skip_comment(comment_text, username)
                        
                        if should_skip:
                            self.logger.info(f"Skipping comment {comment_id}: {skip_reason}")
                            self.processed_comments[comment_id] = {
                                'processed_at': datetime.now().isoformat(),
                                'action': 'skipped',
                                'reason': skip_reason,
                                'username': username
                            }
                        else:
                            # Scroll comment into view within modal
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", comment)
                                time.sleep(1)
                            except:
                                pass
                            
                            # Reply to comment
                            success = self.reply_to_comment(comment, comment_id, username, comment_text)
                            if success:
                                replied_count += 1
                                # Mark as processed (replied)
                                self.processed_comments[comment_id] = {
                                    'processed_at': datetime.now().isoformat(),
                                    'action': 'replied',
                                    'username': username,
                                    'text': comment_text,
                                    'post_id': post_id
                                }
                                session_processed.add(comment_id)
                            else:
                                # Mark as failed to avoid retry loop in same run
                                self.processed_comments[comment_id] = {
                                    'processed_at': datetime.now().isoformat(),
                                    'action': 'failed',
                                    'username': username,
                                    'text': comment_text,
                                    'post_id': post_id
                                }
                                session_processed.add(comment_id)
                            # Delay between comments
                            delay = self.config.get('bot_settings', {}).get('delay_between_comments', 3)
                            time.sleep(delay)
                        
                        # Persist after each comment so we can skip next time
                        self.save_processed_data(self.processed_comments, 'processed_comments_file')
                    else:
                        self.logger.info(f"Comment {comment_id} already processed")
                    
                except Exception as e:
                    self.logger.error(f"Error processing modal comment {i}: {e}")
                    continue
            
            # Close the view
            if opened_in_modal:
                self.close_post_modal()
            elif opened_on_page:
                # Navigated to post page; go back to group feed
                try:
                    self.driver.back()
                    time.sleep(2)
                except Exception as e:
                    self.logger.warning(f"Error navigating back after processing post: {e}")
            
            self.logger.info(f"Replied to {replied_count} comments in modal for post {post_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error processing post comments in modal: {e}")
            self.close_post_modal()  # Ensure modal is closed even on error
            return False
    
    def close_post_modal(self):
        """Close post modal if it's open"""
        try:
            # Look for X button in modal header
            close_selectors = [
                'div[role="dialog"] [aria-label="Close"]',
                'div[role="dialog"] svg[aria-label="Close"]',
                '[data-testid="modal_close_button"]',
                'div[role="button"][aria-label="Close"]',
                'button[aria-label="Close"]',
                'div[role="dialog"] div[role="button"]',
                '.x1i10hfl.xjbqb8w.x6umtig.x1b1mbwd.xaqea5y.xav7gou.x9f619.x1ypdohk.xt0psk2.xe8uvvx.xdj266r.x11i5rnm.xat24cr.x1mh8g0r.xexx8yu.x4uap5.x18d9i69.xkhd6sd.x16tdsg8.x1hl2dhg.xggy1nq.x1a2a7pz.x1heor9g.xt0b8zv.xo1l8bm'
            ]
            
            for selector in close_selectors:
                try:
                    close_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    self.driver.execute_script("arguments[0].click();", close_button)
                    self.logger.info(f"Closed modal using selector: {selector}")
                    time.sleep(3)
                    return True
                except:
                    continue
            
            # Try clicking on overlay to close modal
            try:
                overlay = self.driver.find_element(By.CSS_SELECTOR, 'div[role="dialog"]')
                # Click on the top-right area where X usually is
                self.driver.execute_script("""
                    var rect = arguments[0].getBoundingClientRect();
                    var x = rect.right - 30;
                    var y = rect.top + 30;
                    var element = document.elementFromPoint(x, y);
                    if (element) element.click();
                """, overlay)
                self.logger.info("Attempted to close modal by clicking top-right area")
                time.sleep(3)
                return True
            except:
                pass
            
            # Try pressing Escape key as fallback
            from selenium.webdriver.common.keys import Keys
            self.driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            time.sleep(3)
            self.logger.info("Closed modal using Escape key")
            return True
            
        except Exception as e:
            self.logger.warning(f"Could not close modal: {e}")
            return False
    
    def _closest_visible(self, base_element, candidates):
        """Return the visible candidate closest to base_element vertically (same comment block)."""
        try:
            visible = [c for c in candidates if c.is_displayed()]
            if not visible:
                return None
            # Use bounding rect center Y distance
            base_rect = self.driver.execute_script(
                "const r=arguments[0].getBoundingClientRect();return {cy:(r.top+r.bottom)/2};",
                base_element,
            )
            best = None
            best_dy = None
            for el in visible:
                try:
                    rect = self.driver.execute_script(
                        "const r=arguments[0].getBoundingClientRect();return {cy:(r.top+r.bottom)/2};",
                        el,
                    )
                    dy = abs((rect or {}).get('cy', 0) - (base_rect or {}).get('cy', 0))
                    if best is None or dy < best_dy:
                        best, best_dy = el, dy
                except Exception:
                    continue
            return best or visible[0]
        except Exception:
            return None

    def _find_reply_button_for_comment(self, comment_element):
        """Find the correct Reply button within the same comment block (top-level)."""
        try:
            # Collect likely reply buttons within this comment block only
            xpath_variants = [
                ".//div[@role='button' and normalize-space()='Reply']",
                ".//*[self::span or self::a or self::div][normalize-space()='Reply']",
                ".//*[@aria-label and contains(translate(@aria-label,'REPLY','reply'),'reply')]",
            ]
            candidates = []
            for xp in xpath_variants:
                try:
                    candidates.extend(comment_element.find_elements(By.XPATH, xp))
                except Exception:
                    continue
            # Filter out elements that belong to nested reply items by ensuring they are descendants of comment_element
            if not candidates:
                return None
            btn = self._closest_visible(comment_element, candidates)
            return btn
        except Exception:
            return None

    def _closest_reply_input_for_comment(self, comment_element):
        """Return the contenteditable reply input that is visually closest (below) to this comment.
        This prevents typing into a previously opened reply box.
        """
        try:
            # Collect possible composers globally (FB uses contenteditable divs)
            selectors = [
                "div[contenteditable='true'][role='textbox']",
                "div[contenteditable='true']",
                "[data-testid='ufi_reply_composer'][contenteditable='true']",
            ]
            candidates = []
            for sel in selectors:
                try:
                    els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                except Exception:
                    els = []
                for e in els:
                    try:
                        if e.is_displayed():
                            candidates.append(e)
                    except Exception:
                        continue

            if not candidates:
                return None

            # Use JS to score by geometric distance from the bottom-center of comment to the top-center of input
            script = """
                const comment = arguments[0];
                const inputs = arguments[1];
                const cr = comment.getBoundingClientRect();
                const cy = (cr.top + cr.bottom) / 2;
                const cx = (cr.left + cr.right) / 2;
                let best = null;
                for (const el of inputs) {
                    try {
                        const r = el.getBoundingClientRect();
                        const iy = (r.top + r.bottom) / 2;
                        const ix = (r.left + r.right) / 2;
                        // Prefer inputs that are near and below the comment (positive dy)
                        const dy = iy - cy;
                        const dx = Math.abs(ix - cx);
                        // Penalize inputs that are far above
                        const score = (dy >= -40 ? 1000 : 0) - Math.abs(dy) - dx * 0.2;
                        if (!best || score > best.score) {
                            best = {el, score};
                        }
                    } catch {}
                }
                return best ? best.el : null;
            """
            best = self.driver.execute_script(script, comment_element, candidates)
            return best
        except Exception:
            return None

    def _find_reply_composer_for_comment(self, comment_element, username: str, timeout: float = 6.0):
        """Find a reply composer that belongs to this comment.
        Strategy:
        1) Prefer a composer inside the comment subtree.
        2) Else, choose the nearest composer below the comment whose aria-label/placeholder references the username.
        3) As a last resort, choose the closest-visible composer but only if it's within 600px vertically below the comment.
        Returns the WebElement or None.
        """
        end_time = time.time() + max(0.5, timeout)
        last_candidate = None
        while time.time() < end_time:
            try:
                # Collect visible candidates
                sels = [
                    "div[contenteditable='true'][role='textbox']",
                    "div[contenteditable='true']",
                    "[data-testid='ufi_reply_composer'][contenteditable='true']",
                ]
                cand = []
                for sel in sels:
                    try:
                        for e in self.driver.find_elements(By.CSS_SELECTOR, sel):
                            try:
                                if e.is_displayed():
                                    cand.append(e)
                            except Exception:
                                continue
                    except Exception:
                        continue
                if not cand:
                    time.sleep(0.2)
                    continue

                # 1) Prefer descendants of the comment element
                inside = []
                try:
                    inside = [e for e in cand if self.driver.execute_script("return arguments[0].contains(arguments[1])", comment_element, e)]
                except Exception:
                    inside = []
                if inside:
                    last_candidate = inside[0]
                    return last_candidate

                # 2) Prefer composer that references username in aria-label or placeholder
                uname = (username or '').strip().lower()
                labeled = []
                for e in cand:
                    try:
                        aria = (e.get_attribute('aria-label') or '').lower()
                        ph = (e.get_attribute('placeholder') or '').lower()
                        if uname and (uname in aria or uname in ph):
                            labeled.append(e)
                    except Exception:
                        continue
                if labeled:
                    # If multiple, choose closest geometrically
                    target = self._closest_reply_input_for_comment(comment_element)
                    if target in labeled:
                        last_candidate = target
                        return last_candidate
                    last_candidate = labeled[0]
                    return last_candidate

                # 3) Nearest below within a reasonable distance
                nearest = self._closest_reply_input_for_comment(comment_element)
                if nearest:
                    try:
                        dy = self.driver.execute_script(
                            "const c=arguments[0].getBoundingClientRect();const i=arguments[1].getBoundingClientRect();return (i.top+i.bottom)/2 - (c.top+c.bottom)/2;",
                            comment_element,
                            nearest,
                        )
                    except Exception:
                        dy = 9999
                    if dy is None or dy > -40:  # not far above
                        last_candidate = nearest
                        return last_candidate

                time.sleep(0.2)
            except Exception:
                time.sleep(0.2)
                continue
        return last_candidate

    def get_comment_id(self, comment_element):
        """Extract comment ID"""
        try:
            # 1) Direct element id
            cid = comment_element.get_attribute('id')
            if cid:
                return cid
            # 2) Look for permalink anchors that carry comment_id param
            try:
                anchors = comment_element.find_elements(By.TAG_NAME, 'a')
                for a in anchors:
                    href = a.get_attribute('href') or ''
                    if 'comment_id=' in href:
                        # extract number after comment_id=
                        part = href.split('comment_id=')[-1]
                        part = part.split('&')[0]
                        if part:
                            return f"c_{part}"
                    if '/permalink/' in href and '/comment/' in href:
                        return href
            except Exception:
                pass
            # 3) data-ft sometimes has json-like content
            df = comment_element.get_attribute('data-ft')
            if df:
                return df
            # 4) Fallback: stable hash of key fields
            snippet = (comment_element.text or '')[:80]
            outer = (comment_element.get_attribute('outerHTML') or '')[:200]
            return f"h_{hash(snippet + outer)}"
        except:
            return None

    def make_comment_key(self, post_id, username, comment_text):
        """Build a stable, cross-run key for a comment using author+text+post."""
        try:
            base = f"{post_id}|{(username or '').strip().lower()}|{(comment_text or '').strip().lower()}"
            h = hashlib.sha1(base.encode('utf-8')).hexdigest()[:16]
            return f"k_{h}"
        except Exception:
            return f"k_{hash((post_id, username, comment_text))}"
    
    def get_comment_text(self, comment_element):
        """Extract comment text"""
        try:
            # Known noise tokens we should ignore
            noise_tokens = {"like", "reply", "share", "·"}

            # Try multiple selectors for comment body text (avoid author header)
            text_selectors = [
                '[data-ad-preview="message"]',
                '[data-testid="UFI2Comment/body"]',
                'div[dir="auto"]',
                'span[dir="auto"]',
                '.x193iq5w',  # often comment text
            ]

            # If we can determine username early, we can filter it out from candidates
            try:
                username = self.get_comment_username(comment_element)
            except Exception:
                username = None

            best = ""
            for selector in text_selectors:
                try:
                    text_elements = comment_element.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    text_elements = []
                for el in text_elements:
                    try:
                        # Skip obvious author links/headers
                        if el.tag_name.lower() == 'a':
                            continue
                        t = (el.text or '').strip()
                        if not t:
                            continue
                        tl = t.lower()
                        if username and t == username:
                            continue
                        if tl in noise_tokens:
                            continue
                        if re.match(r"^\d+\s*(m|h|d|w)$", tl) or 'ago' in tl:
                            continue
                        # Prefer the longest meaningful candidate
                        if len(t) > len(best):
                            best = t
                    except Exception:
                        continue
                if best:
                    break

            if best:
                return best

            # Fallback: get any text from the comment element and strip username prefix if present
            raw = (comment_element.text or '').strip()
            if not raw:
                return "No text found"
            if username and raw.startswith(username):
                raw = raw[len(username):].lstrip(" \t\n:-—|·")
            return raw if raw else "No text found"
            
        except Exception as e:
            return f"Error extracting text: {str(e)[:50]}"

    def get_comment_username(self, comment_element):
        """Extract the commenter's display name, excluding timestamp anchors like '58m'/'6h'."""
        try:
            # 1) Prefer strong/h3 wrappers commonly used for author
            try:
                el = comment_element.find_element(By.XPATH, ".//strong//a | .//h3//a")
                txt = (el.text or '').strip()
                if txt and not re.match(r"^\d+\s*(m|h|d|w)$", txt.lower()):
                    return txt
            except Exception:
                pass

            # 2) Profile-like anchors; exclude timestamps and comment/permalink links
            anchors = []
            try:
                anchors = comment_element.find_elements(
                    By.XPATH,
                    ".//a[(starts-with(@href,'/profile.php') or contains(@href,'/people/') or contains(@href,'/user/') or contains(@href,'facebook.com'))]"
                )
            except Exception:
                anchors = []
            for a in anchors:
                try:
                    href = (a.get_attribute('href') or '').lower()
                    label = (a.get_attribute('aria-label') or a.text or '').strip().lower()
                    # Skip non-user links
                    if ('/groups/' in href and '/posts/' in href) or ('/permalink/' in href):
                        continue
                    if 'comment_id=' in href or '/comment/' in href or '/comments/' in href:
                        continue
                    if re.match(r"^\d+\s*(m|h|d|w)$", label) or 'ago' in label:
                        continue
                    try:
                        # If it contains a <time>, it's a timestamp
                        a.find_element(By.XPATH, './/time')
                        continue
                    except Exception:
                        pass
                    txt = (a.text or '').strip()
                    if txt and not re.match(r"^\d+\s*(m|h|d|w)$", txt.lower()):
                        return txt
                except Exception:
                    continue

            # 3) Fallback: first non-empty anchor text that isn't time-like
            try:
                any_as = comment_element.find_elements(By.TAG_NAME, 'a')
            except Exception:
                any_as = []
            for a in any_as:
                txt = (a.text or '').strip()
                if txt and not re.match(r"^\d+\s*(m|h|d|w)$", txt.lower()) and 'ago' not in txt.lower():
                    return txt
            return "Unknown"
        except Exception:
            return "Unknown"

    def is_top_level_comment(self, comment_element):
        """Return True if this is a main (root) comment, not a reply."""
        try:
            # Prefer explicit depth hint
            dt = comment_element.get_attribute('data-testid') or ''
            if 'root_depth_0' in dt:
                return True
            if 'root_depth_' in dt and 'root_depth_0' not in dt:
                return False
            # Check aria-label text patterns
            aria = (comment_element.get_attribute('aria-label') or '').lower()
            if 'reply to' in aria or 'rispondi a' in aria:
                return False
            # Heuristic: replies often have a thread line and are nested under a container that already is a comment
            # If this comment has a descendant "View reply" link, it is usually a root comment
            try:
                vr = comment_element.find_elements(By.XPATH, ".//*[normalize-space()='View reply' or normalize-space()='View 1 reply' or contains(normalize-space(),'View') and contains(normalize-space(),'repl')]")
                if vr:
                    return True
            except Exception:
                pass
            # Fallback: if it contains a timestamp + Like | Reply | Share row, consider it root
            try:
                actions = comment_element.find_elements(By.XPATH, ".//*[normalize-space()='Like' or normalize-space()='Reply' or normalize-space()='Share']")
                if actions:
                    # If parent of those actions is same level as comment, assume root
                    return True
            except Exception:
                pass
            # Default to True to avoid missing main comments, but we will still de-duplicate by ID
            return True
        except Exception:
            return True

    def reply_to_comment(self, comment_element, comment_key, username, comment_text):
        """Reply to a specific comment"""
        self.logger.info(f"Attempting to reply to {username}'s comment: {comment_text[:50]}...")
        # Respect cooldown
        remaining = self.is_in_cooldown()
        if remaining:
            self.logger.warning(f"In cooldown for {remaining}s; skipping reply attempt")
            return False
        
        # Find Reply button anchored to this comment only
        reply_clicked = False
        try:
            reply_button = self._find_reply_button_for_comment(comment_element)
            if reply_button:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", reply_button)
                except Exception:
                    pass
                time.sleep(0.3)
                # Blur any currently active input to avoid typing into a previous composer
                try:
                    self.driver.execute_script("if(document.activeElement){document.activeElement.blur();}")
                except Exception:
                    pass
                try:
                    reply_button.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", reply_button)
                self.logger.info("Clicked Reply button")
                reply_clicked = True
            else:
                self.logger.warning("No reply button found within comment block")
        except Exception as e:
            self.logger.warning(f"Error finding reply button: {e}")

        if not reply_clicked:
            self.logger.warning("Could not find reply button")
            return False

        time.sleep(2)  # Wait for reply box to appear

        # Locate the correct reply input belonging to this comment (strict)
        # 1) Prefer a composer INSIDE the comment subtree.
        reply_input = None
        try:
            def _find_inside(drv):
                try:
                    sels = [
                        "div[contenteditable='true'][role='textbox']",
                        "div[contenteditable='true']",
                        "[data-testid='ufi_reply_composer'][contenteditable='true']",
                    ]
                    cand = []
                    for sel in sels:
                        try:
                            for e in drv.find_elements(By.CSS_SELECTOR, sel):
                                try:
                                    if e.is_displayed() and drv.execute_script("return arguments[0].contains(arguments[1])", comment_element, e):
                                        cand.append(e)
                                except Exception:
                                    continue
                        except Exception:
                            continue
                    return cand[0] if cand else None
                except Exception:
                    return None
            reply_input = WebDriverWait(self.driver, 4).until(lambda d: _find_inside(d))
        except Exception:
            reply_input = None

        # 2) If none inside, try labeled with username or nearest below as fallback
        if not reply_input:
            uname_l = (username or '').strip().lower()
            try:
                def _find_labeled_or_nearest(drv):
                    try:
                        sels = [
                            "div[contenteditable='true'][role='textbox']",
                            "div[contenteditable='true']",
                            "[data-testid='ufi_reply_composer'][contenteditable='true']",
                        ]
                        cand = []
                        for sel in sels:
                            try:
                                for e in drv.find_elements(By.CSS_SELECTOR, sel):
                                    try:
                                        if e.is_displayed():
                                            cand.append(e)
                                    except Exception:
                                        continue
                            except Exception:
                                continue
                        if not cand:
                            return None
                        labeled = []
                        for e in cand:
                            try:
                                aria = (e.get_attribute('aria-label') or '').lower()
                                ph = (e.get_attribute('placeholder') or '').lower()
                                if uname_l and (uname_l in aria or uname_l in ph):
                                    labeled.append(e)
                            except Exception:
                                continue
                        if labeled:
                            target = self._closest_reply_input_for_comment(comment_element)
                            return target if target in labeled else labeled[0]
                        # else fallback to nearest below
                        return self._closest_reply_input_for_comment(comment_element)
                    except Exception:
                        return None
                reply_input = WebDriverWait(self.driver, 3).until(lambda d: _find_labeled_or_nearest(d))
            except Exception:
                reply_input = self._find_reply_composer_for_comment(comment_element, username)

        if not reply_input:
            self.logger.error("Could not find reply input field")
            return False
        
        # Get random reply message and sanitize to BMP-safe (avoid ChromeDriver emoji crash)
        reply_message = self.get_random_reply()
        try:
            reply_message_safe = ''.join(ch for ch in reply_message or '' if ord(ch) <= 0xFFFF)
        except Exception:
            reply_message_safe = reply_message or ''
        
        # Clear and type reply with better handling
        try:
            # Scroll the input into view first
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", reply_input)
            time.sleep(1)
            
            # Try clicking with JavaScript to avoid interception
            self.driver.execute_script("arguments[0].focus();", reply_input)
            time.sleep(1)
            # Ensure focus really moved to this input
            try:
                is_focused = self.driver.execute_script("return document.activeElement===arguments[0]", reply_input)
                if not is_focused:
                    try:
                        reply_input.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", reply_input)
                    time.sleep(0.3)
            except Exception:
                pass

            # Wait until the active element is the intended composer or inside this comment
            try:
                def _active_is_bound(drv):
                    try:
                        ae = drv.execute_script("return document.activeElement")
                        if ae is None:
                            return False
                        if ae == reply_input:
                            return True
                        return drv.execute_script("return arguments[0].contains(arguments[1])", comment_element, ae)
                    except Exception:
                        return False
                WebDriverWait(self.driver, 5).until(lambda d: _active_is_bound(d))
            except Exception:
                # Force focus again as fallback
                try:
                    self.driver.execute_script("arguments[0].focus();", reply_input)
                except Exception:
                    pass

            # Validate that this composer is for the intended user when possible
            try:
                aria = (reply_input.get_attribute('aria-label') or '').lower()
                ph = (reply_input.get_attribute('placeholder') or '').lower()
                uname = (username or '').strip().lower()
                if uname and (('reply' in aria and uname not in aria) and (uname not in ph)):
                    self.logger.warning(f"Composer does not reference target user '{username}'. Retrying binder...")
                    # Try clicking Reply again to bind the correct composer
                    try:
                        rb = self._find_reply_button_for_comment(comment_element)
                        if rb:
                            try:
                                rb.click()
                            except Exception:
                                self.driver.execute_script("arguments[0].click();", rb)
                            time.sleep(0.8)
                            reply_input = self._find_reply_composer_for_comment(comment_element, username)
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Append text to the composer without clearing existing content
            inserted = ''
            try:
                inserted = self.driver.execute_script(
                    r"""
                    var el = arguments[0];
                    var text = arguments[1];
                    try { el.focus(); } catch(e) {}
                    try {
                        var cur = (el.innerText||el.textContent||'');
                        if (cur && !/\s$/.test(cur)) { text = ' ' + text; }
                        el.appendChild(document.createTextNode(text));
                    } catch(e) { try { el.textContent += (' ' + text); } catch(_) {} }
                    try { el.dispatchEvent(new InputEvent('input', {bubbles:true})); } catch(e) {}
                    return (el && (el.innerText||el.textContent||''));
                    """,
                    reply_input,
                    reply_message_safe,
                )
            except Exception:
                pass

            # If still not present, fall back to a single send_keys that APPENDS (no select-all)
            if not inserted or (reply_message.strip() and reply_message.strip() not in (inserted or '')):
                reply_input.send_keys(reply_message_safe if (inserted or '').endswith(' ') else ' ' + reply_message_safe)
                time.sleep(0.4)

            # Try clicking a Send button in the composer first (more reliable than Enter)
            sent_by_button = False
            try:
                reply_area = reply_input.find_element(By.XPATH, "./ancestor::div[contains(@class, 'comment') or contains(@role, 'group')]")
            except Exception:
                reply_area = None
            if reply_area is not None:
                try:
                    send_candidates = reply_area.find_elements(By.XPATH, ".//div[@role='button' and (contains(., 'Send') or contains(@aria-label, 'Send') or contains(@aria-label,'Reply'))]")
                except Exception:
                    send_candidates = []
                for btn in send_candidates:
                    try:
                        if btn.is_displayed():
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            except Exception:
                                pass
                            try:
                                btn.click()
                            except Exception:
                                self.driver.execute_script("arguments[0].click();", btn)
                            sent_by_button = True
                            break
                    except Exception:
                        continue

                # Before sending, ensure composer has non-empty text to avoid blank replies
                try:
                    composer_text_now = self.driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trim()", reply_input) or ''
                except Exception:
                    composer_text_now = ''
                if not composer_text_now:
                    self.logger.warning("Composer is empty after insertion; aborting send to avoid blank reply")
                    return False

            if not sent_by_button:
                # Send by pressing Enter (primary path)
                reply_input.send_keys(Keys.ENTER)
                time.sleep(1.1)
            
            # Detect if still unsent (composer still focused or text not cleared)
            try:
                still_focused = self.driver.execute_script("return document.activeElement===arguments[0]", reply_input)
            except Exception:
                still_focused = False
            # Read the text from the composer via JS (more accurate than WebElement.text right away)
            try:
                current_text = self.driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trim()", reply_input) or ''
            except Exception:
                current_text = ''
            submitted = not still_focused and current_text == ''
            
        except Exception as e:
            self.logger.warning(f"Error typing in reply input: {e}")
            # Fallback: append without selecting existing content (strip non-BMP)
            try:
                # If current text doesn't end with space, prepend one before appending
                try:
                    cur = self.driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trimEnd()", reply_input) or ''
                except Exception:
                    cur = ''
                spacer = '' if (cur.endswith(' ') or cur == '') else ' '
                reply_input.send_keys(spacer + reply_message_safe)
                time.sleep(0.4)
                reply_input.send_keys(Keys.ENTER)
                time.sleep(1)
                submitted = True
            except Exception:
                submitted = False
        
        self.logger.info(f"Typed reply message: {reply_message}")
        
        # Fallback submit only if Enter likely failed
        if not submitted:
            try:
                # Look for submit button in the reply area
                reply_area = reply_input.find_element(By.XPATH, "./ancestor::div[contains(@class, 'comment') or contains(@role, 'group')]")
                send_candidates = reply_area.find_elements(By.XPATH, ".//div[@role='button' and (contains(., 'Send') or contains(@aria-label, 'Send'))]")
                for btn in send_candidates:
                    if btn.is_displayed():
                        try:
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        except Exception:
                            pass
                        try:
                            btn.click()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", btn)
                        submitted = True
                        break
            except Exception:
                pass
            try:
                reply_input.send_keys(Keys.ENTER)
                self.logger.info("Sent reply using Enter key")
                submitted = True
            except Exception as e:
                self.logger.warning(f"Error sending Enter key: {e}")
        
        # Last resort: try Ctrl+Enter
        if not submitted:
            try:
                reply_input.send_keys(Keys.CONTROL + Keys.ENTER)
                self.logger.info("Sent reply using Ctrl+Enter")
                submitted = True
            except Exception as e:
                self.logger.warning(f"Error sending Ctrl+Enter: {e}")
        
        # Check for rate-limit dialog right after attempting to send
        if self.detect_and_handle_rate_limit_dialog():
            self.logger.error("Reply blocked by rate-limit. Entering cooldown and aborting this comment.")
            return False

        if submitted:
            time.sleep(2)  # Wait for reply to post

            # Blur the composer so the next reply doesn't reuse it (avoid ESC which can close the post)
            try:
                self.driver.execute_script("arguments[0].blur();", reply_input)
            except Exception:
                pass

            # Step 1: Check for explicit Facebook error status (Declined / Pending dialogs)
            reply_status = self._check_reply_status(comment_element, username, reply_message)

            # Step 2: Try to verify reply appearance in the DOM
            verified = self._verify_reply_posted(comment_element, username, reply_message)

            # Step 3: Reconcile — decision table:
            #   declined / pending  → trust Facebook's explicit signal
            #   sent + verified     → ✅ confirmed sent
            #   sent + not verified → ⚠️ unconfirmed (submitted but reply not found in DOM;
            #                          Facebook may have silently dropped it)
            #   failed (shouldn't reach here; handled below)
            if reply_status in ("declined", "pending"):
                final_status = reply_status
            elif verified:
                final_status = "sent"
            else:
                # Submitted but DOM verification failed — Facebook can silently drop replies
                # without showing any error dialog. Mark as unconfirmed so the operator knows.
                final_status = "unconfirmed"
                self.logger.warning(
                    f"Reply to {username} submitted but NOT found in DOM — "
                    f"marking as UNCONFIRMED (possible silent drop by Facebook). "
                    f"reply='{reply_message[:60]}'"
                )

            # Step 4: Persist with correct final status
            self.processed_comments[comment_key] = {
                'processed_at': datetime.now().isoformat(),
                'action': 'replied' if final_status == 'sent' else ('unconfirmed' if final_status == 'unconfirmed' else final_status),
                'username': username,
                'comment_text': comment_text[:100],
                'reply_message': reply_message,
                'reply_status': final_status,
                'verified': verified,
            }

            # Step 5: Send Telegram notification with correct final status
            self._send_reply_telegram_notification(username, comment_text, reply_message, final_status)

            # Log the reply to the shared dashboard DB — once, here, at the real
            # reply moment. Counted per session (not from len(processed_comments)).
            try:
                acc = str(getattr(self, 'account_number', '') or '').strip()
                if final_status == 'sent':
                    self.session_replies += 1
                    _cr_tracker.log_event("reply_sent", account_name=acc)
                elif final_status in ('failed', 'declined'):
                    self.session_failures += 1
                    _cr_tracker.log_event("reply_failed", account_name=acc)
            except Exception:
                pass

            status_emoji = {"declined": "❌", "pending": "⏳", "sent": "✅", "failed": "❌", "unconfirmed": "⚠️"}.get(final_status, "⚠️")
            verified_tag = " (verified)" if verified and final_status == "sent" else ""
            self.logger.info(f"{status_emoji} Reply to {username}: '{reply_message}' - Status: {final_status.upper()}{verified_tag}")
            print(f"{status_emoji} Replied to {username}: '{reply_message}' - Status: {final_status.upper()}{verified_tag}")
            return final_status not in ("failed", "declined")
        else:
            self.logger.error("Could not submit reply")
            # Send Telegram notification for failed reply
            self._send_reply_telegram_notification(username, comment_text, reply_message, "failed")
            try:
                self.session_failures += 1
                _cr_tracker.log_event("reply_failed", account_name=str(getattr(self, 'account_number', '') or '').strip())
            except Exception:
                pass
            return False

    def _check_reply_status(self, comment_element, username, reply_message):
        """Check if reply was sent, declined, or is pending. Returns: 'sent', 'declined', 'pending'"""
        try:
            # Look for status indicators in the reply area
            # Common patterns: "Declined", "Pending", "See feedback", "Learn more"
            status_indicators = [
                ("declined", ["declined", "see feedback"]),
                ("pending", ["pending", "learn more"]),
            ]

            # Search within the comment subtree for any status text
            for status_type, keywords in status_indicators:
                for keyword in keywords:
                    try:
                        # Look for elements containing the status keyword
                        xpath = f".//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword}')]"
                        elements = comment_element.find_elements(By.XPATH, xpath)
                        for el in elements:
                            if el.is_displayed():
                                text = (el.text or "").lower()
                                # Check if this is a reply status (not a comment text)
                                if keyword in text and username.lower() in text:
                                    return status_type
                    except Exception:
                        continue

            # Additional check: look for reply from current user and check its status
            try:
                # Find all reply elements under this comment
                reply_elements = comment_element.find_elements(By.XPATH, ".//div[contains(@class, 'reply')] | .//div[contains(@role, 'article')]")
                for reply in reply_elements:
                    try:
                        reply_text = (reply.text or "").lower()
                        # Check if this is our reply
                        if reply_message.lower() in reply_text:
                            # Check for status indicators near this reply
                            for status_type, keywords in status_indicators:
                                for keyword in keywords:
                                    if keyword in reply_text:
                                        return status_type
                    except Exception:
                        continue
            except Exception:
                pass

            return "sent"
        except Exception as e:
            self.logger.debug(f"Could not check reply status: {e}")
            return "sent"

    def _verify_reply_posted(self, comment_element, username, reply_message):
        """Verify that reply was actually posted.
        Facebook renders bot replies as siblings/children OUTSIDE the original comment's
        subtree, so we must search both inside AND globally on the page.
        Returns True if reply is confirmed posted, False otherwise."""
        try:
            # Wait a moment for the reply to appear in the DOM
            time.sleep(2)

            # Build keyword list — keep even very short words like "dm", "hi"
            # but skip common stop-words that appear everywhere
            STOPWORDS = {'ran', 'the', 'and', 'for', 'to', 'in', 'a', 'an', 'of', 'or', 'at'}
            original_words = reply_message.lower().split()
            reply_words = [w for w in original_words if len(w) >= 2 and w not in STOPWORDS]
            if not reply_words:
                reply_words = original_words  # keep everything as last resort

            def _words_match(text, words, threshold):
                """Return True if at least `threshold` of `words` appear in `text`."""
                tl = text.lower()
                return sum(1 for w in words if w in tl) >= threshold

            def _threshold(word_list):
                """Dynamic threshold: 1 for ≤3 words, 2 for ≤6, else 3."""
                n = len(word_list)
                if n <= 3:
                    return 1
                if n <= 6:
                    return 2
                return 3

            thresh = _threshold(reply_words)

            # ── Method A: Search INSIDE the comment subtree ──────────────────────
            # Covers cases where Facebook does nest the reply inside the same article
            try:
                inner_text = (comment_element.text or "").lower()
                if _words_match(inner_text, reply_words, thresh):
                    self.logger.debug("Verified reply via comment subtree full-text")
                    return True
            except Exception:
                pass

            # ── Method B: Search SIBLING / PARENT containers on the page ─────────
            # Facebook typically renders: [original comment article] [reply articles as siblings]
            # Walk up a few ancestors and scan their full text
            try:
                for levels in range(1, 6):
                    ancestor_xpath = "/".join([".."] * levels)
                    try:
                        ancestor = comment_element.find_element(By.XPATH, ancestor_xpath)
                        anc_text = (ancestor.text or "").lower()
                        if _words_match(anc_text, reply_words, thresh):
                            self.logger.debug(f"Verified reply via ancestor level {levels}")
                            return True
                    except Exception:
                        break
            except Exception:
                pass

            # ── Method C: Global page search for reply text ──────────────────────
            # Look for any [role=article] on the page that contains our reply words
            try:
                articles = self.driver.find_elements(By.XPATH, "//*[@role='article']")
                for art in articles:
                    try:
                        art_text = (art.text or "").lower()
                        if _words_match(art_text, reply_words, thresh):
                            self.logger.debug("Verified reply via global article scan")
                            return True
                    except Exception:
                        continue
            except Exception:
                pass

            # ── Method D: Global page source check (fast, no element overhead) ───
            try:
                page_text = self.driver.execute_script(
                    "return document.body ? document.body.innerText : ''") or ""
                page_text_lower = page_text.lower()
                # Require reply words AND the username to appear together somewhere
                if username.lower() in page_text_lower and _words_match(page_text_lower, reply_words, thresh):
                    self.logger.debug("Verified reply via page body text")
                    return True
            except Exception:
                pass

            # ── Method E: Composer cleared = reply was submitted ─────────────────
            # If we reach here all DOM checks failed, but if Facebook accepted the
            # keypress (composer text was cleared), treat it as sent.
            # This is checked by the caller via `submitted` already, but we add one
            # extra check: if there is NO composer currently visible for this comment,
            # it means focus left → reply was submitted.
            try:
                composers = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "div[contenteditable='true'][role='textbox'], div[contenteditable='true']"
                )
                active_composers = [c for c in composers if c.is_displayed()]
                if not active_composers:
                    self.logger.debug("Verified reply via no active composer (reply box closed)")
                    return True
            except Exception:
                pass

            self.logger.warning(
                f"Could not DOM-verify reply to {username} — "
                f"reply='{reply_message[:50]}' words={reply_words} thresh={thresh}"
            )
            return False
        except Exception as e:
            self.logger.debug(f"Error verifying reply: {e}")
            return False

    def _extract_group_name_from_url(self, group_url):
        """Extract group name from Facebook group URL. Returns group name or 'Unknown Group'"""
        try:
            if not group_url:
                return "Unknown Group"

            # Try to extract from /groups/NAME/ pattern
            import re
            match = re.search(r'/groups/([^/?#]+)', group_url)
            if match:
                name = match.group(1)
                # Clean up the name (replace dashes/underscores with spaces, title case)
                name = name.replace('-', ' ').replace('_', ' ')
                # Remove any query parameters that might have been included
                name = name.split('?')[0].split('#')[0]
                return name.title()

            # Try to extract from permalink pattern
            match = re.search(r'facebook\.com/groups/([^/?#]+)', group_url)
            if match:
                name = match.group(1).replace('-', ' ').replace('_', ' ')
                return name.title()

            return "Unknown Group"
        except Exception as e:
            self.logger.debug(f"Could not extract group name from URL: {e}")
            return "Unknown Group"

    def _scrape_group_name_from_page(self):
        """Scrape the actual group name from the Facebook page after loading.
        Returns group name string."""
        try:
            # Method 1: Look for h1 with group name (common in Facebook group pages)
            try:
                h1_elements = self.driver.find_elements(By.XPATH, "//h1")
                for h1 in h1_elements:
                    text = (h1.text or "").strip()
                    if text and len(text) > 2 and len(text) < 100:
                        # Check if it's not just "Facebook" or other generic text
                        if text.lower() not in ['facebook', 'home', 'groups']:
                            return text
            except Exception:
                pass

            # Method 2: Look for title element and parse group name
            try:
                title = self.driver.title or ""
                # Facebook titles usually: "Group Name | Facebook" or "Group Name"
                if "|" in title:
                    name = title.split("|")[0].strip()
                    if name and name.lower() != "facebook":
                        return name
                elif title and title.lower() != "facebook":
                    return title.strip()
            except Exception:
                pass

            # Method 3: Look for specific Facebook group header selectors
            try:
                selectors = [
                    "//div[@role='main']//h1",
                    "//div[contains(@class, 'x1heor9g')]//h1",  # FB group header
                    "//span[contains(@class, 'x193iq5w') and string-length()>2]",
                    "//a[contains(@href, '/groups/')]//h1",
                ]
                for selector in selectors:
                    try:
                        elements = self.driver.find_elements(By.XPATH, selector)
                        for el in elements:
                            text = (el.text or "").strip()
                            if text and len(text) > 2 and len(text) < 100:
                                if text.lower() not in ['facebook', 'home', 'groups', 'menu']:
                                    return text
                    except Exception:
                        continue
            except Exception:
                pass

            # Method 4: Try to get from meta tags
            try:
                meta = self.driver.find_element(By.XPATH, "//meta[@property='og:title']")
                content = meta.get_attribute("content") or ""
                if content:
                    return content.strip()
            except Exception:
                pass

            # Fallback: use URL-based extraction
            return self._extract_group_name_from_url(self.current_group_url)
        except Exception as e:
            self.logger.debug(f"Could not scrape group name from page: {e}")
            return self._extract_group_name_from_url(self.current_group_url)

    def _send_reply_telegram_notification(self, username, comment_text, reply_message, status):
        try:
            group_name = getattr(self, 'current_group_name', None) or "Unknown Group"
            group_url = getattr(self, 'current_group_url', None) or "Unknown Link"
            if not hasattr(self, 'recent_replies'):
                self.recent_replies = []
            self.recent_replies.insert(0, {
                'time': datetime.now().strftime('%H:%M:%S'),
                'username': username,
                'status': status,
            })
            self.recent_replies = self.recent_replies[:10]
            
            total_posts = len(self.processed_posts)
            total_comments = len(self.processed_comments)
            replied_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'replied'])
            skipped_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'skipped'])
            
            post_url = getattr(self, 'current_post_url', None) or "Unknown Post Link"
            event_text = (
                f"✅ <b>Replied to</b> {username} in <a href=\"{group_url}\">{group_name}</a>\n"
                f"     📮 <b>Post:</b> <a href=\"{post_url}\">Click here to view Post</a>\n"
                f"     💬 <b>User Comment:</b> \"<i>{comment_text}</i>\"\n"
                f"     📝 <b>Reply Sent:</b> \"<i>{reply_message}</i>\""
            )
            
            if not hasattr(self, 'telemetry_events'):
                self.telemetry_events = []
            self.telemetry_events.insert(0, event_text)
            self.telemetry_events = self.telemetry_events[:3]
            
            self._save_telemetry(
                status=f"Active - processing group comments",
                stats={
                    "posts": total_posts,
                    "comments": total_comments,
                    "replied": replied_comments,
                    "skipped": skipped_comments
                },
                recent_events=self.telemetry_events
            )
        except Exception as e:
            self.logger.debug(f"Telemetry reply notice failed: {e}")

    def run_bot(self, account_number):
        """Main bot execution function"""
        try:
            self.account_number = str(account_number)
            print(f"\n🤖 Starting Facebook Comment Reply Bot")
            print(f"📱 Using account: {account_number}")
            
            if not self.setup_driver():
                print("❌ Failed to setup browser driver")
                return False
            
            # Try to load cookies with retry logic
            account_fname = f"{account_number}_cookies.json"
            if not self.load_cookies(account_number):
                print("❌ Initial login failed - starting recovery process...")
                self.handle_logout_and_refresh(account_fname)  # This will retry until fixed
                print("✅ Successfully logged in after recovery")
            else:
                print(f"✅ Successfully logged in")
                
            # Initial active telemetry heartbeat!
            self._save_telemetry(
                status="Active - bot started",
                stats={"posts": 0, "comments": 0, "replied": 0, "skipped": 0}
            )
            
            # Sheets CSV reload control for groups
            sheets_csv_cfg = self.config.get('sheets_csv', {})
            csv_enabled = bool(sheets_csv_cfg.get('enabled')) and bool(sheets_csv_cfg.get('url'))
            reload_seconds = int(sheets_csv_cfg.get('reload_seconds', 300))
            print(f"📋 Processing {len(self.groups)} groups")

            continuous_mode = self.config.get('bot_settings', {}).get('continuous_mode', False)
            cycle_minutes = self.config.get('bot_settings', {}).get('continuous_cycle_minutes', 30)
            
            if continuous_mode:
                print(f"🔄 Continuous mode enabled - will cycle every {cycle_minutes} minutes")
                cycle_count = 0
                
            while True:
                if continuous_mode:
                    cycle_count += 1
                    print(f"\n🔄 Starting cycle #{cycle_count}")
                    
                # Hot-reload groups if due
                try:
                    if csv_enabled and (time.time() - self.last_groups_reload >= reload_seconds):
                        self.logger.info("Reload interval reached for groups. Fetching latest groups from Sheets CSV...")
                        latest_groups = self.load_groups() or []
                        if latest_groups:
                            existing_set = set(self.groups)
                            new_items = [g for g in latest_groups if g not in existing_set]
                            if new_items:
                                self.groups.extend(new_items)
                                self.last_groups_reload = time.time()
                except Exception:
                    pass
                    
                for i, group_url in enumerate(self.groups):
                    print(f"\n📂 Processing group {i+1}/{len(self.groups)}")
                    
                    self._save_telemetry(
                        status=f"Active - processing group {i+1}/{len(self.groups)}",
                        stats={
                            "posts": len(self.processed_posts),
                            "comments": len(self.processed_comments),
                            "replied": len([c for c in self.processed_comments.values() if c.get('action') == 'replied']),
                            "skipped": len([c for c in self.processed_comments.values() if c.get('action') == 'skipped'])
                        }
                    )
                    
                    # Check for logout before processing each group
                    if self.is_logged_out():
                        account_fname = f"{account_number}_cookies.json"
                        print("🔴 Logout detected - starting recovery process...")
                        self.handle_logout_and_refresh(account_fname)  # This will retry until fixed
                        print("✅ Successfully recovered from logout")
                    
                    self.process_group_posts(group_url)

                    # Delay between groups
                    delay = self.config.get('bot_settings', {}).get('delay_between_groups', 15)
                    time.sleep(delay)
                    
                    # Periodic logout check
                    logout_check_interval = self.config.get('bot_settings', {}).get('logout_check_interval', 300)
                    if time.time() - self.last_logout_check > logout_check_interval:
                        self.last_logout_check = time.time()
                        if self.is_logged_out():
                            account_fname = f"{account_number}_cookies.json"
                            print("🔴 Periodic logout check - starting recovery process...")
                            self.handle_logout_and_refresh(account_fname)  # This will retry until fixed
                            print("✅ Successfully recovered from periodic logout check")

                    # Periodic Telegram stats check
                    self.check_and_send_telegram_stats()

                if not continuous_mode:
                    break
                    
                print(f"\n⏳ Waiting {cycle_minutes} minutes before next cycle...")
                wait_seconds = cycle_minutes * 60
                start_wait = time.time()
                while time.time() - start_wait < wait_seconds:
                    elapsed = time.time() - start_wait
                    remaining_mins = int((wait_seconds - elapsed) / 60)
                    self._save_telemetry(
                        status=f"Active - waiting for next cycle ({remaining_mins}m left)",
                        stats={
                            "posts": len(self.processed_posts),
                            "comments": len(self.processed_comments),
                            "replied": len([c for c in self.processed_comments.values() if c.get('action') == 'replied']),
                            "skipped": len([c for c in self.processed_comments.values() if c.get('action') == 'skipped'])
                        }
                    )
                    time.sleep(10)
        
        finally:
            # Never quit driver - keep Chrome open for continuous operation
            pass
    
    def show_stats(self):
        """Show bot statistics"""
        total_posts = len(self.processed_posts)
        total_comments = len(self.processed_comments)
        replied_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'replied'])
        skipped_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'skipped'])
        
        print(f"\n📊 Bot Statistics:")
        print(f"   Posts processed: {total_posts}")
        print(f"   Comments processed: {total_comments}")
        print(f"   Comments replied: {replied_comments}")
        print(f"   Comments skipped: {skipped_comments}")

    def send_stats_to_telegram(self, force=False):
        try:
            total_posts = len(self.processed_posts)
            total_comments = len(self.processed_comments)
            replied_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'replied'])
            skipped_comments = len([c for c in self.processed_comments.values() if c.get('action') == 'skipped'])
            self._save_telemetry(
                stats={
                    "posts": total_posts,
                    "comments": total_comments,
                    "replied": replied_comments,
                    "skipped": skipped_comments
                }
            )
            return 999999
        except Exception as e:
            self.logger.debug(f"Telemetry stats notice failed: {e}")
            return None

    def check_and_send_telegram_stats(self, force=False):
        self.send_stats_to_telegram(force=force)

    def check_and_send_telegram_stats(self, force=False):
        """Check interval and send periodic statistics to Telegram if due"""
        try:
            telegram_config = self.config.get('telegram', {})
            if not telegram_config.get('enabled'):
                return

            min_interval = telegram_config.get('min_interval_sec', 900)
            now = time.time()

            if force or (now - self.last_telegram_stats) >= min_interval:
                self.send_stats_to_telegram(force=force)
        except Exception as e:
            self.logger.debug(f"Failed to check/send Telegram stats: {e}")

def main():
    """Main menu function"""
    bot = FacebookCommentBot()
    
    while True:
        print("\n" + "="*50)
        print("🤖 FACEBOOK GROUP COMMENT REPLY BOT")
        print("="*50)
        print("1. Start Bot")
        print("2. Show Statistics")
        print("3. View Logs")
        print("4. Exit")
        print("5. Start Multi-Tab Posts Mode (posts.txt or per-account Google Sheet)")
        print("-"*50)
        
        choice = input("Select option (1-5): ").strip()
        
        if choice == '1':
            account_number = input("Enter account number (e.g., 1 for 1_cookies.json): ").strip()
            if account_number:
                bot.run_bot(account_number)
            else:
                print("❌ Invalid account number")
        
        elif choice == '2':
            bot.show_stats()
        
        elif choice == '3':
            try:
                log_file = bot.config.get('logging', {}).get('log_file', 'logs/bot.log')
                with open(log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    print(f"\n📄 Last 20 log entries:")
                    print("-"*50)
                    for line in lines[-20:]:
                        print(line.strip())
            except FileNotFoundError:
                print("❌ Log file not found")
        
        elif choice == '4':
            print("👋 Goodbye!")
            break
        elif choice == '5':
            account_number = input("Enter account number (e.g., 1 for 1_cookies.json): ").strip()
            if account_number:
                # Config-driven auto selection: if enabled+use_for_option5+url -> use sheet without prompting
                cfg = bot.config.get('posts_per_account_sheet', {}) or {}
                auto = bool(cfg.get('enabled')) and bool(cfg.get('use_for_option5')) and bool(cfg.get('url'))
                if auto:
                    use_sheet = True
                else:
                    # Fallback to prompt if auto conditions are not fully met
                    default_use = cfg.get('use_for_option5', False)
                    prompt = f"Use per-account Google Sheet posts for account {account_number}? (y/N) [default {'Y' if default_use else 'N'}]: "
                    ans = input(prompt).strip().lower()
                    use_sheet = default_use if ans == '' else ans in ('y','yes','1','true','t')
                    if use_sheet and not (cfg.get('enabled') and cfg.get('url')):
                        print("⚠️ Per-account posts sheet is not enabled or URL missing in config. Falling back to posts.txt")
                        use_sheet = False
                bot.run_posts_multitab(account_number, use_account_posts_sheet=use_sheet)
        
        else:
            print("❌ Invalid option. Please select 1-4.")

if __name__ == "__main__":
    main()