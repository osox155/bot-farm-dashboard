import logging
import atexit
import signal
import sys
import os
import shutil
from pathlib import Path
import json
import time
import csv
import io
import requests
import threading
import random
import re
from datetime import datetime
import pyperclip
import pyotp
# SQLite stats database for persistent tracking and dashboard
try:
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from stats_tracker import get_tracker
    stats_tracker = get_tracker("ReplyBot")
    del _sys
except Exception:
    class _NullTracker:
        def start_session(self, *a, **kw): pass
        def end_session(self, *a, **kw): pass
        def log_event(self, *a, **kw): pass
        def log_login_failure(self, *a, **kw): pass
        def log_login_success(self, *a, **kw): pass
        def set_account_status(self, *a, **kw): pass
    stats_tracker = _NullTracker()

# Global in-memory statistics dict used by menu, reports, and stats tracking
bot_statistics = {}
stats_lock = threading.Lock()

# --- Telegram alert/report state (module-scoped) ---
# These were referenced throughout but never defined, so any code path that
# touched them (notably the failed-login alert) raised NameError, which
# propagated out of run_account into `finally: driver.quit()` and closed the
# browser — defeating the "keep Chrome open and retry" login loop. They hold the
# single rolling Telegram message ids (persisted to disk), the per-message edit
# throttle timestamps, and the cross-thread lock guarding the consolidated
# failed-login alert that all account threads share.
_STATE_DIR = Path(__file__).resolve().parent / "telemetry_state"
FAILED_LOGIN_STATE_FILE = _STATE_DIR / "failed_login_state.json"
STATUS_STATE_FILE = _STATE_DIR / "status_state.json"
REPORT_STATE_FILE = _STATE_DIR / "report_state.json"
STATS_CHECKPOINT_FILE = _STATE_DIR / "stats_checkpoint.json"
FAILED_LOGIN_LOCK = threading.Lock()
LAST_FAILED_ALERT_EDIT_TS = 0.0
LAST_STATUS_EDIT_TS = 0.0
LAST_REPORT_EDIT_TS = 0.0

def _telegram_alerts_only(config=None):
    """True when routine Telegram status/report sends should be suppressed,
    leaving only failed-login/logout alerts. Stats now live on the dashboard.
    Controlled by config telegram.alerts_only (default True)."""
    try:
        cfg = config if config is not None else load_config()
        tg = (cfg or {}).get('telegram', {}) or {}
        return bool(tg.get('alerts_only', True))
    except Exception:
        return True

def _alerts_via_broker(config=None):
    """True when this bot should NOT send failed-login alerts to Telegram
    directly, and instead let the central telemetry_broker.py send a single
    consolidated alert for all bots (fed by the telemetry file this bot writes).
    Default True so Telegram has exactly one voice. The bot still writes its
    telemetry/stats; only the direct Telegram API call is skipped."""
    try:
        cfg = config if config is not None else load_config()
        tg = (cfg or {}).get('telegram', {}) or {}
        return bool(tg.get('alerts_via_broker', True))
    except Exception:
        return True

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

class DeduplicationFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.last_record = None
    def filter(self, record):
        current = (record.levelno, record.getMessage())
        if current == self.last_record:
            return False
        self.last_record = current
        return True

# -------- Persistent failed-login notifications (Consolidated one-message alert) --------
def _load_failed_login_state():
    """Load consolidated failed-login alert state.
    New format: {"message_id": int, "accounts": {account_name: reason_text}}
    Legacy format (mapping account->msg_id) is auto-upgraded in-memory.
    """
    try:
        if FAILED_LOGIN_STATE_FILE.exists():
            raw = json.load(open(FAILED_LOGIN_STATE_FILE, 'r', encoding='utf-8'))
            # If legacy format detected, convert to new structure without message_id
            if isinstance(raw, dict) and raw and 'message_id' not in raw and 'accounts' not in raw:
                # Legacy stored account->message_id; keep only account names as needing attention
                return {"message_id": None, "accounts": {k: "Login failed (check cookies)" for k in raw.keys()}}
            if isinstance(raw, dict):
                # Ensure keys exist
                raw.setdefault('message_id', None)
                raw.setdefault('accounts', {})
                return raw
    except Exception:
        pass
    return {"message_id": None, "accounts": {}}

def _save_failed_login_state(state):
    try:
        FAILED_LOGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(FAILED_LOGIN_STATE_FILE, state)
    except Exception:
        pass

def _maybe_throttled_report_update(reason=None, force=False):
    """Edit the single Telegram REPORT message with throttling (~3s, or instant if force=True)."""
    global LAST_REPORT_EDIT_TS
    try:
        now = time.time()
        # Fast 3s throttle for near-instant updates, or skip if force=True
        if not force and (now - LAST_REPORT_EDIT_TS < 3):
            return
        report = generate_statistics_report()
        if reason:
            report += f"\n\nℹ️ {reason}"
        cfg = load_config()
        # Pass force=force to underlying function
        if send_or_update_telegram_report(report, cfg, force=force):
            LAST_REPORT_EDIT_TS = now
    except Exception:
        pass

# ------- Shared login detection (used by Options 1, 2, and 11) -------
def detect_logged_in_state(driver, logger, config):
    """Robustly determine if Messenger is logged in. Returns True/False.
    Mirrors the robust checks used in Options 1 & 2 to avoid false alerts.
    """
    try:
        retries = 15
        logged_in = None
        for _ in range(retries):
            negative = False
            # URL-based negatives
            try:
                cur_url = (driver.current_url or "").lower()
                if any(k in cur_url for k in ["/login", "recover", "checkpoint", "twofactor"]):
                    negative = True
            except Exception:
                pass

            # Text-based negatives
            try:
                page_text = (driver.page_source or "").lower()
                if any(marker in page_text for marker in [
                    "find your account",
                    "create new account",
                    "two-factor",
                    "wrong password",
                    "email address or phone number",
                    "messenger helps you connect",
                    "> log in <",  # inner text variants
                    "log in to facebook",
                ]):
                    negative = True
            except Exception:
                pass

            # DOM-based negatives (inputs)
            try:
                email_inputs = driver.find_elements(By.XPATH, "//input[@name='email' or @type='email']")
                if any(e.is_displayed() for e in email_inputs):
                    negative = True
            except Exception:
                pass

            # Positive indicators
            textbox_present = False
            grid_present = False
            try:
                elems = driver.find_elements(By.XPATH, "//div[@role='textbox']")
                textbox_present = len(elems) > 0 and any(e.is_displayed() for e in elems)
            except Exception:
                textbox_present = False
            try:
                grid_elems = driver.find_elements(By.XPATH, "//div[@role='grid']")
                grid_present = any(e.is_displayed() for e in grid_elems)
            except Exception:
                grid_present = False

            if not negative and (textbox_present or grid_present):
                logged_in = True
                break
            if negative:
                logged_in = False
                break

            time.sleep(1.2)

        if logged_in is None:
            # If uncertain after retries, default to not logged in to be safe for alerts
            logged_in = False
    except Exception:
        # On unexpected detection errors, default to not logged in to avoid false positives
        logged_in = False
    return logged_in

# ------- Auto re-login via Google Sheets cookie refresh (shared) -------

def _render_failed_login_alert(accounts_dict):
    header = "❗ 3 Accounts with Login Issues"
    if not accounts_dict:
        return f"{header}\n\nAll accounts recovered."
    lines = [f"- `{name}` — {reason}" for name, reason in sorted(accounts_dict.items())]
    body = (
        f"{header}\n\n"
        + "\n".join(lines)
        + "\n\n🔧 Action: Update cookies for the listed accounts and restart."
    )
    return body

def save_telemetry(bot_name, account, status=None, failed_logins=None, stats=None, recent_events=None):
    try:
        import time
        import json
        import sys
        import traceback
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        telemetry_dir = os.path.abspath(os.path.join(base_dir, "..", "telemetry"))
        os.makedirs(telemetry_dir, exist_ok=True)
        filename = f"{bot_name}_{account}.json"
        filepath = os.path.join(telemetry_dir, filename)
        
        data = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                pass
        
        data["bot_name"] = bot_name
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
        import traceback
        print(f"[ERROR] save_telemetry failed: {e}")
        traceback.print_exc()

def update_account_status(account_name, status_text):
    try:
        clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
        with stats_lock:
            replies = bot_statistics.get('option1_replies', {}).get(clean_acc, 0) + bot_statistics.get('option2_replies', {}).get(clean_acc, 0)
            messages = bot_statistics.get('option11_messages', {}).get(clean_acc, 0) + bot_statistics.get('option12_messages', {}).get(clean_acc, 0)
        save_telemetry(
            bot_name="ReplyBot",
            account=clean_acc,
            status=status_text,
            stats={"replies": replies, "messages": messages}
        )
    except Exception:
        pass

def send_or_update_failed_login_notice(account_name, config, reason_text="Login failed (check cookies)"):
    clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
    save_telemetry(
        bot_name="ReplyBot",
        account=clean_acc,
        status="Logged Out",
        failed_logins={clean_acc: reason_text}
    )
    return True

def cleanup_telegram_messages_and_files():
    try:
        import sys
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        telemetry_dir = os.path.abspath(os.path.join(base_dir, "..", "telemetry"))
        if os.path.exists(telemetry_dir):
            for f in os.listdir(telemetry_dir):
                if f.startswith("ReplyBot_") and f.endswith(".json"):
                    try:
                        os.remove(os.path.join(telemetry_dir, f))
                    except Exception:
                        pass
    except Exception:
        pass

def clear_failed_login_notice(account_name, config):
    clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
    save_telemetry(
        bot_name="ReplyBot",
        account=clean_acc,
        status="Running",
        failed_logins={}
    )
    return True


class DeduplicationFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.last_record = None
    def filter(self, record):
        current = (record.levelno, record.getMessage())
        if current == self.last_record:
            return False
        self.last_record = current
        return True

# -------- Persistent failed-login notifications (Consolidated one-message alert) --------
def _load_failed_login_state():
    """Load consolidated failed-login alert state.
    New format: {"message_id": int, "accounts": {account_name: reason_text}}
    Legacy format (mapping account->msg_id) is auto-upgraded in-memory.
    """
    try:
        if FAILED_LOGIN_STATE_FILE.exists():
            raw = json.load(open(FAILED_LOGIN_STATE_FILE, 'r', encoding='utf-8'))
            # If legacy format detected, convert to new structure without message_id
            if isinstance(raw, dict) and raw and 'message_id' not in raw and 'accounts' not in raw:
                # Legacy stored account->message_id; keep only account names as needing attention
                return {"message_id": None, "accounts": {k: "Login failed (check cookies)" for k in raw.keys()}}
            if isinstance(raw, dict):
                # Ensure keys exist
                raw.setdefault('message_id', None)
                raw.setdefault('accounts', {})
                return raw
    except Exception:
        pass
    return {"message_id": None, "accounts": {}}

def _save_failed_login_state(state):
    try:
        FAILED_LOGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(FAILED_LOGIN_STATE_FILE, state)
    except Exception:
        pass

def _maybe_throttled_report_update(reason=None, force=False):
    """Edit the single Telegram REPORT message with throttling (~3s, or instant if force=True)."""
    global LAST_REPORT_EDIT_TS
    try:
        now = time.time()
        # Fast 3s throttle for near-instant updates, or skip if force=True
        if not force and (now - LAST_REPORT_EDIT_TS < 3):
            return
        report = generate_statistics_report()
        if reason:
            report += f"\n\nℹ️ {reason}"
        cfg = load_config()
        # Pass force=force to underlying function
        if send_or_update_telegram_report(report, cfg, force=force):
            LAST_REPORT_EDIT_TS = now
    except Exception:
        pass

# ------- Shared login detection (used by Options 1, 2, and 11) -------
def detect_logged_in_state(driver, logger, config):
    """Robustly determine if Messenger is logged in. Returns True/False.
    Mirrors the robust checks used in Options 1 & 2 to avoid false alerts.
    """
    try:
        retries = 15
        logged_in = None
        for _ in range(retries):
            negative = False
            # URL-based negatives
            try:
                cur_url = (driver.current_url or "").lower()
                if any(k in cur_url for k in ["/login", "recover", "checkpoint", "twofactor"]):
                    negative = True
            except Exception:
                pass

            # Text-based negatives
            try:
                page_text = (driver.page_source or "").lower()
                if any(marker in page_text for marker in [
                    "find your account",
                    "create new account",
                    "two-factor",
                    "wrong password",
                    "email address or phone number",
                    "messenger helps you connect",
                    "> log in <",  # inner text variants
                    "log in to facebook",
                ]):
                    negative = True
            except Exception:
                pass

            # DOM-based negatives (inputs)
            try:
                email_inputs = driver.find_elements(By.XPATH, "//input[@name='email' or @type='email']")
                if any(e.is_displayed() for e in email_inputs):
                    negative = True
            except Exception:
                pass

            # Positive indicators
            textbox_present = False
            grid_present = False
            try:
                elems = driver.find_elements(By.XPATH, "//div[@role='textbox']")
                textbox_present = len(elems) > 0 and any(e.is_displayed() for e in elems)
            except Exception:
                textbox_present = False
            try:
                grid_elems = driver.find_elements(By.XPATH, "//div[@role='grid']")
                grid_present = any(e.is_displayed() for e in grid_elems)
            except Exception:
                grid_present = False

            if not negative and (textbox_present or grid_present):
                logged_in = True
                break
            if negative:
                logged_in = False
                break

            time.sleep(1.2)

        if logged_in is None:
            # If uncertain after retries, default to not logged in to be safe for alerts
            logged_in = False
    except Exception:
        # On unexpected detection errors, default to not logged in to avoid false positives
        logged_in = False
    return logged_in

# ------- Auto re-login via Google Sheets cookie refresh (shared) -------

def _render_failed_login_alert(accounts_dict):
    header = "❗ 3 Accounts with Login Issues"
    if not accounts_dict:
        return f"{header}\n\nAll accounts recovered."
    lines = [f"- `{name}` — {reason}" for name, reason in sorted(accounts_dict.items())]
    body = (
        f"{header}\n\n"
        + "\n".join(lines)
        + "\n\n🔧 Action: Update cookies for the listed accounts and restart."
    )
    return body

def send_or_update_failed_login_notice(account_name, config, reason_text="Login failed (check cookies)"):
    """Add or update an account in the single consolidated alert message.
    This never touches the rolling status message.

    Anti-spam and robustness:
    - Throttle edits to avoid Telegram rate limits (which previously caused duplicate sends).
    - If an edit fails with a transient error (e.g., Too Many Requests), do NOT send a new message.
    - Only send a new message when the previous one is truly gone (e.g., 'message to edit not found').
    - Treat 'message is not modified' as success to avoid unnecessary retries.
    """
    telegram_config = (config or {}).get('telegram', {})
    bot_token = telegram_config.get('bot_token')
    chat_id = telegram_config.get('chat_id')
    if not bot_token or not chat_id:
        return False

    # Log to persistent database
    try:
        clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
        stats_tracker.log_login_failure(clean_acc, reason=reason_text)
    except Exception:
        pass

    with FAILED_LOGIN_LOCK:
        state = _load_failed_login_state()
        accounts = state.get('accounts', {})
        accounts[account_name] = reason_text
        state['accounts'] = accounts
        # Also write to telemetry file so the broker picks up the event
        try:
            clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
            save_telemetry(
                bot_name="ReplyBot",
                account=clean_acc,
                status="Logged Out",
                failed_logins={clean_acc: reason_text}
            )
        except Exception:
            pass
        # The telemetry file is now written; telemetry_broker.py turns it into
        # the single consolidated Telegram alert that covers every bot. Skip this
        # bot's own direct Telegram send so the operator gets one message, not two.
        if _alerts_via_broker(config):
            return True
        body = _render_failed_login_alert(accounts)
        msg_id = state.get('message_id')
        edit_failed = False
        transient_error = False
        global LAST_FAILED_ALERT_EDIT_TS
        now_ts = time.time()
        # Throttle edits to once every ~8 seconds
        should_throttle = (now_ts - LAST_FAILED_ALERT_EDIT_TS) < 8
        if msg_id:
            if not should_throttle:
                url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                payload = {"chat_id": chat_id, "message_id": int(msg_id), "text": body, "parse_mode": "Markdown"}
                res = _telegram_request(url, payload)
                if res and res.get('ok'):
                    _save_failed_login_state(state)
                    LAST_FAILED_ALERT_EDIT_TS = now_ts
                    return True
                else:
                    # Inspect error to decide whether to attempt sendMessage fallback
                    edit_failed = True
                    try:
                        desc = (res or {}).get('description', '') or ''
                        code = (res or {}).get('error_code')
                    except Exception:
                        desc = ''
                        code = None
                    # Consider these as transient; do not create new messages
                    transient_markers = [
                        'Too Many Requests',
                        'retry after',
                        'Bad Request: message is not modified',
                        'Bad Request: message is not modified:',
                    ]
                    if any(m.lower() in desc.lower() for m in transient_markers):
                        transient_error = True
                        # Treat 'message is not modified' as success
                        if 'message is not modified' in desc.lower():
                            _save_failed_login_state(state)
                            LAST_FAILED_ALERT_EDIT_TS = now_ts
                            return True
                    # If message is too old to edit, or not found, allow fallback to send
                    non_exist_markers = [
                        'message to edit not found',
                        'message not found',
                        'message can\'t be edited',
                        'message is too old to be edited',
                    ]
                    if not any(m.lower() in desc.lower() for m in non_exist_markers):
                        # Not a definitive non-existence; if transient, bail out without sending new
                        if transient_error:
                            return False
                    # else: fall through to attempt sending a new message
            else:
                # Throttled: persist state only; next allowed cycle will edit
                _save_failed_login_state(state)
                return True
        # If edit failed or no message yet, send new message
        # Re-check state just before sending to avoid duplicates
        state2 = _load_failed_login_state()
        if state2.get('message_id'):
            # Another thread already created it; try edit again
            msg_id2 = state2.get('message_id')
            # Respect throttle when attempting the competing message edit as well
            if not ((time.time() - LAST_FAILED_ALERT_EDIT_TS) < 8):
                url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                payload = {"chat_id": chat_id, "message_id": int(msg_id2), "text": body, "parse_mode": "Markdown"}
                res = _telegram_request(url, payload)
                if res and res.get('ok'):
                    _save_failed_login_state(state2 | {'accounts': accounts})
                    LAST_FAILED_ALERT_EDIT_TS = time.time()
                    return True
                else:
                    edit_failed = True
            else:
                _save_failed_login_state(state2 | {'accounts': accounts})
                return True
        # Send new consolidated message
        # Avoid sending a new message if we had a transient error
        if transient_error:
            return False
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": body, "parse_mode": "Markdown"}
        res = _telegram_request(url, payload)
        if res and res.get('ok') and res.get('result', {}).get('message_id'):
            new_id = int(res['result']['message_id'])
            # Final race check: if file already has a different id, delete ours
            state3 = _load_failed_login_state()
            existing_id = state3.get('message_id')
            if existing_id and int(existing_id) != new_id:
                if edit_failed:
                    # Old id is stale; delete old and keep the new one
                    _telegram_delete_message(bot_token, chat_id, existing_id)
                    state3['message_id'] = new_id
                    state3['accounts'] = accounts
                    _save_failed_login_state(state3)
                    # Best-effort edit to confirm the kept message is valid and synced
                    try:
                        url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                        payload = {"chat_id": chat_id, "message_id": int(new_id), "text": body, "parse_mode": "Markdown"}
                        _telegram_request(url, payload)
                    except Exception:
                        pass
                    LAST_FAILED_ALERT_EDIT_TS = time.time()
                    return True
                else:
                    # True race with a live message; delete our duplicate and keep existing
                    _telegram_delete_message(bot_token, chat_id, new_id)
                    # Edit the existing one with latest body
                    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                    payload = {"chat_id": chat_id, "message_id": int(existing_id), "text": body, "parse_mode": "Markdown"}
                    _telegram_request(url, payload)
                    _save_failed_login_state({'message_id': int(existing_id), 'accounts': accounts})
                    LAST_FAILED_ALERT_EDIT_TS = time.time()
                    return True
            # Otherwise, write our id
            state3['message_id'] = new_id
            state3['accounts'] = accounts
            _save_failed_login_state(state3)
            LAST_FAILED_ALERT_EDIT_TS = time.time()
            return True
        return False

def cleanup_telegram_messages_and_files():
    """Delete Telegram status, report, and alert messages (if present) and remove their state files.
    Safe to call multiple times.
    """
    try:
        cfg = load_config()
        tg = (cfg or {}).get('telegram', {})
        bot_token = tg.get('bot_token')
        chat_id = tg.get('chat_id')
        
        # Delete rolling status message and file
        try:
            if STATUS_STATE_FILE.exists():
                state = json.load(open(STATUS_STATE_FILE, 'r', encoding='utf-8'))
                msg_id = state.get('message_id')
                if bot_token and chat_id and msg_id:
                    _telegram_delete_message(bot_token, chat_id, msg_id)
                try:
                    STATUS_STATE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
            
        # Delete report message and file
        try:
            if REPORT_STATE_FILE.exists():
                rstate = json.load(open(REPORT_STATE_FILE, 'r', encoding='utf-8'))
                msg_id_r = rstate.get('message_id')
                if bot_token and chat_id and msg_id_r:
                    _telegram_delete_message(bot_token, chat_id, msg_id_r)
                try:
                    REPORT_STATE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
            
        # Delete failed login alert message and file
        try:
            if FAILED_LOGIN_STATE_FILE.exists():
                fstate = json.load(open(FAILED_LOGIN_STATE_FILE, 'r', encoding='utf-8'))
                msg_id2 = fstate.get('message_id')
                if bot_token and chat_id and msg_id2:
                    _telegram_delete_message(bot_token, chat_id, msg_id2)
                try:
                    FAILED_LOGIN_STATE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass

def clear_failed_login_notice(account_name, config):
    """Remove an account from the consolidated alert; delete alert if none remain."""
    clean_acc = str(account_name).replace("_cookies.json", "").replace("_cookies", "").replace("cookies_", "").replace(".json", "").strip()
    save_telemetry(
        bot_name="ReplyBot",
        account=clean_acc,
        status="Running",
        failed_logins={}
    )

    # Log login success to persistent database
    try:
        stats_tracker.log_login_success(clean_acc)
    except Exception:
        pass

    # Telemetry was already rewritten with failed_logins={} above, so the broker
    # drops this account from the consolidated alert on its own. Skip the direct
    # Telegram edit when alerts are centralized in the broker.
    if _alerts_via_broker(config):
        return True

    # Update the failed login alert
    telegram_config = (config or {}).get('telegram', {})
    bot_token = telegram_config.get('bot_token')
    chat_id = telegram_config.get('chat_id')
    if not bot_token or not chat_id:
        return False
    with FAILED_LOGIN_LOCK:
        state = _load_failed_login_state()
        accounts = state.get('accounts', {})
        if clean_acc in accounts:
            del accounts[clean_acc]
        state['accounts'] = accounts
        if accounts:
            body = _render_failed_login_alert(accounts)
            msg_id = state.get('message_id')
            if msg_id:
                url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
                payload = {"chat_id": chat_id, "message_id": int(msg_id), "text": body, "parse_mode": "Markdown"}
                _telegram_request(url, payload)
            _save_failed_login_state(state)
        else:
            # No more failed accounts — delete the alert message
            msg_id = state.get('message_id')
            if msg_id:
                _telegram_delete_message(bot_token, chat_id, msg_id)
            try:
                FAILED_LOGIN_STATE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
    return True

def generate_statistics_report():
    """Build a single emoji-rich statistics report from global bot_statistics.
    Ensures message stays below Telegram's limit by compacting long sections.
    """
    MAX_LEN = 3800  # keep under 4096 with margin
    def _append_limited_lines(lines, title, unit):
        nonlocal report
        # Try to add lines; if too long, truncate and add summary
        remaining = MAX_LEN - len(report)
        if remaining <= 0:
            report += f"\n… (more {title.lower()} hidden)"
            return
        # Rough average line length, fallback to cap lines
        cap = max(3, min(len(lines), remaining // 40))
        shown = lines[:cap]
        report += "".join(shown)
        hidden = len(lines) - len(shown)
        if hidden > 0:
            total_more = sum(int(re.findall(r"(\d+)", l.strip())[-1]) for l in lines[cap:]) if lines and hidden else 0
            report += f"   … and {hidden} more accounts ({total_more} {unit})\n"

    with stats_lock:
        report = "📊 **BOT PERFORMANCE REPORT** 📊\n\n"
        disp_start = bot_statistics.get('start_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        disp_end = bot_statistics.get('end_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        report += f"⏰ **Session Duration:** {disp_start} - {disp_end}\n\n"

        # Option 1 & 2
        report += "🤖 **AUTO-REPLY STATISTICS**\n"
        report += "━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        total_option1 = sum(bot_statistics.get('option1_replies', {}).values())
        total_option2 = sum(bot_statistics.get('option2_replies', {}).values())

        report += f"👤 **Single Account (Option 1):** {total_option1} total replies\n"
        lines_o1 = [f"   - {account}: {count}\n" for account, count in bot_statistics.get('option1_replies', {}).items()]
        _append_limited_lines(lines_o1, "Option 1", "replies")

        report += f"\n👥 **Multiple Accounts (Option 2):** {total_option2} total replies\n"
        lines_o2 = [f"   - {account}: {count}\n" for account, count in bot_statistics.get('option2_replies', {}).items()]
        _append_limited_lines(lines_o2, "Option 2", "replies")

        # Option 11 & 12
        report += "\n💬 **ADVANCED MESSAGING**\n"
        report += "━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        total_option11 = sum(bot_statistics.get('option11_messages', {}).values())
        total_option12 = sum(bot_statistics.get('option12_messages', {}).values())

        report += f"🏠 **Main Chat (Option 11):** {total_option11} total messages\n"
        lines_o11 = [f"   - {account}: {count}\n" for account, count in bot_statistics.get('option11_messages', {}).items()]
        _append_limited_lines(lines_o11, "Option 11", "messages")

        report += f"\n📤 **Bulk Send (Option 12):** {total_option12} total messages\n"
        lines_o12 = [f"   - {account}: {count}\n" for account, count in bot_statistics.get('option12_messages', {}).items()]
        _append_limited_lines(lines_o12, "Option 12", "messages")

        # Summary
        report += "\n🎯 **SUMMARY**\n"
        report += "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        report += f"✅ Total Auto-Replies: {total_option1 + total_option2}\n"
        report += f"✅ Total Advanced Messages: {total_option11 + total_option12}\n"
        active_accounts = set(
            list(bot_statistics.get('option1_replies', {}).keys()) + 
            list(bot_statistics.get('option2_replies', {}).keys()) + 
            list(bot_statistics.get('option11_messages', {}).keys()) +
            list(bot_statistics.get('option12_messages', {}).keys())
        )
        report += f"✅ Active Accounts: {len(active_accounts)}\n"
        report += "\n🚀 **Bot session status** 🚀"
        # Final hard trim as a safeguard
        if len(report) > MAX_LEN:
            report = report[:MAX_LEN - 50] + "\n… (truncated)"
        return report

def _telegram_request(url, payload):
    """Robust Telegram API request handler.
    Returns JSON response even on non-OK status codes to allow error inspection.
    """
    try:
        r = requests.post(url, data=payload, timeout=10)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error_code": r.status_code, "description": r.text}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def _telegram_delete_message(bot_token, chat_id, message_id):
    """Delete a Telegram message with light retries.
    Treat 'message to delete not found' as success (already gone).
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": int(message_id)}
        for _ in range(3):
            res = _telegram_request(url, payload)
            if res and res.get('ok'):
                return True
            # Inspect error description for tolerant success/failure
            try:
                desc = (res or {}).get('description', '') or ''
            except Exception:
                desc = ''
            if 'message to delete not found' in desc.lower():
                return True  # already gone
            # Short sleep before retry on transient errors
            if 'too many requests' in desc.lower() or 'retry after' in desc.lower():
                time.sleep(1.2)
                continue
            # Other errors likely not recoverable here
            break
    except Exception:
        pass
    return False

def _atomic_write_json(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False

def send_or_update_telegram_status(text, config, force=False):
    # Routine status updates are suppressed in alerts-only mode (stats live on
    # the dashboard now). Failed-login alerts use a separate function.
    if _telegram_alerts_only(config):
        return None
    import re
    account = "Summary"
    status_line = "Running"

    match_info = re.search(r"ℹ️\s*(.*)", text)
    if match_info:
        status_line = match_info.group(1).strip()
    elif "recovered from previous" in text.lower():
        status_line = "Recovered and running"
    elif "bot launched" in text.lower():
        status_line = "Bot launched"
    elif "rdp" in text.lower():
        last_line = text.strip().split("\n")[-1]
        status_line = last_line.strip()

    match_acc = re.search(r"(?:account|by)\s*([0-9a-zA-Z_-]+)", status_line.lower())
    if not match_acc:
        match_acc = re.search(r"message sent by\s*([0-9a-zA-Z_-]+)", text.lower())

    if match_acc:
        account = match_acc.group(1).replace("_cookies", "").strip()
        if account.lower() == 's':
            account = "Summary"

    save_telemetry(
        bot_name="ReplyBot",
        account=account,
        status=status_line,
        stats=None
    )

    telegram_config = (config or {}).get('telegram', {})
    bot_token = telegram_config.get('bot_token')
    chat_id = telegram_config.get('chat_id')
    if not bot_token or not chat_id:
        return False

    state = {"message_id": None}
    try:
        if STATUS_STATE_FILE.exists():
            with open(STATUS_STATE_FILE, 'r', encoding='utf-8') as f:
                stored = json.load(f)
                if isinstance(stored, dict):
                    state.update(stored)
    except Exception:
        pass

    msg_id = state.get('message_id')
    edit_failed = False

    if msg_id:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
        payload = {"chat_id": chat_id, "message_id": int(msg_id), "text": text, "parse_mode": "Markdown"}
        res = _telegram_request(url, payload)
        if res and res.get('ok'):
            _atomic_write_json(STATUS_STATE_FILE, state)
            return True
        else:
            edit_failed = True
            try:
                desc = (res or {}).get('description', '') or ''
            except Exception:
                desc = ''
            if 'message to edit' not in desc.lower() and 'message not found' not in desc.lower():
                pass
            else:
                msg_id = None
    else:
        edit_failed = True

    if edit_failed and not msg_id:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        res = _telegram_request(url, payload)
        if res and res.get('ok'):
            try:
                new_msg_id = res['result']['message_id']
            except Exception:
                new_msg_id = None
            if new_msg_id:
                state['message_id'] = new_msg_id
                _atomic_write_json(STATUS_STATE_FILE, state)
                return True

    return False

def send_or_update_telegram_report(text, config, force=False):
    # Routine summary reports are suppressed in alerts-only mode.
    if _telegram_alerts_only(config):
        return None
    telegram_config = (config or {}).get('telegram', {})
    bot_token = telegram_config.get('bot_token')
    chat_id = telegram_config.get('chat_id')
    if not bot_token or not chat_id:
        return False

    with stats_lock:
        replies = sum(bot_statistics.get('option1_replies', {}).values()) + sum(bot_statistics.get('option2_replies', {}).values())
        messages = sum(bot_statistics.get('option11_messages', {}).values()) + sum(bot_statistics.get('option12_messages', {}).values())
    save_telemetry(
        bot_name="ReplyBot",
        account="Summary",
        status="Reporting",
        stats={"replies": replies, "messages": messages}
    )

    # Persistent report message management (same pattern as failed-login alert)
    state = {"message_id": None, "text_hash": ""}
    try:
        if REPORT_STATE_FILE.exists():
            with open(REPORT_STATE_FILE, 'r', encoding='utf-8') as f:
                stored = json.load(f)
                if isinstance(stored, dict):
                    state.update(stored)
    except Exception:
        pass

    text_hash = str(hash(text)) if not force else ""
    msg_id = state.get('message_id')
    edit_failed = False

    if msg_id:
        # Try to edit existing message
        if text_hash != state.get('text_hash') or force:
            url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
            payload = {"chat_id": chat_id, "message_id": int(msg_id), "text": text, "parse_mode": "Markdown"}
            res = _telegram_request(url, payload)
            if res and res.get('ok'):
                state['text_hash'] = text_hash
                _atomic_write_json(REPORT_STATE_FILE, state)
                return True
            else:
                edit_failed = True
                try:
                    desc = (res or {}).get('description', '') or ''
                except Exception:
                    desc = ''
                # If message was deleted, send a new one
                if 'message to edit' not in desc.lower() and 'message not found' not in desc.lower():
                    pass
                else:
                    msg_id = None
        else:
            return True  # Text unchanged, skip
    else:
        edit_failed = True

    if edit_failed and not msg_id:
        # Send new message
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        res = _telegram_request(url, payload)
        if res and res.get('ok'):
            try:
                new_msg_id = res['result']['message_id']
            except Exception:
                new_msg_id = None
            if new_msg_id:
                state['message_id'] = new_msg_id
                state['text_hash'] = text_hash
                _atomic_write_json(REPORT_STATE_FILE, state)
                return True

    return False

def send_telegram_report(text, config):
    return send_or_update_telegram_report(text, config)

def save_stats_checkpoint():
    try:
        STATS_CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
            with stats_lock:
                json.dump(bot_statistics, f)
        return True
    except Exception:
        return False

def recover_and_report_previous_session():
    """If previous session ended abruptly, recover stats into memory and notify via status message."""
    try:
        if STATS_CHECKPOINT_FILE.exists():
            cfg = load_config()
            try:
                state = json.load(open(STATS_CHECKPOINT_FILE, 'r', encoding='utf-8'))
            except Exception:
                state = None
            if state:
                # RECOVER STATS INTO MEMORY
                with stats_lock:
                    for k, v in state.items():
                        if k in bot_statistics:
                            # Merge dicts for replies/messages, replace primitives
                            if isinstance(v, dict) and isinstance(bot_statistics[k], dict):
                                bot_statistics[k].update(v)
                            else:
                                bot_statistics[k] = v
                
                # Non-spammy: just update the rolling status with a recovery note
                try:
                    send_or_update_telegram_status("🧯 Recovered from previous unexpected stop. Resuming...\n\n" + generate_statistics_report(), cfg, force=True)
                except Exception:
                    pass
            # Do not delete checkpoint here; keep for later updates
    except Exception:
        pass


def _maybe_throttled_status_update(reason=None, force=False):
    """Edit the single Telegram status message with throttling (~3s, or instant if force=True)."""
    global LAST_STATUS_EDIT_TS
    try:
        now = time.time()
        # Fast 3s throttle for near-instant updates, or skip if force=True
        if not force and (now - LAST_STATUS_EDIT_TS < 3):
            return
        report = generate_statistics_report()
        if reason:
            report += f"\n\nℹ️ {reason}"
        cfg = load_config()
        # Pass force=force to underlying function
        if send_or_update_telegram_status(report, cfg, force=force):
            LAST_STATUS_EDIT_TS = now
    except Exception:
        pass

# Final-report sending guard
REPORT_LOCK = threading.Lock()
REPORT_SENT = False

def send_final_report_once(reason=None):
    """Generate and send the final Telegram report exactly once, thread-safe."""
    global REPORT_SENT
    try:
        with REPORT_LOCK:
            if REPORT_SENT:
                return False
            # Ensure end time is set
            if not bot_statistics.get('end_time'):
                bot_statistics['end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            report = generate_statistics_report()
            if reason:
                report += f"\n\nℹ️ Shutdown reason: {reason}"
            # Print to console
            print("\n" + "="*50)
            print("BOT SESSION STATISTICS")
            print("="*50)
            print(report.replace('**', '').replace('*', ''))
            print("="*50)
            # End persistent database session
            try:
                stats_tracker.end_session()
            except Exception:
                pass
            # Send via Telegram - delete status message first, then send/update report
            cfg = load_config()
            try:
                # Delete the rolling status message since we're sending final report
                tg = (cfg or {}).get('telegram', {})
                bot_token = tg.get('bot_token')
                chat_id = tg.get('chat_id')
                if bot_token and chat_id and STATUS_STATE_FILE.exists():
                    state = json.load(open(STATUS_STATE_FILE, 'r', encoding='utf-8'))
                    msg_id = state.get('message_id')
                    if msg_id:
                        _telegram_delete_message(bot_token, chat_id, msg_id)
                    STATUS_STATE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            send_or_update_telegram_report(report, cfg)
            REPORT_SENT = True
            return True
    except Exception:
        # Do not raise during shutdown paths
        return False

def _on_process_exit():
    # Only notify; do not clear logs or forcefully stop Chrome here to preserve state
    send_final_report_once("Process exit")
    try:
        kill_orphaned_chrome_processes()
    except Exception:
        pass
    # Always clean Telegram message state and messages on exit
    try:
        cleanup_telegram_messages_and_files()
    except Exception:
        pass

def _on_signal(signum, frame):
    try:
        send_final_report_once(f"Signal {signum}")
        try:
            kill_orphaned_chrome_processes()
        except Exception:
            pass
    finally:
        # Let default handlers proceed
        pass
    # Also cleanup Telegram messages and files on signals
    try:
        cleanup_telegram_messages_and_files()
    except Exception:
        pass

# Register atexit and signal handlers early
atexit.register(_on_process_exit)
try:
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _on_signal)
except Exception:
    # Signal registration can fail in some embedded environments
    pass

# Windows console control handler (captures console close, logoff, shutdown)
try:
    import ctypes
    from ctypes import wintypes

    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6

    def _console_ctrl_handler(ctrl_type):
        try:
            if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT, CTRL_BREAK_EVENT, CTRL_C_EVENT):
                send_final_report_once(f"Console control event: {ctrl_type}")
                try:
                    kill_orphaned_chrome_processes()
                except Exception:
                    pass
                # Give a moment for cleanup to finish, then allow OS to close console
                try:
                    import time as _t
                    _t.sleep(1)
                except Exception:
                    pass
                # Return False so default handler proceeds and console closes
                return False
        except Exception:
            return False
        return False

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _handler_func = HandlerRoutine(_console_ctrl_handler)
    kernel32.SetConsoleCtrlHandler.argtypes = (HandlerRoutine, wintypes.BOOL)
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.SetConsoleCtrlHandler(_handler_func, True)
    
    # Create a Job Object to ensure child processes die when this process exits unexpectedly
    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

    _JOB_HANDLE = kernel32.CreateJobObjectW(None, None)
    if _JOB_HANDLE:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(_JOB_HANDLE, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info))
    else:
        _JOB_HANDLE = None
except Exception:
    pass

# ---------- Background reporting and session monitoring ----------
# Optional periodic auto-report to mitigate sudden power loss (disabled if 0)
_LAST_REPORT_SNAPSHOT = {
    'option1': 0,
    'option2': 0,
    'option11': 0,
}

def _get_totals():
    with stats_lock:
        return (
            sum(bot_statistics.get('option1_replies', {}).values()),
            sum(bot_statistics.get('option2_replies', {}).values()),
            sum(bot_statistics.get('option11_messages', {}).values()),
        )

def periodic_reporter():
    try:
        cfg = load_config()
        minutes = int(cfg.get('telegram', {}).get('auto_report_minutes', 0) or 0)
    except Exception:
        minutes = 0
    if minutes <= 0:
        return
    interval = max(5, minutes)  # minimum 5 minutes
    while True:
        try:
            time.sleep(interval * 60)
            o1, o2, o11 = _get_totals()
            changed = (o1 != _LAST_REPORT_SNAPSHOT['option1'] or
                       o2 != _LAST_REPORT_SNAPSHOT['option2'] or
                       o11 != _LAST_REPORT_SNAPSHOT['option11'])
            if not changed:
                continue
            # Attempt to send report first; only advance snapshot on success
            try:
                report = generate_statistics_report()
                cfg = load_config()
                ok = send_or_update_telegram_report(report, cfg)
                if ok:
                    # Update snapshot after a successful send/edit
                    _LAST_REPORT_SNAPSHOT['option1'] = o1
                    _LAST_REPORT_SNAPSHOT['option2'] = o2
                    _LAST_REPORT_SNAPSHOT['option11'] = o11
                    # Save checkpoint for recovery
                    save_stats_checkpoint()
                else:
                    # Keep snapshot unchanged so next cycle retries the same update
                    pass
            except Exception:
                # Keep thread alive on unexpected errors; snapshot remains unchanged
                pass
        except Exception:
            # Keep thread alive
            continue

def clear_logs():
    try:
        logs_dir = Path(__file__).with_name('logs')
        if logs_dir.exists() and logs_dir.is_dir():
            shutil.rmtree(logs_dir, ignore_errors=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            print("Logs cleared.")
    except Exception:
        pass

def _get_current_session_id():
    try:
        from ctypes import wintypes, byref
        sess = wintypes.DWORD()
        if hasattr(kernel32, 'ProcessIdToSessionId'):
            kernel32.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
            if kernel32.ProcessIdToSessionId(os.getpid(), byref(sess)):
                return int(sess.value)
    except Exception:
        pass
    return None

def session_watchdog():
    # Detect RDP disconnect/logoff/shutdown via WTS APIs
    try:
        WTSAPI32 = ctypes.WinDLL('Wtsapi32.dll', use_last_error=True)
        from ctypes import wintypes, byref
        WTS_CURRENT_SERVER_HANDLE = wintypes.HANDLE(0)
        # WTSInfoClass for connection state is 8 (WTSConnectState)
        WTSConnectState = 8
        WTSFreeMemory = WTSAPI32.WTSFreeMemory
        WTSFreeMemory.argtypes = (wintypes.LPVOID,)
        WTSQuerySessionInformationW = WTSAPI32.WTSQuerySessionInformationW
        # Buffer is LPVOID for generality; size returned in bytes
        WTSQuerySessionInformationW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
        WTSQuerySessionInformationW.restype = wintypes.BOOL

        sess_id = _get_current_session_id()
        if sess_id is None:
            return
        last_state = None
        disconnected_since = None
        # Grace period before we consider a disconnect as final
        try:
            cfg = load_config()
            grace_min = int((cfg.get('watchdog', {}) or {}).get('rdp_disconnect_grace_minutes', 10))
        except Exception:
            grace_min = 10
        grace_sec = max(1, grace_min) * 60
        while True:
            try:
                pBuffer = ctypes.c_void_p()
                bytes_ret = wintypes.DWORD()
                ok = WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, sess_id, WTSConnectState, byref(pBuffer), byref(bytes_ret))
                if ok:
                    try:
                        # Interpret buffer as DWORD (WTS_CONNECTSTATE_CLASS)
                        state = -1
                        if pBuffer and bytes_ret.value >= ctypes.sizeof(wintypes.DWORD):
                            state = ctypes.cast(pBuffer, ctypes.POINTER(wintypes.DWORD)).contents.value
                    finally:
                        WTSFreeMemory(pBuffer)
                    # State handling
                    if last_state is None:
                        last_state = state
                    # Immediate final on Down (6)
                    if state == 6:
                        _maybe_throttled_status_update("RDP shutting down")
                        send_final_report_once("RDP Down (shutdown/logoff)")
                        try:
                            kill_orphaned_chrome_processes()
                        except Exception:
                            pass
                        try:
                            clear_logs()
                        except Exception:
                            pass
                        return
                    # Disconnected (4): only update status; do not finalize or stop the bot
                    if state == 4:
                        if disconnected_since is None:
                            disconnected_since = time.time()
                            _maybe_throttled_status_update("RDP disconnected (grace period)")
                        else:
                            if time.time() - disconnected_since >= grace_sec:
                                _maybe_throttled_status_update("RDP disconnect sustained; bot keeps running")
                                # Keep running; do not finalize
                    else:
                        # Any non-disconnected state resets grace timer
                        disconnected_since = None
                    last_state = state
                time.sleep(10)
            except Exception:
                time.sleep(10)
                continue
    except Exception:
        return

def console_watchdog():
    # Detect when the console window is closed (e.g., VS Code Kill Terminal)
    try:
        # Get initial console window handle
        GetConsoleWindow = kernel32.GetConsoleWindow
        GetConsoleWindow.restype = wintypes.HWND
        initial_hwnd = GetConsoleWindow()
        if not initial_hwnd:
            return
        while True:
            try:
                current = GetConsoleWindow()
                if not current:
                    # Console window gone -> trigger final report and cleanup
                    send_final_report_once("Console window closed")
                    try:
                        kill_orphaned_chrome_processes()
                    except Exception:
                        pass
                    try:
                        clear_logs()
                    except Exception:
                        pass
                    return
                time.sleep(5)
            except Exception:
                time.sleep(5)
                continue
    except Exception:
        return

def _get_parent_pid():
    # Use Toolhelp snapshot to get our parent PID reliably in Windows
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32))

        hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if int(hSnap) == 0 or int(hSnap) == -1:
            return None
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32FirstW(hSnap, ctypes.byref(pe)):
                return None
            mypid = os.getpid()
            while True:
                if pe.th32ProcessID == mypid:
                    return int(pe.th32ParentProcessID)
                if not kernel32.Process32NextW(hSnap, ctypes.byref(pe)):
                    break
        finally:
            try:
                kernel32.CloseHandle(hSnap)
            except Exception:
                pass
    except Exception:
        return None

def _is_process_alive(pid):
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        hProc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not hProc:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(hProc, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            try:
                kernel32.CloseHandle(hProc)
            except Exception:
                pass
    except Exception:
        return False

def parent_watchdog():
    # If the parent terminal host dies, treat it as stop signal and cleanup
    try:
        ppid = _get_parent_pid()
        if not ppid or ppid == 0:
            return
        while True:
            try:
                if not _is_process_alive(ppid):
                    send_final_report_once("Parent terminal process exited")
                    try:
                        kill_orphaned_chrome_processes()
                    except Exception:
                        pass
                    try:
                        clear_logs()
                    except Exception:
                        pass
                    return
                time.sleep(5)
            except Exception:
                time.sleep(5)
                continue
    except Exception:
        return

def start_background_reporters():
    # Start periodic reporter (if enabled in config) and session watchdog
    try:
        t1 = threading.Thread(target=periodic_reporter, daemon=True)
        t1.start()
    except Exception:
        pass
    try:
        t2 = threading.Thread(target=session_watchdog, daemon=True)
        t2.start()
    except Exception:
        pass
    try:
        t3 = threading.Thread(target=console_watchdog, daemon=True)
        t3.start()
    except Exception:
        pass
    try:
        t4 = threading.Thread(target=parent_watchdog, daemon=True)
        t4.start()
    except Exception:
        pass
    try:
        t5 = threading.Thread(target=inactivity_watchdog, daemon=True)
        t5.start()
    except Exception:
        pass
    try:
        t7 = threading.Thread(target=session_limit_watchdog, daemon=True)
        t7.start()
    except Exception:
        pass
    # Heartbeat status updater (edits the single message even without stat changes)
    try:
        t6 = threading.Thread(target=status_heartbeat, daemon=True)
        t6.start()
    except Exception:
        pass

def status_heartbeat():
    try:
        cfg = load_config()
        hb = int((cfg.get('telegram', {}) or {}).get('status_heartbeat_seconds', 60))
    except Exception:
        hb = 60
    hb = max(15, hb)
    while True:
        try:
            _maybe_throttled_status_update()
            time.sleep(hb)
        except Exception:
            time.sleep(hb)
            continue

# Unhandled exception hook to send report
def _excepthook(exc_type, exc, tb):
    try:
        _maybe_throttled_status_update("Unhandled exception")
        send_final_report_once(f"Unhandled exception: {exc_type.__name__}")
        try:
            kill_orphaned_chrome_processes()
        except Exception:
            pass
    finally:
        _orig_excepthook(exc_type, exc, tb)

_orig_excepthook = sys.excepthook
sys.excepthook = _excepthook

# Global Chrome process tracking
BOT_CHROME_PROCESSES = []
AUTO_POPUP_CLOSER_ENABLED = True
PAUSED = False


def load_cookies_from_file(filepath):
    """Load cookies from a JSON file and convert them to Selenium-compatible format."""
    with open(filepath, "r") as file:
        cookies = json.load(file)
    selenium_cookies = []
    for cookie in cookies:
        cookie_dict = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie["domain"],
            "path": cookie.get("path", "/"),
            "secure": cookie.get("secure", False),
            "httpOnly": cookie.get("httpOnly", False),
        }
        if "expiry" in cookie or "expirationDate" in cookie:
            cookie_dict["expiry"] = int(cookie.get("expiry", cookie.get("expirationDate", 0)))
        selenium_cookies.append(cookie_dict)
    return selenium_cookies


def click_accept_if_present(driver, logger=None):
    """Click the 'Accept' button in a request popup if it is visible. Returns True if clicked."""
    try:
        xpath_variants = [
            "//div[@role='button'][.='Accept']",
            "//span[text()='Accept']/ancestor::*[@role='button']",
            "//button[.='Accept']"
        ]
        for xp in xpath_variants:
            buttons = driver.find_elements(By.XPATH, xp)
            for btn in buttons:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    if logger:
                        logger.info("Clicked 'Accept' button to open chat textbox.")
                    time.sleep(1)
                    return True
    except Exception as e:
        if logger:
            logger.debug(f"No Accept button found or click failed: {e}")
    return False

def close_temporary_block_popup(driver, logger=None):
    """Robustly close Messenger popups: 'You're Temporarily Blocked' and PIN/restore chat history popups."""
    import time
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    def log(msg):
        if logger:
            logger.info(msg)
        # Remove print fallback to prevent console contamination
    
    # First, try the aggressive login popup closer if auto popup closer is enabled
    if AUTO_POPUP_CLOSER_ENABLED:
        popup_handled = aggressive_login_popup_closer(driver, logger)
        if popup_handled:
            log("Login popup handled by aggressive popup closer.")
            return
    
    # Try to close any 'Close' button popup
    try:
        # Try to close "You're Temporarily Blocked" popup if it appears
        blocked_popup = driver.find_elements(By.XPATH, '//div[contains(text(), "You can no longer use Messenger")]')
        if blocked_popup:
            close_btn = driver.find_element(By.XPATH, '//div[@aria-label="Close"]')
            close_btn.click()
            log("Closed 'You're Temporarily Blocked' popup.")
            time.sleep(1)
    except Exception:
        pass

    # Try to close PIN/restore chat history popup (click both X and 'Don't restore messages')
    # Try X once, then try 'Don't restore messages' up to 3 times
    try:
        close_btn = driver.find_element(By.XPATH, '//div[@aria-label="Close"]')
        close_btn.click()
    except Exception:
        pass
    
    # Enhanced selectors for "Don't restore messages" button
    selectors = [
        '//span[contains(text(), "Don\'t restore messages")]/ancestor::div[@role="button"]',
        '//div[contains(text(), "Don\'t restore messages")]/ancestor::div[@role="button"]',
        '//span[contains(text(), "Don\'t restore messages")]',
        '//button[contains(text(), "Don\'t restore messages")]',
        '//div[contains(text(), "Don\'t restore messages")]',
        '//div[@role="button"][contains(., "Don\'t restore")]',
        '//div[contains(@style, "rgb(24, 119, 242)")][@role="button"]',  # Blue button
        '//div[contains(@class, "primary")][@role="button"]'  # Primary button
    ]
    
    max_attempts = 5  # Increased attempts
    for attempt in range(max_attempts):
        # Check if the popup is present
        try:
            popup = driver.find_element(By.XPATH, '//*[contains(text(), "Continue without restoring") or contains(text(), "restore your chat history")]')
        except Exception:
            break
        
        clicked = False
        for sel in selectors:
            try:
                btn = driver.find_element(By.XPATH, sel)
                # Try multiple clicking strategies
                strategies = [
                    lambda: btn.click(),
                    lambda: driver.execute_script("arguments[0].click();", btn),
                    lambda: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));", btn)
                ]
                
                for i, strategy in enumerate(strategies, 1):
                    try:
                        if btn.is_displayed() and btn.is_enabled():
                            strategy()
                            if logger:
                                logger.info(f"Clicked 'Don't restore messages' with selector {sel} strategy {i} (attempt {attempt+1})")
                            time.sleep(1)
                            clicked = True
                            break
                    except Exception:
                        continue
                
                if clicked:
                    break
                    
            except Exception:
                continue
        
        if not clicked:
            if logger:
                logger.warning(f"Attempt {attempt+1}: Could not click 'Don't restore messages' (button not found or not clickable)")
            time.sleep(1)
        else:
            # After a click, check if popup is still present next loop
            continue
    else:
        if logger:
            logger.error("Failed to click 'Don't restore messages' after 5 attempts.")


def click_requests_icon(driver, logger=None):
    """Click on the 'Requests' icon on the left sidebar and verify the page loaded."""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            # Try to find and click the requests icon
            requests_icon = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//a[contains(@aria-label, "Requests")]')
                )
            )
            requests_icon.click()
            if logger:
                logger.info(f"Clicked Requests icon (attempt {attempt + 1})")
            
            # Wait and verify the requests page actually loaded
            time.sleep(2)
            
            # Check for indicators that requests page loaded:
            # 1. "Requests" header/title visible
            # 2. "You may know" or "Spam" tabs visible
            # 3. URL contains /requests
            verification_methods = [
                lambda: driver.find_element(By.XPATH, '//h1[contains(text(), "Requests")]'),
                lambda: driver.find_element(By.XPATH, '//span[text()="You may know"]'),
                lambda: driver.find_element(By.XPATH, '//span[text()="Spam"]'),
                lambda: driver.find_element(By.XPATH, '//div[contains(text(), "No message requests")]'),
                lambda: driver.find_element(By.XPATH, '//div[contains(text(), "No chats selected")]'),
                lambda: "messenger.com/requests" in driver.current_url
            ]
            
            page_loaded = False
            for method_num, method in enumerate(verification_methods, 1):
                try:
                    if method():
                        page_loaded = True
                        if logger:
                            logger.info(f"Requests page verified loaded (method {method_num}, attempt {attempt + 1})")
                        break
                except Exception:
                    continue
            
            if page_loaded:
                # Additional small delay to ensure full render
                time.sleep(2)
                return True
            else:
                if logger:
                    logger.warning(f"Requests page not verified after click (attempt {attempt + 1}), retrying...")
                time.sleep(2)
                
        except Exception as e:
            if logger:
                logger.warning(f"Error clicking Requests icon (attempt {attempt + 1}): {type(e).__name__}: {e}")
            time.sleep(2)
    
    if logger:
        logger.error(f"Failed to load Requests page after {max_attempts} attempts")
    return False

def verify_on_requests_page(driver, logger=None):
    """Verify that we are currently on the Requests page. Returns True if on requests page, False otherwise."""
    try:
        # Quick checks to verify we're on the requests page
        checks = [
            lambda: driver.find_element(By.XPATH, '//span[text()="You may know"]'),
            lambda: driver.find_element(By.XPATH, '//span[text()="Spam"]'),
            lambda: driver.find_element(By.XPATH, '//h1[contains(text(), "Requests")]'),
            lambda: "messenger.com/requests" in driver.current_url
        ]
        
        for check in checks:
            try:
                if check():
                    return True
            except Exception:
                continue
        
        return False
    except Exception:
        return False

def ensure_on_requests_page(driver, logger=None):
    """Verify we are on Requests page, and click Requests icon if not. Returns True if on requests page."""
    if verify_on_requests_page(driver, logger):
        return True
    
    if logger:
        logger.warning("Not on Requests page, clicking Requests icon...")
    
    return click_requests_icon(driver, logger)

def switch_to_tab(driver, tab_name, logger=None):
    """Switch to the specified tab ('You may know' or 'Spam') with retry logic."""
    max_attempts = 5
    
    # Multiple XPath strategies to find the tab
    xpath_strategies = [
        f'//span[text()="{tab_name}"]',  # Exact text match
        f'//span[contains(text(), "{tab_name}")]',  # Contains text
        f'//div[contains(text(), "{tab_name}")]',  # Div contains text
        f'//button[contains(., "{tab_name}")]',  # Button contains text
        f'//a[contains(., "{tab_name}")]',  # Link contains text
        f'//div[@role="tab" and contains(., "{tab_name}")]',  # Tab role
        f'//*[@role="button" and contains(., "{tab_name}")]',  # Button role
    ]
    
    for attempt in range(max_attempts):
        try:
            # Try each XPath strategy
            tab_element = None
            for xpath in xpath_strategies:
                try:
                    tab_element = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    if tab_element:
                        if logger:
                            logger.debug(f"Found '{tab_name}' tab using xpath: {xpath}")
                        break
                except Exception:
                    continue
            
            if tab_element:
                # Try multiple clicking strategies
                click_strategies = [
                    lambda: tab_element.click(),
                    lambda: driver.execute_script("arguments[0].click();", tab_element),
                    lambda: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));", tab_element)
                ]
                
                clicked = False
                for strategy_num, strategy in enumerate(click_strategies, 1):
                    try:
                        strategy()
                        clicked = True
                        if logger:
                            logger.debug(f"Clicked '{tab_name}' tab using strategy {strategy_num}")
                        break
                    except Exception:
                        continue
                
                if clicked:
                    # Wait for tab content to load
                    time.sleep(3)
                    
                    # Verify the tab is now active (check for visual indicators)
                    try:
                        # Look for active/highlighted state
                        active_indicators = [
                            f'//span[text()="{tab_name}"][@aria-selected="true"]',
                            f'//span[text()="{tab_name}"]/parent::*[contains(@class, "active")]',
                            f'//span[text()="{tab_name}"]/ancestor::*[contains(@style, "border-bottom")]',
                            f'//span[text()="{tab_name}"]/ancestor::*[contains(@class, "selected")]'
                        ]
                        
                        for indicator_xpath in active_indicators:
                            try:
                                if driver.find_element(By.XPATH, indicator_xpath):
                                    if logger:
                                        logger.info(f"Successfully switched to '{tab_name}' tab (attempt {attempt + 1})")
                                    return True
                            except Exception:
                                continue
                        
                        # If no active indicator found, assume success if we got this far
                        if logger:
                            logger.info(f"Switched to '{tab_name}' tab (attempt {attempt + 1}, no active indicator found)")
                        return True
                        
                    except Exception:
                        # Tab switch likely succeeded even if verification failed
                        if logger:
                            logger.info(f"Switched to '{tab_name}' tab (attempt {attempt + 1})")
                        return True
                else:
                    if logger:
                        logger.warning(f"Could not click '{tab_name}' tab (attempt {attempt + 1})")
            else:
                if logger:
                    logger.warning(f"Could not find '{tab_name}' tab (attempt {attempt + 1})")
                
                # If on requests page but tab not found, maybe it's already selected
                try:
                    current_url = driver.current_url
                    if "messenger.com/requests" in current_url:
                        # Check if we're already on the right content
                        if tab_name == "You may know":
                            # Look for "You may know" specific content
                            driver.find_element(By.XPATH, '//span[contains(text(), "You may know")]')
                        else:
                            driver.find_element(By.XPATH, '//span[contains(text(), "Spam")]')
                        
                        if logger:
                            logger.info(f"Tab '{tab_name}' appears to already be active")
                        return True
                except Exception:
                    pass
            
            # Wait before retry
            time.sleep(2)
            
        except Exception as e:
            if logger:
                logger.warning(f"Error switching to '{tab_name}' tab (attempt {attempt + 1}): {type(e).__name__}")
            time.sleep(2)
    
    if logger:
        logger.error(f"Failed to switch to '{tab_name}' tab after {max_attempts} attempts")
    return False


def has_new_threads(driver, logger=None):
    """Check if there are any real, actionable new message threads in the current tab."""
    try:
        threads = driver.find_elements(By.XPATH, '//div[@role="row"]')
        real_threads = []
        for t in threads:
            text = t.text.strip()
            # Filter out rows that are empty or contain placeholder/empty state text
            if not text or 'No message requests' in text or 'You may know' in text or 'Spam' in text:
                continue
            # Skip threads previously marked as uncontactable/no-textbox
            chat_name = text.split('\n')[0]
            if chat_name in IGNORED_REQUESTS:
                if logger:
                    logger.debug(f"Skipping ignored chat: {chat_name}")
                continue
            real_threads.append(t)
        if real_threads:
            if logger:
                logger.info(f"Found {len(real_threads)} new message thread(s).")
            # Remove print fallback to prevent console contamination
            return real_threads
        else:
            return []
    except Exception as e:
        if logger:
            logger.error(f"Error checking for new threads: {type(e).__name__}: {e}")
        # Remove print fallback to prevent console contamination
        return []


def accept_and_reply(driver, reply_message, config, logger, option_type='option1'):
    """Accept message requests and send replies."""
    threads = has_new_threads(driver, logger)
    if not threads:
        logger.info("No threads to process. Skipping interaction.")
        return

    processed_threads = set()
    for i, thread in enumerate(threads):
        try:
            thread_id = thread.text.strip()
            if not thread_id or thread_id in processed_threads:
                continue
            chat_name = thread.text.strip().split('\n')[0] if thread.text.strip() else "(unknown chat)"
        except Exception as e:
            logger.warning(f"Could not get thread info for thread {i}: {e}")
            continue
            
        logger.info(f"Preparing to reply to chat: {chat_name}")
        retry_count = 0
        max_retries = 3
        opened = False
        
        while retry_count < max_retries:
            try:
                # Re-find the thread element to avoid stale element issues
                fresh_threads = has_new_threads(driver, logger)
                if not fresh_threads or i >= len(fresh_threads):
                    logger.warning(f"Thread {i} no longer available after refresh")
                    break
                    
                current_thread = fresh_threads[i]
                
                # Try multiple clicking strategies
                click_success = False
                click_strategies = [
                    lambda: current_thread.click(),
                    lambda: driver.execute_script("arguments[0].click();", current_thread),
                    lambda: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));", current_thread)
                ]
                
                for strategy_num, strategy in enumerate(click_strategies, 1):
                    try:
                        if current_thread.is_displayed() and current_thread.is_enabled():
                            strategy()
                            logger.info(f"Clicked chat '{chat_name}' using strategy {strategy_num}")
                            click_success = True
                            break
                    except Exception as click_e:
                        logger.debug(f"Click strategy {strategy_num} failed: {click_e}")
                        continue
                
                if not click_success:
                    raise Exception("All click strategies failed")
                
                time.sleep(config.get("delay_click_request", 3))
                
                # In case opening the thread surfaced the load error, try recovery
                recover_couldnt_load_chats(driver, logger)
                
                # Confirm chat opened by checking for message box (more reliable than header)
                chat_opened = False
                verification_methods = [
                    # Method 1: Look for message textbox (most reliable)
                    lambda: driver.find_element(By.XPATH, '//div[@role="textbox"]').is_displayed(),
                    # Method 2: Look for message input area
                    lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "message") or contains(@placeholder, "message")]').is_displayed(),
                    # Method 3: Look for chat conversation area
                    lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "conversation") or contains(@role, "log")]').is_displayed()
                ]
                
                for method_num, method in enumerate(verification_methods, 1):
                    try:
                        if method():
                            logger.info(f"Chat '{chat_name}' opened successfully (verified by method {method_num})")
                            chat_opened = True
                            break
                    except Exception:
                        continue
                
                if chat_opened:
                    # Cache the opened chat URL to recover if Messenger redirects away
                    try:
                        cached_chat_url = driver.current_url
                        logger.debug(f"Cached chat URL for '{chat_name}': {cached_chat_url}")
                    except Exception:
                        cached_chat_url = None
                    opened = True
                    break
                else:
                    logger.warning(f"Chat '{chat_name}' may not have opened properly. Retrying...")
                    retry_count += 1
                    time.sleep(2)
                    
            except Exception as e:
                retry_count += 1
                logger.error(f"Could not click chat '{chat_name}': {type(e).__name__}: {e} (attempt {retry_count}/{max_retries})")
                time.sleep(2)
        if not opened:
            logger.info(f"Skipping chat '{chat_name}' after {max_retries} failed attempts to open.")
            continue
        # Click 'Accept' button if present to reveal message box
        clicked_accept = click_accept_if_present(driver, logger)
        if clicked_accept:
            time.sleep(config.get("delay_accept", 2))
        # Legacy fallback kept if needed
        # Accept button auto-click handled above
        try:
            accept_button = driver.find_element(By.XPATH, '//div[text()="Accept"]')
            accept_button.click()
            time.sleep(config.get("delay_accept", 2))
        except Exception:
            pass
        # Detect uncontactable or no-textbox cases, ignore and return to list
        try:
            # Common banners/messages for uncontactable chats
            uncontactable_xpath_variants = [
                "//div[contains(text(), 'not contactable on Messenger')]",
                "//div[contains(text(), 'You can no longer reply')]",
                "//span[contains(text(), 'not contactable') or contains(text(), \"can't reply\")]",
                "//span[contains(text(), 'not contactable') or contains(text(), 'can’t reply')]",
                "//div[contains(text(), 'This person is not available')]",
            ]
            uncontactable_found = False
            for xp in uncontactable_xpath_variants:
                try:
                    elems = driver.find_elements(By.XPATH, xp)
                    if any(e.is_displayed() for e in elems):
                        uncontactable_found = True
                        break
                except Exception:
                    continue
            if uncontactable_found:
                IGNORED_REQUESTS.add(chat_name)
                logger.warning(f"Ignoring chat '{chat_name}' permanently: uncontactable.")
                try:
                    driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
                except Exception:
                    pass
                try:
                    driver.get("https://www.messenger.com/e2ee/requests/")
                except Exception:
                    pass
                time.sleep(1)
                continue
        except Exception:
            pass

        # Find the message box, paste the reply, and ensure send even if redirected
        send_success = False
        processed_message = process_random_message(reply_message)
        # Use a distinctive snippet to verify appearance in the chat
        verify_snippet = processed_message[:30].strip()
        for send_attempt in range(3):
            try:
                # If we were redirected or chat lost, recover using cached URL
                try:
                    off_route = is_interrupted_page(driver) or (driver.current_url and "e2ee/requests" in driver.current_url)
                except Exception:
                    off_route = False
                if off_route and 'cached_chat_url' in locals() and cached_chat_url:
                    logger.warning("Detected off-route or unavailable page during send. Recovering to cached chat URL…")
                    try:
                        driver.get(cached_chat_url)
                        time.sleep(config.get("delay_chat_load", 5))
                        recover_couldnt_load_chats(driver, logger)
                        auto_close_popups(driver, logger)
                    except Exception:
                        pass

                # Ensure textbox is visible/clickable
                message_box = None
                try:
                    message_box = WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.XPATH, '//div[@role="textbox"]'))
                    )
                except Exception:
                    # Try clicking Accept again if it reappeared
                    click_accept_if_present(driver, logger)
                    try:
                        message_box = WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.XPATH, '//div[@role="textbox"]'))
                        )
                    except Exception as e2:
                        logger.warning(f"Send attempt {send_attempt+1}: textbox not ready: {e2}")
                        continue

                # Focus the textbox via multiple strategies
                focus_strategies = [
                    lambda: message_box.click(),
                    lambda: driver.execute_script("arguments[0].click();", message_box),
                    lambda: driver.execute_script("arguments[0].focus();", message_box)
                ]
                focused = False
                for strategy in focus_strategies:
                    try:
                        if message_box.is_displayed() and message_box.is_enabled():
                            strategy()
                            focused = True
                            break
                    except Exception:
                        continue
                if not focused:
                    logger.warning(f"Send attempt {send_attempt+1}: could not focus textbox.")
                    continue

                # Paste and send
                pyperclip.copy(processed_message)
                message_box.send_keys(Keys.CONTROL, "v")
                logger.info(f"Pasted the message from clipboard for chat '{chat_name}'")
                time.sleep(0.8)
                message_box.send_keys(Keys.RETURN)
                time.sleep(max(1, int(config.get("delay_between_messages", 3))))

                # Verify the message appears in the conversation (best-effort)
                verify_ok = False
                try:
                    if verify_snippet:
                        elems = driver.find_elements(By.XPATH, f"//*[contains(normalize-space(.), '{verify_snippet.replace("'", "\\'")}')]")
                        verify_ok = any(e.is_displayed() for e in elems)
                except Exception:
                    verify_ok = False

                # Also treat staying on the chat with textbox visible as success (fallback)
                if not verify_ok:
                    try:
                        verify_ok = bool(driver.find_elements(By.XPATH, '//div[@role="textbox"]'))
                    except Exception:
                        pass

                if verify_ok:
                    # Update statistics for option 1
                    account_name = os.path.basename(logger.handlers[0].baseFilename).replace('.log', '') if logger.handlers else 'unknown'
                    update_statistics(option_type, account_name)
                    logger.info(f"Message sent successfully to {chat_name}")
                    send_success = True
                    break
                else:
                    logger.warning(f"Send verification failed for '{chat_name}' (attempt {send_attempt+1}). Retrying…")
                    # If verification failed and we have the cached URL, try reopening before next attempt
                    if 'cached_chat_url' in locals() and cached_chat_url:
                        try:
                            driver.get(cached_chat_url)
                            time.sleep(config.get("delay_chat_load", 5))
                            recover_couldnt_load_chats(driver, logger)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Could not send message to '{chat_name}': {type(e).__name__}: {e}")
                continue

        if not send_success:
            # If textbox not found or send repeatedly failed, mark ignore and return to list
            IGNORED_REQUESTS.add(chat_name)
            logger.warning(f"Failed to confirm sending to '{chat_name}'. Marked as ignored for this run and returning to list.")
            try:
                driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            except Exception:
                pass
            try:
                driver.get("https://www.messenger.com/e2ee/requests/")
            except Exception:
                pass
            time.sleep(1)
            continue

        # Mark as processed only after verified send
        processed_threads.add(thread_id)
        logger.info(f"Marked chat '{chat_name}' as processed")
            
        # Navigate back to message requests list (multiple strategies)
        back_navigation_success = False
        navigation_strategies = [
            # Strategy 1: Look for back arrow button
            lambda: driver.find_element(By.XPATH, '//div[@aria-label="Back" or @aria-label="Go back"]').click(),
            # Strategy 2: Look for close button
            lambda: driver.find_element(By.XPATH, '//div[@aria-label="Close"]').click(),
            # Strategy 3: Look for X button
            lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "Close") or text()="×"]').click(),
            # Strategy 4: Press Escape key
            lambda: driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE),
            # Strategy 5: Navigate back to requests URL
            lambda: driver.get("https://www.messenger.com/e2ee/requests/")
        ]
        
        for strategy_num, strategy in enumerate(navigation_strategies, 1):
            try:
                strategy()
                logger.info(f"Navigated back using strategy {strategy_num} after replying to '{chat_name}'")
                back_navigation_success = True
                time.sleep(2)
                break
            except Exception as nav_e:
                logger.debug(f"Navigation strategy {strategy_num} failed: {nav_e}")
                continue
        
        if not back_navigation_success:
            logger.warning(f"Could not navigate back after replying to '{chat_name}' - trying to continue anyway")
        
        # Wait a moment before processing next chat
        time.sleep(1)
        continue



def current_account_name(logger):
    try:
        return logger.name.replace('bot_', '')
    except Exception:
        return 'unknown'

def is_logged_out(driver):
    try:
        url = driver.current_url or ""
        page_source = driver.page_source.lower()
        
        # Check for Messenger Continue as page - this is logged IN, not out
        if "messenger.com" in url and ("continue as" in page_source or "continue" in page_source):
            # Check for profile picture or name indicator on Continue as page
            if driver.find_elements(By.XPATH, "//img[contains(@alt, 'profile picture')]") or \
               driver.find_elements(By.XPATH, "//button[contains(text(), 'Continue')]"):
                # This is the Continue as page, not logged out
                return False
        
        if "login" in url or "recover" in url:
            return True
        if driver.find_elements(By.NAME, 'email') or driver.find_elements(By.NAME, 'pass'):
            return True
        if driver.find_elements(By.XPATH, "//button[contains(., 'Log in') or contains(., 'Login')]"):
            if not driver.find_elements(By.XPATH, "//div[@role='textbox']"):
                return True
        return False
    except Exception:
        return False

LAST_TELEGRAM_NOTIFY = {}

# Throttle map for chat load recovery per account
CHAT_LOAD_RECOVERY = {}

def recover_couldnt_load_chats(driver, logger=None, min_interval=30):
    """Detect the 'Couldn't load chats' popup and recover by clicking Reload or refreshing.
    Throttled per account to avoid loops.
    Returns True if a recovery action was attempted.
    """
    try:
        # Detect error text anywhere in the page
        err_elems = driver.find_elements(By.XPATH, "//*[contains(., \"Couldn't load chats\")] | //*[@role='alert' and contains(., 'load chats')]")
        if not any(e.is_displayed() for e in err_elems):
            return False
        acct = current_account_name(logger) if logger else 'unknown'
        now = time.time()
        last = CHAT_LOAD_RECOVERY.get(acct, 0)
        if now - last < min_interval:
            # Recently recovered; skip to avoid loops
            return False
        # Try clicking any visible Reload button
        clicked = False
        reload_selectors = [
            "//button[normalize-space(text())='Reload']",
            "//div[@role='button'][normalize-space(.)='Reload']",
            "//*[@aria-label='Reload']",
        ]
        for sel in reload_selectors:
            try:
                btns = driver.find_elements(By.XPATH, sel)
                for b in btns:
                    if b.is_displayed() and b.is_enabled():
                        driver.execute_script("arguments[0].click();", b)
                        clicked = True
                        break
                if clicked:
                    break
            except Exception:
                continue
        if not clicked:
            try:
                driver.refresh()
                clicked = True
            except Exception:
                clicked = False
        if clicked:
            CHAT_LOAD_RECOVERY[acct] = now
            if logger:
                logger.info("Detected 'Couldn't load chats'. Triggered reload to recover.")
            # Small wait to allow reload
            time.sleep(2)
            return True
        return False
    except Exception:
        return False

def notify_telegram(config, text):
    """Send a Telegram message with simple de-dup throttling to avoid spam.
    Uses message text as the key; suppresses repeats within telegram_min_interval_sec (default 300s).
    """
    try:
        token = config.get('telegram_bot_token')
        chat_id = config.get('telegram_chat_id')
        if not token or not chat_id:
            return False
        # Throttle
        min_interval = int(config.get('telegram_min_interval_sec', 300))
        now = time.time()
        last = LAST_TELEGRAM_NOTIFY.get(text, 0)
        if now - last < min_interval:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=10)
        if resp.ok:
            LAST_TELEGRAM_NOTIFY[text] = now
        return resp.ok
    except Exception:
        return False

def refresh_account_cookies(fname, accounts_dir, config, logger=None):
    try:
        # 1) Try Google Sheets API mode if enabled
        gs = (config or {}).get('google_sheets') or {}
        if gs.get('enabled') and (gs.get('mode') == 'api'):
            try:
                import gspread
                from google.oauth2.service_account import Credentials
                
                service_account_file = gs.get('service_account_json', 'service_account.json')
                spreadsheet_id = gs.get('spreadsheet_id')
                sheet_name = gs.get('sheet_name', 'Sheet1')
                account_col = gs.get('account_column', 'account_file')
                json_col = gs.get('json_column', 'cookies_json')
                
                if not spreadsheet_id:
                    if logger:
                        logger.error("Google Sheets API: No spreadsheet_id configured")
                    # Fall through to other methods
                else:
                    scopes = ['https://www.googleapis.com/auth/spreadsheets']
                    creds = Credentials.from_service_account_file(service_account_file, scopes=scopes)
                    client = gspread.authorize(creds)
                    spreadsheet = client.open_by_key(spreadsheet_id)
                    worksheet = spreadsheet.worksheet(sheet_name)
                    
                    # Get all records
                    records = worksheet.get_all_records()
                    
                    # Find row with matching account (tolerant to spaces/case)
                    matched_row = None
                    target_fname = fname.strip().lower()
                    
                    # Normalize header keys for record lookup
                    norm_json_col = json_col.strip().lower()
                    
                    for row in records:
                        # Find the actual key that matches our desired account_col
                        current_acct_val = ""
                        actual_json_key = None
                        
                        for k, v in row.items():
                            k_norm = str(k).strip().lower()
                            if k_norm == account_col.strip().lower():
                                current_acct_val = str(v).strip().lower()
                            if k_norm == norm_json_col:
                                actual_json_key = k
                        
                        # Match logic: check with and without .json extension
                        if current_acct_val == target_fname or \
                           current_acct_val.replace('.json', '') == target_fname.replace('.json', ''):
                            matched_row = row
                            record_json_key = actual_json_key
                            break
                    
                    if not matched_row:
                        if logger:
                            logger.error(f"Google Sheets API: No row found for account {fname}. Searched column '{account_col}' in {len(records)} records.")
                    else:
                        # Use the actual key found (handles trailing spaces in sheet headers)
                        raw_json = str(matched_row.get(record_json_key or json_col, '')).strip()
                        if logger:
                            logger.info(f"Google Sheets API: Found cookies for {fname} (length={len(raw_json)})")
                        
                        if not raw_json:
                            if logger:
                                logger.error(f"Google Sheets API: Empty cookies for {fname}")
                        else:
                            try:
                                data = json.loads(raw_json)
                                if not isinstance(data, list):
                                    raise ValueError('cookies_json is not a JSON list')
                                
                                path = os.path.join(accounts_dir, fname)
                                with open(path, 'w', encoding='utf-8') as f:
                                    json.dump(data, f, ensure_ascii=False, indent=2)
                                
                                if logger:
                                    logger.info(f"Updated cookies for {fname} from Google Sheets API")
                                return True
                            except Exception as je:
                                if logger:
                                    logger.error(f"Google Sheets API: Invalid JSON for {fname}: {je}")
                    
                    # Fall through if we didn't return True
            except Exception as ge:
                if logger:
                    logger.error(f"Google Sheets API error for {fname}: {type(ge).__name__}: {ge}")
                # fall through to other methods
        
        # 2) Try Google Sheets (published CSV) if enabled — also runs as fallback when API mode failed
        if gs.get('enabled') and gs.get('published_csv_url') and (gs.get('mode') in ('published_csv', 'api')):
            url = gs.get('published_csv_url')
            account_col = gs.get('account_column', 'account_file')
            json_col = gs.get('json_column', 'cookies_json')
            if url:
                try:
                    resp = requests.get(url, timeout=15)
                    resp.raise_for_status()
                    # Use StringIO so csv module can properly handle quoted embedded newlines
                    sio = io.StringIO(resp.text)
                    # Detect delimiter (supports CSV or TSV)
                    sample = resp.text[:2048]
                    try:
                        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                        reader = csv.DictReader(sio, dialect=dialect)
                    except Exception:
                        # Fallback to comma
                        sio.seek(0)
                        reader = csv.DictReader(sio)

                    # Normalize header names to be tolerant to spaces/BOM/case
                    def _norm(s):
                        try:
                            return str(s).replace('\ufeff', '').strip().lower().replace(' ', '')
                        except Exception:
                            return ''

                    # Robust header matching for CSV (tolerant to spaces/case)
                    norm_acct_target = account_col.strip().lower()
                    norm_json_target = json_col.strip().lower()

                    acct_key = None
                    json_key = None

                    for h in (reader.fieldnames or []):
                        h_norm = str(h).strip().lower()
                        if h_norm == norm_acct_target or h_norm in ['account_file', 'account', 'accountfile']:
                            acct_key = h
                        if h_norm == norm_json_target or h_norm in ['cookies_json', 'cookies', 'cookie_json']:
                            json_key = h

                    if not acct_key or not json_key:
                        if logger:
                            logger.error(f"Google Sheet CSV: Required headers not found (target: '{account_col}', '{json_col}'). Have: {fieldnames}")
                        return False
                    else:
                        if logger:
                            logger.info(f"Google Sheet CSV: Using columns acct='{acct_key}', json='{json_key}'")

                    matched_row = None
                    for row in reader:
                        if not row:
                            continue
                        acct_val = str(row.get(acct_key, '')).strip()
                        if logger:
                            try:
                                logger.debug(f"Google Sheet CSV: Row account_file='{acct_val}'")
                            except Exception:
                                pass
                        if acct_val == fname:
                            matched_row = row
                            break
                    if not matched_row:
                        if logger:
                            logger.error(f"Google Sheet CSV: No row found for account {fname} in column '{account_col}'.")
                        return False
                    raw_json = str(matched_row.get(json_key, '')).strip()
                    if logger:
                        try:
                            preview = (raw_json[:80] + '...') if len(raw_json) > 80 else raw_json
                            logger.info(f"Google Sheet CSV: Matched account '{fname}', cookies_json length={len(raw_json)} preview='{preview}'")
                        except Exception:
                            pass
                    if not raw_json:
                        if logger:
                            logger.error(f"Google Sheet CSV: Empty '{json_key}' for account {fname}.")
                        return False
                    try:
                        data = json.loads(raw_json)
                        if not isinstance(data, list):
                            raise ValueError('cookies_json is not a JSON list')
                    except Exception as je:
                        if logger:
                            logger.error(f"Google Sheet CSV: Invalid JSON for {fname}: {type(je).__name__}: {je}")
                        return False
                    path = os.path.join(accounts_dir, fname)
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    if logger:
                        logger.info(f"Updated cookies for {fname} from Google Sheets CSV")
                    return True
                except Exception as ge:
                    if logger:
                        logger.error(f"Google Sheets CSV fetch error for {fname}: {type(ge).__name__}: {ge}")
                    # fall through to remote_cookie_sources

        # 3) Fallback to remote_cookie_sources mapping (raw JSON URLs)
        sources = config.get('remote_cookie_sources', {}) or {}
        url = sources.get(fname) or sources.get(fname.split('_')[0])
        if not url:
            if logger:
                gs = (config or {}).get('google_sheets') or {}
                if gs.get('enabled') and gs.get('mode') == 'api':
                    logger.error(f"Google Sheets API failed to refresh cookies for {fname}. Check logs above for details.")
                else:
                    logger.error(f"No remote cookie URL configured for {fname} (remote_cookie_sources)")
            return False
        # Mega.nz links are not direct-raw JSON and will return an HTML viewer page.
        if 'mega.nz' in url:
            if logger:
                logger.error(
                    "Remote URL points to Mega.nz viewer page. Please supply a direct RAW JSON URL (e.g., https://raw.githubusercontent.com/... or Pastebin raw)."
                )
            return False
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        path = os.path.join(accounts_dir, fname)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if logger:
            logger.info(f"Downloaded and updated cookies for {fname} from remote source")
        return True
    except Exception as e:
        if logger:
            logger.error(f"Failed to refresh cookies for {fname}: {type(e).__name__}: {e}")
        return False

def reload_cookies_into_driver(driver, cookies):
    """Robustly inject cookies by visiting both Facebook and Messenger domains.
    Handles 'expirationDate' vs 'expiry' field names used by different exporters.
    """
    try:
        if not cookies:
            return False
            
        # 1) Inject Facebook cookies
        try:
            driver.get("https://www.facebook.com/")
            time.sleep(1)
            for ck in cookies:
                try:
                    if ".facebook.com" in (ck.get("domain") or ""):
                        c = ck.copy()
                        # Handle Selenium vs JSON-export field names
                        if 'expirationDate' in c and 'expiry' not in c:
                            c['expiry'] = int(c.pop('expirationDate'))
                        # Remove potentially incompatible fields
                        for f in ['expiry', 'expirationDate']:
                            if f in c and (c[f] is None or c[f] < 0):
                                c.pop(f, None)
                        driver.add_cookie(c)
                except Exception:
                    continue
        except Exception:
            pass

        # 2) Inject Messenger cookies
        try:
            driver.get("https://www.messenger.com/")
            time.sleep(1)
            for ck in cookies:
                try:
                    if ".messenger.com" in (ck.get("domain") or ""):
                        c = ck.copy()
                        if 'expirationDate' in c and 'expiry' not in c:
                            c['expiry'] = int(c.pop('expirationDate'))
                        for f in ['expiry', 'expirationDate']:
                            if f in c and (c[f] is None or c[f] < 0):
                                c.pop(f, None)
                        driver.add_cookie(c)
                except Exception:
                    continue
            driver.refresh()
            time.sleep(2)
        except Exception:
            pass
            
        return True
    except Exception:
        return False

def is_interrupted_page(driver):
    """Heuristics to detect when navigation is stuck on an invalid/interruption page."""
    try:
        url = (driver.current_url or "").lower()
        if "e2ee/requests" in url or "page_not_found" in url or "unavailable" in url:
            return True
        html = (driver.page_source or "").lower()
        if "this page isn't available" in html or "this page isn" in html:
            return True
    except Exception:
        return False
    return False

def rehome_if_interrupted(driver, logger, config):
    """If on an interruption page or off-site, go back to messenger home and recover UI.
    Skips rehoming if on login pages to prevent redirect loops during logout recovery."""
    try:
        current_url = (driver.current_url or "").lower()
        
        # Don't rehome if on login/recovery pages - let logout handler deal with it
        if any(skip in current_url for skip in ["facebook.com/login", "/recover", "checkpoint", "two-factor", "2fa"]):
            return False
        
        url_ok = current_url.startswith("https://www.messenger.com/")
        
        if (not url_ok) or is_interrupted_page(driver):
            logger.info("Detected interruption/off-route. Rehoming to messenger.com ...")
            driver.get("https://www.messenger.com/")
            time.sleep(config.get("delay_chat_load", 5))
            # Attempt banner recovery and popup close
            try:
                recover_couldnt_load_chats(driver, logger)
            except Exception:
                pass
            try:
                auto_close_popups(driver, logger)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False

def handle_logout_and_refresh(driver, logger, config, account_fname):
    # Post or update the persistent per-account notice instead of spamming Telegram
    send_or_update_failed_login_notice(account_fname, config, "🔴 Logged out — attempting cookie refresh…")
    ok = refresh_account_cookies(account_fname, "accounts", config, logger)
    if not ok:
        logger.error("Remote cookie refresh failed.")
        # Keep the persistent notice, updating its reason text
        send_or_update_failed_login_notice(account_fname, config, "🔴 Cookie refresh FAILED — please update cookies.")
        return False
    try:
        new_cookies = load_cookies_from_file(os.path.join("accounts", account_fname))
        if reload_cookies_into_driver(driver, new_cookies):
            time.sleep(config.get('delay_retry_login', 4))
            if not is_logged_out(driver):
                logger.info("Auto re-login successful after cookie refresh.")
                # Clear the persistent notice on success
                clear_failed_login_notice(account_fname, config)
                return True
        logger.error("Re-login after cookie refresh did not succeed.")
        # Keep the persistent notice to signal ongoing problem
        send_or_update_failed_login_notice(account_fname, config, "🔴 Still logged out after cookie refresh.")
    except Exception as e:
        logger.error(f"Error during re-login: {type(e).__name__}: {e}")
    return False

def generate_totp_code(totp_secret):
    """Generate TOTP 2FA code from secret."""
    try:
        if not totp_secret:
            return None
        totp = pyotp.TOTP(totp_secret.replace(" ", "").strip())
        return totp.now()
    except Exception as e:
        print(f"Error generating TOTP: {e}")
        return None

def update_cookies_to_google_sheets(account_fname, cookies_json, config, logger=None):
    """Update cookies JSON back to Google Sheets after fresh login."""
    import json
    try:
        gs_cfg = (config or {}).get('google_sheets') or {}
        if not gs_cfg.get('enabled'):
            if logger:
                logger.info("Google Sheets sync not enabled, skipping cookie upload")
            return False
        
        # Check mode: api or published_csv
        mode = gs_cfg.get('mode', 'published_csv')
        
        if mode == 'api':
            # Use Google Sheets API to write cookies
            try:
                import gspread
                from google.oauth2.service_account import Credentials
            except ImportError:
                if logger:
                    logger.error("gspread not installed. Run: pip install gspread")
                return False
            
            try:
                # Load service account credentials
                service_account_file = gs_cfg.get('service_account_json', 'service_account.json')
                spreadsheet_id = gs_cfg.get('spreadsheet_id')
                sheet_name = gs_cfg.get('sheet_name', 'Sheet1')
                account_col = gs_cfg.get('account_column', 'account_file')
                json_col = gs_cfg.get('json_column', 'cookies_json')
                
                if not spreadsheet_id:
                    if logger:
                        logger.error("No spreadsheet_id configured for API mode")
                    return False
                
                # Authenticate and open spreadsheet
                scopes = ['https://www.googleapis.com/auth/spreadsheets']
                creds = Credentials.from_service_account_file(service_account_file, scopes=scopes)
                client = gspread.authorize(creds)
                spreadsheet = client.open_by_key(spreadsheet_id)
                worksheet = spreadsheet.worksheet(sheet_name)
                
                # Find the row with matching account
                account_cell = worksheet.find(account_fname, in_column=1)
                
                # Convert cookies to JSON string
                cookies_str = json.dumps(cookies_json, ensure_ascii=False, indent=2)
                
                if account_cell:
                    # Update existing row
                    row = account_cell.row
                    # Find json column by normalized name
                    json_col_idx = None
                    try:
                        headers = worksheet.row_values(1)
                        norm_target = json_col.strip().lower()
                        for i, h in enumerate(headers):
                            if str(h).strip().lower() == norm_target:
                                json_col_idx = i + 1
                                break
                    except Exception:
                        pass
                    
                    if not json_col_idx:
                        json_col_idx = 2  # Fallback to column B
                        if logger:
                            logger.warning(f"Could not find header '{json_col}' (even after stripping). Defaulting to column {json_col_idx}.")
                    
                    worksheet.update_cell(row, json_col_idx, cookies_str)
                    if logger:
                        logger.info(f"Updated cookies for {account_fname} in Google Sheets (row {row})")
                else:
                    # Append new row
                    worksheet.append_row([account_fname, cookies_str])
                    if logger:
                        logger.info(f"Added new row for {account_fname} to Google Sheets")
                
                return True
                
            except Exception as e:
                if logger:
                    logger.error(f"Error writing to Google Sheets API: {e}")
                return False
        
        else:
            # Published CSV mode - can't write back, just log
            if logger:
                logger.info(f"Account {account_fname} logged in. Cookies saved locally only (published_csv mode)")
            return True
            
    except Exception as e:
        if logger:
            logger.error(f"Error updating cookies to Google Sheets: {e}")
        return False

def login_with_credentials(driver, username, password, totp_secret, logger=None, config=None):
    """
    Perform fresh login to Facebook/Messenger using email, password, and TOTP 2FA.
    Returns True if login successful, False otherwise.
    """
    try:
        if not username or not password:
            if logger:
                logger.error("Username or password not provided for fresh login")
            return False
        
        if logger:
            logger.info(f"Starting fresh login for {username}")
        
        # Navigate to Facebook login page (more reliable than Messenger for credentials)
        driver.get("https://www.facebook.com/login")
        time.sleep(3)
        
        # Clear any existing cookies
        driver.delete_all_cookies()
        driver.refresh()
        time.sleep(2)
        
        # Find and fill email field (multiple strategies for Facebook's dynamic fields)
        email_field = None
        email_selectors = [
            (By.ID, "email"),
            (By.NAME, "email"),
            (By.CSS_SELECTOR, "input[type='text'][name='email']"),
            (By.CSS_SELECTOR, "input[placeholder*='Email' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='email' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='Email' i]"),
            (By.XPATH, "//input[@type='text' or @type='email'][contains(@aria-label, 'Email') or contains(@placeholder, 'Email')]"),
            (By.XPATH, "//input[contains(@name, 'email') or contains(@id, 'email')]"),
            (By.CSS_SELECTOR, "form input[type='text']:first-of-type"),  # First text input in form
        ]
        
        for by, selector in email_selectors:
            try:
                email_field = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((by, selector))
                )
                if email_field and email_field.is_displayed():
                    if logger:
                        logger.info(f"Found email field using: {selector}")
                    break
            except Exception:
                continue
        
        if not email_field:
            # Try finding any visible text input that looks like email
            try:
                inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email']")
                for inp in inputs:
                    if inp.is_displayed() and inp.size['height'] > 20:  # Reasonable size
                        placeholder = inp.get_attribute('placeholder') or ''
                        aria_label = inp.get_attribute('aria-label') or ''
                        if 'email' in placeholder.lower() or 'mail' in placeholder.lower() or \
                           'email' in aria_label.lower() or 'mail' in aria_label.lower() or \
                           inp.location['y'] < 300:  # Upper part of page
                            email_field = inp
                            if logger:
                                logger.info("Found email field via scan")
                            break
            except Exception:
                pass
        
        if not email_field:
            if logger:
                logger.error("Could not find email field with any selector strategy")
            return False
        
        try:
            email_field.clear()
            email_field.send_keys(username)
            if logger:
                logger.info(f"Entered email: {username}")
        except Exception as e:
            if logger:
                logger.error(f"Could not enter email: {e}")
            return False
        
        # Find and fill password field (multiple strategies)
        pass_field = None
        pass_selectors = [
            (By.ID, "pass"),
            (By.NAME, "pass"),
            (By.NAME, "password"),
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[placeholder*='Password' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='Password' i]"),
            (By.XPATH, "//input[@type='password']"),
            (By.XPATH, "//input[contains(@name, 'pass') or contains(@id, 'pass')]"),
        ]
        
        for by, selector in pass_selectors:
            try:
                pass_field = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((by, selector))
                )
                if pass_field and pass_field.is_displayed():
                    if logger:
                        logger.info(f"Found password field using: {selector}")
                    break
            except Exception:
                continue
        
        if not pass_field:
            if logger:
                logger.error("Could not find password field")
            return False
        
        try:
            pass_field.clear()
            pass_field.send_keys(password)
            if logger:
                logger.info("Entered password")
        except Exception as e:
            if logger:
                logger.error(f"Could not enter password: {e}")
            return False
        
        # Click login button (multiple strategies)
        login_clicked = False
        login_btn_selectors = [
            (By.NAME, "login"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(text(), 'Log in') or contains(text(), 'Login')]"),
            (By.XPATH, "//button[contains(@type, 'submit')]"),
            (By.CSS_SELECTOR, "[data-testid='royal_login_button']"),
            (By.CSS_SELECTOR, "[role='button'][type='submit']"),
        ]
        
        for by, selector in login_btn_selectors:
            try:
                login_btn = driver.find_element(by, selector)
                if login_btn and login_btn.is_displayed():
                    login_btn.click()
                    login_clicked = True
                    if logger:
                        logger.info(f"Clicked login button using: {selector}")
                    break
            except Exception:
                continue
        
        if not login_clicked:
            # Try alternative: submit with Enter key on password field
            try:
                pass_field.send_keys(Keys.RETURN)
                login_clicked = True
                if logger:
                    logger.info("Submitted with Enter key")
            except Exception as e:
                if logger:
                    logger.error(f"Could not click login button: {e}")
                return False
        
        time.sleep(4)
        
        # Check for 2FA/TOTP field
        current_url = driver.current_url.lower()
        if logger:
            logger.info(f"After login, URL is: {current_url}")
        
        # Various indicators of 2FA page
        is_2fa_page = any(indicator in current_url or indicator in driver.page_source.lower() 
                          for indicator in [
                              "two-factor", "2fa", "twofactor", "approvals_code",
                              "checkpoint", "authentication", "security_code",
                              "login/checkpoint", "confirmlogin"
                          ])
        
        if is_2fa_page or "two" in driver.page_source.lower():
            if logger:
                logger.info("2FA/TOTP page detected")
            
            # Check if 2FA is explicitly disabled per user request
            if (config or {}).get('disable_2fa', False):
                if logger:
                    logger.warning("2FA is disabled in config. Skipping 2FA entry logic.")
                return False  # Return False to trigger cookie refresh retry loop
            
            if not totp_secret:
                if logger:
                    logger.error("2FA required but no TOTP secret provided")
                return False
            
            # Try to find 2FA input field once (will reuse for all attempts)
            code_field = None
            code_selectors = [
                'input[name="approvals_code"]',
                'input[aria-label*="code" i]',
                'input[placeholder*="code" i]',
                'input#recovery_code',
                'input[type="text"]',
                'input[autocomplete="one-time-code"]',
                'input[data-testid="recovery_code_input"]'
            ]
            
            for selector in code_selectors:
                try:
                    code_field = WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    if code_field and code_field.is_displayed():
                        if logger:
                            logger.info(f"Found 2FA code field with selector: {selector}")
                        break
                except Exception:
                    continue
            
            if not code_field:
                # Try finding by partial text
                try:
                    labels = driver.find_elements(By.XPATH, 
                        '//*[contains(text(), "code") or contains(text(), "Code") or contains(text(), "authentication")]')
                    for label in labels:
                        try:
                            parent = label.find_element(By.XPATH, "..")
                            inputs = parent.find_elements(By.TAG_NAME, "input")
                            if inputs:
                                code_field = inputs[0]
                                if logger:
                                    logger.info("Found 2FA field via text search")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            
            if not code_field:
                if logger:
                    logger.error("Could not find 2FA code input field")
                return False
            
            # Try 2FA code up to 5 times with fresh TOTP codes
            max_2fa_attempts = 5
            code_accepted = False
            last_code = None
            
            for attempt in range(max_2fa_attempts):
                # Generate fresh TOTP code for each attempt
                totp_code = generate_totp_code(totp_secret)
                if not totp_code:
                    if logger:
                        logger.error(f"Attempt {attempt + 1}: Failed to generate TOTP code")
                    time.sleep(2)
                    continue
                
                # If same code as last attempt, wait for next 30-second window
                if totp_code == last_code and attempt > 0:
                    if logger:
                        logger.info(f"Same code as before ({totp_code}), waiting for next 30s window...")
                    # Wait up to 30 seconds for code to change
                    for wait_sec in range(30):
                        time.sleep(1)
                        new_code = generate_totp_code(totp_secret)
                        if new_code != totp_code:
                            totp_code = new_code
                            if logger:
                                logger.info(f"Got new code after {wait_sec + 1}s: {totp_code}")
                            break
                    else:
                        if logger:
                            logger.warning("Code didn't change after 30s, using same code anyway")
                
                last_code = totp_code
                
                if logger:
                    logger.info(f"Attempt {attempt + 1}/{max_2fa_attempts}: Entering TOTP code: {totp_code}")
                
                try:
                    # Re-find the code field on each attempt (page may have changed)
                    code_field = None
                    for selector in code_selectors:
                        try:
                            code_field = WebDriverWait(driver, 2).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                            )
                            if code_field and code_field.is_displayed():
                                break
                        except Exception:
                            continue
                    
                    if not code_field:
                        # Code field not found - check if we already succeeded (on remember_browser page)
                        current_url = driver.current_url.lower()
                        if "remember_browser" in current_url or "trust" in current_url:
                            if logger:
                                logger.info(f"Attempt {attempt + 1}: No code field found, but on remember_browser page - 2FA already successful!")
                            code_accepted = True
                            break
                        
                        if logger:
                            logger.error(f"Attempt {attempt + 1}: Could not find code field")
                        time.sleep(2)
                        continue
                    
                    # Clear field properly: Select All + Delete + Type new code
                    code_field.click()
                    code_field.send_keys(Keys.CONTROL + "a")  # Select all
                    code_field.send_keys(Keys.DELETE)         # Delete
                    time.sleep(0.5)
                    code_field.send_keys(totp_code)
                    time.sleep(1)
                    
                    # Submit the code
                    try:
                        submit_btn = driver.find_element(By.XPATH, 
                            '//button[contains(text(), "Continue") or contains(text(), "Submit") or @type="submit"]')
                        submit_btn.click()
                        if logger:
                            logger.info(f"Attempt {attempt + 1}: Clicked submit button")
                    except Exception:
                        code_field.send_keys(Keys.RETURN)
                        if logger:
                            logger.info(f"Attempt {attempt + 1}: Submitted with Enter key")
                    
                    # Wait for page to change (whatever page it is)
                    if logger:
                        logger.info(f"Attempt {attempt + 1}: Waiting for page to load...")
                    
                    # Wait up to 10 seconds for page to settle
                    time.sleep(3)
                    current_url = driver.current_url.lower()
                    page_source = driver.page_source.lower()
                    
                    if logger:
                        logger.info(f"Attempt {attempt + 1}: Current URL: {current_url}")
                    
                    # FIRST: Check for "Remember browser" / "Trust this device" page (SUCCESS!)
                    if "remember_browser" in current_url or "trust" in current_url or "save_browser" in current_url:
                        if logger:
                            logger.info("SUCCESS! Trust/Remember browser page detected - 2FA code was accepted!")
                        
                        # Try to click Continue/Save/Trust button
                        for _ in range(5):
                            try:
                                trust_btn = driver.find_element(By.XPATH, 
                                    "//button[contains(text(), 'Trust') or contains(text(), 'Continue') or contains(text(), 'Save') or @type='submit']")
                                if trust_btn and trust_btn.is_displayed():
                                    trust_btn.click()
                                    if logger:
                                        logger.info("Clicked trust/continue button")
                                    break
                            except Exception:
                                pass
                            time.sleep(1)
                        
                        time.sleep(3)  # Wait for action to complete
                        current_url = driver.current_url.lower()
                        if logger:
                            logger.info(f"After trust page: {current_url}")
                        
                        # Code was accepted, break out of retry loop
                        code_accepted = True
                        if logger:
                            logger.info(f"2FA completed successfully on attempt {attempt + 1}")
                        break
                    
                    # Check for checkpoint/challenge pages (need manual intervention)
                    if any(check in current_url for check in ["checkpoint", "challenge", "confirm", "review", "suspended", "disabled"]):
                        if logger:
                            logger.warning(f"Facebook requires manual action: {current_url}")
                            logger.warning("Stopping - please check the browser window to see what Facebook wants")
                        return False
                    
                    # Check if we're still on 2FA page (URL contains two_factor)
                    if "two_factor" in current_url or "two-factor" in current_url:
                        # Check if code input field is still there - if yes, code was rejected
                        code_field_still_there = False
                        try:
                            for selector in code_selectors:
                                try:
                                    field = driver.find_element(By.CSS_SELECTOR, selector)
                                    if field and field.is_displayed():
                                        code_field_still_there = True
                                        break
                                except:
                                    continue
                        except:
                            pass
                        
                        if code_field_still_there:
                            # 2FA field still present - code was rejected
                            if any(err in page_source for err in ["doesn't match", "incorrect", "wrong", "try again"]):
                                if logger:
                                    logger.warning(f"Attempt {attempt + 1}: Code rejected by Facebook (error message shown)")
                            else:
                                if logger:
                                    logger.warning(f"Attempt {attempt + 1}: Still on 2FA page, code may have been rejected")
                            
                            if attempt < max_2fa_attempts - 1:
                                if logger:
                                    logger.info("Will retry with fresh TOTP code...")
                                time.sleep(2)
                                continue
                            else:
                                if logger:
                                    logger.error(f"All {max_2fa_attempts} attempts failed")
                                return False
                        else:
                            # No 2FA field but still on two_factor URL - might be success transitioning
                            if logger:
                                logger.info("No 2FA field found, page may be transitioning...")
                            time.sleep(3)
                            # Check again
                            current_url = driver.current_url.lower()
                            if "remember_browser" in current_url or "trust" in current_url:
                                if logger:
                                    logger.info("Now on trust page - 2FA successful!")
                                code_accepted = True
                                break
                    
                    # If we get here and URL changed away from 2FA, it's likely success
                    if "two_factor" not in current_url and "two-factor" not in current_url:
                        if logger:
                            logger.info(f"Left 2FA page, now on: {current_url}")
                        # Check for trust page again
                        if "remember_browser" in current_url or "trust" in current_url:
                            if logger:
                                logger.info("On trust page - 2FA successful!")
                        else:
                            if logger:
                                logger.info("On unexpected page after 2FA, assuming success")
                        code_accepted = True
                        if logger:
                            logger.info(f"2FA completed successfully on attempt {attempt + 1}")
                        break
                    
                except Exception as e:
                    if logger:
                        logger.error(f"Attempt {attempt + 1}: Error during 2FA: {e}")
                    if attempt < max_2fa_attempts - 1:
                        time.sleep(2)
                        continue
                    return False
            
            if not code_accepted:
                if logger:
                    logger.error(f"Failed all {max_2fa_attempts} 2FA attempts")
                return False
        
        # After 2FA (or if no 2FA needed), check current page
        current_url = driver.current_url.lower()
        page_source = driver.page_source.lower()
        
        # If we're on an unknown/error page, stop and let user see it
        if any(bad in current_url for bad in ["login", "checkpoint", "error", "blocked", "challenge"]):
            if "messenger.com/login" not in current_url and "facebook.com/login" not in current_url:
                if logger:
                    logger.error(f"Stopped at unexpected page: {current_url}")
                    logger.error("Please check the browser to see what Facebook is showing")
                return False
        
        # Navigate to Messenger
        if logger:
            logger.info("Navigating to Messenger...")
        driver.get("https://www.messenger.com/")
        
        # Wait for Messenger to load and handle "Continue as" if present
        messenger_loaded = False
        continue_clicked = False
        
        for _ in range(20):  # Wait up to 20 seconds
            current_url = driver.current_url.lower()
            page_source = driver.page_source.lower()
            
            # Handle "Continue as" page - must click before checking success
            if "continue" in page_source or "switch accounts" in page_source:
                if not continue_clicked:
                    if logger:
                        logger.info("Continue as page detected, clicking...")
                    try:
                        continue_btn = driver.find_element(By.XPATH, 
                            "//button[contains(text(), 'Continue')] | //a[contains(text(), 'Continue')] | //button[@type='submit']")
                        if continue_btn and continue_btn.is_displayed():
                            continue_btn.click()
                            continue_clicked = True
                            if logger:
                                logger.info("Clicked Continue button, waiting for Messenger to load...")
                    except Exception as e:
                        if logger:
                            logger.warning(f"Could not click Continue: {e}")
                else:
                    # Already clicked, just waiting for load
                    if logger:
                        logger.debug("Waiting for Messenger to load after clicking Continue...")
            
            # Check if Messenger is fully loaded (chats visible or main interface)
            if "messenger.com" in current_url and not ("login" in current_url):
                # Additional check: look for Messenger UI elements
                try:
                    # Look for chat list, new message button, or search bar
                    chat_indicators = [
                        "//div[@role='navigation']",  # Left sidebar
                        "//a[contains(@href, '/t/')]",  # Chat threads
                        "//div[contains(@aria-label, 'Chats')]",  # Chats section
                        "//div[@role='textbox']",  # Message input
                        "//span[contains(text(), 'New message')]",  # New message button
                    ]
                    
                    for indicator in chat_indicators:
                        try:
                            if driver.find_elements(By.XPATH, indicator):
                                messenger_loaded = True
                                if logger:
                                    logger.info(f"Messenger fully loaded (found: {indicator})")
                                break
                        except:
                            continue
                    
                    if messenger_loaded:
                        break
                        
                except Exception:
                    pass
            
            time.sleep(1)
        
        if messenger_loaded:
            if logger:
                logger.info(f"Login successful for {username}")
            time.sleep(2)
            return True
        else:
            if logger:
                logger.error(f"Failed to load Messenger. URL: {driver.current_url}")
            return False
            
    except Exception as e:
        if logger:
            logger.error(f"Error during fresh login: {type(e).__name__}: {e}")
        return False

def load_account_credentials(account_fname, accounts_dir, config):
    """
    Load credentials for an account from separate credential file.
    Tries: accounts/{account}.credentials.json first, then falls back to config.fb_credentials
    """
    import os
    import json
    
    # Try separate credential file first
    # Flexible filename matching: 2_cookies.json -> 2.credentials.json or 2_cookies.credentials.json
    base_name = account_fname
    for suffix in ['_cookies.json', '.json', '.txt']:
        if base_name.lower().endswith(suffix):
            base_name = base_name[:-len(suffix)]
            break
            
    candidate_names = [
        f"{base_name}.credentials.json",
        f"{account_fname}.credentials.json",
        f"{base_name}.json" # Maybe the file is just named account.json
    ]
    
    for cname in candidate_names:
        cpath = os.path.join(accounts_dir, cname)
        if os.path.exists(cpath):
            try:
                with open(cpath, 'r', encoding='utf-8') as f:
                    creds = json.load(f)
                if creds.get('username') and creds.get('password'):
                    if logger:
                        logger.info(f"Loaded credentials for {account_fname} from {cname}")
                    return creds
            except Exception:
                continue
    
    # Fallback to config.json fb_credentials
    fb_creds = (config or {}).get('fb_credentials', {})
    account_creds = fb_creds.get(account_fname) or fb_creds.get(base_name) or {}
    
    return account_creds

def _get_login_tracker(account_name):
    """Get or initialize login attempt tracker for an account."""
    global LOGIN_ATTEMPT_TRACKER
    if account_name not in LOGIN_ATTEMPT_TRACKER:
        LOGIN_ATTEMPT_TRACKER[account_name] = {
            'count': 0,
            'last_attempt': 0,
            'backoff_until': 0,
            'success_count': 0
        }
    return LOGIN_ATTEMPT_TRACKER[account_name]

def _record_login_attempt(account_name, success=False):
    """Record a login attempt and update backoff."""
    global LOGIN_ATTEMPT_TRACKER
    tracker = _get_login_tracker(account_name)
    now = time.time()
    
    if success:
        # Reset on success
        tracker['count'] = 0
        tracker['success_count'] += 1
        tracker['backoff_until'] = 0
    else:
        tracker['count'] += 1
        # Cap backoff at 60s to avoid long idle times as requested
        backoff = min(BASE_BACKOFF_SECONDS * tracker['count'], 60)
        tracker['backoff_until'] = now + backoff
    
    tracker['last_attempt'] = now
    return tracker

def _should_attempt_login(account_name):
    """Check if we should attempt login (respect backoff)."""
    tracker = _get_login_tracker(account_name)
    now = time.time()
    
    # Check if in backoff period
    if now < tracker['backoff_until']:
        remaining = int(tracker['backoff_until'] - now)
        return False, remaining
    
    # Check if max attempts reached
    if tracker['count'] >= MAX_LOGIN_ATTEMPTS:
        return False, -1  # Max attempts reached
    
    return True, 0

def handle_logout_and_refresh_with_credentials(driver, logger, config, account_fname):
    """
    Enhanced logout handler that tries cookie refresh first, then falls back to credentials login.
    Includes loop prevention with exponential backoff.
    """
    # Check if we should attempt login (respect backoff)
    should_attempt, backoff_remaining = _should_attempt_login(account_fname)
    
    if not should_attempt:
        if backoff_remaining > 0:
            logger.warning(f"Login backoff active for {account_fname}. Waiting {backoff_remaining}s before retry.")
            send_or_update_failed_login_notice(account_fname, config, 
                f"🔴 Login backoff: waiting {backoff_remaining}s before retry...")
            time.sleep(min(backoff_remaining, 30))  # Wait up to 30s at a time
            return False
        else:
            logger.error(f"Max login attempts ({MAX_LOGIN_ATTEMPTS}) reached for {account_fname}. Giving up.")
            send_or_update_failed_login_notice(account_fname, config, 
                f"🔴 Max login attempts reached. Please restart bot or update credentials.")
            return False
    
    # Check if auto-login with credentials is enabled
    auto_login_enabled = config.get('auto_login_with_credentials', True)
    
    # Record this attempt
    _record_login_attempt(account_fname, success=False)
    
    # First try the normal cookie refresh
    send_or_update_failed_login_notice(account_fname, config, f"🔴 Logged out — attempt #{_get_login_tracker(account_fname)['count']}/5, trying cookie refresh...")
    ok = refresh_account_cookies(account_fname, "accounts", config, logger)
    
    if ok:
        try:
            new_cookies = load_cookies_from_file(os.path.join("accounts", account_fname))
            if reload_cookies_into_driver(driver, new_cookies):
                time.sleep(config.get('delay_retry_login', 4))
                if not is_logged_out(driver):
                    logger.info("Auto re-login successful after cookie refresh.")
                    _record_login_attempt(account_fname, success=True)
                    clear_failed_login_notice(account_fname, config)
                    return True
        except Exception as e:
            logger.error(f"Cookie refresh login failed: {e}")
    
    # Get credentials to check if this account has TOTP/credentials configured
    account_creds = load_account_credentials(account_fname, "accounts", config)
    username = account_creds.get('username') or account_creds.get('email')
    password = account_creds.get('password')
    totp_secret = account_creds.get('totp_secret')
    has_credentials = username and password and totp_secret
    
    # If no credentials/TOTP configured, keep retrying with cookies only (manual update needed)
    if not has_credentials:
        logger.warning(f"No credentials/TOTP found for {account_fname}. Keeping cookie-only mode.")
        logger.info(f"Please update cookies in Google Sheets manually for {account_fname}")
        send_or_update_failed_login_notice(account_fname, config, 
            f"🔴 Cookie expired — No credentials configured. Please update cookies in Google Sheets manually.")
        # Wait before next retry to avoid spamming
        time.sleep(30)
        return False
    
    # If cookie refresh failed and auto-login with credentials is disabled, stop here
    if not auto_login_enabled:
        logger.warning("Cookie refresh failed and auto_login_with_credentials is disabled.")
        send_or_update_failed_login_notice(account_fname, config, "🔴 Cookie refresh failed — auto-login disabled in config.")
        return False
    
    # If cookie refresh failed, try credentials login (only for accounts with TOTP)
    logger.warning("Cookie refresh failed, attempting credentials login with TOTP...")
    send_or_update_failed_login_notice(account_fname, config, 
        f"🔴 Cookie failed — attempt #{_get_login_tracker(account_fname)['count']}/5, credentials login...")
    
    # Attempt fresh login
    login_success = login_with_credentials(driver, username, password, totp_secret, logger, config)
    
    if login_success:
        logger.info(f"Fresh login successful for {account_fname}")
        
        # Record successful login (resets attempt counter)
        _record_login_attempt(account_fname, success=True)
        
        # Save new cookies to local file
        try:
            new_cookies = driver.get_cookies()
            if new_cookies:
                cookie_path = os.path.join("accounts", account_fname)
                with open(cookie_path, 'w', encoding='utf-8') as f:
                    json.dump(new_cookies, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved new cookies to {account_fname}")
                
                # Try to update Google Sheets
                update_cookies_to_google_sheets(account_fname, new_cookies, config, logger)
        except Exception as e:
            logger.error(f"Error saving new cookies: {e}")
        
        clear_failed_login_notice(account_fname, config)
        return True
    else:
        logger.error(f"Fresh login failed for {account_fname}")
        send_or_update_failed_login_notice(account_fname, config, 
            f"🔴 Login failed (attempt #{_get_login_tracker(account_fname)['count']}/5) — check credentials/TOTP.")
        return False

def process_message_requests(driver, reply_message, config, logger, option_type='option1'):
    """Continuously process message requests."""
    import os
    while True:
        if os.path.exists("pause.flag"):
            logger.info("[PAUSED] Bot is paused. Waiting...")
            time.sleep(2)
            continue
        # Check for logout FIRST (before rehoming) to prevent redirect loops
        if is_logged_out(driver):
            account_name = current_account_name(logger)
            success = handle_logout_and_refresh_with_credentials(driver, logger, config, account_name)
            if success:
                # Login successful, wait for page to fully load before continuing
                logger.info(f"Re-login successful for {account_name}, waiting for Messenger to load...")
                time.sleep(5)
                # Navigate to messenger to ensure we're on the right page
                try:
                    driver.get("https://www.messenger.com/")
                    time.sleep(3)
                except Exception as e:
                    logger.warning(f"Error navigating to messenger after re-login: {e}")
            else:
                # Login failed, wait longer before next attempt (backoff is handled inside the function)
                logger.warning(f"Re-login failed for {account_name}, waiting before retry...")
                time.sleep(10)
            continue
        
        # Only rehome if not logged out (logout check happens first above)
        try:
            rehome_if_interrupted(driver, logger, config)
        except Exception:
            pass
        
        # Auto-recover if Messenger shows 'Couldn't load chats'
        recover_couldnt_load_chats(driver, logger)
        
        # Auto close popups if enabled
        auto_close_popups(driver, logger)
        
        logger.info("Checking for new message requests...")
        try:
            account_name = current_account_name(logger)
            update_account_status(account_name, "🟢 Active - waiting for next check")
        except Exception:
            pass

        # Click the 'Requests' icon with retry until page loads
        requests_loaded = click_requests_icon(driver, logger)
        if not requests_loaded:
            logger.warning("Could not load Requests page, will retry in next iteration...")
            time.sleep(config.get("delay_check_requests", 60))
            continue
        
        # In case the click revealed the error banner, recover and proceed
        recover_couldnt_load_chats(driver, logger)
        time.sleep(config.get("delay_click_request", 3))

        # Check the 'You may know' tab
        you_may_know_ok = switch_to_tab(driver, "You may know", logger)
        if not you_may_know_ok:
            logger.warning("Could not switch to 'You may know' tab, will try Spam tab...")
        
        threads = has_new_threads(driver, logger)
        if threads:
            accept_and_reply(driver, reply_message, config, logger, option_type=option_type)
        else:
            logger.info("No new threads in 'You may know'. Switching to 'Spam'...")

        # Verify we are still on Requests page before switching to Spam tab
        # (accept_and_reply may have navigated away)
        on_requests = ensure_on_requests_page(driver, logger)
        if not on_requests:
            logger.warning("Could not ensure Requests page is loaded before Spam tab, will retry...")
            time.sleep(config.get("delay_check_requests", 60))
            continue

        # Check the 'Spam' tab if no messages are found in 'You may know'
        spam_ok = switch_to_tab(driver, "Spam", logger)
        if not spam_ok:
            logger.warning("Could not switch to 'Spam' tab, will retry in next iteration...")
        
        threads = has_new_threads(driver, logger)
        if threads:
            accept_and_reply(driver, reply_message, config, logger, option_type=option_type)
        else:
            logger.info(f"No new threads in 'Spam'. Waiting for {config.get('delay_check_requests',60)} seconds...")
            time.sleep(config.get("delay_check_requests", 60))

        # Return to the 'You may know' tab for the next iteration (best effort)
        switch_to_tab(driver, "You may know", logger)


import os

def main():
    global bot_statistics
    # Initialize session tracking
    bot_statistics['start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    accounts_dir = "accounts"
    reply_message_filepath = "reply_message.txt"

    # List available cookie files
    account_files = [f for f in os.listdir(accounts_dir) if f.endswith('_cookies.json')]
    if not account_files:
        print("No account cookie files found in 'accounts' folder.")
        return
    print("Available accounts:")
    for idx, fname in enumerate(account_files, 1):
        print(f"{idx}. {fname}")
    choice = input("Enter account numbers to run (comma-separated for multiple): ")
    try:
        selected_idxs = [int(x.strip()) - 1 for x in choice.split(',')]
        selected_files = [account_files[i] for i in selected_idxs]
    except Exception:
        print("Invalid selection.")
        return
    with open(reply_message_filepath, "r", encoding="utf-8") as file:
        reply_message = file.read().strip()

def run_account(fname, reply_message, accounts_dir, x, y, w, h, config, win_mode='custom', option_type='option1'):
    global bot_statistics
    # Track session start if not already set
    if not bot_statistics.get('start_time'):
        bot_statistics['start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, f"{fname}.log")
    logger = logging.getLogger(f"bot_{fname}")
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    class FSyncFileHandler(logging.FileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()
            try:
                if hasattr(self.stream, 'fileno'):
                    os.fsync(self.stream.fileno())
            except Exception:
                pass
    fh = FSyncFileHandler(log_file_path, mode='w', encoding='utf-8')
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    try:
        update_account_status(fname, "Initializing Chrome...")
    except Exception:
        pass

    # Pre-sync cookies from Google Sheets before first login to avoid stale local cookies
    try:
        gs_cfg = (config or {}).get('google_sheets') or {}
        if gs_cfg.get('enabled') and (gs_cfg.get('published_csv_url') or gs_cfg.get('mode') == 'api'):
            refresh_account_cookies(fname, accounts_dir, config, logger)
    except Exception:
        pass
    cookies = load_cookies_from_file(os.path.join(accounts_dir, fname))
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-popup-blocking")
    # Add DevTools suppression (same as Options 11&12)
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--silent")
    
    # Handle window mode: maximized or custom size
    if win_mode == 'maximized':
        chrome_options.add_argument("--start-maximized")
    
    service = Service(ChromeDriverManager().install())
    # Suppress service logs (same as Options 11&12)
    devnull_file = open(os.devnull, 'w')
    service.log_output = devnull_file

    driver = webdriver.Chrome(service=service, options=chrome_options)
    # Track this Chrome process
    track_chrome_process(driver, fname)

    # Apply window settings
    if win_mode == 'maximized':
        driver.maximize_window()
        logger.info(f"Window maximized for {fname}")
    else:
        driver.set_window_size(w, h)
        driver.set_window_position(x, y)
        logger.info(f"Window set to {w}x{h} at position ({x},{y}) for {fname}")
    try:
        # Persistent login: keep re-fetching cookies from Google Sheets and
        # retrying until login succeeds. We never give up and never close Chrome
        # on a failed login — the operator updates the cookies in the sheet and
        # the next refresh picks them up automatically.
        retry_delay = config.get("login_retry_delay_seconds", 60)
        logged_in = False
        attempt = 0
        while not logged_in:
            attempt += 1
            # Cookies were pre-synced before this loop, so attempt 1 uses those.
            # From attempt 2 on, re-pull from the sheet each time so a manual
            # cookie update is picked up without restarting the bot.
            if attempt > 1:
                try:
                    refresh_account_cookies(fname, accounts_dir, config, logger)
                except Exception:
                    pass
                cookies = load_cookies_from_file(os.path.join(accounts_dir, fname))

            driver.get("https://www.messenger.com/")
            driver.delete_all_cookies()
            for cookie in cookies:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
            driver.refresh()
            time.sleep(config.get("delay_retry_login", 4))

            if "login" not in driver.current_url and "recover" not in driver.current_url:
                logged_in = True
                logger.info(f"Login successful for {fname} on attempt {attempt}.")
                try:
                    clear_failed_login_notice(fname, config)
                except Exception:
                    pass
                try:
                    update_account_status(fname, "🟢 Active - running")
                except Exception:
                    pass

                # Save fresh cookies locally for all accounts
                try:
                    fresh_cookies = driver.get_cookies()
                    if fresh_cookies:
                        cookie_path = os.path.join(accounts_dir, fname)
                        with open(cookie_path, 'w', encoding='utf-8') as f:
                            json.dump(fresh_cookies, f, ensure_ascii=False, indent=2)
                        logger.info(f"Saved fresh cookies locally for {fname}")

                        # Only update Google Sheets for accounts WITH credentials (2FA accounts)
                        # Accounts without credentials use manual cookie updates
                        account_creds = load_account_credentials(fname, accounts_dir, config)
                        has_credentials = account_creds.get('username') and account_creds.get('password') and account_creds.get('totp_secret')

                        if has_credentials:
                            gs = (config or {}).get('google_sheets') or {}
                            if gs.get('enabled'):
                                update_cookies_to_google_sheets(fname, fresh_cookies, config, logger)
                        else:
                            logger.info(f"Account {fname} has no credentials - skipping Google Sheets cookie update (manual mode)")
                except Exception as e:
                    logger.warning(f"Could not save fresh cookies after login: {e}")
                break  # success

            # Login failed: keep the browser open and retry. Cookies are
            # re-pulled from the sheet at the top of the next iteration.
            logger.warning(f"[LOGIN FAILED] Attempt {attempt} for {fname}. Keeping Chrome open; retrying with fresh cookies from the sheet in {retry_delay}s.")
            try:
                driver.execute_script(f"document.title = '[LOGIN RETRY {attempt}] ' + document.title;")
            except Exception:
                pass
            try:
                update_account_status(fname, "🔴 Login failed — retrying (update cookies in sheet)")
            except Exception:
                pass
            # Send the consolidated alert only once (on the first failure) so the
            # login-failure counter isn't re-incremented on every retry.
            if attempt == 1:
                send_or_update_failed_login_notice(fname, config, "🔴 Login failed — please update cookies in Google Sheets. Retrying automatically until it works.")
            time.sleep(retry_delay)
        close_temporary_block_popup(driver, logger)
        process_message_requests(driver, reply_message, config, logger, option_type=option_type)
    except KeyboardInterrupt:
        logger.info(f"Script stopped manually for {fname}.")
    except Exception as e:
        logger.error(f"Unexpected error for {fname}: {type(e).__name__}: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        logger.info(f"Browser closed for {fname}.")
        try:
            update_account_status(fname, "🔴 Stopped / Finished")
        except Exception:
            pass
        # Update end time
        bot_statistics['end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Attempt final report once (idempotent across threads)
        send_final_report_once(f"Account thread exited: {fname}")

def get_account_files():
    accounts_dir = "accounts"
    return [f for f in os.listdir(accounts_dir) if f.endswith('_cookies.json')]

def list_accounts():
    files = get_account_files()
    if not files:
        print("No account cookie files found in 'accounts' folder.")
        return
    print("Accounts:")
    for idx, fname in enumerate(files, 1):
        print(f"{idx}. {fname}")

def add_account():
    accounts_dir = "accounts"
    new_name = input("Enter a name for the new account (e.g., 7_cookies.json): ").strip()
    path = os.path.join(accounts_dir, new_name)
    if os.path.exists(path):
        print("Account file already exists.")
        return
    print("Paste the cookie JSON content for this account:")
    content = input()
    try:
        json.loads(content)
    except Exception:
        print("Invalid JSON format.")
        return
    with open(path, "w") as f:
        f.write(content)
    print(f"Account {new_name} added.")

def rename_account():
    files = get_account_files()
    list_accounts()
    idx = input("Enter the number of the account to rename: ").strip()
    try:
        idx = int(idx) - 1
        old = files[idx]
    except Exception:
        print("Invalid selection.")
        return
    new_name = input("Enter new name (e.g., 8_cookies.json): ").strip()
    os.rename(os.path.join("accounts", old), os.path.join("accounts", new_name))
    print(f"Renamed {old} to {new_name}.")

def remove_account():
    files = get_account_files()
    list_accounts()
    idx = input("Enter the number of the account to remove: ").strip()
    try:
        idx = int(idx) - 1
        fname = files[idx]
    except Exception:
        print("Invalid selection.")
        return
    os.remove(os.path.join("accounts", fname))
    print(f"Removed {fname}.")

def edit_reply_message():
    path = "reply_message.txt"
    print("Current reply message:")
    with open(path, "r", encoding="utf-8") as f:
        print(f.read())
    print("Enter new reply message (end with a blank line):")
    lines = []
    while True:
        l = input()
        if l.strip() == "":
            break
        lines.append(l)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("Reply message updated.")

# Pause/Resume global
PAUSED = False

# Auto popup closer global
AUTO_POPUP_CLOSER_ENABLED = True

# Track Chrome processes started by this bot
BOT_CHROME_PROCESSES = []
 
# Persistently ignored request thread IDs/names (uncontactable or no textbox)
IGNORED_REQUESTS = set()

# Track login/recovery attempts per account to prevent infinite loops (set high for persistent retries)
LOGIN_ATTEMPT_TRACKER = {}  # {account_name: {'count': int, 'last_attempt': timestamp, 'backoff_until': timestamp}}
MAX_LOGIN_ATTEMPTS = 999999
BASE_BACKOFF_SECONDS = 30  # Start with 30 seconds for frequent retries

def track_chrome_process(driver, account_name="unknown"):
    """Track a Chrome process started by this bot."""
    global BOT_CHROME_PROCESSES
    try:
        # Get the process ID of the Chrome driver
        if hasattr(driver, 'service') and hasattr(driver.service, 'process'):
            pid = driver.service.process.pid
            chrome_pids = []
            try:
                # Try to capture current child chrome.exe PIDs (non-recursive snapshot)
                import subprocess
                ps_cmd = (
                    f"Get-CimInstance Win32_Process | Where-Object {{$_.ParentProcessId -eq {pid} -and $_.Name -ieq 'chrome.exe'}} | "
                    "Select-Object -ExpandProperty ProcessId"
                )
                output = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd],
                                                stderr=subprocess.DEVNULL, timeout=3)
                for line in output.decode(errors='ignore').splitlines():
                    line = line.strip()
                    if line.isdigit():
                        chrome_pids.append(int(line))
            except Exception:
                pass
            BOT_CHROME_PROCESSES.append({
                'pid': pid,
                'account': account_name,
                'driver': driver,
                'chrome_pids': chrome_pids,
            })
            print(f"Tracking ChromeDriver PID {pid} for account {account_name} (chrome children: {len(chrome_pids)})")

            # Also add chromedriver to the Job so it is killed if this process dies abruptly
            try:
                if '_JOB_HANDLE' in globals() and _JOB_HANDLE:
                    PROCESS_ALL_ACCESS = 0x1F0FFF
                    hProc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
                    if hProc:
                        kernel32.AssignProcessToJobObject(_JOB_HANDLE, hProc)
                    # Assign known child chrome.exe PIDs too
                    for cpid in chrome_pids:
                        try:
                            hChild = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, int(cpid))
                            if hChild:
                                kernel32.AssignProcessToJobObject(_JOB_HANDLE, hChild)
                        except Exception:
                            continue
            except Exception:
                pass
    except Exception as e:
        print(f"Warning: Could not track Chrome process for {account_name}: {e}")

def process_random_message(message):
    """
    Process message with {{RAN_M(option1|option2|option3)}} syntax to randomly select options.
    """
    def replace_random(match):
        options = match.group(1).split('|')
        return random.choice(options)
    
    # Replace all {{RAN_M(...)}} patterns
    processed_message = re.sub(r'\{\{RAN_M\(([^}]+)\)\}\}', replace_random, message)
    return processed_message

def update_statistics(option_type, account_name, count=1):
    """
    Update bot statistics for tracking replies and messages.
    Sends instant Telegram update when count increases (force=True).
    """
    with stats_lock:
        if option_type == 'option1':
            bot_statistics['option1_replies'][account_name] = bot_statistics.get('option1_replies', {}).get(account_name, 0) + count
        elif option_type == 'option2':
            bot_statistics['option2_replies'][account_name] = bot_statistics.get('option2_replies', {}).get(account_name, 0) + count
        elif option_type == 'option11':
            bot_statistics['option11_messages'][account_name] = bot_statistics.get('option11_messages', {}).get(account_name, 0) + count
        elif option_type == 'option12':
            bot_statistics['option12_messages'][account_name] = bot_statistics.get('option12_messages', {}).get(account_name, 0) + count
    # Touch activity and persist checkpoint
    global LAST_ACTIVITY_TS
    LAST_ACTIVITY_TS = time.time()
    save_stats_checkpoint()
    try:
        update_account_status(account_name, "🟢 Active - running")
    except Exception:
        pass
    # Log to persistent database
    try:
        event_type = "reply_sent" if option_type in ("option1", "option2") else "message_sent"
        stats_tracker.log_event(event_type, account_name=account_name, option_type=option_type)
    except Exception:
        pass
    # INSTANT update to Telegram when message is sent (force=True)
    # This ensures real-time tracking with no missing updates
    _maybe_throttled_status_update(f"✅ Message sent by {account_name}", force=True)
    _maybe_throttled_report_update(f"✅ Message sent by {account_name}", force=True)


def inactivity_watchdog():
    """If there's no activity for inactivity_minutes, send final report and cleanup."""
    try:
        cfg = load_config()
        minutes = int((cfg.get('watchdog', {}) or {}).get('inactivity_minutes', 15))
    except Exception:
        minutes = 15
    threshold = max(5, minutes) * 60
    last_totals = _get_totals()
    while True:
        try:
            time.sleep(30)
            o1, o2, o11 = _get_totals()
            global LAST_ACTIVITY_TS
            if (o1, o2, o11) != last_totals:
                last_totals = (o1, o2, o11)
                LAST_ACTIVITY_TS = time.time()
                save_stats_checkpoint()
                continue
            if time.time() - LAST_ACTIVITY_TS > threshold:
                # Only send a heartbeat status note; do not finalize or stop the bot
                _maybe_throttled_status_update("No activity detected recently (watchdog)")
                save_stats_checkpoint()
                # Do not return; keep monitoring
        except Exception:
            continue

def session_limit_watchdog():
    """Stop the bot after the configured session hours (default 6h). Sends a final notification and exits.
    Keeps the persistent status message intact."""
    try:
        cfg = load_config()
        hours = float((cfg.get('watchdog', {}) or {}).get('session_limit_hours', 6))
    except Exception:
        hours = 6.0
    hours = max(0.5, hours)
    # Determine start time
    try:
        with stats_lock:
            st = bot_statistics.get('start_time')
        if st:
            start_dt = datetime.strptime(st, '%Y-%m-%d %H:%M:%S')
        else:
            start_dt = datetime.now()
    except Exception:
        start_dt = datetime.now()
    deadline = start_dt.timestamp() + hours * 3600
    while True:
        try:
            time.sleep(10)
            if time.time() >= deadline:
                _maybe_throttled_status_update(f"RDP {int(hours)}h session finished; stopping bot")
                send_final_report_once(f"RDP {int(hours)}h session finished")
                try:
                    kill_orphaned_chrome_processes()
                except Exception:
                    pass
                # Hard exit to ensure termination
                os._exit(0)
        except Exception:
            time.sleep(10)
            continue

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def get_window_settings(config):
    win = config.get("window", {})
    mode = win.get("mode", "maximized")
    width = int(win.get("width", 800))
    height = int(win.get("height", 600))
    stagger = int(win.get("stagger_offset", 40))
    return mode, width, height, stagger

def toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    status = "PAUSED" if PAUSED else "RESUMED"
    print(f"Bot is now {status}.")
    if PAUSED:
        with open("pause.flag", "w") as f:
            f.write("paused")
    else:
        if os.path.exists("pause.flag"):
            os.remove("pause.flag")

def toggle_auto_popup_closer():
    global AUTO_POPUP_CLOSER_ENABLED
    AUTO_POPUP_CLOSER_ENABLED = not AUTO_POPUP_CLOSER_ENABLED
    status = "ENABLED" if AUTO_POPUP_CLOSER_ENABLED else "DISABLED"
    print(f"Auto popup closer is now {status}.")
    if AUTO_POPUP_CLOSER_ENABLED:
        print("The bot will now automatically close popups:")
        print("- Close 'X' buttons on any popup")
        print("- Click 'Don't restore messages' on chat history popups")
    else:
        print("Auto popup closer is disabled. Popups will not be automatically closed.")

def aggressive_login_popup_closer(driver, logger=None):
    """Aggressively close login-time popups with multiple attempts"""
    if not AUTO_POPUP_CLOSER_ENABLED:
        return
    
    # Focus on "Don't restore messages" popup specifically
    restore_popup_found = False
    
    # Try multiple times with different strategies
    for attempt in range(3):
        try:
            # Strategy 1: Look for blue buttons (primary action buttons)
            blue_buttons = driver.find_elements(By.XPATH, "//div[@role='button'][contains(@style, 'rgb(24, 119, 242)') or contains(@class, 'primary')]")
            for btn in blue_buttons:
                try:
                    if btn.is_displayed() and ("restore" in btn.text.lower() or "don't" in btn.text.lower()):
                        driver.execute_script("arguments[0].click();", btn)
                        if logger:
                            logger.info(f"Clicked blue restore button (attempt {attempt + 1})")
                        restore_popup_found = True
                        time.sleep(1)
                        break
                except Exception:
                    continue
            
            if restore_popup_found:
                break
                
            # Strategy 2: Look for any button containing "Don't restore"
            restore_buttons = driver.find_elements(By.XPATH, "//*[contains(text(), 'Don\'t restore') or contains(text(), 'dont restore') or contains(text(), 'Continue without')]")
            for btn in restore_buttons:
                try:
                    if btn.is_displayed():
                        # Try clicking the button itself or its parent
                        clickable = btn if btn.tag_name in ['button', 'div'] else btn.find_element(By.XPATH, "./ancestor::*[@role='button' or @onclick or contains(@class, 'button')][1]")
                        driver.execute_script("arguments[0].click();", clickable)
                        if logger:
                            logger.info(f"Clicked restore text button (attempt {attempt + 1})")
                        restore_popup_found = True
                        time.sleep(1)
                        break
                except Exception:
                    continue
            
            if restore_popup_found:
                break
                
            # Strategy 3: Look for modal dialogs and click their primary buttons
            modals = driver.find_elements(By.XPATH, "//div[@role='dialog' or contains(@class, 'modal')]")
            for modal in modals:
                try:
                    if modal.is_displayed():
                        # Find primary/blue buttons in the modal
                        primary_btns = modal.find_elements(By.XPATH, ".//div[@role='button'][contains(@style, 'blue') or contains(@class, 'primary')]")
                        if primary_btns:
                            driver.execute_script("arguments[0].click();", primary_btns[0])
                            if logger:
                                logger.info(f"Clicked modal primary button (attempt {attempt + 1})")
                            restore_popup_found = True
                            time.sleep(1)
                            break
                except Exception:
                    continue
            
            if restore_popup_found:
                break
                
            time.sleep(0.5)  # Wait before next attempt
            
        except Exception as e:
            if logger:
                logger.debug(f"Login popup closer attempt {attempt + 1} error: {e}")
            continue
    
    return restore_popup_found

def auto_close_popups(driver, logger=None):
    """Automatically close common Messenger popups if AUTO_POPUP_CLOSER_ENABLED is True"""
    if not AUTO_POPUP_CLOSER_ENABLED:
        return
    
    def safe_click_element(element, description="element"):
        """Safely click an element with multiple strategies and retries"""
        strategies = [
            lambda: element.click(),  # Normal click
            lambda: driver.execute_script("arguments[0].click();", element),  # JavaScript click
            lambda: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));", element),  # Dispatch click event
            lambda: click_element_by_coordinates(element),  # Coordinate-based click
        ]
        
        for i, strategy in enumerate(strategies, 1):
            try:
                if element.is_displayed() and element.is_enabled():
                    strategy()
                    if logger:
                        logger.info(f"Auto-clicked {description} (strategy {i})")
                    time.sleep(0.5)
                    return True
            except Exception as e:
                if logger and i == len(strategies):  # Log only on final failure
                    logger.debug(f"Failed to click {description}: {e}")
                continue
        return False
    
    def click_element_by_coordinates(element):
        """Click element using screen coordinates as fallback"""
        try:
            # Get element location and size
            location = element.location_once_scrolled_into_view
            size = element.size
            
            # Use ActionChains for precise clicking
            from selenium.webdriver.common.action_chains import ActionChains
            actions = ActionChains(driver)
            actions.move_to_element_with_offset(element, 0, 0).click().perform()
            return True
        except Exception:
            return False
    
    def click_by_text_detection(text_to_find):
        """Find and click text anywhere on the page using multiple methods"""
        try:
            # Method 1: Find by exact text content
            elements = driver.find_elements(By.XPATH, f"//*[contains(text(), '{text_to_find}')]")
            for element in elements:
                if safe_click_element(element, f"text '{text_to_find}'"):
                    return True
            
            # Method 2: Find by partial text match (case insensitive)
            elements = driver.find_elements(By.XPATH, f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text_to_find.lower()}')]")
            for element in elements:
                if safe_click_element(element, f"text '{text_to_find}' (case insensitive)"):
                    return True
            
            # Method 3: Find by aria-label or title containing the text
            elements = driver.find_elements(By.XPATH, f"//*[contains(@aria-label, '{text_to_find}') or contains(@title, '{text_to_find}')]")
            for element in elements:
                if safe_click_element(element, f"aria-label/title '{text_to_find}'"):
                    return True
                    
            return False
        except Exception:
            return False
    
    try:
        # 1. Handle "Continue without restoring?" popup - click "Don't restore messages" (PRIORITY)
        # First try text-based detection (most reliable)
        if click_by_text_detection("Don't restore messages"):
            return
        if click_by_text_detection("Don't restore"):
            return
        
        # Then try specific selectors
        dont_restore_selectors = [
            "//span[contains(text(), \"Don't restore messages\")]/ancestor::div[@role='button']",
            "//div[contains(text(), \"Don't restore messages\")]/ancestor::div[@role='button']",
            "//span[text()=\"Don't restore messages\"]/parent::*",
            "//div[text()=\"Don't restore messages\"]",
            "//button[contains(., \"Don't restore messages\")]",
            "//div[@role='button'][contains(., \"Don't restore\")]",
            "//span[contains(text(), \"Don't restore\")]/ancestor::*[@role='button']",
            "//div[contains(@class, 'blue') or contains(@style, 'blue')][contains(text(), 'restore')]",
            "//button[contains(@class, 'primary') or contains(@class, 'blue')]"
        ]
        
        for selector in dont_restore_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                for element in elements:
                    if safe_click_element(element, "'Don't restore messages' button"):
                        return  # Exit after successful click
            except Exception:
                continue
        
        # 2. Generic 'X' close button on blocking popups
        generic_close_x_selectors = [
            "//div[@role='button' and (@aria-label='Close' or @aria-label='Dismiss')]",
            "//button[@aria-label='Close' or @aria-label='Dismiss']",
            "//div[@role='button'][.='×' or .='X' or .='x']",
            "//span[.='×' or .='X' or .='x']/ancestor::*[@role='button']"
        ]
        for selector in generic_close_x_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                for element in elements:
                    if safe_click_element(element, "generic close 'X' button"):
                        return  # exit after closing
            except Exception:
                continue
        
        # 3. Handle "Your chats aren't backed up" popup (PRIORITY - from screenshot)
        backup_popup_selectors = [
            "//div[contains(text(), 'Your chats aren\'t backed up')]/ancestor::div//div[@aria-label='Close']",
            "//div[contains(text(), 'Your chats aren\'t backed up')]/ancestor::*//button[@aria-label='Close']",
            "//div[contains(text(), 'Your chats aren\'t backed up')]/ancestor::*//div[@role='button'][contains(@aria-label, 'Close')]",
            "//div[contains(text(), 'chats aren\'t backed up')]/ancestor::div//div[@role='button'][contains(@aria-label, 'Close')]",
            "//div[contains(text(), 'backed up')]/ancestor::div//*[@aria-label='Close']",
            "//div[contains(text(), 'backed up')]/following-sibling::*[@aria-label='Close']",
            "//div[contains(text(), 'backed up')]/parent::*//*[@aria-label='Close']"
        ]
        
        for selector in backup_popup_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                for element in elements:
                    if safe_click_element(element, "'Your chats aren't backed up' close button"):
                        return  # Exit after successful click
            except Exception:
                continue
        
        # 3. Close any popup with X button (generic close button - enhanced)
        close_selectors = [
            "[aria-label='Close']",
            "[aria-label='close']", 
            "div[role='button'][aria-label*='Close']",
            "button[aria-label*='Close']",
            "//div[@role='button']//svg[contains(@viewBox, '24')]",
            "//button//svg[contains(@viewBox, '24')]",
            "div[role='button'] svg[fill*='currentColor']",
            "//div[contains(@class, 'close') or contains(@class, 'Close')]",
            "//span[text()='×']/parent::*",
            "//div[text()='×']",
            # Additional selectors for Messenger popups
            "//div[@role='dialog']//div[@aria-label='Close']",
            "//div[@role='dialog']//button[@aria-label='Close']",
            "//div[contains(@class, 'popup')]//div[@aria-label='Close']",
            "//div[contains(@class, 'modal')]//div[@aria-label='Close']",
            "//div[contains(@class, 'notification')]//div[@aria-label='Close']",
            "//div[@role='button'][contains(@style, 'cursor: pointer')][contains(., '×')]",
            "//button[contains(@style, 'cursor: pointer')][contains(., '×')]",
            "//div[@role='button']//i[contains(@class, 'close')]",
            "//button//i[contains(@class, 'close')]"
        ]
        
        for selector in close_selectors:
            try:
                if selector.startswith('//'):
                    elements = driver.find_elements(By.XPATH, selector)
                else:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                
                for element in elements:
                    if safe_click_element(element, "X close button"):
                        return  # Exit after successful click
            except Exception:
                continue
        
        # 3. Handle PIN restore popup - click X or Cancel
        pin_selectors = [
            "//div[contains(text(), 'Enter your PIN to restore')]/ancestor::div[contains(@role, 'dialog')]//div[@role='button'][contains(@aria-label, 'Close')]",
            "//div[contains(text(), 'PIN to restore')]/ancestor::*//button[contains(text(), 'Cancel')]",
            "//div[contains(text(), 'PIN to restore')]/ancestor::*//span[text()='Cancel']/parent::*",
            "//div[contains(text(), 'PIN to restore')]/ancestor::*//div[@role='button'][contains(@aria-label, 'Close')]"
        ]
        
        for selector in pin_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                for element in elements:
                    if safe_click_element(element, "PIN restore popup close button"):
                        return  # Exit after successful click
            except Exception:
                continue
        
        # 4. Handle "Chrome is being controlled" notification
        chrome_notification_selectors = [
            "//div[contains(text(), 'Chrome is being controlled')]/following-sibling::*//button",
            "//div[contains(text(), 'Chrome is being controlled')]/ancestor::*//button[contains(@aria-label, 'Close')]",
            "//div[contains(text(), 'automated test software')]/ancestor::*//button"
        ]
        
        for selector in chrome_notification_selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                for element in elements:
                    if safe_click_element(element, "Chrome notification close button"):
                        return  # Exit after successful click
            except Exception:
                continue
                
    except Exception as e:
        if logger:
            logger.debug(f"Auto popup closer error: {e}")
        pass

import sys

def launch_accounts(single=False):
    """Launch auto-reply accounts using threading (same structure as Options 11&12)"""
    import os
    import threading
    accounts_dir = "accounts"
    reply_message_filepath = "reply_message.txt"
    config = load_config()
    files = get_account_files()
    if not files:
        print("No account cookie files found in 'accounts' folder.")
        return
    if not os.path.exists("logs"):
        os.makedirs("logs")
    
    if single:
        list_accounts()
        idx = input("Enter the number of the account to run: ").strip()
        try:
            idx = int(idx) - 1
            selected_files = [files[idx]]
        except Exception:
            print("Invalid selection.")
            return
    else:
        list_accounts()
        raw = input("Enter account numbers to run (comma-separated), or type 'all': ").strip().lower()
        if raw == 'all':
            selected_files = files
        else:
            try:
                idxs = [s for s in raw.split(',') if s.strip()]
                selected_files = [files[int(i.strip()) - 1] for i in idxs]
            except Exception:
                print("Invalid selection.")
                return
    
    with open(reply_message_filepath, "r", encoding="utf-8") as file:
        reply_message = file.read().strip()
    
    # Window positioning logic - read from config
    win_mode, win_width, win_height, stagger_offset = get_window_settings(config)
    positions = []
    for idx, fname in enumerate(selected_files):
        # Stagger windows with configured offset to avoid complete overlap
        x = (idx % 3) * stagger_offset
        y = (idx // 3) * stagger_offset
        positions.append((fname, reply_message, accounts_dir, x, y, win_width, win_height, config, win_mode))
    
    # Launch each account using threading with window positioning
    threads = []
    for fname, reply_message, accounts_dir, x, y, w, h, cfg, mode in positions:
        size_info = f"size ({w}x{h})" if mode == "custom" else "maximized"
        print(f"[Started] Auto-reply bot for {fname} at position ({x},{y}) {size_info}. Logs in logs/{fname}.log.")
        def run_account_thread(account_file=fname, message=reply_message, acc_dir=accounts_dir, pos_x=x, pos_y=y, width=w, height=h, cfg=config, win_mode=mode):
            run_account(account_file, message, acc_dir, pos_x, pos_y, width, height, cfg, win_mode, option_type=('option1' if single else 'option2'))
        thread = threading.Thread(target=run_account_thread, daemon=True)
        thread.start()
        threads.append(thread)
    print("Launched all selected accounts with positioned windows (logs in logs/). Use menu to view logs.")
    return threads



def launch_main_chat_accounts():
    """Launch auto-reply main chat accounts using same structure as launch_accounts"""
    import os
    import threading
    accounts_dir = "accounts"
    config = load_config()
    files = get_account_files()
    if not files:
        print("No account cookie files found in 'accounts' folder.")
        return
    if not os.path.exists("logs"):
        os.makedirs("logs")
    # Use sane defaults and skip timing prompts per user request
    delay = 3
    message_count = 1
    repeat_count = 0  # 0 = run continuously
    
    selected_files = select_accounts()
    if not selected_files:
        return
    
    # Window positioning logic - read from config
    win_mode, win_width, win_height, stagger_offset = get_window_settings(config)
    positions = []
    for idx, fname in enumerate(selected_files):
        # Stagger windows with configured offset to avoid complete overlap
        x = (idx % 3) * stagger_offset
        y = (idx // 3) * stagger_offset
        positions.append((fname, x, y, win_width, win_height, delay, message_count, repeat_count, win_mode))
    
    # Launch each account using threading with window positioning
    threads = []
    for fname, x, y, w, h, d, mc, rc, mode in positions:
        size_info = f"size ({w}x{h})" if mode == "custom" else "maximized"
        print(f"[Started] Auto-reply main chat for {fname} at position ({x},{y}) {size_info}. Logs in logs/mainchat_{fname}.log.")
        def run_main_chat_thread(account_file=fname, pos_x=x, pos_y=y, width=w, height=h, delay_val=d, msg_count=mc, repeat_val=rc, win_mode=mode):
            auto_reply_main_chat(delay_val, msg_count, repeat_val, [account_file], pos_x, pos_y, width, height, win_mode)
        thread = threading.Thread(target=run_main_chat_thread, daemon=True)
        thread.start()
        threads.append(thread)
    print("Launched all selected main chat accounts with positioned windows (logs in logs/). Use menu to view logs.")
    return threads


def launch_bulk_send_accounts():
    """Launch bulk send accounts using same structure as launch_accounts"""
    import os
    import threading
    accounts_dir = "accounts"
    config = load_config()
    files = get_account_files()
    if not files:
        print("No account cookie files found in 'accounts' folder.")
        return
    if not os.path.exists("logs"):
        os.makedirs("logs")
    
    print("\n[Bulk Send Message Timing Settings]")
    try:
        delay = float(input("Delay between messages (seconds, default=3): ") or 3)
        repeat_count = int(input("Repeat count (how many times to scan for new messages, =1, forever=0 -default): ") or 0)
    except Exception:
        print("Invalid input. Using defaults.")
        delay, repeat_count = 3, 1
    
    selected_files = select_accounts()
    if not selected_files:
        return
    
    # Window positioning logic - read from config
    win_mode, win_width, win_height, stagger_offset = get_window_settings(config)
    positions = []
    for idx, fname in enumerate(selected_files):
        # Stagger windows with configured offset to avoid complete overlap
        x = (idx % 3) * stagger_offset
        y = (idx // 3) * stagger_offset
        positions.append((fname, x, y, win_width, win_height, delay, repeat_count, win_mode))
    
    # Launch each account using threading with window positioning
    threads = []
    for fname, x, y, w, h, d, rc, mode in positions:
        size_info = f"size ({w}x{h})" if mode == "custom" else "maximized"
        print(f"[Started] Bulk send message for {fname} at position ({x},{y}) {size_info}. Logs in logs/bulk_{fname}.log.")
        def run_bulk_send_thread(account_file=fname, pos_x=x, pos_y=y, width=w, height=h, delay_val=d, repeat_val=rc, win_mode=mode):
            bulk_send_message(delay_val, repeat_val, [account_file], pos_x, pos_y, width, height, win_mode)
        thread = threading.Thread(target=run_bulk_send_thread, daemon=True)
        thread.start()
        threads.append(thread)
    print("Launched all selected bulk send accounts with positioned windows (logs in logs/). Use menu to view logs.")
    return threads


def menu():
    global bot_statistics, stats_lock
    import subprocess, sys, os, signal, time
    bot_threads = []  # Changed from bot_processes to bot_threads
    # Ensure session start time is initialized once when menu starts
    if not bot_statistics.get('start_time'):
        bot_statistics['start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Start persistent database session
    try:
        accounts_dir = "accounts"
        account_files = [f for f in os.listdir(accounts_dir) if f.endswith('_cookies.json')] if os.path.isdir(accounts_dir) else []
        stats_tracker.start_session(accounts=[f.replace("_cookies.json", "") for f in account_files])
    except Exception:
        pass
    # Ensure background reporters are running
    start_background_reporters()
    while True:
        print("\n==== Messenger Auto-Reply Bot Menu ====")
        print("1. Start bot with single account")
        print("2. Start bot with multiple accounts")
        print("3. List accounts")
        print("4. Add account")
        print("5. Remove account")
        print("6. Rename account")
        print("7. Edit reply message")
        print("8. Pause/Resume bot")
        print("9. View logs for account")
        print("10. Exit")
        print("11. Auto-reply in main chat")
        print("12. Bulk send message to groups/people")
        print("13. Toggle auto popup closer (currently: ON)")
        choice = input("Select an option: ").strip()
        if choice == "1":
            threads = launch_accounts(single=True)
            if threads:
                bot_threads.extend(threads)
        elif choice == "2":
            threads = launch_accounts(single=False)
            if threads:
                bot_threads.extend(threads)
        elif choice == "3":
            list_accounts()
        elif choice == "4":
            add_account()
        elif choice == "5":
            remove_account()
        elif choice == "6":
            rename_account()
        elif choice == "7":
            edit_reply_message()
        elif choice == "8":
            toggle_pause()
        elif choice == "9":
            list_accounts()
            idx = input("Enter the number of the account to view logs: ").strip()
            try:
                idx = int(idx) - 1
                files = get_account_files()
                fname = files[idx]
                log_path = f"logs/{fname}.log"
                import os
                if not os.path.exists(log_path):
                    print(f"Log file {log_path} does not exist yet.")
                else:
                    import subprocess
                    # Use PowerShell tail for live updates
                    subprocess.Popen([
                        "cmd", "/c", f"start cmd /k powershell Get-Content -Path '{log_path}' -Wait"
                    ])
            except Exception:
                print("Invalid selection or failed to open log.")
        elif choice == "10":
            print("Stopping all running bots and cleaning up...")
            # For threads, we can't terminate them directly like processes
            # But we can kill Chrome processes and clean up logs
            time.sleep(2)
            # Send final stats before cleanup
            send_final_report_once("Menu option 10: Exit")
            kill_orphaned_chrome_processes()
            delete_all_logs()
            print("All bots closed and logs deleted. Returning to menu.")
            bot_threads.clear()
            continue
        elif choice == "11":
            threads = launch_main_chat_accounts()
            if threads:
                bot_threads.extend(threads)
        elif choice == "12":
            threads = launch_bulk_send_accounts()
            if threads:
                bot_threads.extend(threads)
        elif choice == "13":
            toggle_auto_popup_closer()
        else:
            print("Invalid option. Please select a valid menu item.")

def kill_orphaned_chrome_processes():
    """Close Chrome windows started by this bot.
    1) Attempt driver.quit() for each tracked driver
    2) Force kill PID tree via taskkill as fallback
    """
    import subprocess
    global BOT_CHROME_PROCESSES
    
    killed_count = 0
    for process_info in BOT_CHROME_PROCESSES[:]:
        pid = process_info.get('pid')
        drv = process_info.get('driver')
        child_chromes = process_info.get('chrome_pids', [])
        # Try graceful quit first
        try:
            if drv:
                try:
                    drv.quit()
                except Exception:
                    pass
        finally:
            # Ensure process tree is terminated
            if pid:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], 
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    killed_count += 1
                except Exception:
                    pass
            # Also directly kill observed chrome.exe children (in case re-parented)
            for cpid in child_chromes:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(cpid), "/T"], 
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                except Exception:
                    pass
    
    BOT_CHROME_PROCESSES.clear()
    print(f"Closed {killed_count} Chrome window(s) started by this bot.")

def delete_all_logs():
    import os, glob, logging
    log_dir = "logs"
    if os.path.exists(log_dir):
        # Close all logging handlers first to release file handles
        for logger_name in list(logging.Logger.manager.loggerDict.keys()):
            logger = logging.getLogger(logger_name)
            for handler in logger.handlers[:]:
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    logger.removeHandler(handler)
        
        # Now delete all log files
        for f in glob.glob(os.path.join(log_dir, "*.log")):
            try:
                os.remove(f)
                print(f"Deleted log file: {f}")
            except Exception as e:
                print(f"Could not delete {f}: {e}")
        print("All log files deleted.")

def parse_recipients(filepath="recipients.txt", logger=None):
    """Parse recipients.txt and return a list of (url, id, type) tuples."""
    recipients = []
    import re
    if not os.path.exists(filepath):
        if logger:
            logger.error(f"Recipient file {filepath} not found.")
        else:
            print(f"Recipient file {filepath} not found.")
        return recipients
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Match group: /t/ID, person: /e2ee/t/ID
            match = re.match(r"https://www\.messenger\.com/(e2ee/)?t/(\w+)", line)
            if match:
                is_person = match.group(1) is not None
                id_ = match.group(2)
                recipients.append((line, id_, "person" if is_person else "group"))
    return recipients

def select_accounts():
    """Prompt user to select account(s) for action. Returns list of selected account files."""
    files = get_account_files()
    if not files:
        print("No account cookie files found in 'accounts' folder.")
        return []
    list_accounts()
    idxs = input("Enter account numbers to use (comma-separated for multiple): ").split(",")
    try:
        selected_files = [files[int(i.strip()) - 1] for i in idxs]
    except Exception:
        print("Invalid selection.")
        return []
    return selected_files

def auto_reply_main_chat(delay=3, message_count=1, repeat_count=1, selected_files=None, pos_x=0, pos_y=0, width=800, height=600, win_mode='custom'):
    import sys
    import os
    import contextlib
    import logging
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    import pyperclip
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import TimeoutException

    # Logger setup for real-time log writing
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    account_file = selected_files[0] if selected_files else "mainchat"
    log_file_path = os.path.join(log_dir, f"mainchat_{account_file}.log")
    logger = logging.getLogger(f"mainchat_{account_file}")
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    fh = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    class FlushAndFsyncHandler(logging.Handler):
        def emit(self, record):
            fh.emit(record)
            fh.flush()
            try:
                os.fsync(fh.stream.fileno())
            except Exception:
                pass
    logger.addHandler(FlushAndFsyncHandler())
    
    # Summary log file path - tracks group names and message counts
    summary_log_file_path = os.path.join(log_dir, f"summary_mainchat_{account_file}.log")
    
    # Initialize summary tracking by reading existing log file
    group_message_counts = {}  # {group_name: message_count}
    
    def read_summary_log():
        """Read existing summary log and parse group message counts"""
        counts = {}
        if os.path.exists(summary_log_file_path):
            try:
                with open(summary_log_file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if ' - Group: ' in line and ' | Messages sent: ' in line:
                            # Parse: "2025-07-20 19:42:13,781 - Group: 'GroupName' | Messages sent: 1 | Chat ID: 123/"
                            try:
                                parts = line.split(" - Group: '", 1)
                                if len(parts) == 2:
                                    group_part = parts[1]
                                    group_name = group_part.split("' | Messages sent: ")[0]
                                    count_part = group_part.split("' | Messages sent: ")[1]
                                    count = int(count_part.split(" | Chat ID: ")[0])
                                    counts[group_name] = count
                            except Exception:
                                continue
            except Exception:
                pass
        return counts
    
    def write_summary_log(group_counts):
        """Write the complete summary log with updated counts"""
        try:
            with open(summary_log_file_path, 'w', encoding='utf-8') as f:
                for group_name, count in group_counts.items():
                    # Get the chat_id from the current tracking if available
                    chat_id = "unknown"
                    if hasattr(auto_reply_main_chat, 'group_chat_ids'):
                        chat_id = auto_reply_main_chat.group_chat_ids.get(group_name, "unknown")
                    
                    # Format timestamp like logging (with milliseconds)
                    import datetime
                    now = datetime.datetime.now()
                    timestamp = now.strftime('%Y-%m-%d %H:%M:%S,') + f"{now.microsecond // 1000:03d}"
                    f.write(f"{timestamp} - Group: '{group_name}' | Messages sent: {count} | Chat ID: {chat_id}\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.warning(f"Could not write summary log: {e}")
    
    # Read existing summary log
    group_message_counts = read_summary_log()
    
    # Initialize group-to-chat-id mapping for summary log
    if not hasattr(auto_reply_main_chat, 'group_chat_ids'):
        auto_reply_main_chat.group_chat_ids = {}

    config = load_config()
    reply_message = ""
    try:
        with open("reply_message.txt", "r", encoding="utf-8") as f:
            reply_message = f.read().strip()
    except FileNotFoundError:
        print("reply_message.txt not found.")
        return
    if not reply_message:
        print("Reply message is empty.")
        return

    if not selected_files:
        print("No accounts selected.")
        return

    for f in selected_files:
        try:
            update_account_status(f, "Waiting in queue")
        except Exception: pass

    for fname in selected_files:
        try:
            update_account_status(fname, "Initializing Chrome...")
        except Exception: pass
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, f"mainchat_{fname}.log")
        
        logger = logging.getLogger(f"mainchat_bot_{fname}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        
        fh = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        # --- Force flush after each log record and fsync ---
        class FlushAndFsyncHandler(logging.Handler):
            def emit(self, record):
                # Don't emit again, just flush the existing handler
                fh.flush()
                try:
                    os.fsync(fh.stream.fileno())
                except Exception:
                    pass
        if not any(isinstance(h, FlushAndFsyncHandler) for h in logger.handlers):
            logger.addHandler(FlushAndFsyncHandler())

        # Deduplication filter
        if not any(isinstance(f, DeduplicationFilter) for f in logger.filters):
            logger.addFilter(DeduplicationFilter())

        logger.info(f"Starting bot for account: {fname}")
        if win_mode == 'maximized':
            logger.info(f"Window will be maximized")
        else:
            logger.info(f"Window will be positioned at ({pos_x},{pos_y}) with size ({width}x{height})")
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument("--silent")
        
        # Handle window mode
        if win_mode == 'maximized':
            chrome_options.add_argument("--start-maximized")
        
        # Auto-download matching ChromeDriver version
        chromedriver_path = ChromeDriverManager().install()
        service = Service(chromedriver_path)
        devnull_file = open(os.devnull, 'w')
        service.log_output = devnull_file
        
        driver = None
        # Make teardown behavior configurable and visible to finally block
        abort_reason = None
        keep_open_on_fail = bool(config.get("keep_browser_open_on_fail", True))
        try:
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("WebDriver initialized.")
            # Track this Chrome process for targeted cleanup on shutdown
            track_chrome_process(driver, fname)
            
            # Apply window settings based on mode
            if win_mode == 'maximized':
                driver.maximize_window()
                logger.info(f"Window maximized successfully")
            else:
                driver.set_window_size(width, height)
                driver.set_window_position(pos_x, pos_y)
                logger.info(f"Window positioned successfully at ({pos_x},{pos_y}) with size ({width}x{height})")
            
            # --- NEW: Persistent login loop until successful ---
            logged_in = False
            while not logged_in:
                # Attempt Google Sheets cookie refresh before loading local cookies (if enabled)
                try:
                    gs_cfg = (config or {}).get('google_sheets') or {}
                    if gs_cfg.get('enabled'):
                        refresh_account_cookies(fname, "accounts", config, logger)
                except Exception:
                    pass

                cookies = load_cookies_from_file(os.path.join("accounts", fname))
                # Inject cookies to their proper domains
                try:
                    driver.get("https://www.facebook.com/")
                    driver.delete_all_cookies()
                    for c in cookies:
                        try:
                            if "facebook.com" in (c.get("domain") or ""):
                                driver.add_cookie(c)
                        except Exception: pass
                except Exception: pass
                try:
                    driver.get("https://www.messenger.com/")
                    for c in cookies:
                        try:
                            if "messenger.com" in (c.get("domain") or ""):
                                driver.add_cookie(c)
                        except Exception: pass
                    driver.refresh()
                except Exception: pass
                
                logger.info(f"Loaded cookies for {fname}, checking login status...")
                time.sleep(config.get("delay_retry_login", 5))

                # Robust login detection
                for attempt in range(5):
                    cur_url = (driver.current_url or "").lower()
                    if "login" in cur_url or "recover" in cur_url:
                        logged_in = False
                        break
                    
                    # Positive indicators
                    textbox_present = False
                    try:
                        elems = driver.find_elements(By.XPATH, '//div[@role="textbox"]')
                        textbox_present = len(elems) > 0 and any(e.is_displayed() for e in elems)
                    except: pass

                    if textbox_present:
                        logged_in = True
                        break
                    
                    try:
                        grid_elems = driver.find_elements(By.XPATH, "//div[@role='grid']")
                        if any(e.is_displayed() for e in grid_elems):
                            logged_in = True
                            break
                    except: pass
                    
                    time.sleep(2)

                if not logged_in:
                    logger.warning(f"Login failed for {fname}. Retrying with fresh cookies from sheet in 60s...")
                    try:
                        send_or_update_failed_login_notice(fname, config, "Login failed — Retrying persistently...")
                    except: pass
                    time.sleep(60)
                else:
                    logger.info("Login successful.")
                    try:
                        clear_failed_login_notice(fname, config)
                    except: pass
                    try:
                        update_account_status(fname, "🟢 Active - running")
                    except: pass
                    close_temporary_block_popup(driver)

                
                loops = 0
                while True:
                    if repeat_count > 0 and loops >= repeat_count:
                        logger.info(f"Repeat count reached for account {fname}.")
                        break
                    loops += 1

                    if os.path.exists("pause.flag"):
                        logger.info("Bot is paused. Waiting...")
                        time.sleep(2)
                        continue

                    # Auto-recover if Messenger shows 'Couldn't load chats'
                    recover_couldnt_load_chats(driver, logger)
                    
                    # Auto close popups if enabled
                    auto_close_popups(driver, logger)
                    
                    # Browser/session health check
                    try:
                        # This will raise WebDriverException if browser is dead
                        _ = driver.current_url
                    except Exception as e:
                        logger.critical(f"Browser session lost or crashed: {type(e).__name__}: {e}. Exiting loop.")
                    # Re-check login state mid-run; if logged out, alert and stop like Options 1 & 2
                    try:
                        # Check for logout during run - Use unified robust handler
                        if is_logged_out(driver):
                            logger.warning(f"Logout detected for {fname} in Main Chat loop. Attempting recovery...")
                            if handle_logout_and_refresh_with_credentials(driver, logger, config, fname):
                                logger.info("Login recovered successfully.")
                                time.sleep(5)
                                driver.get("https://www.messenger.com/")
                                time.sleep(3)
                            else:
                                logger.warning("Recovery failed, retrying in next loop iteration...")
                                time.sleep(30)
                                continue
                    except Exception:
                        pass

                    try:
                        logger.info("Waiting for main page and searching for unread group chats...")
                        
                        # Find all chat threads (group or personal) in the main chat list
                        chat_list_xpath = "//div[@role='grid']//div[@role='row']"
                        all_chats = WebDriverWait(driver, 20).until(
                            EC.presence_of_all_elements_located((By.XPATH, chat_list_xpath))
                        )

                        if not all_chats:
                            logger.info("No chat threads found.")
                        else:
                            logger.info(f"Found {len(all_chats)} chat thread(s). Will process up to {message_count} per repeat.")
                            if not hasattr(auto_reply_main_chat, "sent_chats"):
                                auto_reply_main_chat.sent_chats = {}  # {chat_id: last_sent_timestamp, ...}
                            if not hasattr(auto_reply_main_chat, "ignored_chats"):
                                auto_reply_main_chat.ignored_chats = set()  # set of chat_id or group_name
                            sent_chats = auto_reply_main_chat.sent_chats
                            ignored_chats = auto_reply_main_chat.ignored_chats
                            chats_sent = 0
                            delay_resend = config.get("delay_resend_same_chat", 600)
                            while chats_sent < message_count:
                                all_chats = driver.find_elements(By.XPATH, chat_list_xpath)
                                now = time.time()
                                eligible_found = False
                                for idx, chat in enumerate(all_chats):
                                    try:
                                        link_elem = chat.find_element(By.XPATH, ".//a[contains(@href, '/t/')]")
                                        href = link_elem.get_attribute("href")
                                        # Only process group chats (not personal)
                                        if "/e2ee/t/" in href:
                                            continue
                                        chat_id = href.split("/t/")[-1].split("?")[0]
                                        # Try to get group name for logging/tracking
                                        group_name = None
                                        try:
                                            group_name_elem = chat.find_element(By.XPATH, ".//span[@dir='auto']")
                                            group_name = group_name_elem.text.strip()
                                        except Exception:
                                            group_name = chat_id
                                        # Ignore chats that are known to be paused/restricted
                                        if chat_id in ignored_chats or (group_name and group_name in ignored_chats):
                                            logger.info(f"Skipping ignored group '{group_name}' ({chat_id}).")
                                            continue
                                        last_sent = sent_chats.get(chat_id, 0)
                                        if now - last_sent < delay_resend:
                                            logger.info(f"Skipping group '{group_name}' ({chat_id}), sent {int(now - last_sent)}s ago, delay {delay_resend}s")
                                            continue
                                        logger.info(f"Opening chat '{group_name}' (sidebar) at index {idx+1}...")
                                        click_success = False
                                        
                                        for attempt in range(3):
                                            try:
                                                # Re-find the chat element to avoid stale element issues
                                                fresh_chats = driver.find_elements(By.XPATH, chat_list_xpath)
                                                if idx >= len(fresh_chats):
                                                    logger.warning(f"Chat {idx} no longer available after refresh")
                                                    break
                                                    
                                                current_chat = fresh_chats[idx]
                                                
                                                # Try multiple clicking strategies (same as Options 1&2)
                                                click_strategies = [
                                                    lambda: current_chat.click(),
                                                    lambda: driver.execute_script("arguments[0].click();", current_chat),
                                                    lambda: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));", current_chat)
                                                ]
                                                
                                                chat_clicked = False
                                                for strategy_num, strategy in enumerate(click_strategies, 1):
                                                    try:
                                                        if current_chat.is_displayed() and current_chat.is_enabled():
                                                            strategy()
                                                            logger.info(f"Clicked chat '{group_name}' using strategy {strategy_num}")
                                                            chat_clicked = True
                                                            break
                                                    except Exception as click_e:
                                                        logger.debug(f"Click strategy {strategy_num} failed: {click_e}")
                                                        continue
                                                
                                                if not chat_clicked:
                                                    raise Exception("All click strategies failed")
                                                
                                                time.sleep(3)  # Wait for chat to load
                                                # If the error banner appears after opening the chat, recover
                                                recover_couldnt_load_chats(driver, logger)
                                                
                                                # Verify chat opened using textbox detection (same as Options 1&2)
                                                chat_opened = False
                                                verification_methods = [
                                                    # Method 1: Look for message textbox (most reliable)
                                                    lambda: driver.find_element(By.XPATH, '//div[@role="textbox"]').is_displayed(),
                                                    # Method 2: Look for message input area
                                                    lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "message") or contains(@placeholder, "message")]').is_displayed(),
                                                    # Method 3: Look for chat conversation area
                                                    lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "conversation") or contains(@role, "log")]').is_displayed()
                                                ]
                                                
                                                for method_num, method in enumerate(verification_methods, 1):
                                                    try:
                                                        if method():
                                                            logger.info(f"Chat '{group_name}' opened successfully (verified by method {method_num})")
                                                            chat_opened = True
                                                            break
                                                    except Exception:
                                                        continue
                                                
                                                if not chat_opened:
                                                    logger.error(f"Chat '{group_name}' did not open properly")
                                                    raise Exception("Chat did not open properly")

                                                # Mark success and break out of attempts loop
                                                click_success = True
                                                break
                                            except Exception as e:
                                                logger.warning(f"Attempt {attempt+1}: Error clicking chat '{group_name}' at index {idx+1}: {e}")
                                                time.sleep(2)

                                        # After attempts, skip if not successful
                                        if not click_success:
                                            logger.warning(f"Failed to open chat '{group_name}' after 3 attempts. Skipping to next thread.")
                                            continue
                                            
                                        # CRITICAL: Verify we're in the correct chat before sending message
                                        chat_verification_success = False
                                        for verify_attempt in range(3):
                                            try:
                                                # Wait for chat to fully load
                                                time.sleep(2)
                                                
                                                # Method 1: Check URL contains the chat ID
                                                current_url = driver.current_url
                                                if chat_id in current_url:
                                                    logger.info(f"Chat verification successful: URL contains chat ID '{chat_id}'")
                                                    chat_verification_success = True
                                                    break
                                                    
                                                # Method 2: Check chat header/title
                                                try:
                                                    chat_header = driver.find_element(By.XPATH, "//h1[contains(@class, 'x1heor9g')]")
                                                    if chat_header and group_name.lower() in chat_header.text.lower():
                                                        logger.info(f"Chat verification successful: Header matches '{group_name}'")
                                                        chat_verification_success = True
                                                        break
                                                except:
                                                    pass
                                                    
                                                # Method 3: Check for specific chat elements
                                                try:
                                                    conversation_area = driver.find_element(By.XPATH, "//div[@role='main']//div[contains(@aria-label, 'Conversation')]")
                                                    if conversation_area:
                                                        logger.info(f"Chat verification successful: Conversation area found")
                                                        chat_verification_success = True
                                                        break
                                                except:
                                                    pass
                                                    
                                                logger.warning(f"Chat verification attempt {verify_attempt + 1}: Could not verify correct chat opened")
                                                time.sleep(1)
                                                
                                            except Exception as verify_e:
                                                logger.warning(f"Chat verification attempt {verify_attempt + 1} failed: {verify_e}")
                                                time.sleep(1)
                                                
                                        if not chat_verification_success:
                                            logger.error(f"CRITICAL: Could not verify correct chat '{group_name}' opened. Skipping to prevent wrong message sending.")
                                            # Navigate back and continue to next chat
                                            try:
                                                driver.get("https://www.messenger.com/")
                                                time.sleep(2)
                                            except:
                                                pass
                                            continue
                                        message_box_xpath = "//div[@aria-label='Message' and @role='textbox']"
                                        message_box = None
                                        for box_attempt in range(3):
                                            try:
                                                message_box = WebDriverWait(driver, 5).until(
                                                    EC.presence_of_element_located((By.XPATH, message_box_xpath))
                                                )
                                                
                                                # Try multiple clicking strategies
                                                click_strategies = [
                                                    lambda: message_box.click(),
                                                    lambda: driver.execute_script("arguments[0].click();", message_box),
                                                    lambda: driver.execute_script("arguments[0].focus();", message_box)
                                                ]
                                                
                                                clicked = False
                                                for strategy_num, strategy in enumerate(click_strategies, 1):
                                                    try:
                                                        if message_box.is_displayed() and message_box.is_enabled():
                                                            strategy()
                                                            logger.info(f"Clicked message box for '{group_name}' using strategy {strategy_num}")
                                                            clicked = True
                                                            break
                                                    except Exception:
                                                        continue
                                                
                                                if clicked:
                                                    break
                                                else:
                                                    logger.warning(f"Attempt {box_attempt + 1}: Could not click message box for '{group_name}'")
                                                    time.sleep(1)
                                                    
                                            except Exception as e:
                                                logger.warning(f"Attempt {box_attempt + 1}: Could not find message box for '{group_name}': {e}")
                                                time.sleep(1)
                                        
                                        if not message_box or not message_box.is_displayed():
                                            # Check if chat is paused/restricted/unavailable (same as Option 11)
                                            chat_status_indicators = [
                                                "This chat is paused",
                                                "This conversation is paused", 
                                                "You can't message this person",
                                                "This person isn't available",
                                                "Chat is unavailable",
                                                "Conversation unavailable"
                                            ]
                                            
                                            page_text = driver.page_source.lower()
                                            is_paused_chat = any(indicator.lower() in page_text for indicator in chat_status_indicators)
                                            
                                            if is_paused_chat:
                                                logger.warning(f"Chat '{group_name}' is paused/unavailable. This chat should be avoided in future.")
                                            else:
                                                logger.error(f"No usable message box found for '{group_name}' after 3 attempts")
                                            
                                            # Add to ignored chats to avoid future attempts
                                            ignored_chats.add(chat_id)
                                            if group_name:
                                                ignored_chats.add(group_name)
                                            logger.info(f"Chat '{group_name}' will be permanently ignored in future runs.")
                                            continue
                                        # Enhanced message sending with better error handling
                                        try:
                                            # Process random message syntax
                                            processed_message = process_random_message(reply_message)
                                            pyperclip.copy(processed_message)
                                            message_box.send_keys(Keys.CONTROL, "v")
                                            logger.info(f"Pasted the message from clipboard for chat '{group_name}'")
                                            time.sleep(1)
                                            # Update statistics for option 11 (auto_reply_main_chat should report as option 11)
                                            account_name = os.path.basename(logger.handlers[0].baseFilename).replace('.log', '') if logger.handlers else 'unknown'
                                            update_statistics('option11', account_name)
                                            message_box.send_keys(Keys.RETURN)
                                            logger.info(f"Message sent successfully to group '{group_name}' ({chat_id})")
                                            
                                            # Update summary tracking
                                            if group_name in group_message_counts:
                                                group_message_counts[group_name] += 1
                                            else:
                                                group_message_counts[group_name] = 1
                                            
                                            # Store chat_id for summary log
                                            auto_reply_main_chat.group_chat_ids[group_name] = chat_id
                                            
                                            # Update summary log file (rewrite with updated counts)
                                            write_summary_log(group_message_counts)
                                            
                                            # Mark as processed immediately after successful send
                                            sent_chats[chat_id] = time.time()
                                            # Also save by group name for better tracking
                                            if group_name and group_name != chat_id:
                                                sent_chats[group_name] = time.time()
                                            chats_sent += 1
                                            logger.info(f"Marked chat '{group_name}' ({chat_id}) as processed (sent count: {chats_sent})")
                                            logger.info(f"Chat will be skipped for {config.get('delay_resend_same_chat', 600)} seconds")
                                            
                                            time.sleep(config.get("delay_between_messages", 3))
                                            
                                        except Exception as send_e:
                                            logger.error(f"Could not send message to '{group_name}': {type(send_e).__name__}: {send_e}")
                                            continue
                                            
                                        # Navigate back to main chat list (prioritize back button, no page reload)
                                        back_navigation_success = False
                                        navigation_strategies = [
                                            # Strategy 1: Look for back arrow button (most efficient)
                                            lambda: driver.find_element(By.XPATH, '//div[@aria-label="Back" or @aria-label="Go back"]').click(),
                                            # Strategy 2: Look for close button
                                            lambda: driver.find_element(By.XPATH, '//div[@aria-label="Close"]').click(),
                                            # Strategy 3: Press Escape key
                                            lambda: driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE),
                                            # Strategy 4: Click on messenger logo/home (avoid page reload)
                                            lambda: driver.find_element(By.XPATH, '//a[contains(@href, "/")]').click(),
                                            # Strategy 5: Navigate back to main messenger page (last resort)
                                            lambda: driver.get("https://www.messenger.com/")
                                        ]
                                        
                                        for strategy_num, strategy in enumerate(navigation_strategies, 1):
                                            try:
                                                strategy()
                                                logger.info(f"Navigated back to chat list using strategy {strategy_num} after replying to '{group_name}'")
                                                back_navigation_success = True
                                                time.sleep(2)
                                                break
                                            except Exception as nav_e:
                                                logger.debug(f"Navigation strategy {strategy_num} failed: {nav_e}")
                                                continue
                                        
                                        if not back_navigation_success:
                                            logger.warning(f"Could not navigate back after replying to '{group_name}' - trying to continue anyway")
                                        
                                        eligible_found = True
                                        break  # Only send to one chat per loop iteration
                                    except Exception as e:
                                        logger.error(f"Error sending message to chat index {idx+1}: {e}")
                                if not eligible_found:
                                    logger.info("No eligible group chat found to send message.")
                                    break

                        # After processing, loop will repeat if repeat_count > 1

                    except TimeoutException:
                        logger.info("No unread group chats found in the current view.")
                    except Exception as e:
                        logger.critical(f"An unexpected error occurred in the main loop: {e}")
                        try:
                            driver.save_screenshot(os.path.join(log_dir, f"error_screenshot_{fname}.png"))
                        except Exception:
                            logger.debug("Could not take screenshot after error; driver likely unavailable.")
                        try:
                            driver.refresh()
                        except Exception:
                            logger.debug("Could not refresh after error; driver likely unavailable.")

                    logger.info(f"Loop finished. Waiting for {delay} seconds before next check.")
                    try:
                        update_account_status(fname, "🟢 Active - waiting for next check")
                    except: pass
                    time.sleep(delay)

        except Exception as e:
            logger.critical(f"A critical error occurred for account {fname}: {e}")
            if driver:
                try:
                    driver.save_screenshot(os.path.join(log_dir, f"error_screenshot_{fname}.png"))
                except Exception:
                    logger.debug("Could not take screenshot after critical error; driver likely unavailable.")
        finally:
            try:
                update_account_status(fname, "🔴 Stopped / Finished")
            except: pass
            if driver:
                should_quit = True
                try:
                    if abort_reason in ("login_fail", "midrun_logout") and keep_open_on_fail:
                        should_quit = False
                except Exception:
                    pass
                if should_quit:
                    driver.quit()
                    logger.info("WebDriver quit.")
                else:
                    logger.info("Keeping WebDriver open for inspection (login failure).")
            devnull_file.close()

def send_message_to_chat(driver, url, message, config, logger, option_type='option11'):
    """Enhanced message sending with robust error handling and verification"""
    chat_name = url.split('/')[-1] if '/' in url else url
    
    try:
        # Pre-check and rehome if browser is on an interruption/off-route page
        try:
            rehome_if_interrupted(driver, logger, config)
        except Exception:
            pass
        logger.info(f"Navigating to chat: {chat_name}")
        driver.get(url)
        time.sleep(config.get("delay_chat_load", 5))
        # If chat load error is present after navigation, recover before proceeding
        recover_couldnt_load_chats(driver, logger)
        
        # Auto close popups if enabled
        auto_close_popups(driver, logger)
        # Recover again in case closing popups revealed the error banner
        recover_couldnt_load_chats(driver, logger)
        
        # Verify chat opened using multiple methods (same as Options 1&2)
        chat_opened = False
        verification_methods = [
            # Method 1: Look for message textbox (most reliable)
            lambda: driver.find_element(By.XPATH, '//div[@role="textbox"]').is_displayed(),
            # Method 2: Look for message input area
            lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "message") or contains(@placeholder, "message")]').is_displayed(),
            # Method 3: Look for chat conversation area
            lambda: driver.find_element(By.XPATH, '//div[contains(@aria-label, "conversation") or contains(@role, "log")]').is_displayed()
        ]
        
        for method_num, method in enumerate(verification_methods, 1):
            try:
                if method():
                    logger.info(f"Chat '{chat_name}' opened successfully (verified by method {method_num})")
                    chat_opened = True
                    break
            except Exception:
                continue
        
        if not chat_opened:
            logger.error(f"Chat '{chat_name}' did not open properly")
            return False
            
        # CRITICAL: Enhanced chat verification before sending message (same as Option 11)
        chat_verification_success = False
        for verify_attempt in range(3):
            try:
                # Wait for chat to fully load
                time.sleep(2)
                
                # Method 1: Check URL contains the chat ID
                current_url = driver.current_url
                if chat_name in current_url:
                    logger.info(f"Chat verification successful: URL contains chat ID '{chat_name}'")
                    chat_verification_success = True
                    break
                    
                # Method 2: Check chat header/title
                try:
                    chat_header = driver.find_element(By.XPATH, "//h1[contains(@class, 'x1heor9g')]")
                    if chat_header and chat_header.is_displayed():
                        logger.info(f"Chat verification successful: Header found for '{chat_name}'")
                        chat_verification_success = True
                        break
                except:
                    pass
                    
                # Method 3: Check for specific chat elements
                try:
                    conversation_area = driver.find_element(By.XPATH, "//div[@role='main']//div[contains(@aria-label, 'Conversation')]")
                    if conversation_area:
                        logger.info(f"Chat verification successful: Conversation area found for '{chat_name}'")
                        chat_verification_success = True
                        break
                except:
                    pass
                    
                logger.warning(f"Chat verification attempt {verify_attempt + 1}: Could not verify correct chat opened for '{chat_name}'")
                time.sleep(1)
                
            except Exception as verify_e:
                logger.warning(f"Chat verification attempt {verify_attempt + 1} failed for '{chat_name}': {verify_e}")
                time.sleep(1)
                
        if not chat_verification_success:
            logger.error(f"CRITICAL: Could not verify correct chat '{chat_name}' opened. Skipping to prevent wrong message sending.")
            return False
        
        # Ensure 'Accept' clicked if necessary to reveal textbox
        click_accept_if_present(driver, logger)
        # Find and click message box with multiple attempts
        message_box = None
        for attempt in range(3):
            try:
                message_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, '//div[@role="textbox"]'))
                )
                
                # Try multiple clicking strategies
                click_strategies = [
                    lambda: message_box.click(),
                    lambda: driver.execute_script("arguments[0].click();", message_box),
                    lambda: driver.execute_script("arguments[0].focus();", message_box)
                ]
                
                clicked = False
                for strategy_num, strategy in enumerate(click_strategies, 1):
                    try:
                        if message_box.is_displayed() and message_box.is_enabled():
                            strategy()
                            logger.info(f"Clicked message box for '{chat_name}' using strategy {strategy_num}")
                            clicked = True
                            break
                    except Exception:
                        continue
                
                if clicked:
                    break
                else:
                    logger.warning(f"Attempt {attempt + 1}: Could not click message box for '{chat_name}'")
                    time.sleep(1)
                    
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}: Could not find message box for '{chat_name}': {e}")
                time.sleep(1)
        
        if not message_box or not message_box.is_displayed():
            # Check if chat is paused/restricted/unavailable (same as Option 11)
            chat_status_indicators = [
                "This chat is paused",
                "This conversation is paused", 
                "You can't message this person",
                "This person isn't available",
                "Chat is unavailable",
                "Conversation unavailable"
            ]
            
            page_text = driver.page_source.lower()
            is_paused_chat = any(indicator.lower() in page_text for indicator in chat_status_indicators)
            
            if is_paused_chat:
                logger.warning(f"Chat '{chat_name}' is paused/unavailable. This chat should be avoided in future.")
            else:
                logger.error(f"No usable message box found for '{chat_name}' after 3 attempts")
            
            return False
        
        # Enhanced message sending with better error handling
        try:
            time.sleep(1)
            # Process random message syntax for option 11
            processed_message = process_random_message(message)
            pyperclip.copy(processed_message)
            message_box.send_keys(Keys.CONTROL, "v")
            logger.info(f"Pasted the message from clipboard for chat '{chat_name}'")
            time.sleep(1)
            # Update statistics for option 11
            account_name = os.path.basename(logger.handlers[0].baseFilename).replace('.log', '') if logger.handlers else 'unknown'
            update_statistics(option_type, account_name)
            message_box.send_keys(Keys.RETURN)
            logger.info(f"Message sent successfully to '{chat_name}'")
            
            time.sleep(config.get("delay_between_messages", 3))
            return True
            
        except Exception as send_e:
            logger.error(f"Could not send message to '{chat_name}': {type(send_e).__name__}: {send_e}")
            return False
            
    except Exception as e:
        logger.error(f"Error accessing chat '{chat_name}': {type(e).__name__}: {e}")
        return False

def bulk_send_message(delay=3, repeat_count=1, selected_files=None, pos_x=0, pos_y=0, width=800, height=600, win_mode='custom'):
    import logging
    import os
    from selenium.common.exceptions import TimeoutException

    # Only set up per-account loggers below. Use a temporary logger for critical errors only if needed.
    recipients = parse_recipients()
    if not recipients:
        print("No valid recipients found in recipients.txt.")
        return

    try:
        with open("reply_message.txt", "r", encoding="utf-8") as file:
            reply_message = file.read().strip()
        if not reply_message:
            print("Reply message is empty.")
            return
    except FileNotFoundError:
        print("reply_message.txt not found.")
        return

    if selected_files is None:
        # This should not happen if called from subprocess, but as a fallback
        selected_files = select_accounts()
    if not selected_files:
        return

    for f in selected_files:
        try:
            update_account_status(f, "Waiting in queue")
        except Exception: pass

    config = load_config()

    for fname in selected_files:
        try:
            update_account_status(fname, "Initializing Chrome...")
        except Exception: pass
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, f"bulk_{fname}.log")
        
        logger = logging.getLogger(f"bulk_bot_{fname}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        
        fh = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        # --- Force flush after each log record and fsync ---
        class FlushAndFsyncHandler(logging.Handler):
            def emit(self, record):
                # Don't emit again, just flush the existing handler
                fh.flush()
                try:
                    os.fsync(fh.stream.fileno())
                except Exception:
                    pass
        if not any(isinstance(h, FlushAndFsyncHandler) for h in logger.handlers):
            logger.addHandler(FlushAndFsyncHandler())

        # Deduplication filter
        if not any(isinstance(f, DeduplicationFilter) for f in logger.filters):
            logger.addFilter(DeduplicationFilter())

        # Stacktrace suppression filter
        class StacktraceSuppressionFilter(logging.Filter):
            def filter(self, record):
                # Remove Selenium stacktrace lines from error log messages
                msg = record.getMessage()
                if 'Stacktrace:' in msg or '\tGetHandleVerifier' in msg:
                    return False
                return True
        if not any(isinstance(f, StacktraceSuppressionFilter) for f in logger.filters):
            logger.addFilter(StacktraceSuppressionFilter())

        logger.info(f"Starting bulk message bot for account: {fname}")
        logger.info(f"Found {len(recipients)} recipient(s). Repeat count: {repeat_count}, Delay: {delay}s.")
        if win_mode == 'maximized':
            logger.info(f"Window will be maximized")
        else:
            logger.info(f"Window will be positioned at ({pos_x},{pos_y}) with size ({width}x{height})")
        

        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument("--silent")
        
        # Handle window mode
        if win_mode == 'maximized':
            chrome_options.add_argument("--start-maximized")
        
        # Auto-download matching ChromeDriver version
        chromedriver_path = ChromeDriverManager().install()
        service = Service(chromedriver_path)
        devnull_file = open(os.devnull, 'w')
        service.log_output = devnull_file
        
        driver = None
        try:
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("WebDriver initialized.")
            # Track this Chrome process for targeted cleanup on shutdown
            track_chrome_process(driver, fname)

            # Apply window settings based on mode
            if win_mode == 'maximized':
                driver.maximize_window()
                logger.info(f"Window maximized successfully")
            else:
                driver.set_window_size(width, height)
                driver.set_window_position(pos_x, pos_y)
                logger.info(f"Window positioned successfully at ({pos_x},{pos_y}) with size ({width}x{height})")
            
            # --- NEW: Persistent login loop for Bulk Mode ---
            logged_in = False
            while not logged_in:
                # Attempt Google Sheets cookie refresh (if enabled)
                try:
                    gs_cfg = (config or {}).get('google_sheets') or {}
                    if gs_cfg.get('enabled'):
                        refresh_account_cookies(fname, "accounts", config, logger)
                except Exception: pass

                cookies = load_cookies_from_file(os.path.join("accounts", fname))
                driver.get("https://www.messenger.com/")
                driver.delete_all_cookies()
                for cookie in cookies:
                    try: driver.add_cookie(cookie)
                    except: pass
                driver.refresh()
                logger.info(f"Loaded cookies for {fname} in Bulk mode, checking login status...")
                time.sleep(config.get("delay_retry_login", 5))

                logged_in = detect_logged_in_state(driver, logger, config)
                if not logged_in:
                    logger.error(f"Failed to login for {fname} in Bulk mode. Attempting recovery loop...")
                    if handle_logout_and_refresh_with_credentials(driver, logger, config, fname):
                        logged_in = True
                        logger.info("Login recovered.")
                    else:
                        logger.warning("Recovery failed. Retrying full cookie refresh in 60s...")
                        time.sleep(60)
                else:
                    logger.info("Login successful.")
                    try:
                        clear_failed_login_notice(fname, config)
                    except: pass
                    try:
                        update_account_status(fname, "🟢 Active - running")
                    except: pass
                    close_temporary_block_popup(driver)

                
                loops = 0
                while True:
                    if repeat_count > 0 and loops >= repeat_count:
                        logger.info(f"Repeat count reached for account {fname}.")
                        break
                    loops += 1
                    logger.info(f"Starting send loop {loops}/{repeat_count if repeat_count > 0 else '∞'}")
                    
                    # Auto-recover if Messenger shows 'Couldn't load chats'
                    recover_couldnt_load_chats(driver, logger)

                    # Auto close popups if enabled
                    auto_close_popups(driver, logger)
                    # Ensure we are on messenger home if any interruption occurred
                    try:
                        rehome_if_interrupted(driver, logger, config)
                    except Exception:
                        pass
                    
                    for url, rid, rtype in recipients:
                        logger.info(f"Sending message to {url}")
                        # Recover just before attempting to open each chat
                        recover_couldnt_load_chats(driver, logger)
                        try:
                            rehome_if_interrupted(driver, logger, config)
                        except Exception:
                            pass
                        success = send_message_to_chat(driver, url, reply_message, config, logger, option_type='option12')
                        
                        if success:
                            logger.info(f"Message sent successfully to {url}.")
                        else:
                            logger.error(f"Could not send message to {url}")
                        try:
                            update_account_status(fname, "🟢 Active - waiting for next check")
                        except: pass
                        time.sleep(delay)

                    logger.info(f"Finished send loop {loops}.")

        except Exception as e:
            logger.critical(f"A critical error occurred for account {fname}: {e}")
            # Ensure bulk option also contributes to the consolidated failed-login alert
            try:
                send_or_update_failed_login_notice(fname, config, "🔴 Bulk mode setup error — check/refresh cookies")
            except Exception:
                pass
            if driver:
                driver.save_screenshot(os.path.join(log_dir, f"error_screenshot_bulk_{fname}.png"))
        finally:
            try:
                update_account_status(fname, "🔴 Stopped / Finished")
            except: pass
            if driver:
                should_quit = True
                try:
                    if abort_reason in ("login_fail", "midrun_logout") and keep_open_on_fail:
                        should_quit = False
                except Exception:
                    pass
                if should_quit:
                    driver.quit()
                    logger.info("WebDriver quit.")
                else:
                    logger.info("Keeping WebDriver open for inspection (login failure).")
            devnull_file.close()

if __name__ == "__main__":
    # Attempt recovery report from previous unexpected stop
    try:
        recover_and_report_previous_session()
    except Exception:
        pass
    # Start background reporters early
    try:
        # Ensure start_time is set and send an initial status immediately
        with stats_lock:
            if not bot_statistics.get('start_time'):
                bot_statistics['start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            _maybe_throttled_status_update("Bot launched")
        except Exception:
            pass
        start_background_reporters()
    except Exception:
        pass
    menu()
