#!/usr/bin/env python3
"""
FewFeed Bot - Template Profile Version
Guides user to create a master profile, then uses it as a template.
"""

import os
import sys
import atexit
import json
import time
import threading
import random
import shutil 

# Cross-bot stats tracker (SQLite)
try:
    _ff_tracker_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if _ff_tracker_dir not in sys.path:
        sys.path.insert(0, _ff_tracker_dir)
    from stats_tracker import get_tracker
    _ff_tracker = get_tracker("FewFeed")
    del _ff_tracker_dir
except Exception:
    class _Null:
        def log_event(self, *a, **kw): pass
        def log_login_failure(self, *a, **kw): pass
        def log_login_success(self, *a, **kw): pass
    _ff_tracker = _Null()

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import subprocess
import pyautogui
import re
from pywinauto.application import Application
from pywinauto.keyboard import send_keys
import ctypes
from ctypes import wintypes
import win32clipboard as wc
import win32con
import hashlib
from datetime import datetime, timezone
import logging
import contextlib

# Per-session guard: ensure we only prime (click the first result row) once per browser session
FIRST_ROW_PRIMED_SESSIONS = set()

def _is_primed(driver) -> bool:
    try:
        sid = getattr(driver, 'session_id', None)
        return sid in FIRST_ROW_PRIMED_SESSIONS
    except Exception:
        return False

def ff_find_view_post_button(context):
    """Return the green 'View Post' button element if present.
    Tries multiple robust selectors to survive UI changes and locales.
    """
    # 1) Direct text (case-insensitive) on button or link
    for xp in [
        ".//button[contains(translate(normalize-space(.), 'VIEWPOST', 'viewpost'), 'view post')]",
        ".//a[contains(translate(normalize-space(.), 'VIEWPOST', 'viewpost'), 'view post')]",
    ]:
        try:
            elems = context.find_elements(By.XPATH, xp)
            btn = next((e for e in elems if e.is_displayed()), None)
            if btn:
                return btn
        except Exception:
            pass
    # 2) Inside success toast/card that contains the success text
    for xp in [
        ".//div[contains(translate(., 'SUCCESSFULLY', 'successfully'), 'successfully') and contains(translate(., 'POST', 'post'), 'post')]//button",
        ".//div[contains(translate(., 'SUCCESSFULLY POST TO', 'successfully post to'), 'successfully post to')]//button",
    ]:
        try:
            elems = context.find_elements(By.XPATH, xp)
            btn = next((e for e in elems if e.is_displayed()), None)
            if btn:
                return btn
        except Exception:
            pass
    # 3) Any visible green-looking action button near success block
    for xp in [
        ".//button[contains(@class,'bg-green') or contains(@class,'text-white')][.//*[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')] or contains(@class,'font-extrabold')]",
        ".//button[contains(@class,'bg-green') and contains(@class,'cursor-pointer')]",
    ]:
        try:
            elems = context.find_elements(By.XPATH, xp)
            btn = next((e for e in elems if e.is_displayed()), None)
            if btn:
                return btn
        except Exception:
            pass
    return None

def ff_results_container_ready(driver, timeout=3) -> bool:
    """Return True if the left results container appears present/visible for this driver.
    This checks ONLY within the driver's own tabs, so it works for multiple accounts in parallel.
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            # Prefer the container that has rows with svg status icons
            rows = driver.find_elements(By.XPATH, "//div[contains(@class,'flex') and .//*[name()='svg']]")
            if rows:
                return True
            # Sometimes the disclosure needs a click to reveal the list
            with contextlib.suppress(Exception):
                btns = driver.find_elements(By.XPATH, "//button[contains(@class,'headlessui-disclosure-button')]")
                btn = next((b for b in btns if b.is_displayed()), None)
                if btn:
                    driver.execute_script("arguments[0].click();", btn); time.sleep(0.2)
            # Pulse-scroll the container to force lazy load
            ff_pulse_results_container_scroll(driver)
        except Exception:
            pass
        time.sleep(0.1)
    return False

def _mark_primed(driver):
    try:
        sid = getattr(driver, 'session_id', None)
        if sid:
            FIRST_ROW_PRIMED_SESSIONS.add(sid)
    except Exception:
        pass

def _clear_primed(driver):
    try:
        sid = getattr(driver, 'session_id', None)
        if sid in FIRST_ROW_PRIMED_SESSIONS:
            FIRST_ROW_PRIMED_SESSIONS.remove(sid)
    except Exception:
        pass
import requests
import csv
import io

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

# --- Helper Paths ---
def get_base_path():
    """Return the directory where the script/exe is located."""
    return os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))

# --- Facebook session helpers ---
def fb_logged_out(driver) -> bool:
    """Detect if the current driver is logged out of Facebook.
    We consider it logged OUT only when one of the following is true:
      - URL contains '/login' or '/recover' on facebook.com
      - A visible form with email/phone + password and a visible Log in/Connexion button exists
      - No 'c_user' cookie for .facebook.com while page shows a login form
    Otherwise, we treat it as logged IN.
    """
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    if "facebook.com/login" in url or "facebook.com/recover" in url:
        return True
    try:
        # Detect login form heuristics (language agnostic via field types)
        has_user = len(driver.find_elements(By.CSS_SELECTOR, "input[name='email'], input[name='text'], input[type='text']")) > 0
        has_pass = len(driver.find_elements(By.CSS_SELECTOR, "input[name='pass'], input[type='password']")) > 0
        # Buttons with common texts across locales
        login_btn = None
        for xp in [
            "//button[contains(translate(., 'LOGIN', 'login'), 'log in') or contains(., 'Connexion') or contains(., 'Iniciar sesión')]",
            "//input[@type='submit' and (contains(translate(@value, 'LOGIN', 'login'), 'log in') or contains(@value,'Connexion') or contains(@value,'Iniciar'))]",
        ]:
            with contextlib.suppress(Exception):
                elems = driver.find_elements(By.XPATH, xp)
                login_btn = next((e for e in elems if e.is_displayed()), None)
                if login_btn:
                    break
        if has_user and has_pass and login_btn:
            return True
    except Exception:
        pass
    # Cookie-based heuristic
    try:
        cu = driver.get_cookie('c_user')
        if cu and cu.get('value'):
            return False
    except Exception:
        pass
    # Default to logged-in unless strong evidence of login page
    return False

def _clear_recovering(account_id):
    with contextlib.suppress(Exception):
        ACCOUNT_STATE["recovering"].pop(int(account_id), None)

# --- Clipboard (CF_HDROP) helper ---
_CLIPBOARD_SIGNATURE = None  # (folder_hash, file_count)

def set_clipboard_file_list(file_paths):
    """Put a list of file paths onto the Windows clipboard in CF_HDROP format (UNICODE)."""
    # Build double-null-terminated UTF-16LE string of full paths
    files_str = "\0".join(file_paths) + "\0\0"
    files_bytes = files_str.encode('utf-16le')

    class DROPFILES(ctypes.Structure):
        _fields_ = [
            ("pFiles", wintypes.DWORD),
            ("pt", wintypes.POINT),
            ("fNC", wintypes.BOOL),
            ("fWide", wintypes.BOOL),
        ]

    size = ctypes.sizeof(DROPFILES) + len(files_bytes)
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    hGlobal = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, size)
    if not hGlobal:
        raise RuntimeError("GlobalAlloc failed for CF_HDROP buffer")
    ptr = ctypes.windll.kernel32.GlobalLock(hGlobal)
    if not ptr:
        raise RuntimeError("GlobalLock failed for CF_HDROP buffer")
    try:
        df = DROPFILES.from_address(ptr)
        df.pFiles = ctypes.sizeof(DROPFILES)
        # df.pt defaults to (0,0)
        df.fNC = False
        df.fWide = True
        ctypes.memmove(ptr + ctypes.sizeof(DROPFILES), files_bytes, len(files_bytes))
    finally:
        ctypes.windll.kernel32.GlobalUnlock(hGlobal)

    wc.OpenClipboard()
    try:
        wc.EmptyClipboard()
        wc.SetClipboardData(win32con.CF_HDROP, hGlobal)
    finally:
        wc.CloseClipboard()

def ensure_clipboard_loaded_for_folder(folder, force=False):
    """Load all images from folder to clipboard in CF_HDROP format.
    If force=True, always reload even if signature matches (useful after text clipboard operations)."""
    global _CLIPBOARD_SIGNATURE
    exts = (".jpg", ".jpeg", ".png", ".gif")
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
    files = [os.path.normpath(p) for p in files]
    if not files:
        raise Exception(f"No images found in: {folder}")
    # signature = hash of folder path + file names
    sig_src = folder + "|" + "|".join(sorted(os.path.basename(p) for p in files))
    sig = hashlib.sha1(sig_src.encode('utf-8')).hexdigest(), len(files)
    if force or _CLIPBOARD_SIGNATURE != sig:
        set_clipboard_file_list(files)
        _CLIPBOARD_SIGNATURE = sig
    return files

# --- Configuration ---
CONFIG_PATH = os.path.join(get_base_path(), 'config.json')

def load_config():
    # Defaults
    default_cfg = {
        "thread_value": 3,
        "delay_value": 5,
        "enable_auto_post": False,
        "post_with_images": False,
        "extension_id": ""
    }
    if not os.path.exists(CONFIG_PATH):
        return default_cfg
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        default_cfg.update(data)
    except Exception:
        pass

def _tg_build_text(accounts: list[int]) -> str:
    # Bot identifier is fixed as "2" per user requirement (do not change this number)
    lines = ["❗ 2 Accounts with Login Issues"]
    for aid in sorted(accounts):
        lines.append(f"- {aid}_cookies.json — 🔴 Logged out — attempting cookie refresh…")
    lines.append("\n🛠️ Action: Update cookies for the listed accounts and restart.")
    return "\n".join(lines)

def save_telemetry(bot_name, account, status=None, failed_logins=None, stats=None, recent_events=None):
    try:
        import time
        import json

        # Log to shared database (login state only). FewFeed is a posting bot;
        # its periodic status pings carry a cumulative "posts" count and must NOT
        # be logged as reply/message events (that inflated the dashboard counters).
        try:
            acc = str(account).replace("_cookies.json", "").replace("_cookies", "").replace(".json", "").strip()
            if failed_logins is not None and isinstance(failed_logins, dict):
                if failed_logins:
                    _ff_tracker.log_login_failure(acc, reason=list(failed_logins.values())[0])
                else:
                    _ff_tracker.log_login_success(acc)
        except Exception:
            pass

        telemetry_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "telemetry"))
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
        print(f"Telemetry save failed: {e}")

def tg_alert_add(account_id: int):
    save_telemetry(
        bot_name="FewFeed",
        account=account_id,
        status="Logged Out",
        failed_logins={str(account_id): "Logged out — cookie expired or needs 2FA recovery"}
    )

def tg_alert_remove(account_id: int):
    save_telemetry(
        bot_name="FewFeed",
        account=account_id,
        status="Running",
        failed_logins={}
    )

config = load_config()

# duplicate definition removed

BASE_DIR = get_base_path()
ACCOUNTS_DIR = os.path.join(BASE_DIR, 'accounts')
TEMPLATE_PROFILE_DIR = os.path.join(BASE_DIR, 'template_chrome_profile')
SESSION_PROFILES_DIR = os.path.join(BASE_DIR, 'session_profiles')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

active_drivers = {}
# Track if images have been pasted already for an account in this session
pasted_images_accounts = set()
# Global lock to serialize system hotkey usage across multiple accounts
hotkey_lock = threading.Lock()
# Global lock to serialize Google Sheets API usage across threads/accounts
GS_LOCK = threading.Lock()
TG_LOCK = threading.Lock()
running_threads = {}
_thread_ctx = threading.local()  # per-thread account context for logging
SHUTTING_DOWN = threading.Event()
TELEGRAM_ALERTS = {}  # account_id -> {message_id:int, text:str}
TELEGRAM_GLOBAL = {"message_id": None, "accounts": set(), "text": ""}
ACCOUNT_STATE = {"recovering": {}}  # recovering[account_id]=True while FB login is being recovered

def set_account_recovering(account_id: int, value: bool):
    try:
        ACCOUNT_STATE["recovering"][int(account_id)] = bool(value)
    except Exception:
        pass

def is_account_recovering(account_id: int) -> bool:
    try:
        return bool(ACCOUNT_STATE["recovering"].get(int(account_id)))
    except Exception:
        return False

# Launch manager globals: keep menu interactive while launching sequentially in background
launch_manager_thread = None
launch_queue = []
launch_queue_lock = threading.Lock()
launch_queue_event = threading.Event()

# ----------------- Navigation Helpers -----------------

def open_tool_tab(driver, url: str = "https://fewfeed.online", wait_seconds: int = 15):
    """Open the FewFeed tool page in a new tab, log in if needed, then wait for tools.

    Flow:
      1. Open https://fewfeed.online in a new tab.
      2. Call fewfeed_login() — navigates to sign-in, fills credentials, waits for
         'Use this tool' buttons (or skips if already logged in).
      3. Call handle_extension_popup() for final extension readiness check.

    Works in the background (no focus needed). Returns True when page is ready.
    """
    current_handles = driver.window_handles.copy()
    driver.execute_script(f"window.open('{url}', '_blank');")
    # Wait for the new tab / handle
    end_time = time.time() + wait_seconds
    while time.time() < end_time:
        handles = driver.window_handles
        if len(handles) > len(current_handles):
            new_handle = (set(handles) - set(current_handles)).pop()
            driver.switch_to.window(new_handle)
            try:
                # Wait for URL to contain fewfeed
                WebDriverWait(driver, 8).until(lambda d: 'fewfeed' in d.current_url.lower())
                # Wait for page to be fully loaded and interactive
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                # Always attempt FewFeed login first (navigates to sign-in page,
                # fills credentials, waits for homepage + 'Use this tool' buttons)
                try:
                    fewfeed_login(driver, step_sleep=2, timeout=40)
                except Exception as e:
                    dbg(f"[open_tool_tab] fewfeed_login error: {e}")
                # Final extension-popup / tool readiness check
                return handle_extension_popup(driver, step_sleep=2, mode='web', max_retries=3)
            except Exception:
                return False

def close_fewfeed_tabs(driver):
    """Close all tabs whose URL or title contains 'fewfeed'. Keep Facebook tab open."""
    try:
        handles = driver.window_handles[:]
        for h in handles:
            try:
                driver.switch_to.window(h)
                u = ''
                t = ''
                with contextlib.suppress(Exception):
                    u = (driver.current_url or '').lower()
                with contextlib.suppress(Exception):
                    t = (driver.title or '').lower()
                if 'fewfeed' in (u + ' ' + t):
                    driver.close()
            except Exception:
                continue
        # switch to any remaining handle
        with contextlib.suppress(Exception):
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
    except Exception:
        pass
        time.sleep(0.2)
    return False

def _launch_manager_loop():
    """Background loop that processes queued account IDs in parallel."""
    while not SHUTTING_DOWN.is_set():
        launch_queue_event.wait(timeout=0.5)
        if SHUTTING_DOWN.is_set():
            break
        # Get all queued accounts at once for parallel launch
        accounts_to_launch = []
        with launch_queue_lock:
            if launch_queue:
                accounts_to_launch = launch_queue.copy()
                launch_queue.clear()
                launch_queue_event.clear()
        
        if not accounts_to_launch:
            continue
            
        # Launch all accounts in parallel threads
        launch_threads = []
        for account_id in accounts_to_launch:
            try:
                print(f"Starting parallel launch for account: {account_id}")
                thread = threading.Thread(
                    target=_parallel_launch_worker,
                    args=(account_id,),
                    daemon=True
                )
                thread.start()
                launch_threads.append(thread)
                # Small stagger to avoid all accounts hitting Chrome at exact same moment
                time.sleep(0.5)
            except Exception as e:
                print(f"[account_{account_id}] Thread launch error: {e}")
        
        # Optional: wait for all launches to complete before processing next batch
        for thread in launch_threads:
            try:
                thread.join(timeout=30)  # Don't wait forever
            except Exception:
                pass

def _parallel_launch_worker(account_id):
    """Worker function to launch a single account in parallel."""
    try:
        launch_account(account_id, detach_after_post=True)
        with contextlib.suppress(Exception):
            log_path = os.path.join(LOGS_DIR, f"account_{account_id}.log")
            print(f"[account_{account_id}] Detailed log: {log_path}")
    except Exception as e:
        print(f"[account_{account_id}] Launch error: {e}")

def _start_launch_manager_if_needed():
    global launch_manager_thread
    if launch_manager_thread is None or not launch_manager_thread.is_alive():
        launch_manager_thread = threading.Thread(target=_launch_manager_loop, daemon=True)
        launch_manager_thread.start()

# --- Debug/log helper to avoid console spam ---
def dbg(message: str, account_id: int = None):
    try:
        if SHUTTING_DOWN.is_set():
            return
        aid = account_id if account_id is not None else getattr(_thread_ctx, 'account_id', None)
        if aid is not None:
            acc_log(aid, str(message), silent=True)
    except Exception:
        pass

# --- Logging helpers ---
def get_account_logger(account_id):
    """Return a per-account logger writing to logs/account_<id>.log."""
    try:
        acc_id = str(account_id)
    except Exception:
        acc_id = str(account_id)
    name = f"fewfeed.account.{acc_id}"
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(os.path.join(LOGS_DIR, f"account_{acc_id}.log"), encoding='utf-8')
        fmt = logging.Formatter('%(asctime)s %(message)s')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.propagate = False
    return logger

def acc_log(account_id, message: str, silent: bool = False):
    """Write a message to the per-account log; optionally echo to console when not silent."""
    try:
        if SHUTTING_DOWN.is_set():
            return
        msg = message if isinstance(message, str) else str(message)
    except Exception:
        msg = str(message)
    # Always include an account tag
    tag = f"[account_{account_id}] "
    logger = get_account_logger(account_id)
    logger.info(tag + msg)
    if not silent:
        try:
            print(tag + msg)
        except Exception:
            pass

def close_account_loggers():
    """Close file handlers for per-account loggers so Windows can delete files."""
    try:
        # Close handlers on named loggers like 'account_1', 'account_2', etc.
        for name, logger in logging.root.manager.loggerDict.items():
            try:
                if isinstance(logger, logging.Logger) and name.startswith('account_'):
                    for h in list(logger.handlers):
                        try:
                            h.flush()
                        except Exception:
                            pass
                        try:
                            h.close()
                        except Exception:
                            pass
                        try:
                            logger.removeHandler(h)
                        except Exception:
                            pass
            except Exception:
                continue
        # Also close handlers kept in a custom account_loggers dict, if present
        try:
            gl = globals()
            if 'account_loggers' in gl and isinstance(gl['account_loggers'], dict):
                for _, lg in list(gl['account_loggers'].items()):
                    try:
                        for h in list(lg.handlers):
                            with contextlib.suppress(Exception):
                                h.flush()
                            with contextlib.suppress(Exception):
                                h.close()
                            with contextlib.suppress(Exception):
                                lg.removeHandler(h)
                    except Exception:
                        pass
                gl['account_loggers'].clear()
        except Exception:
            pass
        # Also close handlers on root just in case
        root = logging.getLogger()
        for h in list(root.handlers):
            with contextlib.suppress(Exception):
                h.flush()
            with contextlib.suppress(Exception):
                h.close()
            with contextlib.suppress(Exception):
                root.removeHandler(h)
    except Exception:
        pass

# --- Chrome Profile Management ---
def compact_template_profile(dry_run=False):
    """Reduce the size of template_chrome_profile/ by removing non-essential data.
    Preserves:
      - Default/Preferences (and Secure Preferences if present)
      - Default/Extensions/* (all extensions) and Default/Local Extension Settings/*
    Removes typical heavy folders: Cache, Code Cache, GPUCache, Service Worker, Storage,
    Media Cache, Safe Browsing, Session Storage, IndexedDB, Shared Proto DB, etc.
    Also deletes large files (>5MB) except inside preserved directories.
    """
    base = os.path.join(BASE_DIR, 'template_chrome_profile')
    default_dir = os.path.join(base, 'Default')
    if not os.path.isdir(default_dir):
        print("template_chrome_profile/Default not found; nothing to compact.")
        return

    preserve_dirs = {
        os.path.join(default_dir, 'Extensions'),
        os.path.join(default_dir, 'Local Extension Settings'),
    }
    preserve_files = {
        os.path.join(default_dir, 'Preferences'),
        os.path.join(default_dir, 'Secure Preferences'),
    }
    # Directory names that can be safely removed if found under Default/
    removable_dir_names = {
        'Cache','Code Cache','GPUCache','Service Worker','Storage','Media Cache',
        'Session Storage','IndexedDB','Shared Proto DB','Safe Browsing','WebStorage',
        'DawnCache','Download Service','OptimizationGuide','Reporting and NEL',
        'ShaderCache','GrShaderCache','Network Action Predictor','TransportSecurity',
        'Top Sites','Visited Links','Shortcuts','Feature Engagement Tracker','Sessions'
    }
    # File globs to remove under Default/ (case-insensitive)
    removable_file_globs = [
        'History*','Top Sites*','Favicons*','Visited Links*','Shortcuts','*.log','*.ldb','*.sqlite*','*.tmp'
    ]

    removed_items = []
    skipped_items = []

    def _is_under_preserve(path):
        p = os.path.normpath(path)
        if any(os.path.normpath(p).startswith(os.path.normpath(d)+os.sep) for d in preserve_dirs):
            return True
        if p in preserve_files:
            return True
        return False

    # 1) Remove known heavy directories directly under Default/
    try:
        for name in os.listdir(default_dir):
            full = os.path.join(default_dir, name)
            if os.path.isdir(full) and name in removable_dir_names and not _is_under_preserve(full):
                if dry_run:
                    skipped_items.append(f"DRYRUN: remove dir {full}")
                else:
                    shutil.rmtree(full, ignore_errors=True)
                    removed_items.append(full)
    except Exception:
        pass

    # 2) Remove large files and matching globs inside Default/ excluding preserved dirs
    try:
        for root, dirs, files in os.walk(default_dir):
            # Skip preserved subtrees
            if _is_under_preserve(root):
                continue
            for fname in files:
                fpath = os.path.join(root, fname)
                if _is_under_preserve(fpath):
                    continue
                # Delete by glob
                low = fname.lower()
                match = False
                for pat in removable_file_globs:
                    # very small globbing: support '*' and suffix/prefix checks
                    if pat.startswith('*') and pat.endswith('*'):
                        if pat.strip('*').lower() in low:
                            match = True
                            break
                    elif pat.startswith('*'):
                        if low.endswith(pat.strip('*').lower()):
                            match = True
                            break
                    elif pat.endswith('*'):
                        if low.startswith(pat[:-1].lower()):
                            match = True
                            break
                    else:
                        if low == pat.lower():
                            match = True
                            break
                if match:
                    try:
                        if dry_run:
                            skipped_items.append(f"DRYRUN: remove file {fpath}")
                        else:
                            os.remove(fpath)
                            removed_items.append(fpath)
                        continue
                    except Exception:
                        pass
                # Delete large files (>5MB) outside preserved dirs
                try:
                    if os.path.getsize(fpath) > 5 * 1024 * 1024:
                        if dry_run:
                            skipped_items.append(f"DRYRUN: remove large file {fpath}")
                        else:
                            os.remove(fpath)
                            removed_items.append(fpath)
                except Exception:
                    pass
    except Exception:
        pass

    print(f"Template compaction complete. Removed {len(removed_items)} items.")

def menu_compact_template():
    try:
        ans = input("\nThis will shrink template_chrome_profile by removing caches and non-essential data. Continue? (y/N): ").strip().lower()
        if ans != 'y':
            print("Skipped.")
            return
        compact_template_profile(dry_run=False)
    except Exception as e:
        print(f"Compaction error: {e}")

# --- Google Sheets helpers ---
def _gs_enabled():
    gs = config.get('google_sheets', {}) or {}
    return bool(gs.get('enabled') and gs.get('spreadsheet_id') and gs.get('service_account_json')) and gspread is not None and Credentials is not None

_gs_cache = {"client": None, "sheet": None, "ws_name": None}

# --- UI helpers ---
def enable_developer_mode_via_selenium(driver):
    """Enable Chrome Extensions Developer Mode by navigating to chrome://extensions
    and clicking the toggle via Selenium. Called right after the browser launches.
    Safe to call even if already enabled — checks state first.

    Shadow DOM path (confirmed via DevTools):
      extensions-manager > #shadow-root
        > extensions-toolbar > #shadow-root
          > div.more-actions
            > cr-toggle#devMode > #shadow-root
                > span#bar   <-- actual click target
    """
    original_handle = None
    ext_handle = None
    try:
        original_handles = driver.window_handles[:]
        original_handle = driver.current_window_handle

        # Open chrome://extensions in a new tab
        driver.execute_script("window.open('chrome://extensions/', '_blank');")
        time.sleep(1.5)

        new_handles = [h for h in driver.window_handles if h not in original_handles]
        if not new_handles:
            return False
        ext_handle = new_handles[0]
        driver.switch_to.window(ext_handle)
        time.sleep(1.5)

        # Check current state and click #bar inside cr-toggle#devMode's shadow root
        result = driver.execute_script("""
            try {
                var mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return 'no-mgr';

                var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
                if (!toolbar || !toolbar.shadowRoot) return 'no-toolbar';

                var toggle = toolbar.shadowRoot.querySelector('cr-toggle#devMode');
                if (!toggle) return 'no-toggle';

                // Read aria-pressed which is more reliable than .checked
                var pressed = toggle.getAttribute('aria-pressed');
                if (pressed === 'true') return 'already-enabled';

                // Click the #bar span inside cr-toggle's own shadow root
                // (this is the actual interactive element Chrome responds to)
                var bar = toggle.shadowRoot ? toggle.shadowRoot.querySelector('#bar') : null;
                if (bar) {
                    bar.click();
                    return 'clicked-bar';
                }
                // Fallback: dispatch a real mouse click event on the toggle itself
                toggle.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return 'clicked-toggle-event';
            } catch(e) {
                return 'error:' + e;
            }
        """)

        time.sleep(0.8)

        # Close the extensions tab and switch back
        driver.close()
        driver.switch_to.window(original_handle)
        return True

    except Exception:
        try:
            if ext_handle and ext_handle in driver.window_handles:
                driver.close()
        except Exception:
            pass
        try:
            if original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass
        return False

def ensure_extension_loaded_and_pinned(driver, ext_folder: str = ""):
    """Ensure the FewFeed extension (fewfeedv2) is installed via "Load unpacked"
    and pinned to the toolbar.

    Flow:
      1. Open chrome://extensions in a new tab.
      2. If FewFeed is already installed, skip to pinning.
      3. Otherwise, click the "Load unpacked" button, type the folder path
         into the native folder-picker dialog using pyautogui, and press Enter.
      4. Pin the extension to the toolbar.
      5. Close the extensions tab and return to the original window.
    """
    if not ext_folder or not os.path.isdir(ext_folder):
        ext_folder = os.path.join(BASE_DIR, 'fewfeedv2')
        ext_folder = os.path.abspath(ext_folder)

    if not os.path.isdir(ext_folder):
        return False

    original_handle = None
    ext_handle = None

    def _cleanup():
        try:
            if ext_handle and ext_handle in driver.window_handles:
                driver.switch_to.window(ext_handle)
                driver.close()
        except Exception:
            pass
        try:
            if original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass

    try:
        original_handles = driver.window_handles[:]
        original_handle = driver.current_window_handle

        # --- Open chrome://extensions in a new tab ---
        driver.execute_script("window.open('chrome://extensions/', '_blank');")
        time.sleep(1.5)
        new_handles = [h for h in driver.window_handles if h not in original_handles]
        if not new_handles:
            return False
        ext_handle = new_handles[0]
        driver.switch_to.window(ext_handle)
        time.sleep(1.5)

        # --- Check if FewFeed is already installed ---
        found = driver.execute_script("""
            try {
                var mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return 0;
                var itemList = mgr.shadowRoot.querySelector('extensions-item-list');
                if (!itemList || !itemList.shadowRoot) return 0;
                var items = itemList.shadowRoot.querySelectorAll('extensions-item');
                for (var i = 0; i < items.length; i++) {
                    var sr = items[i].shadowRoot;
                    if (!sr) continue;
                    var nameEl = sr.querySelector('#name');
                    if (nameEl && nameEl.textContent.toLowerCase().indexOf('fewfeed') !== -1) return 1;
                }
                return 0;
            } catch(e) { return -1; }
        """)

        if found != 1:
            # --- Not installed — click "Load unpacked" and use native folder picker ---
            print("[ext] Extension not found — clicking Load unpacked...")
            clicked = driver.execute_script("""
                try {
                    function findLoadBtn(root, depth) {
                        if (depth > 6) return null;
                        var children = root.children || [];
                        for (var i = 0; i < children.length; i++) {
                            var el = children[i];
                            var txt = (el.textContent || '').trim();
                            var label = (el.getAttribute('aria-label') || '').trim();
                            if (txt === 'Load unpacked' || label === 'Load unpacked') return el;
                            if (el.shadowRoot) {
                                var res = findLoadBtn(el.shadowRoot, depth + 1);
                                if (res) return res;
                            }
                        }
                        return null;
                    }
                    var mgr = document.querySelector('extensions-manager');
                    if (!mgr || !mgr.shadowRoot) return 'no-mgr';
                    var btn = findLoadBtn(mgr.shadowRoot, 0);
                    if (!btn) {
                        var toolbar = mgr.shadowRoot.querySelector('extensions-toolbar');
                        if (toolbar && toolbar.shadowRoot) btn = findLoadBtn(toolbar.shadowRoot, 0);
                    }
                    if (btn) { btn.click(); return 'clicked'; }
                    return 'not-found';
                } catch(e) { return 'error:' + e; }
            """)

            if 'clicked' in str(clicked):
                # The native folder picker should now be open.
                # Wait for the dialog to appear, type the path, and press Enter.
                time.sleep(1.5)
                pyautogui.typewrite(ext_folder, interval=0.03)
                pyautogui.press('enter')
                time.sleep(3)
            else:
                print(f"[ext] Could not find 'Load unpacked' button: {clicked}")
                _cleanup()
                return False

        # --- Enable if disabled ---
        driver.execute_script("""
            try {
                var mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return;
                var itemList = mgr.shadowRoot.querySelector('extensions-item-list');
                if (!itemList || !itemList.shadowRoot) return;
                var items = itemList.shadowRoot.querySelectorAll('extensions-item');
                for (var i = 0; i < items.length; i++) {
                    var sr = items[i].shadowRoot;
                    if (!sr) continue;
                    var nameEl = sr.querySelector('#name');
                    if (!nameEl || nameEl.textContent.toLowerCase().indexOf('fewfeed') === -1) continue;
                    var enableToggle = sr.querySelector('#enableToggle');
                    if (enableToggle && enableToggle.getAttribute('aria-pressed') === 'false') {
                        var bar = enableToggle.shadowRoot ? enableToggle.shadowRoot.querySelector('#bar') : null;
                        if (bar) bar.click(); else enableToggle.click();
                    }
                    break;
                }
            } catch(e) {}
        """)
        time.sleep(0.5)

        # --- Pin via the pin button inside extensions-item shadow DOM ---
        driver.execute_script("""
            try {
                var mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return;
                var itemList = mgr.shadowRoot.querySelector('extensions-item-list');
                if (!itemList || !itemList.shadowRoot) return;
                var items = itemList.shadowRoot.querySelectorAll('extensions-item');
                for (var i = 0; i < items.length; i++) {
                    var sr = items[i].shadowRoot;
                    if (!sr) continue;
                    var nameEl = sr.querySelector('#name');
                    if (!nameEl || nameEl.textContent.toLowerCase().indexOf('fewfeed') === -1) continue;
                    var pinBtn = sr.querySelector('#pin-button');
                    if (!pinBtn) {
                        var btns = sr.querySelectorAll('cr-icon-button, button');
                        for (var j = 0; j < btns.length; j++) {
                            var lbl = (btns[j].getAttribute('aria-label') || '').toLowerCase();
                            if (lbl.indexOf('pin') !== -1) { pinBtn = btns[j]; break; }
                        }
                    }
                    if (pinBtn) {
                        var lbl = (pinBtn.getAttribute('aria-label') || '').toLowerCase();
                        var pressed = pinBtn.getAttribute('aria-pressed');
                        if (pressed !== 'true' && lbl.indexOf('unpin') === -1) {
                            pinBtn.click();
                        }
                    }
                    break;
                }
            } catch(e) {}
        """)
        time.sleep(0.5)

        _cleanup()
        return True

    except Exception:
        _cleanup()
        return False

def enable_developer_mode_in_profile(profile_dir):
    """Enable Chrome developer mode by modifying the Preferences file directly.
    This must be called BEFORE launching Chrome with this profile.
    """
    try:
        # Developer mode is stored in Default/Preferences or Preferences
        pref_paths = [
            os.path.join(profile_dir, 'Default', 'Preferences'),
            os.path.join(profile_dir, 'Preferences'),
        ]

        for pref_path in pref_paths:
            if os.path.exists(pref_path):
                try:
                    with open(pref_path, 'r', encoding='utf-8') as f:
                        prefs = json.load(f)
                except Exception:
                    prefs = {}

                # Enable developer mode in preferences
                if 'extensions' not in prefs:
                    prefs['extensions'] = {}
                if 'ui' not in prefs['extensions']:
                    prefs['extensions']['ui'] = {}

                prefs['extensions']['ui']['developer_mode'] = True

                # Write back
                with open(pref_path, 'w', encoding='utf-8') as f:
                    json.dump(prefs, f, separators=(',', ':'))

                print(f"Developer mode enabled in profile preferences.")
                return True

        # If no existing preferences, create one in Default
        default_dir = os.path.join(profile_dir, 'Default')
        os.makedirs(default_dir, exist_ok=True)
        pref_path = os.path.join(default_dir, 'Preferences')

        prefs = {
            'extensions': {
                'ui': {
                    'developer_mode': True
                }
            }
        }

        with open(pref_path, 'w', encoding='utf-8') as f:
            json.dump(prefs, f, separators=(',', ':'))

        print(f"Developer mode enabled in new profile preferences.")
        return True

    except Exception as e:
        print(f"Note: Could not enable developer mode in profile: {e}")
        return False

def _omnibox_navigate(url_text: str, settle=1.0):
    """Navigate by pasting into Omnibox to avoid wrong slashes (chrome:///)."""
    try:
        set_clipboard_text(url_text)
        pyautogui.hotkey('ctrl', 'l'); time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'v'); time.sleep(0.1)
        pyautogui.press('enter')
        time.sleep(settle)
    except Exception:
        # fallback: typewrite
        try:
            pyautogui.hotkey('ctrl', 'l'); time.sleep(0.2)
            pyautogui.typewrite(url_text, interval=0.01); pyautogui.press('enter')
            time.sleep(settle)
        except Exception:
            pass

def _iter_profile_preferences(user_data_dir: str):
    """Yield existing Preferences file paths for likely profiles under user_data_dir."""
    candidates = [
        os.path.join(user_data_dir, 'Default', 'Preferences'),
        os.path.join(user_data_dir, 'Default', 'Secure Preferences'),
    ]
    try:
        for name in os.listdir(user_data_dir):
            if name.startswith('Profile '):
                p1 = os.path.join(user_data_dir, name, 'Preferences')
                p2 = os.path.join(user_data_dir, name, 'Secure Preferences')
                candidates.extend([p1, p2])
    except Exception:
        pass
    for pref in candidates:
        if os.path.exists(pref):
            yield pref

def _find_extension_id_in_prefs(user_data_dir: str, prefer_path: str = "", name_hint: str = "fewfeed"):
    """Return extension id if an enabled extension matching path or name hint is found."""
    prefer_path_norm = os.path.normpath(prefer_path).lower() if prefer_path else ""
    for pref_path in _iter_profile_preferences(user_data_dir):
        try:
            with open(pref_path, 'r', encoding='utf-8', errors='ignore') as f:
                prefs = json.load(f)
            settings = (((prefs or {}).get('extensions') or {}).get('settings')) or {}
            for ext_id, info in settings.items():
                try:
                    if info.get('state', 0) != 1:
                        continue
                    # match by path first if provided
                    pth = os.path.normpath(info.get('path', '')).lower()
                    if prefer_path_norm and pth == prefer_path_norm:
                        return ext_id
                    # else try by manifest name hint
                    man = info.get('manifest', {}) if isinstance(info.get('manifest', {}), dict) else {}
                    nm = str(man.get('name', '')).lower()
                    if name_hint and name_hint.lower() in nm:
                        return ext_id
                except Exception:
                    continue
        except Exception:
            continue
    return ""

def wait_for_extension_install(user_data_dir: str, ext_folder: str, timeout_sec: int = 300):
    """Poll Preferences until target extension appears enabled. Returns ext_id or ''."""
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        ext_id = _find_extension_id_in_prefs(user_data_dir, prefer_path=ext_folder, name_hint='fewfeed')
        if ext_id:
            return ext_id
        # Also check Extensions directory for any installed IDs (unpacked may not appear here, but try)
        try:
            ext_dir = os.path.join(user_data_dir, 'Default', 'Extensions')
            if os.path.isdir(ext_dir):
                for d in os.listdir(ext_dir):
                    if len(d) >= 20:  # extension id-like
                        return d
        except Exception:
            pass
        time.sleep(1.0)
    return ""

def gs_get_worksheet():
    """Return a gspread worksheet for posted groups (column-per-account)."""
    if not _gs_enabled():
        return None
    gs_cfg = config.get('google_sheets', {})
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        with GS_LOCK:
            if not _gs_cache["client"]:
                creds = Credentials.from_service_account_file(os.path.join(get_base_path(), gs_cfg['service_account_json']), scopes=scopes)
                _gs_cache["client"] = gspread.authorize(creds)
            if not _gs_cache["sheet"]:
                _gs_cache["sheet"] = _gs_cache["client"].open_by_key(gs_cfg['spreadsheet_id'])
            ws_name = gs_cfg.get('worksheet_name', 'posted_groups')
            if _gs_cache.get("ws_name") != ws_name:
                try:
                    ws = _gs_cache["sheet"].worksheet(ws_name)
                except gspread.WorksheetNotFound:
                    ws = _gs_cache["sheet"].add_worksheet(title=ws_name, rows=20000, cols=100)
                _gs_cache["ws_name"] = ws_name
                _gs_cache["ws"] = ws
            return _gs_cache.get("ws")
    except Exception as e:
        print(f"[GS] Error initializing Google Sheets: {e}")
        return None

def _ensure_account_columns(ws, account_id: int):
    """Ensure the worksheet has Account N (Group Name) and Posts N (Post URL) columns for the account."""
    try:
        # Each account needs 2 columns: Group Name (Account N) and Post URL (Posts N)
        group_id_col = (account_id - 1) * 2 + 1
        post_url_col = (account_id - 1) * 2 + 2
        
        # Expand columns if needed
        if ws.col_count < post_url_col:
            ws.add_cols(post_url_col - ws.col_count)
        
        # Ensure headers on row 1
        group_id_header = ws.cell(1, group_id_col).value
        post_url_header = ws.cell(1, post_url_col).value
        
        if not group_id_header or group_id_header.strip() == "":
            ws.update_cell(1, group_id_col, f"Account {account_id}")
        if not post_url_header or post_url_header.strip() == "":
            ws.update_cell(1, post_url_col, f"Posts {account_id}")
    except Exception as e:
        print(f"[GS] ensure columns error: {e}")

def _ensure_account_column(ws, account_id: int):
    """Legacy function - redirect to new columns function."""
    _ensure_account_columns(ws, account_id)

def gs_fetch_posted_groups(account_id):
    """Return a set of group Names from the account's data (Account N column), exact text."""
    ws = gs_get_worksheet()
    if not ws:
        return set()
    # Retry a few times in case of transient API/network errors
    for attempt in range(3):
        try:
            acc_id = int(account_id)
            with GS_LOCK:
                # Ensure we have enough columns for Group Name (Account N) and Post URL (Posts N)
                _ensure_account_columns(ws, acc_id)
                # Get Account N (group name) column (odd columns: 1, 3, 5, etc.)
                name_col = (acc_id - 1) * 2 + 1
                col_vals = ws.col_values(name_col)
            # Skip header row (row 1)
            group_names = [v for v in col_vals[1:] if v and str(v).strip()]
            return set(group_names)
        except Exception as e:
            if attempt == 2:
                print(f"[GS] fetch error: {e}")
                return set()
            time.sleep(0.6 * (attempt + 1))

def gs_append_post_success(account_id, group_data):
    """Append group data (ID and URL pairs) to the account's columns.
    group_data should be a list of tuples: [(group_id, post_url), ...]
    """
    ws = gs_get_worksheet()
    if not ws:
        return False
    # Retry a few times to handle concurrent updates or quota hiccups
    for attempt in range(3):
        try:
            acc_id = int(account_id)
            if not group_data:
                return True
            with GS_LOCK:
                _ensure_account_columns(ws, acc_id)
                
                # Get column numbers for this account
                group_id_col = (acc_id - 1) * 2 + 1
                post_url_col = (acc_id - 1) * 2 + 2
                
                # Find next empty row and avoid duplicates
                group_id_vals = ws.col_values(group_id_col)
                next_row = max(2, len(group_id_vals) + 1)
                existing_group_ids = set(v for v in group_id_vals[1:] if v)
                
                # Filter out duplicates
                filtered = [(gid, url) for gid, url in group_data if gid and gid not in existing_group_ids]
                if not filtered:
                    return True
                
                # Prepare data for batch update
                def _col_letter(n):
                    s = ""
                    while n:
                        n, rem = divmod(n - 1, 26)
                        s = chr(65 + rem) + s
                    return s
                
                group_id_col_letter = _col_letter(group_id_col)
                post_url_col_letter = _col_letter(post_url_col)
                
                # Update both columns simultaneously
                start = next_row
                end = next_row + len(filtered) - 1
                
                # Ensure the sheet has enough rows (avoid limits)
                try:
                    if hasattr(ws, 'row_count') and end > ws.row_count:
                        ws.add_rows(end - ws.row_count)
                except Exception:
                    pass
                
                # Group ID column update
                group_id_values = [[gid] for gid, url in filtered]
                group_id_range = f"{group_id_col_letter}{start}:{group_id_col_letter}{end}"
                ws.update(values=group_id_values, range_name=group_id_range, value_input_option="RAW")
                
                # Post URL column update
                post_url_values = [[url] for gid, url in filtered]
                post_url_range = f"{post_url_col_letter}{start}:{post_url_col_letter}{end}"
                ws.update(values=post_url_values, range_name=post_url_range, value_input_option="RAW")
                
            return True
        except Exception as e:
            if attempt == 2:
                print(f"[GS] append error: {e}")
                return False
            time.sleep(0.8 * (attempt + 1))

# --- Cookie Sheet + Telegram helpers ---
def _tg_cfg():
    try:
        tg = (config.get('telegram') or {})
        if tg.get('enabled') and tg.get('bot_token') and tg.get('chat_id'):
            return tg
    except Exception:
        pass
    return None

def telegram_send_or_update(account_id: int, text: str):
    cfg = _tg_cfg()
    if not cfg:
        return
    try:
        with TG_LOCK:
            prev = TELEGRAM_ALERTS.get(account_id)
            if prev and prev.get('message_id'):
                # edit existing only (never send new duplicates)
                if text != prev.get('text'):
                    url = f"https://api.telegram.org/bot{cfg['bot_token']}/editMessageText"
                    requests.post(url, data={
                        'chat_id': cfg['chat_id'],
                        'message_id': prev['message_id'],
                        'text': text
                    }, timeout=10)
                    prev['text'] = text
            else:
                url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
                r = requests.post(url, data={'chat_id': cfg['chat_id'], 'text': text}, timeout=10)
                try:
                    mid = (r.json() or {}).get('result', {}).get('message_id')
                except Exception:
                    mid = None
                TELEGRAM_ALERTS[account_id] = {'message_id': mid, 'text': text}
    except Exception:
        pass

def telegram_delete(account_id: int):
    cfg = _tg_cfg()
    if not cfg:
        return
    try:
        with TG_LOCK:
            prev = TELEGRAM_ALERTS.get(account_id)
            if prev and prev.get('message_id'):
                url = f"https://api.telegram.org/bot{cfg['bot_token']}/deleteMessage"
                requests.post(url, data={'chat_id': cfg['chat_id'], 'message_id': prev['message_id']}, timeout=10)
            TELEGRAM_ALERTS.pop(account_id, None)
    except Exception:
        pass

def fetch_cookie_map_from_csv():
    """Download the CSV from config['cookies_sheet_csv_url'] and return {account_file: cookies_list}."""
    url = str(config.get('cookies_sheet_csv_url') or '').strip()
    if not url:
        return {}
    try:
        # cache buster to avoid stale content
        sep = '&' if '?' in url else '?'
        bust_url = f"{url}{sep}_ts={int(time.time())}"
        resp = requests.get(bust_url, timeout=20, headers={"Cache-Control": "no-cache"})
        resp.raise_for_status()
        data = resp.content.decode('utf-8', errors='ignore')
        try:
            acc_log(getattr(_thread_ctx, 'account_id', 'sheet'), f"[Sheet] Downloaded {len(data)} bytes", silent=True)
        except Exception:
            pass
        # Normalize headers to lowercase/stripped (handle BOM)
        sio = io.StringIO(data)
        raw_reader = csv.reader(sio)
        try:
            headers = next(raw_reader)
        except StopIteration:
            headers = []
        def _norm_key(h):
            h = h.encode('utf-8').decode('utf-8') if isinstance(h, str) else str(h)
            h = h.strip().lower().lstrip('\ufeff')
            # remove spaces and punctuation to be robust (e.g., "cookies json")
            import re as _re
            return _re.sub(r"[^a-z0-9_]+", "_", h)
        norm_headers = [_norm_key(h) for h in headers]
        rows = list(raw_reader)
        result = {}
        for r in rows:
            try:
                row = {norm_headers[i]: (r[i] if i < len(r) else '') for i in range(len(norm_headers))}
                name = (row.get('account_file') or row.get('account') or row.get('file') or '').strip()
                cj = (row.get('cookies_json') or row.get('cookies') or row.get('cookie_json') or row.get('cookies') or '').strip()
                if not name or not cj:
                    continue
                # Normalize JSON text (sheet may contain stray spaces/newlines)
                txt = cj.strip()
                if txt and txt[0] == "'":
                    txt = txt.replace("'", '"')
                if txt.startswith('"[') and txt.endswith(']"'):
                    with contextlib.suppress(Exception):
                        txt = txt.strip('"')
                        txt = txt.replace('\\"', '"')
                def _try_parse_cookie_text(s: str):
                    # primary parse
                    try:
                        return json.loads(s)
                    except Exception:
                        pass
                    # fallback 1: if content starts with '{' assume it's a list of objects separated by commas, wrap with []
                    try:
                        s2 = s.strip()
                        if s2.startswith('{') and s2.endswith('}'):  # likely multiple objects separated by commas/newlines
                            s2 = '[' + s2 + ']'
                        # remove trailing comma before closing bracket if present
                        s2 = s2.replace(',\n]', ']').replace(',\r\n]', ']').replace(', ]', ']')
                        return json.loads(s2)
                    except Exception:
                        pass
                    # fallback 2: try to collect lines into JSON array
                    try:
                        lines = [ln for ln in s.splitlines() if ln.strip()]
                        block = '\n'.join(lines)
                        if not block.startswith('['):
                            block = '[' + block
                        if not block.endswith(']'):
                            block = block + ']'
                        block = block.replace(',\n]', ']')
                        return json.loads(block)
                    except Exception:
                        return None

                cookies = _try_parse_cookie_text(txt)
                if isinstance(cookies, list):
                    result[name.strip()] = cookies
            except Exception:
                continue
        try:
            acc_log(getattr(_thread_ctx, 'account_id', 'sheet'), f"[Sheet] Parsed accounts: {list(result.keys())[:5]}{'...' if len(result)>5 else ''}", silent=True)
        except Exception:
            pass
        return result
    except Exception:
        return {}



def refresh_cookies_from_sheet(account_id: int):
    """Try to fetch cookies for this account id from the CSV and write to accounts/<id>_cookies.json. Returns list or []."""
    mapping = fetch_cookie_map_from_csv()
    try:
        acc_log(account_id, f"[Sheet] Keys available: {list(mapping.keys())[:5]}{'...' if len(mapping)>5 else ''}", silent=True)
    except Exception:
        pass
    filename = f"{account_id}_cookies.json"
    cookies = mapping.get(filename) or mapping.get(filename.strip()) or mapping.get(filename.strip().lstrip('\ufeff'))
    if not cookies:
        try:
            acc_log(account_id, f"[Sheet] No cookies found for {filename}", silent=True)
            with open('cookies_dump.json', 'w') as f:
                json.dump(mapping, f)
        except Exception:
            pass
        return []
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        with open(os.path.join(ACCOUNTS_DIR, filename), 'w', encoding='utf-8') as f:
            json.dump(cookies, f)
        try:
            acc_log(account_id, f"[Sheet] Saved {len(cookies)} cookies to {filename}", silent=True)
        except Exception:
            pass
    except Exception:
        pass
    return cookies

def _normalize_fb_cookie(c):
    c = dict(c)
    # Remove non CDP fields
    for k in ["hostOnly", "session", "storeId"]:
        c.pop(k, None)
    # Ensure domain and path
    dom = str(c.get('domain') or 'facebook.com')
    if not dom.startswith('.') and 'facebook.com' in dom:
        dom = '.' + dom.split(':')[0]
    if 'facebook.com' not in dom:
        dom = '.facebook.com'
    c['domain'] = dom
    c['path'] = c.get('path') or '/'
    # Ensure secure flag for FB cookies
    c['secure'] = bool(c.get('secure', True))
    # Map samesite values
    ss = (c.get('sameSite') or c.get('SameSite') or '').capitalize()
    if ss in ('Lax', 'None', 'Strict'):
        c['sameSite'] = ss
    else:
        c.pop('sameSite', None)
    # Numeric expires required
    if 'expirationDate' in c and 'expires' not in c:
        with contextlib.suppress(Exception):
            c['expires'] = float(c['expirationDate'])
        c.pop('expirationDate', None)
    # Remove value None cookies
    if c.get('value') in (None, ''):
        c['value'] = ''
    return c

def inject_cookies(driver, cookies):
    if not cookies:
        return False
    try:
        norm = [_normalize_fb_cookie(c) for c in cookies if isinstance(c, dict)]
        # Clear old cookies first to prevent conflicts
        driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
        driver.execute_cdp_cmd('Network.enable', {})
        # Inject in chunks to avoid size limits
        CHUNK = 100
        for i in range(0, len(norm), CHUNK):
            driver.execute_cdp_cmd('Network.setCookies', {'cookies': norm[i:i+CHUNK]})
        driver.execute_cdp_cmd('Network.disable', {})
        return True
    except Exception as e:
        try:
            acc_log(getattr(_thread_ctx, 'account_id', '?'), f"Cookie injection error: {e}", silent=True)
        except Exception:
            pass
        return False

def is_fb_logged_in(driver, intrusive: bool = False) -> bool:
    """Check Facebook login status.
    - Non-intrusive (default): do NOT navigate; rely on c_user cookie and current URL not being /login.
    - Intrusive: navigate to Settings and ensure it isn't /login.
    """
    try:
        # Check cookies via CDP first
        try:
            all_cookies = driver.execute_cdp_cmd('Network.getAllCookies', {}).get('cookies', [])
        except Exception:
            all_cookies = []
        has_c_user = any(c.get('name') == 'c_user' and 'facebook.com' in (c.get('domain') or '') for c in all_cookies)
        try:
            cur = (driver.current_url or '').lower()
        except Exception:
            cur = ''
        if not intrusive:
            # If current tab is on FB login, clearly logged out
            if 'facebook.com/login' in cur:
                return False
            # Otherwise consider logged in if c_user is present
            return has_c_user
        # Intrusive path: navigate to Settings to validate session explicitly
        if 'facebook.com/settings' not in cur:
            driver.get('https://www.facebook.com/settings')
        WebDriverWait(driver, 7).until(lambda d: 'facebook.com' in d.current_url.lower())
        url = driver.current_url.lower()
        if '/login' in url:
            return False
        return has_c_user
    except Exception:
        return False

def start_fb_monitor(driver, account_id: int):
    """Background monitor: if logged out, fetch cookies from sheet and retry until restored, then delete Telegram alert and resume automation."""
    def _loop():
        backoff = 5
        max_backoff = 300
        while not SHUTTING_DOWN.is_set():
            time.sleep(10)
            try:
                # Non-intrusive check while tool might be active
                if is_fb_logged_in(driver, intrusive=False):
                    acc_log(account_id, "[Monitor] Session OK.", silent=True)
                    tg_alert_remove(account_id)
                    backoff = 5
                    continue
                # Logged out detected -> stop tool tabs and focus on Facebook recovery
                tg_alert_add(account_id)
                set_account_recovering(account_id, True)
                with contextlib.suppress(Exception):
                    close_fewfeed_tabs(driver)
                # Try refresh from sheet and inject
                cookies = refresh_cookies_from_sheet(account_id)
                if cookies and inject_cookies(driver, cookies):
                    acc_log(account_id, "[Monitor] Injected refreshed cookies; opening Settings...", silent=True)
                    driver.get('https://www.facebook.com/settings')
                    try:
                        WebDriverWait(driver, 10).until(lambda d: 'facebook.com/settings' in d.current_url.lower() and '/login' not in d.current_url.lower())
                        # success -> clear alert and resume automation if needed
                        tg_alert_remove(account_id)
                        set_account_recovering(account_id, False)
                        try:
                            # ensure FewFeed tab is open before resuming
                            if not open_tool_tab(driver):
                                time.sleep(2)
                                open_tool_tab(driver)
                            automate_fewfeed(driver, account_id, assume_page_loaded=True, silent=True, detach_after_post=True)
                        except Exception:
                            pass
                        backoff = 5
                        continue
                    except Exception:
                        pass
                # If not successful, increase backoff and retry
                time.sleep(backoff)
                backoff = min(max_backoff, int(backoff * 1.7))
            except Exception:
                time.sleep(10)
                continue
    threading.Thread(target=_loop, daemon=True).start()

def ff_uncheck_excluded_groups(driver, exclude_group_ids, step_sleep=None):
    """Search and uncheck each group by ID/name in exclude_group_ids using the RIGHT list's search box.
    Targets only the right panel (checkbox list) and avoids clicking outside.
    exclude_group_ids can be group IDs or group names.
    """
    if not exclude_group_ids:
        return
    # Use customizable speed
    if step_sleep is None:
        step_sleep = config.get('group_exclusion_speed', 0.2)
    try:
        # 1) Locate the RIGHT-SIDE container (has many checkboxes and is scrollable)
        try:
            containers = driver.find_elements(
                By.XPATH,
                "//div[(contains(@class,'overflow-y') or contains(@class,'overflow-auto') or contains(@class,'divide-y')) and .//input[@type='checkbox']]"
            )
        except Exception:
            containers = []
        if not containers:
            # Fallback: find the Name search input globally, then climb to a container that has checkboxes
            search_probe = None
            for xp in [
                "//input[@type='search']",
                "//input[contains(@placeholder,'Name') or contains(@placeholder,'name')]",
                "//input[@type='text' and (contains(@placeholder,'Name') or contains(@class,'rounded'))]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    if el.is_displayed():
                        search_probe = el
                        break
                except Exception:
                    continue
            if search_probe:
                try:
                    right_panel = search_probe.find_element(By.XPATH, "./ancestor::div[.//input[@type='checkbox']][1]")
                    containers = [right_panel]
                except Exception:
                    containers = []
        if not containers:
            dbg("[FF] exclude: no right container found")
            return
        # choose container with most checkboxes
        best = None
        best_count = -1
        for c in containers:
            try:
                cnt = len(c.find_elements(By.XPATH, ".//input[@type='checkbox']"))
            except Exception:
                cnt = 0
            if cnt > best_count:
                best = c; best_count = cnt
        right_panel = best

        # 2) Find a search input inside that right panel (prioritize the Name box)
        search_box = None
        for xp in [
            ".//input[@type='search']",
            ".//input[contains(@placeholder,'Name') or contains(@placeholder,'name')]",
            ".//input[@type='text']",
        ]:
            try:
                el = right_panel.find_element(By.XPATH, xp)
                # guard: avoid hidden/zero-size inputs
                if el.is_displayed() and el.size.get('width', 0) > 20:
                    search_box = el
                    break
            except Exception:
                continue
        if not search_box:
            # last resort: global search input near the panel
            try:
                search_box = driver.find_element(By.XPATH, "//input[@type='search' or @type='text']")
            except Exception:
                search_box = None
        if not search_box:
            dbg("[FF] exclude: search box not found in right panel")
            return

        # 3) Work each exclusion
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_box)
        except Exception:
            pass
        try:
            ActionChains(driver).move_to_element(search_box).pause(0.05).perform()
        except Exception:
            pass
        time.sleep(0.2)

        def _norm(txt: str):
            return re.sub(r"\s+", " ", (txt or '').strip()).lower()
        def _norm_loose(txt: str):
            # remove punctuation/emojis; keep letters, digits and spaces
            t = _norm(txt)
            t = re.sub(r"[^a-z0-9 ]+", " ", t)
            return re.sub(r"\s+", " ", t).strip()

        # iterate strictly in given order
        seq = list(exclude_group_ids)
        total = len(seq)
        for idx_name, group_identifier in enumerate(seq, start=1):
            q = (group_identifier or '').strip()
            if not q:
                continue
            dbg(f"[FF] exclude: {idx_name}/{total} searching '{q}' (group ID/name)")
            try:
                # Focus and paste (clipboard) to preserve special chars/emojis
                # robust focus/click
                focused = False
                try:
                    driver.execute_script("arguments[0].focus();", search_box)
                    focused = True
                except Exception:
                    pass
                try:
                    search_box.click(); time.sleep(0.1)
                    focused = True
                except Exception:
                    pass
                if not focused:
                    try:
                        driver.execute_script("arguments[0].click();", search_box)
                        time.sleep(0.1)
                    except Exception:
                        pass
                # Copy query to system clipboard and paste via Ctrl+V
                copied = False
                try:
                    import pyperclip  # type: ignore
                    pyperclip.copy(q)
                    copied = True
                except Exception:
                    copied = False
                # Select all then paste
                try:
                    ActionChains(driver).key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).pause(0.05).perform()
                except Exception:
                    try:
                        search_box.send_keys(Keys.CONTROL, 'a')
                    except Exception:
                        pass
                time.sleep(0.05)
                if copied:
                    try:
                        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).pause(0.05).perform()
                    except Exception:
                        try:
                            search_box.send_keys(Keys.CONTROL, 'v')
                        except Exception:
                            pass
                else:
                    # fallback to direct typing if clipboard unavailable
                    try:
                        search_box.clear(); time.sleep(0.05)
                        search_box.send_keys(q)
                    except Exception:
                        pass
                # send Enter to trigger filter if needed
                try:
                    search_box.send_keys(Keys.ENTER)
                except Exception:
                    pass
                time.sleep(step_sleep)

                # Wait for at least one matching row by text within this panel
                qn = _norm(q)
                qnl = _norm_loose(q)
                # Support table layout: include tr rows, div buttons, etc., as long as they contain a checkbox
                rows_xpath = ".//*[self::tr or self::div or self::button][.//input[@type='checkbox']]"
                WebDriverWait(right_panel, 6).until(
                    lambda d: any((qn in _norm(el.text)) or (qnl in _norm_loose(el.text)) for el in right_panel.find_elements(By.XPATH, rows_xpath))
                )

                # Ensure the search input does not keep focus (which could swallow SPACE/ENTER)
                try:
                    driver.execute_script("arguments[0].blur();", search_box)
                except Exception:
                    pass
                try:
                    ActionChains(driver).move_to_element(right_panel).pause(0.05).click().pause(0.05).perform()
                except Exception:
                    pass
                try:
                    search_box.send_keys(Keys.ESCAPE)
                except Exception:
                    pass

                # Uncheck only rows whose label contains the query (case-insensitive)
                unchecked = 0
                # re-query rows after filter settles to avoid stale elements
                rows = right_panel.find_elements(By.XPATH, rows_xpath)
                idx = 0
                def safe_is_selected(b):
                    try:
                        return b.is_selected()
                    except Exception:
                        return False

                for row in rows:
                    try:
                        # Skip header/select-all rows (checkbox inside thead or within a row with th cells)
                        try:
                            if row.find_elements(By.XPATH, ".//ancestor::thead"):
                                continue
                            if row.find_elements(By.XPATH, ".//th"):
                                continue
                        except Exception:
                            pass
                        label_el = None
                        try:
                            # Prefer the group name cell/span; handles table or flex row
                            label_el = row.find_element(By.XPATH, ".//td[2]//*[self::span or self::div][normalize-space()] | .//span[contains(@class,'font-extrabold') or contains(@class,'truncate')] | .//div[contains(@class,'truncate')] | .//*[contains(@class,'text') and normalize-space()]")
                        except Exception:
                            label_el = row
                        label_txt_norm = _norm(label_el.text)
                        label_txt_loose = _norm_loose(label_el.text)
                        if qn not in label_txt_norm and qnl not in label_txt_loose:
                            continue
                        # Prefer row-level checkbox (avoid header select-all) or an ARIA checkbox control
                        try:
                            # In table layout, the checkbox is usually in first td
                            box = row.find_element(By.XPATH, ".//td[1]//input[@type='checkbox' and contains(@class,'w-4')]")
                        except Exception:
                            try:
                                box = row.find_element(By.XPATH, ".//*[@role='checkbox']")
                            except Exception:
                                box = row.find_element(By.XPATH, ".//input[@type='checkbox']")
                        # ensure row is visible (scroll the panel itself)
                        try:
                            driver.execute_script("arguments[0].scrollTop = arguments[1].offsetTop - arguments[0].clientHeight/3;", right_panel, row)
                            time.sleep(0.05)
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
                        except Exception:
                            pass
                        time.sleep(0.05)
                        # if this match is already unchecked, try next match
                        if not safe_is_selected(box):
                            idx += 1
                            continue
                        # wait until element is clickable
                        try:
                            WebDriverWait(driver, 2).until(EC.element_to_be_clickable(box))
                        except Exception:
                            pass
                        # attempt 1: JS click on checkbox
                        try:
                            driver.execute_script("arguments[0].click();", box)
                            time.sleep(0.05)
                        except Exception:
                            pass
                        # attempt 2: native click on checkbox
                        if safe_is_selected(box):
                            try:
                                box.click(); time.sleep(0.05)
                            except Exception:
                                pass
                        # attempt 2b: native offset click (center of box)
                        if safe_is_selected(box):
                            try:
                                ActionChains(driver).move_to_element_with_offset(box, 2, 2).click().pause(0.05).perform()
                            except Exception:
                                pass
                        # attempt 3: click on label or row
                        if safe_is_selected(box):
                            try:
                                ActionChains(driver).move_to_element(label_el).click().pause(0.05).perform()
                            except Exception:
                                try:
                                    ActionChains(driver).move_to_element(row).click().pause(0.05).perform()
                                except Exception:
                                    pass
                        # attempt 3c: click the checkbox container cell/label
                        if safe_is_selected(box):
                            try:
                                container = None
                                try:
                                    container = row.find_element(By.XPATH, ".//label[.//input[@type='checkbox']]")
                                except Exception:
                                    pass
                                if not container:
                                    try:
                                        container = row.find_element(By.XPATH, ".//td[.//input[@type='checkbox']]")
                                    except Exception:
                                        pass
                                if container:
                                    try:
                                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", container)
                                    except Exception:
                                        pass
                                    try:
                                        driver.execute_script("arguments[0].click();", container)
                                        time.sleep(0.05)
                                    except Exception:
                                        pass
                                    if safe_is_selected(box):
                                        try:
                                            container.click(); time.sleep(0.05)
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                        # attempt 3a: dispatch mouse events (mousedown/mouseup/click)
                        if safe_is_selected(box):
                            try:
                                driver.execute_script(
                                    "var e1=new MouseEvent('mousedown',{bubbles:true}); var e2=new MouseEvent('mouseup',{bubbles:true}); var e3=new MouseEvent('click',{bubbles:true}); arguments[0].dispatchEvent(e1); arguments[0].dispatchEvent(e2); arguments[0].dispatchEvent(e3);",
                                    box,
                                )
                                time.sleep(0.05)
                            except Exception:
                                pass
                        # attempt 3b: focus checkbox and send SPACE
                        if safe_is_selected(box):
                            try:
                                driver.execute_script("arguments[0].focus();", box)
                            except Exception:
                                pass
                            try:
                                ActionChains(driver).move_to_element(box).send_keys(Keys.SPACE).pause(0.05).perform()
                            except Exception:
                                pass
                        # attempt 4: force uncheck with events
                        if safe_is_selected(box):
                            try:
                                driver.execute_script(
                                    "arguments[0].checked=false; arguments[0].dispatchEvent(new Event('input',{bubbles:true})); arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                                    box,
                                )
                                time.sleep(0.05)
                            except Exception:
                                pass
                        # attempt 5: click by coordinates at element center
                        if safe_is_selected(box):
                            try:
                                driver.execute_script(
                                    "var r=arguments[0].getBoundingClientRect(); var x=r.left+r.width/2; var y=r.top+r.height/2; var el=document.elementFromPoint(x,y); if(el){var e1=new MouseEvent('mousedown',{bubbles:true,clientX:x,clientY:y}); var e2=new MouseEvent('mouseup',{bubbles:true,clientX:x,clientY:y}); var e3=new MouseEvent('click',{bubbles:true,clientX:x,clientY:y}); el.dispatchEvent(e1); el.dispatchEvent(e2); el.dispatchEvent(e3);}",
                                    box,
                                )
                                time.sleep(0.05)
                            except Exception:
                                pass
                        # attempt 6: ARIA role checkbox toggle
                        if safe_is_selected(box):
                            try:
                                driver.execute_script(
                                    "if(arguments[0].getAttribute('role')==='checkbox'){arguments[0].setAttribute('aria-checked','false'); arguments[0].dispatchEvent(new Event('change',{bubbles:true}));}",
                                    box,
                                )
                                time.sleep(0.05)
                            except Exception:
                                pass
                        if not safe_is_selected(box):
                            unchecked += 1
                            break  # uncheck only the first matching selected row
                        idx += 1
                    except Exception as e:
                        dbg(f"[FF] exclude: error while processing match '{q}': {e}")
                        continue
                dbg(f"[FF] exclude: '{q}' unchecked {unchecked} item(s)")
                if unchecked == 0:
                    dbg(f"[FF] exclude: no selectable checkbox found for '{q}' (maybe already unchecked or filtered)")

                # Clear search to restore full list before next term
                try:
                    time.sleep(0.1)
                    search_box.clear()
                except Exception:
                    pass
                time.sleep(0.2)
            except Exception as e:
                dbg(f"[FF] exclude: error on '{q}': {e}")
                continue
    except Exception as e:
        dbg(f"[FF] exclude step failed: {e}")

def ff_get_selected_group_names(driver, max_items=500):
    """Best-effort read of checked group names from right-side list."""
    names = []
    # 1) Locate the RIGHT-SIDE list container that actually holds group checkboxes.
    try:
        containers = driver.find_elements(
            By.XPATH,
            "//div[.//input[@type='checkbox'] and (contains(@class,'overflow-y') or contains(@class,'overflow-auto') or contains(@class,'divide-y') or contains(@class,'rounded'))]"
        )
    except Exception:
        return names

def ff_get_post_results(driver, max_items=500):
    """Read the left-side posting results AFTER clicking Post.
    Returns list of tuples: [(group_id, post_url), ...]
    Includes only rows with a non-error status (blue/yellow/green checks),
    and excludes rows with a red X (failed).

    Enhanced for reliable background operation across desktop switches.
    """
    results = []
    # Per-session caches to avoid re-opening the same group's post repeatedly
    # Structure: _url_cache[session_id] -> {label -> url}
    #            _last_clicked[session_id] -> {label -> timestamp}
    sid = getattr(driver, 'session_id', None) or 'default'
    if not hasattr(ff_get_post_results, "_url_cache"):
        ff_get_post_results._url_cache = {}
    if not hasattr(ff_get_post_results, "_last_clicked"):
        ff_get_post_results._last_clicked = {}
    sess_url_cache = ff_get_post_results._url_cache.setdefault(sid, {})
    sess_last_clicked = ff_get_post_results._last_clicked.setdefault(sid, {})
    # Purge stale throttles (>120s) so labels can be retried in long sessions
    try:
        now_ts = time.time()
        stale = [k for k, ts in sess_last_clicked.items() if now_ts - ts > 120]
        for k in stale:
            sess_last_clicked.pop(k, None)
    except Exception:
        pass
    def _clean_label(txt: str) -> str:
        if not txt:
            return ""
        return txt.replace("\r", "\n").split("\n")[0].strip()
    
    def _extract_group_id_from_url(url: str) -> str:
        """Extract Facebook group ID from various URL formats."""
        if not url:
            return ""
        try:
            # Handle different Facebook group URL formats
            import re
            # Pattern for /groups/123456789/
            match = re.search(r'/groups/([0-9]+)/', url)
            if match:
                return match.group(1)
            # Pattern for /groups/groupname/ - use the name as ID
            match = re.search(r'/groups/([^/?]+)/', url)
            if match:
                return match.group(1)
            # Fallback: use the group name from the label
            return ""
        except Exception:
            return ""

    def _view_post_button(driver):
        # Case-insensitive contains 'View Post'
        xps = [
            "//button[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
            "//a[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
            "//div[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
            "//*[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post') and (self::button or self::a or self::div)]",
        ]
        for xp in xps:
            with contextlib.suppress(Exception):
                btns = driver.find_elements(By.XPATH, xp)
                for b in btns:
                    with contextlib.suppress(Exception):
                        if b.is_displayed():
                            return b
        return None

    def _ensure_view_post_ready(max_wait=6.0):
        """Keep trying to click the first result until the global View Post appears."""
        end = time.time() + max_wait
        while time.time() < end:
            btn = _view_post_button(driver)
            if btn is not None:
                return True
            # try the activator paths again
            try:
                # try rows first if any visible
                rows_hint = []
                with contextlib.suppress(Exception):
                    rows_hint = driver.find_elements(By.XPATH, "//div[contains(@class,'flex') and .//*[name()='svg']]")
                _activate_view_post_once(rows_hint=rows_hint, cards_hint=None)
            except Exception:
                pass
            time.sleep(0.4)
        return _view_post_button(driver) is not None

    # One-shot activator to make FewFeed show the global "View Post" UI
    def _activate_view_post_once(rows_hint=None, cards_hint=None):
        try:
            if getattr(ff_get_post_results, "_activated", False) or getattr(ff_get_post_results, "_activated_by_card", False) or getattr(ff_get_post_results, "_hard_row_activated", False):
                return True
            dbg("[FF] Activation: starting activation sequence")
            # 0) Try the FewFeed disclosure button which appears to be used to expand/select
            try:
                btns = driver.find_elements(By.XPATH, "//button[contains(@class,'headlessui-disclosure-button')]")
                btn = next((b for b in btns if b.is_displayed()), None)
                if btn is not None:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.1)
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.4)
                    ff_get_post_results._activated = True
                    dbg("[FF] Activation: clicked headlessui-disclosure button")
                    return True
            except Exception:
                pass
            # 1) Prefer clicking the first row strictly inside the LEFT results container
            container = ff_locate_results_container(driver)
            if container is not None:
                with contextlib.suppress(Exception):
                    row = None
                    strict = container.find_elements(By.XPATH, ".//div[contains(@class,'flex') and .//span[contains(@class,'truncate')] and .//*[name()='svg']]")
                    row = strict[0] if strict else None
                    if row is None:
                        generic = container.find_elements(By.XPATH, ".//div[contains(@class,'flex') and .//*[name()='svg']]")
                        row = generic[0] if generic else None
                    if row is not None:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
                        time.sleep(0.1)
                        try:
                            row.click(); time.sleep(0.1)
                        except Exception:
                            driver.execute_script("arguments[0].click();", row); time.sleep(0.1)
                        ff_get_post_results._hard_row_activated = True
                        dbg("[FF] Activation: clicked row inside results container")
                        return True
            # 2) Try success card click (still constrained away from toolbar)
            if cards_hint:
                with contextlib.suppress(Exception):
                    first_card = cards_hint[0]
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", first_card)
                    time.sleep(0.15)
                    driver.execute_script("arguments[0].click();", first_card)
                    time.sleep(0.5)
                    ff_get_post_results._activated_by_card = True
                    dbg("[FF] Activation: clicked first success card")
                    return True
            # 2) Try clicking first row (various strategies)
            if rows_hint:
                with contextlib.suppress(Exception):
                    first_row = rows_hint[0]
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", first_row)
                    time.sleep(0.15)
                    try:
                        WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, ".")))
                    except Exception:
                        pass
                    # Native click
                    try:
                        first_row.click(); time.sleep(0.15)
                        dbg("[FF] Activation: native click on first row")
                    except Exception:
                        dbg("[FF] Activation: native click failed, trying ActionChains/JS")
                    # ActionChains click
                    with contextlib.suppress(Exception):
                        ActionChains(driver).move_to_element(first_row).pause(0.05).click(first_row).perform()
                        time.sleep(0.15)
                        dbg("[FF] Activation: ActionChains click on first row")
                    # JS click
                    driver.execute_script("arguments[0].click();", first_row)
                    time.sleep(0.2)
                    # Inner element click
                    inner = None
                    for sel in [
                        ".//span[contains(@class,'truncate')]",
                        ".//button",
                        ".//a",
                        ".//div[1]",
                        "."
                    ]:
                        with contextlib.suppress(Exception):
                            cand = first_row.find_element(By.XPATH, sel)
                            if cand and cand.is_displayed():
                                inner = cand; break
                    if inner is not None:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inner)
                        time.sleep(0.05)
                        try:
                            inner.click(); time.sleep(0.1)
                            dbg("[FF] Activation: native click on inner element")
                        except Exception:
                            driver.execute_script("arguments[0].click();", inner)
                            time.sleep(0.1)
                            dbg("[FF] Activation: JS click on inner element")
                    # MouseEvent click
                    with contextlib.suppress(Exception):
                        driver.execute_script("var e=new MouseEvent('click',{bubbles:true});arguments[0].dispatchEvent(e);", first_row)
                        time.sleep(0.1)
                    ff_get_post_results._hard_row_activated = True
                    dbg("[FF] Activation: clicked first success row")
                    return True

            # 2b) Try a row located relative to the Stop button (layout-stable anchor)
            with contextlib.suppress(Exception):
                stop_btn = driver.find_element(By.XPATH, "//button[normalize-space(.)='Stop']")
                candidate = stop_btn.find_element(By.XPATH, "following::div[contains(@class,'flex') and .//*[name()='svg']][1]")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", candidate)
                time.sleep(0.1)
                try:
                    candidate.click(); time.sleep(0.1)
                except Exception:
                    driver.execute_script("arguments[0].click();", candidate); time.sleep(0.1)
                ff_get_post_results._hard_row_activated = True
                dbg("[FF] Activation: clicked row relative to Stop button")
                return True
            # 3) As a final nudge, focus body
            with contextlib.suppress(Exception):
                driver.execute_script("document.body.focus();")
            dbg("[FF] Activation: no target to click")
            return False
        except Exception as e:
            dbg(f"[FF] Activation error: {e}")
            return False

        # FINAL JS-only fallback (DOM walk): try to click Stop-adjacent first result
        try:
            js = r"""
            (function(){
              function clickEl(el){
                try{el.scrollIntoView({block:'center'});}catch(e){}
                try{el.click();}catch(e){}
                try{var e2=new MouseEvent('click',{bubbles:true});el.dispatchEvent(e2);}catch(e){}
              }
              // 1) Try disclosure button
              var disc = document.querySelector('button.headlessui-disclosure-button');
              if(disc && disc.offsetParent!==null){
                clickEl(disc);
                return 'disclosure';
              }
              // 2) Find Stop button by exact text
              var btns = Array.from(document.querySelectorAll('button'));
              var stop = btns.find(b => (b.textContent||'').trim().toLowerCase()==='stop');
              if(stop){
                // look for first result row after Stop area that contains an svg status icon
                var root = stop.parentElement; // ascend a bit to section
                for(var i=0;i<4 && root && root.tagName!='BODY';i++){root=root.parentElement;}
                var cand = (root||document).querySelector("div.flex div.flex, div.flex[role='button'], div.flex");
                if(cand){clickEl(cand);return 'row-after-stop';}
              }
              // 3) Click first visible row with an svg
              var rows = Array.from(document.querySelectorAll("div.flex"));
              var r = rows.find(x => x.querySelector('svg') && x.offsetParent!==null);
              if(r){clickEl(r);return 'row-generic';}
              return 'none';
            })();
            """
            mode = driver.execute_script(js)
            if mode and mode != 'none':
                ff_get_post_results._hard_row_activated = True
                dbg(f"[FF] Activation: JS DOM-walk path -> {mode}")
                return True
        except Exception as e:
            dbg(f"[FF] Activation: JS fallback failed: {e}")
            return False

    # Enhanced background-safe container detection
    try:
        # Ensure page is responsive before scanning
        driver.execute_script("return document.readyState;")
        # Force a gentle interaction to wake up any sleeping elements
        driver.execute_script("document.body.focus();")
        time.sleep(0.1)
        
        containers = driver.find_elements(
            By.XPATH,
            "//div[(contains(@class,'overflow-y') or contains(@class,'overflow-auto')) and not(contains(@class,'hidden'))]"
        )
    except Exception:
        containers = []

    candidates = []
    for c in containers:
        try:
            cb_count = len(c.find_elements(By.XPATH, ".//input[@type='checkbox']"))
        except Exception:
            cb_count = 0
        try:
            svg_count = len(c.find_elements(By.XPATH, ".//*[name()='svg']"))
        except Exception:
            svg_count = 0
        # We prefer few/no checkboxes (not the right list) and many svgs (status icons)
        candidates.append((cb_count, -svg_count, c))
    candidates.sort(key=lambda x: (x[0], x[1]))

    chosen = candidates[0][2] if candidates else None
    if not chosen:
        dbg("[FF] No results container found")
        return results

    rows = []
    try:
        rows = chosen.find_elements(By.XPATH, ".//div[contains(@class,'flex') and .//*[name()='svg']]")
    except Exception:
        rows = []

    # Prefer stricter rows that match the observed DOM: have a truncated label and a 7x7 colored status icon
    try:
        strict_rows = chosen.find_elements(
            By.XPATH,
            ".//div[contains(@class,'flex') and .//span[contains(@class,'truncate')] and .//*[name()='svg' and contains(@class,'w-7') and contains(@class,'h-7')]]"
        )
        if strict_rows:
            rows = strict_rows
            dbg(f"[FF] Using strict row selector: {len(rows)} rows")
    except Exception:
        pass

    # If no rows via strict/icon selector, broaden to any visible text row with a truncated span
    if not rows:
        with contextlib.suppress(Exception):
            broad_rows = chosen.find_elements(By.XPATH, ".//div[contains(@class,'flex') and .//span[contains(@class,'truncate')]]")
            if broad_rows:
                rows = broad_rows
                dbg(f"[FF] Using broad row selector: {len(rows)} rows")

    dbg(f"[FF] Results container rows: {len(rows)} (checkboxes={candidates[0][0] if candidates else 'n/a'})")
    # Preview a few labels to verify parsing per account session
    try:
        preview = []
        for r in rows[:3]:
            txt = r.text or ""
            label = txt.replace("\r", "\n").split("\n")[0].strip()
            if label:
                preview.append(label[:60])
        if preview:
            dbg(f"[FF] Row preview: {preview}")
    except Exception:
        pass

    # Fallback 1: if we couldn't find rows in the chosen container, do a global scan
    if len(rows) == 0:
        try:
            svg_candidates = driver.find_elements(
                By.XPATH,
                "//*[name()='svg' and (contains(@class,'text-blue') or contains(@class,'text-green') or contains(@class,'text-yellow') or contains(@class,'text-success') or contains(@class,'text-primary') or contains(@class,'text-info'))]"
            )
        except Exception:
            svg_candidates = []
        dbg(f"[FF] Fallback: candidate SVGs found globally: {len(svg_candidates)}")
        # Wrap into synthetic rows by walking up to flex row
        temp_rows = []
        for s in svg_candidates:
            try:
                r = s.find_element(By.XPATH, "./ancestor::div[contains(@class,'flex')][1]")
                temp_rows.append(r)
            except Exception:
                continue
        # Deduplicate
        try:
            # use element ids
            seen = set()
            uniq = []
            for r in temp_rows:
                rid = r.id if hasattr(r, 'id') else r
                if rid in seen:
                    continue
                seen.add(rid)
                uniq.append(r)
            rows = uniq
        except Exception:
            rows = temp_rows

    # Fallback 2: explicit button rows with svg + span.truncate (matches screenshots)
    if len(rows) == 0:
        try:
            btn_rows = driver.find_elements(
                By.XPATH,
                "//button[.//*[name()='svg'] and .//span[contains(@class,'truncate')]]"
            )
        except Exception:
            btn_rows = []
        dbg(f"[FF] Fallback2: button rows found: {len(btn_rows)}")
        rows = btn_rows

    # Attempt activation immediately based on rows present
    try:
        if rows:
            _activate_view_post_once(rows_hint=rows, cards_hint=None)
    except Exception:
        pass

    # NEW: Directly detect the success cards (most reliable). If found, prefer this path.
    success_cards = []
    try:
        success_cards = driver.find_elements(
            By.XPATH,
            "//div[contains(translate(., 'SUCCESSFULLY', 'successfully'), 'successfully post to')]"
        )
    except Exception:
        success_cards = []
    if success_cards:
        dbg(f"[FF] Success cards detected: {len(success_cards)}")
        # Try activation using cards
        _activate_view_post_once(rows_hint=None, cards_hint=success_cards)
        # Ensure the View Post button is visible (retry-click the group if needed)
        if not _ensure_view_post_ready(max_wait=5.0):
            dbg("[FF] Could not arm View Post after success card; will retry next cycle")
            return results
        taken = set()
        results_from_cards = []
        newest_card = success_cards[-1]

        # One-time activation: click the first card so FewFeed shows View Post
        if not getattr(ff_get_post_results, "_activated_by_card", False):
            try:
                first_card = success_cards[0]
                # Strategy 1: scroll + JS click on card
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", first_card)
                time.sleep(0.2)
                driver.execute_script("arguments[0].click();", first_card)
                time.sleep(0.8)
                # Strategy 2: try clicking the first text span/button inside the card
                if True:
                    inner = None
                    for sel in [
                        ".//span[contains(@class,'truncate')]",
                        ".//button",
                        ".//a",
                        ".//div[1]",
                        "."
                    ]:
                        with contextlib.suppress(Exception):
                            cand = first_card.find_element(By.XPATH, sel)
                            if cand and cand.is_displayed():
                                inner = cand; break
                    if inner is not None:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", inner)
                        time.sleep(0.1)
                        driver.execute_script("arguments[0].click();", inner)
                        time.sleep(0.6)
                # Strategy 3: dispatch MouseEvent click to ensure bubbling
                with contextlib.suppress(Exception):
                    driver.execute_script("var e=new MouseEvent('click',{bubbles:true});arguments[0].dispatchEvent(e);", first_card)
                    time.sleep(0.4)

                ff_get_post_results._activated_by_card = True
                dbg("[FF] Activated by clicking first success card")
            except Exception as e:
                dbg(f"[FF] Activation click on success card failed: {e}")

        # Parse group info from each card (but don't emit until we have real post URL)
        import re as _re
        parsed_cards = []  # list of (gid, label, card)
        for ci, card in enumerate(success_cards):
            try:
                card_text = ''
                with contextlib.suppress(Exception):
                    card_text = card.text or ''
                lines = [l.strip() for l in (card_text or '').splitlines() if l.strip()]
                label = lines[0] if lines else ''
                gid = ''
                m = _re.search(r"\b(\d{6,})\b", card_text or '')
                if m:
                    gid = m.group(1)
                if not gid and label:
                    gid = label.replace(' ', '_').replace('-', '_')
                if gid and gid not in taken:
                    taken.add(gid)
                    parsed_cards.append((gid, label, card))
            except Exception:
                continue

        # Try to click the View Post on the newest card to replace the URL with the real post URL
        try:
            original_window = driver.current_window_handle
            original_url = driver.current_url
            vp = None
            for sel in [
                ".//button[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
                ".//a[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
            ]:
                with contextlib.suppress(Exception):
                    elems = newest_card.find_elements(By.XPATH, sel)
                    vp = next((e for e in elems if e.is_displayed()), None)
                    if vp:
                        break
            if vp:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", vp)
                time.sleep(0.15)
                driver.execute_script("arguments[0].click();", vp)
                try:
                    WebDriverWait(driver, 8).until(lambda d: len(d.window_handles) > 1 or ('facebook.com' in d.current_url and d.current_url != original_url))
                except Exception:
                    pass
                time.sleep(0.4)
                post_url = ''
                if len(driver.window_handles) > 1:
                    for h in driver.window_handles:
                        if h != original_window:
                            driver.switch_to.window(h)
                            post_url = driver.current_url
                            driver.close()
                            driver.switch_to.window(original_window)
                            break
                else:
                    if 'facebook.com' in driver.current_url and driver.current_url != original_url:
                        post_url = driver.current_url
                        driver.back(); time.sleep(0.5)
                # Validate we captured a real post URL (avoid generic group URLs)
                def _is_real_post(u: str) -> bool:
                    try:
                        u = (u or '').lower()
                        if ('facebook.com' not in u and 'fb.com' not in u):
                            return False
                        patterns = [
                            '/posts/',
                            '/permalink/',
                            'story.php?story_fbid=',
                            '/groups/'
                        ]
                        return any(p in u for p in patterns)
                    except Exception:
                        return False

                if post_url and _is_real_post(post_url):
                    # Map to newest parsed label if available
                    if parsed_cards:
                        newest_label = parsed_cards[-1][1]
                        results_from_cards.append((newest_label, post_url))
                        dbg(f"[FF] Captured post URL for {newest_label}")
                else:
                    dbg("[FF] View Post did not yield a real post URL yet; will retry next cycle")
        except Exception as e:
            dbg(f"[FF] View Post click failed: {e}")

        # Only return entries with real post URLs
        if results_from_cards:
            results.extend(results_from_cards[:max_items])
            dbg(f"[FF] Card-scan returned {len(results)} items with post URLs")
        else:
            dbg("[FF] Card-scan found success but no post URLs yet (waiting)")
        return results

    taken = set()
    scanned = 0
    accepted = 0
    rejected = 0
    
    # NEW: Hard activation — click the first visible result row to arm View Post (if any rows exist)
    if rows and not getattr(ff_get_post_results, "_hard_row_activated", False):
        try:
            first_row = rows[0]
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", first_row)
            time.sleep(0.15)
            # Strategy A: JS click on row
            driver.execute_script("arguments[0].click();", first_row)
            time.sleep(0.4)
            # Strategy B: click a child element if available
            with contextlib.suppress(Exception):
                child = first_row.find_element(By.XPATH, ".//span|.//div|.//button|.//a")
                if child and child.is_displayed():
                    driver.execute_script("arguments[0].click();", child)
                    time.sleep(0.3)
            # Strategy B2: specifically click the truncated label span (per screenshots)
            with contextlib.suppress(Exception):
                trunc = first_row.find_element(By.XPATH, ".//span[contains(@class,'truncate')]")
                if trunc and trunc.is_displayed():
                    driver.execute_script("arguments[0].click();", trunc)
                    time.sleep(0.3)
            # Strategy C: dispatch MouseEvent
            with contextlib.suppress(Exception):
                driver.execute_script("var e=new MouseEvent('click',{bubbles:true});arguments[0].dispatchEvent(e);", first_row)
                time.sleep(0.2)
            ff_get_post_results._hard_row_activated = True
            dbg("[FF] Hard-activated by clicking first success row")
        except Exception as e:
            dbg(f"[FF] Hard activation on first row failed: {e}")

    # First, click the first successful group to activate View Post functionality
    first_group_clicked = False
    for r in rows:
        try:
            # Skip failed rows with any red/danger svg
            if r.find_elements(By.XPATH, ".//*[name()='svg' and (contains(@class,'text-red') or contains(@class,'text-danger'))]"):
                continue
            # Ensure there is at least one svg icon indicating a status in this row
            if not r.find_elements(By.XPATH, ".//*[name()='svg']"):
                continue
            
            # Try to find the clickable group name/button within this row
            clickable_element = None
            for selector in [
                ".//span[contains(@class,'truncate')]",
                ".//div[contains(@class,'truncate')]", 
                ".//button",
                ".//a",
                "."  # fallback to the row itself
            ]:
                try:
                    clickable_element = r.find_element(By.XPATH, selector)
                    if clickable_element.is_displayed():
                        break
                except Exception:
                    continue
            # Perform the actual activation click once
            if clickable_element:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clickable_element)
                except Exception:
                    pass
                time.sleep(0.2)
                driver.execute_script("arguments[0].click();", clickable_element)
                time.sleep(0.6)
                # Ensure "View Post" global button is armed
                _ensure_view_post_ready(max_wait=5.0)
                first_group_clicked = True
                try:
                    ff_get_post_results._activated = True
                except Exception:
                    pass
                dbg("[FF] Clicked first group to activate View Post functionality")
                break
        except Exception:
            continue

    if not first_group_clicked:
        # Last chance: try activation again and ensure the View Post is present
        try:
            _activate_view_post_once(rows_hint=rows, cards_hint=None)
            if _ensure_view_post_ready(max_wait=4.0):
                first_group_clicked = True
            else:
                dbg("[FF] Could not click any group to activate View Post functionality")
                return results
        except Exception:
            dbg("[FF] Could not click any group to activate View Post functionality")
            return results
    
    # Now process each group and extract URLs using View Post
    for idx, r in enumerate(rows):
        try:
            scanned += 1
            # Skip failed rows with any red/danger svg
            if r.find_elements(By.XPATH, ".//*[name()='svg' and (contains(@class,'text-red') or contains(@class,'text-danger'))]"):
                rejected += 1
                continue
            # Ensure there is at least one svg icon indicating a status in this row
            if not r.find_elements(By.XPATH, ".//*[name()='svg']"):
                rejected += 1
                continue
            # Only accept success/in-progress rows (blue/yellow/green) and exclude others
            if not r.find_elements(
                By.XPATH,
                ".//*[name()='svg' and (contains(@class,'text-blue') or contains(@class,'text-green') or contains(@class,'text-yellow') or contains(@class,'text-success') or contains(@class,'text-primary') or contains(@class,'text-info'))]"
            ):
                rejected += 1
                continue
            
            # Extract group name/label
            label = ''
            for xp in [
                ".//span[contains(@class,'truncate')][1]",
                ".//div[contains(@class,'truncate')][1]",
                ".//span[contains(@class,'text')][1]",
                ".//div[contains(@class,'text')][1]",
                ".//div[1]",
            ]:
                try:
                    t = _clean_label(r.find_element(By.XPATH, xp).text)
                    if t:
                        label = t
                        break
                except Exception:
                    continue
            if not label:
                label = _clean_label(r.text)
            if not label:
                rejected += 1
                continue
            
            # If we already have a cached URL for this label, reuse it and avoid clicking again
            cached = sess_url_cache.get(label)
            if cached:
                post_url = cached
            else:
                post_url = ""
            
            # Extract post URL using global success toast's View Post button (reliable)
            try:
                # Throttle: avoid clicking View Post repeatedly for the same label within a window
                last = sess_last_clicked.get(label, 0)
                if post_url:
                    # We already have a URL for this label — skip any clicking entirely
                    raise RuntimeError("skip_view_post_cached_label")
                if time.time() - last < 20:
                    # Recently tried this label — skip re-opening for now
                    raise RuntimeError("skip_view_post_recent_label")
                # Store original windows and URL
                original_window = driver.current_window_handle
                handles_before = driver.window_handles[:]
                original_url = driver.current_url
                
                # The FewFeed UI shows a global success box with a green View Post button.
                # We target that directly, not per-row.
                view_post_button = ff_find_view_post_button(r)
                if not view_post_button:
                    dbg("[FF] View Post button not found on first attempt; pulsing container and retrying")
                    with contextlib.suppress(Exception):
                        ff_pulse_results_container_scroll(driver)
                        time.sleep(0.1)
                    view_post_button = ff_find_view_post_button(r)
                
                if view_post_button:
                    # Scroll to and click the button (robust)
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", view_post_button)
                        time.sleep(0.1)
                    except Exception:
                        pass
                    clicked = False
                    try:
                        view_post_button.click(); clicked = True
                    except Exception:
                        with contextlib.suppress(Exception):
                            driver.execute_script("arguments[0].click();", view_post_button); clicked = True
                    if not clicked:
                        dbg("[FF] Failed to click View Post via DOM; skipping this label for now")
                
                # Wait for a new tab or same-tab navigation to Facebook
                    try:
                        WebDriverWait(driver, 8).until(
                            lambda d: (len(d.window_handles) > len(handles_before)) or (
                                'facebook.com' in (d.current_url or '').lower() and d.current_url != original_url
                            )
                        )
                    except Exception:
                        pass
                    time.sleep(0.4)
                    # Prefer a brand new handle strictly created by the click
                    handles_after = driver.window_handles[:]
                    new_handles = [h for h in handles_after if h not in handles_before]
                    if new_handles:
                        new_handle = new_handles[-1]
                        driver.switch_to.window(new_handle)
                        try:
                            WebDriverWait(driver, 5).until(lambda d: 'facebook.com' in (d.current_url or '').lower())
                        except Exception:
                            pass
                        post_url = driver.current_url
                        # Close only this newly-opened post tab
                        driver.close()
                        # Switch back to original FewFeed window
                        driver.switch_to.window(original_window)
                        # Remember we handled this label
                        try:
                            sess_last_clicked[label] = time.time()
                            sess_url_cache[label] = post_url
                        except Exception:
                            pass
                    else:
                        # Same-tab navigation case
                        if 'facebook.com' in driver.current_url.lower() and driver.current_url != original_url:
                            post_url = driver.current_url
                            driver.back()
                            time.sleep(0.8)
                            try:
                                sess_last_clicked[label] = time.time()
                                sess_url_cache[label] = post_url
                            except Exception:
                                pass
                
                # Small delay to ensure UI is ready for next group
                time.sleep(0.2)
            
            except Exception as e:
                if str(e) in ('skip_view_post_recent_label', 'skip_view_post_cached_label'):
                    pass
                # If anything goes wrong, try to return to original state
                try:
                    # Close any newly opened tab if we somehow switched
                    if len(driver.window_handles) > 1:
                        for handle in driver.window_handles:
                            if handle != original_window:
                                driver.switch_to.window(handle)
                                driver.close()
                        driver.switch_to.window(original_window)
                    elif driver.current_url != original_url:
                        driver.back()
                except Exception:
                    pass
            
            # Use group name as unique key for this mode
            key_name = label
            if key_name not in taken:
                taken.add(key_name)
                # If we have a cached URL, prefer it
                if not post_url:
                    post_url = sess_url_cache.get(key_name, "")
                results.append((key_name, post_url if post_url else ""))
                accepted += 1
                if len(results) >= max_items:
                    break
        except Exception:
            continue

    dbg(f"[FF] Results scan: scanned={scanned}, accepted={accepted}, rejected={rejected}, returned={len(results)}")
    return results

    target = None
    max_cbs = 0
    for c in containers:
        try:
            cbs = c.find_elements(By.XPATH, ".//input[@type='checkbox']")
            if len(cbs) > max_cbs:
                max_cbs = len(cbs)
                target = c
        except Exception:
            continue

    if not target:
        return names

    # 2) From the chosen container, gather rows with a selected checkbox and extract the nearest text label.
    try:
        rows = target.find_elements(By.XPATH, ".//*[.//input[@type='checkbox']]")
    except Exception:
        return names

    def _clean_label(txt: str) -> str:
        # keep only first line, trim spaces
        if not txt:
            return ""
        txt = txt.replace("\r", "\n").split("\n")[0].strip()
        return txt

    blacklist = {"random", "thread", "delay", "post", "stop"}

    for r in rows:
        try:
            cb = r.find_element(By.XPATH, ".//input[@type='checkbox']")
            if not cb.is_selected():
                continue
        except Exception:
            continue

        label_text = ""
        for xp in [
            ".//span[contains(@class,'truncate')][1]",
            ".//div[contains(@class,'truncate')][1]",
            ".//span[contains(@class,'text')][1]",
            ".//div[contains(@class,'text')][1]",
            ".//label[1]",
        ]:
            try:
                el = r.find_element(By.XPATH, xp)
                cand = _clean_label(el.text)
                if cand:
                    label_text = cand
                    break
            except Exception:
                continue

        if not label_text:
            try:
                label_text = _clean_label(r.text)
            except Exception:
                label_text = ""

        if not label_text:
            continue

        lt_low = label_text.lower()
        if any(w in lt_low for w in blacklist):
            continue
        if len(label_text) > 120 or len(label_text) < 2:
            continue
        if re.fullmatch(r"[0-9\-]+", label_text):
            continue

        if label_text not in names:
            names.append(label_text)
            if len(names) >= max_items:
                break

    return names
def get_chrome_profiles():
    """Finds all Google Chrome profiles on the system."""
    profiles = {}
    local_app_data = os.getenv('LOCALAPPDATA')
    if not local_app_data:
        return {}

    chrome_path = os.path.join(local_app_data, 'Google', 'Chrome', 'User Data')
    if not os.path.exists(chrome_path):
        return {}

    # Find all profile directories (Default and Profile X)
    for item in os.listdir(chrome_path):
        if item.startswith('Profile ') or item == 'Default':
            profile_dir = os.path.join(chrome_path, item)
            prefs_file = os.path.join(profile_dir, 'Preferences')
            if os.path.exists(prefs_file):
                try:
                    with open(prefs_file, 'r', encoding='utf-8-sig') as f: # Use utf-8-sig to handle BOM
                        prefs = json.load(f)
                    profile_name = prefs.get('profile', {}).get('name', item)
                    profiles[profile_name] = profile_dir
                except (json.JSONDecodeError, UnicodeDecodeError):
                    profiles[item] = profile_dir # Fallback to directory name
    return profiles

def save_config(data):
    """Safely persist config by merging with the existing file (do not wipe user settings)."""
    base = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                base = json.load(f)
        except Exception:
            base = {}
    base.update(data)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(base, f, indent=2)

def load_config():
    # Defaults (ensure this definition preserves user settings and keys)
    default_cfg = {
        "thread_value": 3,
        "delay_value": 5,
        "enable_auto_post": False,
        "post_with_images": False,
        "extension_id": "",
        # profile: 'template' copies template_chrome_profile (may be large),
        # 'minimal' creates a tiny fresh profile and loads the extension via --load-extension
        "profile_mode": "template",
        # when profile_mode == 'minimal', use this extension folder
        "extension_path": "",
        # background-safe trigger: 'web' navigates to FewFeed site (no hotkeys)
        # set to 'hotkey' only if you want Ctrl+Shift+F extension trigger
        "extension_trigger_mode": "web",
        "step_delay": 2,
        "images_path": "",
        # helper settings for semi-automatic extension setup
        "extension_path": "",
        "extension_shortcut": "Ctrl+Shift+F",
        # results watcher behavior
        "post_watch_seconds": 600,
        "continuous_watch": True,
        "results_min_age": 15,
        # speed customization
        "image_paste_speed": 0.3,
        "group_exclusion_speed": 0.2,
        "extension_retry_attempts": 5,
        "extension_retry_delay": 2,
    }
    if not os.path.exists(CONFIG_PATH):
        return default_cfg
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        default_cfg.update(data)
    except Exception:
        pass
    return default_cfg

# --- Setup Wizard ---
def run_setup_wizard():
    """Guides the user through the first-time setup process."""
    print("\n--- Welcome to the FewFeed Bot Setup Wizard ---")
    print("This one-time setup will help you create a master Chrome profile.")

    # Step 1: Choose a base profile
    profiles = get_chrome_profiles()
    if not profiles:
        print("\nError: Could not find any Chrome profiles.")
        print("Please make sure Google Chrome is installed.")
        return

    print("\nStep 1: Please choose a Chrome profile to use as a base.")
    profile_list = list(profiles.keys())
    for i, name in enumerate(profile_list):
        print(f"  {i+1}. {name}")

    choice = -1
    while choice < 0 or choice >= len(profile_list):
        try:
            choice = int(input(f"Enter your choice (1-{len(profile_list)}): ")) - 1
        except ValueError:
            print("Invalid input. Please enter a number.")

    selected_profile_name = profile_list[choice]
    selected_profile_path = profiles[selected_profile_name]
    print(f"\nYou have selected: {selected_profile_name}")

    # Step 2: Create a temporary copy of the selected profile
    print("\nStep 2: Preparing a temporary profile for setup...")
    temp_setup_profile_dir = os.path.join(BASE_DIR, 'temp_setup_profile')
    if os.path.exists(temp_setup_profile_dir):
        shutil.rmtree(temp_setup_profile_dir, ignore_errors=True)

    try:
        shutil.copytree(selected_profile_path, temp_setup_profile_dir)
        print("Temporary profile created successfully.")
    except Exception as e:
        print(f"\nError creating temporary profile: {e}")
        return

    # Step 3: Install extension in the temporary profile (semi-automatic)
    print("\nStep 3: Install the FewFeed Extension (semi-automatic).")
    print("I will launch Chrome with the temp profile and open chrome://extensions/.")
    print("Developer mode will be enabled automatically.")
    print("Please: Click 'Load unpacked' and select the FewFeed extension folder.")
    # Ask for extension folder (optional; for your reference only)
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    ext_path = cfg.get('extension_path') or input("Enter path to FewFeed extension folder (or leave empty to skip): ").strip()
    if ext_path:
        save_config({"extension_path": ext_path})
    shortcut_combo = cfg.get('extension_shortcut', 'Ctrl+Shift+F')
    print(f"Desired extension shortcut will be: {shortcut_combo}")
    input("\nPress Enter to launch Chrome...")

    # Enable developer mode in the profile BEFORE launching Chrome
    enable_developer_mode_in_profile(temp_setup_profile_dir)

    from selenium.webdriver.chrome.options import Options
    options = Options()
    # Launch the new temporary profile. It's a self-contained User Data dir.
    options.add_argument(f'--user-data-dir={temp_setup_profile_dir}')
    options.add_experimental_option("detach", True)

    try:
        driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        print("\nChrome launched with the temporary profile.")
        time.sleep(1)
        # Bring to front
        try:
            driver.execute_cdp_cmd('Page.bringToFront', {})
        except Exception:
            pass
        # Enable developer mode via Selenium UI (Preferences file is reset by Chrome on launch)
        enable_developer_mode_via_selenium(driver)
        # Open chrome://extensions via omnibox paste (more reliable than typing)
        try:
            driver.get('chrome://newtab/')
        except Exception:
            pass
        time.sleep(1)
        _omnibox_navigate('chrome://extensions/')
        time.sleep(1.5)
        print("\nOn the Extensions page:")
        print("  - Developer mode should already be enabled (toggle should be ON).")
        print("  - Click 'Load unpacked' and choose your FewFeed extension folder.")
        print("  - Go to 'Keyboard shortcuts' and set the shortcut (e.g., Ctrl+Shift+F).")
        print("  - When finished, CLOSE the Chrome window yourself.")
        if ext_path:
            print(f"  (Extension path you entered: {ext_path})")
        print("\nWaiting for you to finish. Once you close the window, setup will continue automatically...")
        # Wait until window is closed (driver should raise on handle access)
        while True:
            try:
                _ = driver.window_handles
                time.sleep(0.75)
            except Exception:
                # window is closed
                break
    except Exception as e:
        print(f"\nError launching Chrome: {e}")
        print("Please ensure Chrome is not running and try again.")
        return

    # Step 4: Save the temporary profile as the new template
    print("\nStep 4: Saving the configured profile as a template...")
    if os.path.exists(TEMPLATE_PROFILE_DIR):
        print("Removing old template directory...")
        shutil.rmtree(TEMPLATE_PROFILE_DIR, ignore_errors=True)
    
    try:
        # Move the temp setup profile to be the permanent template
        shutil.move(temp_setup_profile_dir, TEMPLATE_PROFILE_DIR)
        print("Successfully created template profile!")
    except Exception as e:
        print(f"\nError creating template: {e}")
        return

    # Step 5: Save configuration (merge, don't overwrite user settings)
    save_config({'template_created': True})
    print("\nSetup completed successfully!")
    print("You can now use the bot with your configured template profile.")
    try:
        driver.quit()
    except Exception:
        pass
    print("\n--- Setup Complete! ---")
    print("The bot is now ready to use. Returning to menu...")

# --- Main Bot Logic ---
def get_prompt(account_id):
    """Return the prompt text for a given account.

    Resolution order (first match wins):
      1. prompt/<account_id>.txt               e.g. 2.txt
      2. prompt/account_<account_id>.txt       e.g. account_2.txt
      3. prompt/<account_id>.md | .text        alternate extensions
      4. prompt/default.txt                    fallback
    Returns empty string if nothing found.
    """
    prompts_dir = os.path.join(get_base_path(), 'prompt')
    patterns = [
        f"{account_id}.txt",
        f"account_{account_id}.txt",
        f"{account_id}.md",
        f"{account_id}.text",
    ]
    for fname in patterns:
        path = os.path.join(prompts_dir, fname)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read().strip()
    default_path = os.path.join(prompts_dir, 'default.txt')
    if os.path.exists(default_path):
        with open(default_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read().strip()
    return ""

# --- FewFeed helpers ---
def ff_locate_results_container(driver):
    """Locate the LEFT results container (under the editor), preferring the one with many svg status icons
    and few/no checkboxes (so we avoid the RIGHT selection list).
    Returns the WebElement or None.
    """
    containers = []
    with contextlib.suppress(Exception):
        containers = driver.find_elements(
            By.XPATH,
            "//div[(contains(@class,'overflow-y') or contains(@class,'overflow-auto') or contains(@class,'divide-y') or contains(@class,'rounded'))]"
        )
    if not containers:
        return None
    scored = []
    for c in containers:
        try:
            cb = len(c.find_elements(By.XPATH, ".//input[@type='checkbox']"))
        except Exception:
            cb = 0
        try:
            sv = len(c.find_elements(By.XPATH, ".//*[name()='svg']"))
        except Exception:
            sv = 0
        # Penalize containers that look like the editor toolbar (blue clickable svg icons)
        try:
            toolbar_icons = len(c.find_elements(By.XPATH, ".//*[name()='svg' and contains(@class,'text-blue') and contains(@class,'cursor-pointer')]") )
        except Exception:
            toolbar_icons = 0
        # Higher cb and toolbar penalize; higher sv (status icons) helps
        scored.append((cb + 2*toolbar_icons, -sv, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2] if scored else None
def ff_pulse_results_container_scroll(driver):
    """Lightly scroll the left results container to ensure lazy-loaded rows appear.
    Does NOT scroll the whole window. This keeps UI stable on RDP.
    """
    try:
        containers = []
        try:
            containers = driver.find_elements(
                By.XPATH,
                "//div[(contains(@class,'rounded') or contains(@class,'shadow') or contains(@class,'divide-y') or contains(@class,'overflow-y')) and .//*[name()='svg']]"
            )
        except Exception:
            containers = []
        if not containers:
            return False
        # Choose the one with many svgs and few checkboxes
        scored = []
        for c in containers:
            try:
                cb = len(c.find_elements(By.XPATH, ".//input[@type='checkbox']"))
            except Exception:
                cb = 0
            try:
                sv = len(c.find_elements(By.XPATH, ".//*[name()='svg']"))
            except Exception:
                sv = 0
            scored.append((cb, -sv, c))
        scored.sort(key=lambda x: (x[0], x[1]))
        container = scored[0][2] if scored else None
        if not container:
            return False
        # Pulse: scroll to bottom then back to top
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", container)
        time.sleep(0.05)
        driver.execute_script("arguments[0].scrollTop = 0;", container)
        return True
    except Exception:
        return False
def ff_click_first_result_row(driver) -> bool:
    """Aggressively click the first left-side result row (the list under the editor)
    to arm the global "View Post" button. Returns True if a click was performed.

    Uses strict selectors based on observed DOM in screenshots:
      - stop-relative row
      - a row with span.truncate label and a 7x7 colored svg status icon
      - any visible row under the left results container
    """
    try:
        # Guard 1: if already primed for THIS session, never click again
        if _is_primed(driver):
            return False
        # Guard 2: if View Post is already visible, avoid toggling it off
        with contextlib.suppress(Exception):
            vp = None
            for xp in [
                "//button[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
                "//a[contains(translate(., 'VIEWPOST', 'viewpost'), 'view post')]",
            ]:
                elems = driver.find_elements(By.XPATH, xp)
                vp = next((e for e in elems if e.is_displayed()), None)
                if vp:
                    break
            if vp is not None:
                return False
        # 0) Try the disclosure button (expander) if present
        with contextlib.suppress(Exception):
            btns = driver.find_elements(By.XPATH, "//button[contains(@class,'headlessui-disclosure-button')]")
            btn = next((b for b in btns if b.is_displayed()), None)
            if btn is not None:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.05)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.2)
                dbg("[FF] click-first: toggled disclosure button")

        # 1) Try a row located relative to the Stop button (most stable anchor)
        with contextlib.suppress(Exception):
            stop_btn = driver.find_element(By.XPATH, "//button[normalize-space(.)='Stop']")
            candidate = stop_btn.find_element(By.XPATH, "following::div[contains(@class,'flex') and .//span[contains(@class,'truncate')]][1]")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", candidate)
            time.sleep(0.1)
            # Prefer clicking the truncated label span
            with contextlib.suppress(Exception):
                trunc = candidate.find_element(By.XPATH, ".//span[contains(@class,'truncate')]")
                if trunc and trunc.is_displayed():
                    driver.execute_script("arguments[0].click();", trunc)
                    dbg("[FF] click-first: clicked label span inside results row")
                    _mark_primed(driver)
                    return True
        
        # 2) Try any visible row under the left results container
        with contextlib.suppress(Exception):
            rows = driver.find_elements(By.XPATH, "//div[(contains(@class,'rounded') or contains(@class,'shadow') or contains(@class,'divide-y') or contains(@class,'overflow-y')) and .//*[name()='svg']]//div[contains(@class,'flex') and .//span[contains(@class,'truncate')]]")
            if rows:
                row = rows[0]
                # Click ONLY the label span inside the row to avoid any icon/tool misclick
                with contextlib.suppress(Exception):
                    label = row.find_element(By.XPATH, ".//span[contains(@class,'truncate')]")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label)
                    time.sleep(0.1)
                    driver.execute_script("arguments[0].click();", label)
                    dbg("[FF] click-first: clicked label span inside results row")
                    _mark_primed(driver)
                    return True
                # If no label span, DO NOT click generic area (to avoid toolbar). Skip safely.
                dbg("[FF] click-first: no label span found in row; skipping click to avoid misclick")
                return False
        
        return False
    except Exception:
        pass
    return False

def set_clipboard_text(text: str) -> bool:
    """Copy text (including multiline) to the Windows clipboard.
    Tries pyperclip first, then win32clipboard. Returns True on success.
    """
    # Try pyperclip if available (works well with Unicode and multiline)
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    # Fallback to win32clipboard
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            # CF_UNICODETEXT expects UTF-16-LE ending with double null
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False

def trigger_extension_with_retries(driver, account_id, max_attempts=None, delay=None):
    """Trigger FewFeed extension via Ctrl+Shift+F with retries and fallback to Facebook if popup appears."""
    attempts = max_attempts or int(config.get('extension_retry_attempts', 5))
    wait_delay = delay or float(config.get('extension_retry_delay', 2))
    # If not in hotkey mode, do not attempt to trigger via keyboard at all
    if str(config.get('extension_trigger_mode', 'web')).lower() != 'hotkey':
        acc_log(account_id, "Skip extension trigger: running in web mode", silent=True)
        return False
    
    for attempt in range(attempts):
        acc_log(account_id, f"Extension trigger attempt {attempt + 1}/{attempts}", silent=True)
        
        # Check if popup is present
        def popup_present(timeout=2):
            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(., 'install Chrome Extension') or contains(., 'Install Chrome Extension')]"))
                )
                return True
            except Exception:
                return False
        
        # If popup present, go to Facebook first
        if popup_present():
            acc_log(account_id, "Extension popup detected, going to Facebook first", silent=True)
            try:
                driver.get("https://facebook.com")
                time.sleep(retry_delay)
            except Exception:
                pass
        
        # Trigger extension via Ctrl+Shift+F
        try:
            driver.find_element(By.TAG_NAME, 'body').click()
            driver.execute_script("window.focus();")
            time.sleep(0.5)
            pyautogui.hotkey('ctrl', 'shift', 'f')
            time.sleep(retry_delay)
            
            # Check if FewFeed page opened successfully
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: "fewfeed" in d.current_url.lower() or 
                             len(d.find_elements(By.XPATH, "//*[contains(text(), 'FewFeed') or contains(text(), 'Auto-Post')]")) > 0
                )
                acc_log(account_id, "Extension triggered successfully", silent=True)
                return True
            except Exception:
                pass
                
        except Exception as e:
            acc_log(account_id, f"Extension trigger error: {e}", silent=True)
    
    acc_log(account_id, "Failed to trigger extension after all attempts", silent=True)
    return False

def click_green_icon_robust(driver, account_id, timeout=5, pause=0.2):
    """Click the green image icon reliably using multiple strategies and JS events."""
    selectors = [
        (By.XPATH, "//button[.//svg[contains(@class,'text-green-400')]]"),
        (By.CSS_SELECTOR, ".text-green-400 svg"),
        (By.CSS_SELECTOR, "svg.text-green-400"),
    ]
    end_time = time.time() + timeout
    last_err = None
    while time.time() < end_time:
        for by, sel in selectors:
            try:
                elems = driver.find_elements(by, sel)
                if not elems:
                    continue
                el = elems[0]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                # Prefer clicking a button ancestor if present
                btn = None
                try:
                    btn = el.find_element(By.XPATH, "ancestor::button[1]")
                except Exception:
                    btn = None
                target = btn or el
                # Ensure clickable: remove overlay issues and try JS-driven events
                driver.execute_script("arguments[0].style.pointerEvents='auto';", target)
                try:
                    WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, ".")))
                except Exception:
                    pass
                try:
                    driver.execute_script("arguments[0].click();", target)
                except Exception:
                    # dispatch mouse events if normal click blocked
                    driver.execute_script(
                        "var e=new MouseEvent('mousedown',{bubbles:true});arguments[0].dispatchEvent(e);",
                        target,
                    )
                    driver.execute_script(
                        "var e=new MouseEvent('mouseup',{bubbles:true});arguments[0].dispatchEvent(e);",
                        target,
                    )
                    driver.execute_script(
                        "var e=new MouseEvent('click',{bubbles:true});arguments[0].dispatchEvent(e);",
                        target,
                    )
                time.sleep(pause)
                acc_log(account_id, "Clicked green image icon", silent=True)
                return True
            except Exception as e:
                last_err = e
                continue
        time.sleep(0.2)
    if last_err:
        acc_log(account_id, f"Green icon click failed: {last_err}", silent=True)
    else:
        acc_log(account_id, "Green icon not found", silent=True)
    return False

def fewfeed_login(driver, step_sleep=2, timeout=40):
    """Actively navigate to the FewFeed sign-in page, log in, then wait for
    the homepage to reload with 'Use this tool' buttons visible.

    Exact flow (mirrors what the user described):
      1. Navigate to https://fewfeed.online/api/auth/signin?callbackUrl=https%3A%2F%2Ffewfeed.online%2F
      2. Wait for the Email + Password form to appear.
      3. Fill email (dinkskalr@gmail.com) and password (vitorix2024...).
      4. Click 'Sign In'.
      5. Wait for 'Login successful! Redirecting...' banner.
      6. Wait for the homepage (fewfeed.online) to reload.
      7. Wait for 'Use this tool' buttons — signals FB session recognised.

    Returns True when fully ready, False on any failure.
    """
    FEWFEED_EMAIL    = "dinkskalr@gmail.com"
    FEWFEED_PASSWORD = "vitorix2024..."
    SIGNIN_URL       = "https://fewfeed.online/api/auth/signin?callbackUrl=https%3A%2F%2Ffewfeed.online%2F"

    def _wait_ready(t=10):
        try:
            WebDriverWait(driver, t).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

    def _tools_ready(t=None, reload_on_timeout=True, wait_seconds=5, max_restarts=3):
        """Return True once 'Use this tool' buttons are clickable on the homepage.
        If button doesn't appear within wait_seconds, reload page and try again.
        If upgrade page appears, navigate back to fewfeed.online and re-run the
        full login flow (go-back restart, not just a page load).
        Waits indefinitely until the button appears or max_restarts exceeded."""
        import time
        start_time = time.time()
        reload_count = 0
        restart_count = 0

        while True:
            # Check for upgrade page first
            if _is_upgrade_page():
                restart_count += 1
                if restart_count > max_restarts:
                    dbg(f"[FF-Login] Upgrade page appeared {max_restarts} times, giving up.")
                    return False
                dbg(f"[FF-Login] Upgrade page detected (attempt #{restart_count}/{max_restarts})! "
                    f"Going back to fewfeed.online and re-running login...")
                try:
                    # Use browser back first (mirrors clicking the Back button visible on the upgrade page)
                    try:
                        driver.back()
                        time.sleep(1.5)
                    except Exception:
                        pass
                    # If back() still left us on the upgrade/checkout page, navigate explicitly
                    if _is_upgrade_page():
                        driver.get("https://fewfeed.online")
                        time.sleep(2)
                    # Re-run the full sign-in flow so the session is restored properly
                    try:
                        _wait_ready(t=8)
                        # If not already showing tools, go through sign-in again
                        if not _already_logged_in():
                            dbg("[FF-Login] Re-running sign-in after upgrade page redirect...")
                            driver.get(SIGNIN_URL)
                            _wait_ready(t=10)
                            # Fill credentials again
                            WebDriverWait(driver, 15).until(
                                EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
                            )
                            email_input = WebDriverWait(driver, 8).until(
                                EC.presence_of_element_located(
                                    (By.XPATH,
                                     "//input[@type='email' or @name='email']"
                                     "|//input[contains(translate(@placeholder,'EMAIL','email'),'email')]")
                                )
                            )
                            email_input.clear()
                            email_input.send_keys(FEWFEED_EMAIL)
                            time.sleep(0.3)
                            pass_input = driver.find_element(By.XPATH, "//input[@type='password']")
                            pass_input.clear()
                            pass_input.send_keys(FEWFEED_PASSWORD)
                            time.sleep(0.3)
                            sign_in_btn = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable(
                                    (By.XPATH,
                                     "//button[contains(translate(normalize-space(.), 'SIGNIN', 'signin'), 'sign in')]"
                                     "|//button[@type='submit']")
                                )
                            )
                            driver.execute_script("arguments[0].click();", sign_in_btn)
                            dbg("[FF-Login] Sign In button clicked (upgrade-page recovery).")
                            # Wait for redirect back to homepage
                            try:
                                WebDriverWait(driver, 20).until(
                                    lambda d: (
                                        "fewfeed.online" in (d.current_url or "").lower()
                                        and "signin" not in (d.current_url or "").lower()
                                        and "login" not in (d.current_url or "").lower()
                                    )
                                )
                                _wait_ready(t=8)
                            except Exception:
                                pass
                    except Exception as re_err:
                        dbg(f"[FF-Login] Re-login after upgrade page failed: {re_err}")
                    # Check if we now see the tools
                    try:
                        WebDriverWait(driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Use this tool')]"))
                        )
                        dbg("[FF-Login] 'Use this tool' button found after upgrade-page recovery.")
                        return True
                    except Exception:
                        # Still no button, continue the loop
                        continue
                except Exception as e:
                    dbg(f"[FF-Login] Error during upgrade-page recovery: {e}")
                    time.sleep(1)
                    continue

            try:
                # Check for the button with the specified wait time
                WebDriverWait(driver, wait_seconds).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Use this tool')]")
                    )
                )
                if reload_count > 0:
                    dbg(f"[FF-Login] 'Use this tool' button appeared after {reload_count} reload(s).")
                return True
            except Exception:
                # Button didn't appear within wait_seconds, reload and try again
                if reload_on_timeout:
                    reload_count += 1
                    dbg(f"[FF-Login] 'Use this tool' button not found after {wait_seconds}s, reloading page (attempt #{reload_count})...")
                    try:
                        current_url = driver.current_url
                        # Only reload if we're on fewfeed.online
                        if "fewfeed.online" in current_url.lower():
                            driver.refresh()
                            dbg("[FF-Login] Page reloaded, waiting again...")
                            time.sleep(1)  # Small delay after reload
                        else:
                            # Not on fewfeed, navigate there
                            dbg("[FF-Login] Not on fewfeed.online, navigating there...")
                            driver.get("https://fewfeed.online")
                            time.sleep(1)
                    except Exception as e:
                        dbg(f"[FF-Login] Error during reload: {e}")
                        # Check if driver is still alive
                        try:
                            _ = driver.current_url
                        except Exception:
                            return False  # Driver died, stop waiting
                        time.sleep(1)
                else:
                    # No reload, just check if we should stop
                    if t is not None and (time.time() - start_time) >= t:
                        return False
                    # Check if driver is still alive
                    try:
                        _ = driver.current_url
                    except Exception:
                        return False
                    time.sleep(0.5)

    def _already_logged_in():
        """Return True if we're already on homepage with tools ready (no login needed)."""
        try:
            cur = (driver.current_url or "").lower()
            if "fewfeed.online" in cur and "signin" not in cur and "login" not in cur:
                elems = driver.find_elements(By.XPATH, "//button[contains(., 'Use this tool')]")
                return len(elems) > 0
        except Exception:
            pass
        return False

    def _is_upgrade_page():
        """Return True if currently on the 'Upgrade Your Plan' page."""
        try:
            cur = (driver.current_url or "").lower()
            if "upgrade" in cur or "checkout" in cur or "member/checkouts" in cur:
                # Also check for page content to confirm
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    if "upgrade your plan" in page_text or "basic" in page_text or "professional" in page_text:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    # ── Skip if already logged in ──
    if _already_logged_in():
        dbg("[FF-Login] Already logged in — 'Use this tool' visible, skipping login.")
        return True

    dbg("[FF-Login] Navigating to FewFeed sign-in page...")
    try:
        driver.get(SIGNIN_URL)
        _wait_ready(t=10)
    except Exception as e:
        dbg(f"[FF-Login] Failed to navigate to sign-in page: {e}")
        return False

    # ── Step 1: Wait for the login form ──
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
        )
    except Exception:
        dbg("[FF-Login] Login form did not appear after navigating to sign-in URL.")
        return False

    try:
        # ── Step 2: Fill email ──
        email_input = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//input[@type='email' or @name='email']"
                 "|//input[contains(translate(@placeholder,'EMAIL','email'),'email')]")
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", email_input)
        time.sleep(0.2)
        email_input.clear()
        email_input.send_keys(FEWFEED_EMAIL)
        time.sleep(0.3)
        dbg(f"[FF-Login] Email entered: {FEWFEED_EMAIL}")

        # ── Step 3: Fill password ──
        pass_input = driver.find_element(By.XPATH, "//input[@type='password']")
        pass_input.clear()
        pass_input.send_keys(FEWFEED_PASSWORD)
        time.sleep(0.3)
        dbg("[FF-Login] Password entered.")

        # ── Step 4: Click 'Sign In' button ──
        sign_in_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 "//button[contains(translate(normalize-space(.), 'SIGNIN', 'signin'), 'sign in')]"
                 "|//button[@type='submit']")
            )
        )
        driver.execute_script("arguments[0].click();", sign_in_btn)
        dbg("[FF-Login] Sign In button clicked.")

        # ── Step 5: Wait for 'Login successful! Redirecting...' banner ──
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH,
                     "//*[contains(translate(., 'LOGINSUCCESSFUL', 'loginsuccessful'), 'login successful')]"
                     "|//*[contains(normalize-space(.), 'Redirecting')]")
                )
            )
            dbg("[FF-Login] 'Login successful! Redirecting...' banner detected.")
        except Exception:
            dbg("[FF-Login] Warning: success banner not seen — continuing anyway.")

        # ── Step 6: Wait for redirect back to fewfeed.online homepage ──
        try:
            WebDriverWait(driver, 20).until(
                lambda d: (
                    "fewfeed.online" in (d.current_url or "").lower()
                    and "signin" not in (d.current_url or "").lower()
                    and "login" not in (d.current_url or "").lower()
                )
            )
            _wait_ready(t=8)
            dbg(f"[FF-Login] Redirected to homepage: {driver.current_url}")
        except Exception:
            dbg("[FF-Login] Warning: redirect to homepage timed out — continuing anyway.")

        # ── Step 7: Wait for 'Use this tool' buttons (FB session recognised) ──
        # Wait with automatic page reload if button doesn't appear within 5 seconds
        # Also handles upgrade page by restarting the process (max 3 restarts)
        dbg("[FF-Login] Waiting for 'Use this tool' buttons to appear (will reload/restart if not found)...")
        if _tools_ready(t=None, reload_on_timeout=True, wait_seconds=5, max_restarts=3):
            dbg("[FF-Login] FewFeed login complete — 'Use this tool' buttons are ready.")
            return True

        dbg("[FF-Login] Stopped waiting for 'Use this tool' buttons (driver closed or error).")
        return False

    except Exception as e:
        dbg(f"[FF-Login] Error during FewFeed login: {e}")
        return False


def handle_extension_popup(driver, step_sleep=2, mode: str = "web", max_retries=3):
    """Detects the 'You need to install Chrome Extension' modal and applies recovery:
    1) Navigate to Facebook, then back to FewFeed homepage.
    2) Retry until popup disappears and "Use this tool" buttons are clickable.
    3) In hotkey mode only: try Ctrl+Shift+F as last resort.
    """
    def popup_present(timeout=2):
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(., 'install Chrome Extension') or contains(., 'Install Chrome Extension')]"))
            )
            return True
        except Exception:
            return False
    
    def tools_ready(timeout=None, reload_on_timeout=True, wait_seconds=5):
        """Wait indefinitely (or with specified timeout) for 'Use this tool' button.
        If button doesn't appear within wait_seconds, reload page and try again.
        If upgrade page appears, navigate back to fewfeed.online."""
        import time
        start_time = time.time()
        reload_count = 0

        def _is_upgrade_page():
            """Return True if currently on the 'Upgrade Your Plan' page."""
            try:
                cur = (driver.current_url or "").lower()
                if "upgrade" in cur or "checkout" in cur or "member/checkouts" in cur:
                    try:
                        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                        if "upgrade your plan" in page_text or "basic" in page_text:
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
            return False

        while True:
            # Check for upgrade page first
            if _is_upgrade_page():
                dbg("[handle_extension_popup] Upgrade page detected! Using Back then re-running fewfeed login...")
                try:
                    # Mirror pressing the Back button that is visible on the upgrade page
                    try:
                        driver.back()
                        time.sleep(1.5)
                    except Exception:
                        pass
                    # If still on upgrade page after back(), navigate directly
                    if _is_upgrade_page():
                        driver.get("https://fewfeed.online")
                        time.sleep(2)
                    # Re-run the full FewFeed login so credentials are re-submitted
                    try:
                        fewfeed_login(driver, step_sleep=2, timeout=40)
                    except Exception as re_err:
                        dbg(f"[handle_extension_popup] Re-login after upgrade page failed: {re_err}")
                except Exception as e:
                    dbg(f"[handle_extension_popup] Error during upgrade-page recovery: {e}")
                    time.sleep(1)
                continue

            try:
                WebDriverWait(driver, wait_seconds).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Use this tool')]"))
                )
                if reload_count > 0:
                    dbg(f"[handle_extension_popup] 'Use this tool' button appeared after {reload_count} reload(s).")
                return True
            except Exception:
                if reload_on_timeout:
                    reload_count += 1
                    dbg(f"[handle_extension_popup] 'Use this tool' button not found after {wait_seconds}s, reloading page (attempt #{reload_count})...")
                    try:
                        current_url = driver.current_url
                        if "fewfeed.online" in current_url.lower():
                            driver.refresh()
                            time.sleep(1)
                        else:
                            driver.get("https://fewfeed.online")
                            time.sleep(1)
                    except Exception as e:
                        dbg(f"[handle_extension_popup] Error during reload: {e}")
                        try:
                            _ = driver.current_url
                        except Exception:
                            return False
                        time.sleep(1)
                else:
                    if timeout is not None and (time.time() - start_time) >= timeout:
                        return False
                    try:
                        _ = driver.current_url
                    except Exception:
                        return False
                    time.sleep(0.5)

    # Retry loop to handle persistent popup - but keep waiting for tools indefinitely
    for attempt in range(max_retries):
        if not popup_present():
            # No popup, check if tools are ready (wait indefinitely)
            if tools_ready():
                return True
            # Tools not ready but no popup, wait a bit and retry
            time.sleep(step_sleep)
            continue

        # Popup present, do Facebook bounce
        try:
            driver.get("https://www.facebook.com/settings")
            time.sleep(step_sleep)
            # Return to FewFeed after Facebook bounce
            driver.get("https://fewfeed.online")
            # Wait for page to load completely
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(step_sleep)
        except Exception:
            pass

        # Wait for popup to disappear and tools to be ready (indefinite wait)
        while True:
            if not popup_present(timeout=1) and tools_ready(timeout=2, reload_on_timeout=False):
                return True
            # Check if driver is still alive
            try:
                _ = driver.current_url
            except Exception:
                return False
            time.sleep(0.5)
    
    # If still blocked after retries, optionally try hotkey (only when explicitly requested)
    if mode == "hotkey" and popup_present(timeout=2):
        try:
            try:
                driver.find_element(By.TAG_NAME, 'body').click()
            except Exception:
                pass
            driver.execute_script("window.focus();")
            time.sleep(0.3)
            pyautogui.hotkey('ctrl', 'shift', 'f')
            time.sleep(step_sleep)
        except Exception:
            pass
    
    # Final check
    return not popup_present(timeout=1) and tools_ready(timeout=2)


def automate_fewfeed(driver, account_id, assume_page_loaded=False, silent=False, detach_after_post=False, _restart_attempt=0, _max_restarts=2):
    # clear primed state so the bot knows to click the first row again on restart
    _clear_primed(driver)
    
    # set thread-local account context for dbg()
    try:
        _thread_ctx.account_id = account_id
    except Exception:
        pass
    acc_log(account_id, "Starting FewFeed automation...", silent=True)
    """Automate FewFeed Auto-Post.

    If assume_page_loaded=True, do NOT navigate; we expect the extension
    to have already opened the tool page and this function just fills and posts.
    """
    # Background-safe navigation by default (no global hotkeys needed)
    if not assume_page_loaded:
        mode = str(config.get('extension_trigger_mode', 'web')).lower()
        if mode == 'hotkey':
            acc_log(account_id, "Triggering FewFeed extension via hotkey...", silent=True)
            success = trigger_extension_with_retries(driver, account_id)
            if not success:
                acc_log(account_id, "Failed to trigger extension after retries", silent=True)
                return
        else:
            # Web mode: open FewFeed in a new background tab and switch to it
            try:
                if not open_tool_tab(driver):
                    acc_log(account_id, "Failed to open FewFeed tool tab", silent=True)
                    return
                time.sleep(max(1, config.get('step_delay', 2)))
                handle_extension_popup(driver, step_sleep=config.get('step_delay', 2), mode='web')
            except Exception as e:
                acc_log(account_id, f"Navigation error: {e}", silent=True)
                return

    try:
        # ── Guard: if the upgrade/checkout page appeared before we could act, go back ──
        def _on_upgrade_page():
            try:
                cur = (driver.current_url or "").lower()
                if "upgrade" in cur or "checkout" in cur or "member/checkouts" in cur:
                    try:
                        body = driver.find_element(By.TAG_NAME, "body").text.lower()
                        return "upgrade your plan" in body or "basic" in body or "professional" in body
                    except Exception:
                        return True  # URL alone is strong enough signal
            except Exception:
                pass
            return False

        if _on_upgrade_page():
            acc_log(account_id, "[automate_fewfeed] Upgrade page detected at automation start — going back and re-logging in...", silent=silent)
            try:
                driver.back()
                time.sleep(1.5)
            except Exception:
                pass
            if _on_upgrade_page():
                driver.get("https://fewfeed.online")
                time.sleep(2)
            try:
                fewfeed_login(driver, step_sleep=2, timeout=40)
            except Exception as e:
                acc_log(account_id, f"[automate_fewfeed] Re-login after upgrade page: {e}", silent=silent)

        # Ensure we are on the FewFeed homepage (not deep inside a tool page) before clicking
        try:
            cur = (driver.current_url or "").lower()
            if "fewfeed.online" not in cur:
                driver.get("https://fewfeed.online")
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(1)
        except Exception:
            pass

        # Click "Use this tool" if present (extension may already open the tool page)
        try:
            btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "(//button[contains(., 'Use this tool')])[1]"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.3)
            btn.click()
            acc_log(account_id, "Clicked 'Use this tool' — opening Auto Post tool...", silent=silent)
        except Exception:
            pass

        # Wait for text area
        text_box = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, "//textarea|//div[@contenteditable='true']")))
        prompt_text = get_prompt(account_id)
        # Put prompt into clipboard and paste normally (Ctrl+V)
        pasted = False
        try:
            if set_clipboard_text(prompt_text):
                try:
                    # Click into the editor to focus
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", text_box)
                    text_box.click()
                except Exception:
                    pass
                ActionChains(driver).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
                pasted = True
        except Exception as e:
            acc_log(account_id, f"Clipboard paste failed: {e}", silent=silent)
        if not pasted:
            # Fallback to send_keys if clipboard failed
            try:
                text_box.clear()
            except Exception:
                pass
            try:
                text_box.send_keys(prompt_text)
            except Exception as e:
                acc_log(account_id, f"Failed to insert prompt text: {e}", silent=silent)
        # configurable pause after typing
        step_sleep = config.get('step_delay', 1)
        time.sleep(step_sleep)

        # Set thread and delay values
        try:
            num_inputs = driver.find_elements(By.XPATH, "//input[@type='number']")
            if len(num_inputs) >= 2:
                num_inputs[0].clear(); num_inputs[0].send_keys(str(config.get('thread_value',1)))
                num_inputs[1].clear(); num_inputs[1].send_keys(str(config.get('delay_value',3)))
                time.sleep(step_sleep)
            else:
                acc_log(account_id, "Could not find thread/delay inputs.", silent=silent)
        except Exception as e:
            acc_log(account_id, f"Error setting thread/delay: {e}", silent=silent)

        # Select all groups checkbox (top-left)
        try:
            select_all = WebDriverWait(driver,10).until(
                EC.element_to_be_clickable((By.XPATH, "(//input[@type='checkbox'])[1]"))
            )
            driver.execute_script("arguments[0].click();", select_all)
            time.sleep(step_sleep)
        except Exception:
            acc_log(account_id, "Could not select all groups checkbox.", silent=silent)

        # Exclude groups already posted (Google Sheets per account)
        try:
            if _gs_enabled():
                already_posted_ids = gs_fetch_posted_groups(account_id)
                if already_posted_ids:
                    acc_log(account_id, f"Excluding {len(already_posted_ids)} groups from Google Sheets...", silent=silent)
                    ff_uncheck_excluded_groups(driver, already_posted_ids, step_sleep=step_sleep)
        except Exception as e:
            acc_log(account_id, f"Exclusion step error: {e}", silent=silent)

        if config.get('post_with_images'):
            try:
                # Fast image paste with customizable speed
                images_path = config.get('images_path', '')
                if images_path and os.path.exists(images_path):
                    acc_log(account_id, f"Loading images from: {images_path}", silent=True)
                    # Guard: only paste once per account per session
                    if account_id not in pasted_images_accounts:
                        try:
                            files = ensure_clipboard_loaded_for_folder(images_path, force=True)
                            acc_log(account_id, f"Loaded {len(files)} images to clipboard", silent=True)
                            pasted_images_accounts.add(account_id)
                        except Exception as e:
                            acc_log(account_id, f"Could not load images: {e}", silent=True)
                    else:
                        acc_log(account_id, "Images already loaded for this account this session", silent=True)

                    # Fast image paste - single attempt with customizable speed
                    paste_speed = config.get('image_paste_speed', config.get('image_paste_delay', 0.3))
                    try:
                        # Paste into a focused editable element using WebDriver (works in background)
                        editable = driver.find_element(By.XPATH, "//textarea|//div[@contenteditable='true']")
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", editable)
                        editable.click()
                        time.sleep(paste_speed)
                        # Ensure images list is on clipboard (text ops might have overwritten it)
                        try:
                            ensure_clipboard_loaded_for_folder(images_path, force=True)
                        except Exception:
                            pass
                        editable.send_keys(Keys.CONTROL + 'v')
                        time.sleep(paste_speed)
                        acc_log(account_id, "Images pasted", silent=True)
                    except Exception as e:
                        acc_log(account_id, f"Error pasting images: {e}", silent=True)

                    # Fast green icon click - single attempt
                    try:
                        if not click_green_icon_robust(driver, account_id, timeout=3, pause=paste_speed):
                            acc_log(account_id, "Green image icon not found or not clickable", silent=True)
                    except Exception as e:
                        acc_log(account_id, f"Error clicking green icon: {e}", silent=True)
                else:
                    acc_log(account_id, "Images path not configured or doesn't exist", silent=True)
            except Exception as e:
                acc_log(account_id, f"Image posting error: {e}", silent=True)

        # thread/delay
        try:
            driver.find_element(By.XPATH, "//input[@placeholder='THREADS' or @name='threads']").clear()
            driver.find_element(By.XPATH, "//input[@placeholder='THREADS' or @name='threads']").send_keys(str(config.get('thread_value', 3)))
            driver.find_element(By.XPATH, "//input[@placeholder='DELAY' or @name='delay']").clear()
            driver.find_element(By.XPATH, "//input[@placeholder='DELAY' or @name='delay']").send_keys(str(config.get('delay_value', 5)))
        except Exception:
            pass

        # select all groups checkbox
        try:
            driver.find_element(By.XPATH, "//input[@type='checkbox' and @value='all' or @id='select-all']").click()
        except Exception:
            pass

        # Click Post
        try:
            post_btn = driver.find_element(By.XPATH, "//button[normalize-space()='Post']")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_btn)
            post_btn.click()
            acc_log(account_id, "FewFeed Post button clicked.", silent=silent)
            time.sleep(step_sleep)
            acc_log(account_id, "FewFeed posting triggered. Waiting for completion...", silent=True)
            # Optional: wait for success toast (if present)
            try:
                WebDriverWait(driver,20).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Posting started') or contains(text(),'successfully')]"))
                )
            except Exception:
                pass
            acc_log(account_id, "FewFeed automation finished.", silent=True)
            # Immediately activate first success row to arm the "View Post" button
            try:
                # Try repeatedly for a few seconds so we click as soon as the first result appears
                t0 = time.time()
                primed = False
                while time.time() - t0 < 20:
                    # First try the strict direct-click helper
                    if ff_click_first_result_row(driver):
                        primed = True
                        break
                    # Fallback to extractor that also tries multiple activation paths
                    _ = ff_get_post_results(driver, max_items=1)
                    if getattr(ff_get_post_results, "_hard_row_activated", False) or getattr(ff_get_post_results, "_activated_by_card", False):
                        primed = True
                        break
                    time.sleep(0.4)
                if primed:
                    acc_log(account_id, "Primed results: clicked first success row to show 'View Post'", silent=True)
                else:
                    acc_log(account_id, "Priming results: no success row yet within 20s", silent=True)
            except Exception as e:
                acc_log(account_id, f"Priming results failed: {e}", silent=True)
            # Append successes to Google Sheets based on LEFT results list (blue/yellow/green only)
            # Shared event: signals main thread that watcher needs a restart.
            # Must be defined before _watch_results_loop so the closure can reference it.
            _upgrade_restart_needed = threading.Event()

            def _watch_results_loop():
                try:
                    if SHUTTING_DOWN.is_set():
                        return
                    # If marked recovering but driver is actually logged in, clear flag and continue
                    try:
                        if ACCOUNT_STATE["recovering"].get(int(account_id)):
                            if not fb_logged_out(driver):
                                acc_log(account_id, "Recovery flag cleared: session appears logged in.", silent=True)
                                _clear_recovering(account_id)
                    except Exception:
                        pass

                    if _gs_enabled():
                        # Config-driven continuous watch
                        cfg_local = load_config()
                        post_watch_seconds = int(cfg_local.get('post_watch_seconds', 600))
                        continuous_watch = bool(cfg_local.get('continuous_watch', True))
                        # Enhanced background tracking settings
                        background_check_interval = int(cfg_local.get('background_check_interval', 5))
                        max_consecutive_failures = int(cfg_local.get('max_consecutive_failures', 10))

                        session_seen = set()  # group IDs we already appended this run
                        already_sheet = gs_fetch_posted_groups(account_id)  # existing group IDs in sheet
                        first_seen = {}  # group_id -> timestamp when first observed as success
                        min_age = int(cfg_local.get('results_min_age', 15))
                        consecutive_failures = 0  # track failed detection attempts

                        def _stop_present():
                            try:
                                return len(driver.find_elements(By.XPATH, "//button[normalize-space()='Stop']")) > 0
                            except Exception:
                                return False

                        def _is_upgrade_page():
                            """Return True if currently on the 'Upgrade Your Plan' page."""
                            try:
                                cur = (driver.current_url or "").lower()
                                if "upgrade" in cur or "checkout" in cur or "member/checkouts" in cur:
                                    try:
                                        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                                        if "upgrade your plan" in page_text or "basic" in page_text:
                                            return True
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            return False

                        start = time.time()
                        empty_cycles = 0  # count consecutive cycles with no rows
                        attempt = 0
                        upgrade_restart_count = 0
                        max_upgrade_restarts = 3
                        while True:
                            if SHUTTING_DOWN.is_set():
                                break
                            # Stop scanning if account is recovering (logout detected)
                            if is_account_recovering(account_id):
                                acc_log(account_id, "Watcher exiting due to recovery in progress.", silent=True)
                                break
                            attempt += 1
                            # Use configurable interval for background checks
                            time.sleep(background_check_interval)
                            # Check for upgrade page and restart immediately if detected
                            if _is_upgrade_page():
                                acc_log(account_id, "Posting was interrupted by upgrade page, restarting bot immediately...", silent=True)
                                raise InterruptedError("Posting interrupted by upgrade page, needs restart")
                            # Enhanced background-safe page interaction
                            try:
                                # Keep background interactions minimal to avoid visual scrolling/reloading
                                # Only sanity-check page readiness; no scrolling/back/refresh
                                with contextlib.suppress(Exception):
                                    driver.execute_script("return document.readyState;")
                            except Exception as e:
                                acc_log(account_id, f"Background page interaction failed: {e}", silent=True)
                                # Try to recover by refreshing if page becomes unresponsive
                                try:
                                    driver.refresh()
                                    time.sleep(3)
                                except Exception:
                                    pass
                            # Enhanced background-safe results detection with better error handling
                            try:
                                # Ensure we're on the correct tab before scanning
                                current_url = driver.current_url.lower()
                                if 'fewfeed' not in current_url:
                                    # Try to find and switch to FewFeed tab
                                    for handle in driver.window_handles:
                                        try:
                                            driver.switch_to.window(handle)
                                            if 'fewfeed' in driver.current_url.lower():
                                                break
                                        except Exception:
                                            continue
                                
                                # Nudge only before first priming; once primed, never click group again
                                try:
                                    if not _is_primed(driver):
                                        if ff_click_first_result_row(driver):
                                            time.sleep(0.2)
                                except Exception:
                                    pass
                                # Every few cycles, pulse-scroll the results container to load new rows (no page scroll)
                                try:
                                    if attempt % 3 == 0:
                                        ff_pulse_results_container_scroll(driver)
                                except Exception:
                                    pass
                                # Ensure results container is present for this session before scanning
                                try:
                                    if not ff_results_container_ready(driver, timeout=2):
                                        # As a last local nudge: click-first once if not primed, and pulse again
                                        if not _is_primed(driver):
                                            ff_click_first_result_row(driver)
                                        ff_pulse_results_container_scroll(driver)
                                except Exception:
                                    pass
                                current = ff_get_post_results(driver)
                                consecutive_failures = 0  # Reset failure counter on success
                                
                                # Log progress for background monitoring
                                if current:
                                    acc_log(account_id, f"Detected {len(current)} successful posts in results", silent=True)
                                    empty_cycles = 0
                                else:
                                    empty_cycles += 1
                                    # Light recovery only before priming; otherwise do nothing to avoid toggling
                                    if not _is_primed(driver) and empty_cycles in (3, 6, 9):
                                        with contextlib.suppress(Exception):
                                            ff_click_first_result_row(driver)
                                            _ = _ensure_view_post_ready(max_wait=2.0)
                                    
                            except Exception as e:
                                consecutive_failures += 1
                                acc_log(account_id, f"Results detection failed (attempt {consecutive_failures}): {e}", silent=True)
                                current = []
                                # If too many consecutive failures, try to recover
                                if consecutive_failures >= max_consecutive_failures:
                                    acc_log(account_id, f"Too many detection failures ({consecutive_failures}), attempting page recovery...", silent=True)
                                    try:
                                        # Try to recover by switching back to the FewFeed tab
                                        for handle in driver.window_handles:
                                            driver.switch_to.window(handle)
                                            if 'fewfeed' in driver.current_url.lower():
                                                acc_log(account_id, "Switched back to FewFeed tab for recovery", silent=True)
                                                break
                                        # Reset the page state and try to refresh if needed
                                        driver.execute_script("window.focus(); document.body.focus();")
                                        time.sleep(1)
                                        # If still failing, try a gentle refresh
                                        if consecutive_failures >= max_consecutive_failures * 2:
                                            acc_log(account_id, "Attempting page refresh for recovery", silent=True)
                                            driver.refresh()
                                            time.sleep(3)
                                        consecutive_failures = 0
                                    except Exception as recovery_e:
                                        acc_log(account_id, f"Page recovery failed: {recovery_e}", silent=True)
                            
                            now = time.time()
                            # update first_seen times for currently visible successes
                            for group_id, post_url in current:
                                if group_id not in first_seen:
                                    first_seen[group_id] = now
                            # compute candidates: present long enough, not yet appended, not in sheet
                            candidates = []
                            queued = []
                            for group_id, post_url in current:
                                if group_id in session_seen or group_id in already_sheet:
                                    continue
                                age = now - first_seen.get(group_id, now)
                                if age >= min_age:
                                    candidates.append((group_id, post_url))
                                else:
                                    queued.append((group_id, int(min_age - age)))
                            if queued and not SHUTTING_DOWN.is_set():
                                # show up to 3 queued items with remaining seconds
                                q_preview = ", ".join([f"{gid}({sec}s)" for gid, sec in queued[:3]])
                                acc_log(account_id, f"Waiting to confirm: {q_preview}{'...' if len(queued)>3 else ''}", silent=True)
                            # If we already got real URLs, append immediately (no waiting age)
                            immediate = [(gid, url) for gid, url in current if url and gid not in session_seen and gid not in already_sheet]
                            if immediate and not SHUTTING_DOWN.is_set():
                                ok = gs_append_post_success(account_id, immediate)
                                if ok:
                                    group_ids = [gid for gid, url in immediate]
                                    acc_log(account_id, f"Appended {len(immediate)} immediate group(s): {group_ids[:3]}{'...' if len(immediate)>3 else ''}", silent=True)
                                    session_seen.update(group_ids)
                                    already_sheet.update(group_ids)
                                    save_telemetry("FewFeed", account_id, status="Running", stats={"posts": len(session_seen)})
                                else:
                                    acc_log(account_id, f"Failed to append {len(immediate)} immediate group(s) this attempt.", silent=True)

                            if candidates and not SHUTTING_DOWN.is_set():
                                ok = gs_append_post_success(account_id, candidates)
                                if ok:
                                    group_ids = [gid for gid, url in candidates]
                                    acc_log(account_id, f"Appended {len(candidates)} confirmed group(s): {group_ids[:3]}{'...' if len(candidates)>3 else ''}", silent=True)
                                    session_seen.update(group_ids)
                                    already_sheet.update(group_ids)
                                    save_telemetry("FewFeed", account_id, status="Running", stats={"posts": len(session_seen)})
                                else:
                                    acc_log(account_id, f"Failed to append {len(candidates)} confirmed group(s) this attempt.", silent=True)
                            else:
                                acc_log(account_id, f"Results attempt #{attempt}: no new groups (total_seen={len(session_seen)})", silent=True)

                            # Enhanced exit conditions for better background operation
                            elapsed = time.time() - start
                            if continuous_watch:
                                # Check if posting is still active by looking for Stop button
                                posting_active = _stop_present()
                                if not posting_active and elapsed > 60:  # If no Stop button and been running for a while

                                    acc_log(account_id, "Posting appears complete (no Stop button detected)", silent=True)
                                    break
                                if elapsed >= post_watch_seconds:
                                    acc_log(account_id, f"Continuous watch reached timeout ({post_watch_seconds}s).", silent=True)
                                    break
                            else:
                                # Single-run mode: stop when no Stop button is present
                                if not _stop_present():

                                    acc_log(account_id, "Single-run mode: posting completed.", silent=True)
                                    break
                                if elapsed >= post_watch_seconds:
                                    acc_log(account_id, f"Single-run mode reached timeout ({post_watch_seconds}s).", silent=True)
                                    break
                            
                            # Additional safety check: if browser becomes unresponsive
                            try:
                                driver.execute_script("return document.readyState;")
                            except Exception:
                                acc_log(account_id, "Browser became unresponsive, ending watch", silent=True)
                                break
                except InterruptedError as ie:
                    # Posting was interrupted by upgrade page — set shared flag so the
                    # main thread can detect this even when running as a detached daemon thread.
                    acc_log(account_id, f"Restarting FewFeed posting due to: {ie}", silent=True)
                    _upgrade_restart_needed.set()
                    return  # exit the watcher thread cleanly (don't raise across threads)
                except Exception as e:
                    acc_log(account_id, f"Could not record groups to Google Sheets: {e}", silent=True)

            # Run watcher inline or detach to background
            try:
                if detach_after_post:
                    t = threading.Thread(target=_watch_results_loop, daemon=True)
                    t.start()
                    # Wait for watcher to finish, then check restart flag in main thread
                    t.join()
                    if _upgrade_restart_needed.is_set():
                        raise InterruptedError("Posting interrupted by upgrade page, needs restart")
                else:
                    _watch_results_loop()
            except InterruptedError as ie:
                # Posting was interrupted, need to restart
                acc_log(account_id, f"Watcher interrupted: {ie}. Will restart posting...", silent=True)
                raise
        except InterruptedError:
            raise
        except Exception:
            acc_log(account_id, "Could not click Post button.", silent=True)
    except InterruptedError:
        # Restart posting after upgrade page interruption - retry entire automation
        if _restart_attempt < _max_restarts:
            acc_log(account_id, f"Restarting posting process after upgrade page interruption (attempt {_restart_attempt + 1}/{_max_restarts})...", silent=True)
            try:
                # Go back first (mirrors the Back button visible on the upgrade page)
                try:
                    driver.back()
                    time.sleep(1.5)
                except Exception:
                    pass
                # If still on upgrade/checkout page, navigate explicitly
                try:
                    cur = (driver.current_url or "").lower()
                    if "upgrade" in cur or "checkout" in cur or "member/checkouts" in cur:
                        driver.get("https://fewfeed.online")
                        time.sleep(2)
                except Exception:
                    pass
                # Re-run full login so credentials are resubmitted and session is valid
                try:
                    fewfeed_login(driver, step_sleep=2, timeout=40)
                except Exception as login_e:
                    acc_log(account_id, f"Re-login before restart failed: {login_e}", silent=True)
                # Re-run the full posting automation with incremented restart counter
                return automate_fewfeed(driver, account_id, assume_page_loaded=False, silent=silent, detach_after_post=detach_after_post, _restart_attempt=_restart_attempt + 1, _max_restarts=_max_restarts)
            except Exception as restart_e:
                acc_log(account_id, f"Failed to restart posting: {restart_e}", silent=True)
        else:
            acc_log(account_id, f"Maximum restart attempts ({_max_restarts}) reached. Giving up.", silent=True)
    except Exception as e:
        acc_log(account_id, f"Error during FewFeed automation: {e}", silent=True)
    finally:
        # clear thread-local context
        with contextlib.suppress(Exception):
            _thread_ctx.account_id = None


def launch_account(account_id, detach_after_post=False, manual_mode=False):
    """Launches a browser session for a specific account using the template.

    When manual_mode=True, the browser will open Facebook Settings for the account
    and then pause there without starting FewFeed or automation. This allows you
    to complete any manual steps per account.
    """
    account_name = f"account_{account_id}"
    acc_log(account_id, "Preparing to launch...", silent=True)

    # Create a dedicated session profile (template or minimal)
    session_profile_path = os.path.join(SESSION_PROFILES_DIR, f"session_{account_id}")
    if os.path.exists(session_profile_path):
        shutil.rmtree(session_profile_path, ignore_errors=True)

    profile_mode = str(load_config().get('profile_mode','template')).lower()
    if profile_mode == 'minimal':
        # Small, portable profile directory
        try:
            os.makedirs(session_profile_path, exist_ok=True)
            acc_log(account_id, "Created minimal session profile.", silent=True)
        except Exception as e:
            acc_log(account_id, f"Error: Could not create minimal session profile. {e}", silent=True)
            return
    else:
        try:
            shutil.copytree(TEMPLATE_PROFILE_DIR, session_profile_path)
            acc_log(account_id, "Created isolated session profile.", silent=True)
        except Exception as e:
            acc_log(account_id, f"Error: Could not create session profile from template. {e}", silent=True)
            return

    # Launch Chrome with the new session profile
    options = Options()
    options.add_argument(f'--user-data-dir={session_profile_path}')
    options.add_argument('--profile-directory=Default') # The copied or new profile is 'Default'
    options.add_experimental_option("detach", True)
    # Background-safe window management
    options.add_argument("--start-maximized")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=TranslateUI")
    options.add_argument("--disable-ipc-flooding-protection")
    # Suppress Chrome logs and warnings
    options.add_argument("--log-level=3")
    options.add_argument("--disable-logging")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu-logging")
    options.add_argument("--silent")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option('useAutomationExtension', False)

    # Always load the FewFeed extension via --load-extension flag (no dialog needed)
    ext_path = os.path.join(BASE_DIR, 'fewfeedv2')
    ext_path = os.path.abspath(ext_path)
    ext_path_norm = ext_path.replace('\\', '/')
    if os.path.isdir(ext_path) and os.path.isfile(os.path.join(ext_path, 'manifest.json')):
        acc_log(account_id, f"Loading extension from: {ext_path}", silent=True)
        options.add_argument("--enable-extensions")
        options.add_argument(f"--load-extension={ext_path_norm}")
    else:
        acc_log(account_id, f"Extension folder not found at: {ext_path}", silent=True)

    # Minimal profile tweaks: keep size tiny
    if profile_mode == 'minimal':
        cache_dir = os.path.join(session_profile_path, 'CacheTiny')
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            pass
        options.add_argument(f"--disk-cache-dir={cache_dir}")
        options.add_argument("--disk-cache-size=10485760")  # 10 MB
        options.add_argument("--media-cache-size=1048576")  # 1 MB
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-component-update")

    try:
        import subprocess as _sp
        service = ChromeService(ChromeDriverManager().install())
        service.log_output = _sp.DEVNULL
        driver = webdriver.Chrome(service=service, options=options)
        active_drivers[driver.session_id] = {'driver': driver, 'account_id': account_id, 'profile_path': session_profile_path}
        acc_log(account_id, "Browser launched successfully.", silent=True)

        # Enable Extensions Developer Mode via Selenium (Preferences-file approach
        # is reset by Chrome on launch, so we toggle it through the UI instead)
        enable_developer_mode_via_selenium(driver)

        # Load fewfeedv2 extension if not already installed, then pin it
        ensure_extension_loaded_and_pinned(driver, ext_folder=ext_path)

        # --- Automation Flow ---
        cookie_path = os.path.join(ACCOUNTS_DIR, f"{account_id}_cookies.json")
        cookies = []
        # 1) Prefer local cookie file if exists
        if os.path.exists(cookie_path):
            with open(cookie_path, 'r', encoding='utf-8') as f:
                with contextlib.suppress(Exception):
                    cookies = json.load(f)
        # 2) If none, try fetch from Google Sheet CSV
        if not cookies:
            acc_log(account_id, "No local cookies or file empty. Fetching from Google Sheet...", silent=True)
            cookies = refresh_cookies_from_sheet(account_id)
        # 3) Attempt injection if we have any
        if cookies and inject_cookies(driver, cookies):
            acc_log(account_id, f"Cookies injected ({len(cookies)} entries). Navigating to Facebook Settings...", silent=True)
            driver.get("https://www.facebook.com/settings")
            try:
                WebDriverWait(driver, 15).until(lambda d: "facebook.com/settings" in d.current_url.lower() and "/login" not in d.current_url.lower())
                acc_log(account_id, "Login check passed (Settings).", silent=True)
            except Exception:
                acc_log(account_id, f"Login may have failed; current URL: {driver.current_url}", silent=True)
        else:
            acc_log(account_id, "No cookies available yet; opening Settings for manual login.", silent=True)
            driver.get("https://www.facebook.com/settings")

        # If manual mode, stop here and let the user continue manually in Settings
        if manual_mode:
            # Start session monitor so Telegram alerts will work in manual mode too
            try:
                start_fb_monitor(driver, account_id)
            except Exception:
                pass
            # Immediate alert state: if currently logged out, add alert; otherwise ensure removed
            try:
                if not is_fb_logged_in(driver, intrusive=False):
                    tg_alert_add(account_id)
                else:
                    tg_alert_remove(account_id)
            except Exception:
                pass
            try:
                print(f"[account_{account_id}] Manual mode: opened Facebook Settings and paused. Proceed with manual steps.")
            except Exception:
                pass
            return

        # --- Verify login before opening FewFeed (block until success) ---
        acc_log(account_id, "Verifying Facebook login before starting tool...", silent=True)
        backoff = 5
        while not SHUTTING_DOWN.is_set():
            if is_fb_logged_in(driver, intrusive=True):
                acc_log(account_id, "Facebook session verified.", silent=True)
                break
            tg_alert_add(account_id)
            with contextlib.suppress(Exception):
                close_fewfeed_tabs(driver)
            cookies = refresh_cookies_from_sheet(account_id)
            if cookies and inject_cookies(driver, cookies):
                driver.get('https://www.facebook.com/settings')
                try:
                    WebDriverWait(driver, 10).until(lambda d: 'facebook.com/settings' in d.current_url.lower() and '/login' not in d.current_url.lower())
                    tg_alert_remove(account_id)
                    set_account_recovering(account_id, False)
                    acc_log(account_id, "Login restored during pre-check.", silent=True)
                    break
                except Exception:
                    pass
            time.sleep(backoff)
            backoff = min(300, int(backoff * 1.7))

        # --- Mode-aware navigation: web vs hotkey ---
        trigger_mode = str(config.get('extension_trigger_mode', 'web')).lower()
        
        if trigger_mode == 'hotkey':
            # Original two-tab flow with hotkey trigger
            acc_log(account_id, "Opening Google in a new tab for extension focus...", silent=True)
            driver.execute_script("window.open('https://www.google.com','_blank');")
            tabs = driver.window_handles
            google_tab = tabs[-1]
            fb_tab = tabs[0]
            # Focus Google tab so user sees Google while FB loads
            driver.switch_to.window(google_tab)
            time.sleep(2)  # brief wait to allow FB to reach main page in background

            # Send shortcut once on Facebook tab, serialized and with window brought to front
            driver.switch_to.window(fb_tab)
            pre_handles = driver.window_handles
            # brief jitter to avoid multiple accounts competing at the same moment
            time.sleep(random.uniform(0.2, 1.2))
            with hotkey_lock:
                try:
                    # Bring the FB tab/window to front so the global hotkey hits the right window
                    driver.execute_cdp_cmd('Page.bringToFront', {})
                except Exception:
                    pass
                # Do NOT click the page to avoid accidental Story clicks
                pyautogui.hotkey('ctrl', 'shift', 'f')
                time.sleep(max(1, config.get('step_delay', 2)))

                # Wait for FewFeed tab to appear while lock is held so other accounts don't steal the shortcut
                ff_handle = None
                try:
                    WebDriverWait(driver, 15).until(lambda d: len(d.window_handles) > len(pre_handles))
                    # find the new handle
                    new_handles = [h for h in driver.window_handles if h not in pre_handles]
                    if new_handles:
                        ff_handle = new_handles[-1]
                except Exception:
                    ff_handle = None
        else:
            # Web mode: open FewFeed directly in background-safe way
            acc_log(account_id, "Opening FewFeed tool page directly (web mode)...", silent=True)
            if open_tool_tab(driver):
                ff_handle = driver.current_window_handle
                acc_log(account_id, "FewFeed tool page opened successfully", silent=True)
            else:
                acc_log(account_id, "Failed to open FewFeed tool page", silent=True)
                ff_handle = None

            # If detected, switch to it now
            if ff_handle:
                try:
                    driver.switch_to.window(ff_handle)
                except Exception:
                    pass

        # Secondary wait logic only for hotkey mode (pre_handles only exists there)
        if trigger_mode == 'hotkey' and ('ff_handle' not in locals() or not ff_handle):
            ff_handle = None
            for _ in range(10):
                handles_now = driver.window_handles
                new_handles = [h for h in handles_now if h not in pre_handles]
                if new_handles:
                    ff_handle = new_handles[-1]
                    break
                time.sleep(1)

        # Fallback: look for any tab with FewFeed in URL/title
        if not ff_handle:
            for h in driver.window_handles:
                driver.switch_to.window(h)
                try:
                    if 'fewfeed' in (driver.current_url.lower() + ' ' + driver.title.lower()):
                        ff_handle = h
                        break
                except Exception:
                    pass

        if ff_handle:
            driver.switch_to.window(ff_handle)
        else:
            # As a fallback in web mode, try open again
            if trigger_mode != 'hotkey' and not open_tool_tab(driver):
                acc_log(account_id, "FewFeed tab not detected; will attempt again later.", silent=True)

        # Start the FB session monitor (runs continuously in background)
        start_fb_monitor(driver, account_id)

        # Start FewFeed automation on the tool page that the extension opened
        current_cfg = load_config()
        if current_cfg.get('enable_auto_post'):
            acc_log(account_id, "Auto-post is enabled. Starting FewFeed automation...", silent=True)
            automate_fewfeed(driver, account_id, assume_page_loaded=True, silent=True, detach_after_post=detach_after_post)
            if detach_after_post:
                # We detach after post start; return to allow next account to launch
                acc_log(account_id, "Post started; continuing in background watcher.", silent=True)
            else:
                acc_log(account_id, "FewFeed automation finished. Waiting for completion...", silent=True)
        else:
            acc_log(account_id, "Auto-post is disabled in config.", silent=True)

        acc_log(account_id, "Automation complete. Browser is ready.", silent=True)
        try:
            log_path = os.path.join(LOGS_DIR, f"account_{account_id}.log")
            print(f"[{account_name}] Detailed log: {log_path}")
        except Exception:
            pass

    except Exception as e:
        acc_log(account_id, f"An error occurred: {e}", silent=True)

# --- Menu System ---
def list_accounts():
    if not os.path.exists(ACCOUNTS_DIR):
        os.makedirs(ACCOUNTS_DIR)
    accounts = [f.replace('_cookies.json', '') for f in os.listdir(ACCOUNTS_DIR) if f.endswith('_cookies.json')]
    if not accounts:
        print("No accounts found. Add cookie files to the 'accounts' folder.")
        return []
    print("\nAvailable accounts:")
    for i, acc in enumerate(accounts):
        print(f"  {i+1}. {acc}")
    return accounts

def close_all_browsers():
    if not active_drivers:
        print("No active browsers to close.")
        return
    print("Closing all browsers and cleaning up session profiles...")
    for session_id, data in list(active_drivers.items()):
        try:
            data['driver'].quit()
            shutil.rmtree(data['profile_path'], ignore_errors=True)
            print(f"Closed browser and cleaned profile for {data['account_id']}")
        except Exception as e:
            print(f"Error closing browser for {data['account_id']}: {e}")
        del active_drivers[session_id]

def delete_logs():
    try:
        if os.path.isdir(LOGS_DIR):
            # ensure handlers are closed before deleting
            try:
                close_account_loggers()
            except Exception:
                pass
            # ensure logging is fully shut down
            with contextlib.suppress(Exception):
                logging.shutdown()
            # also close root handlers as a fallback
            for h in list(logging.root.handlers):
                with contextlib.suppress(Exception):
                    h.flush()
                with contextlib.suppress(Exception):
                    h.close()
                with contextlib.suppress(Exception):
                    logging.root.removeHandler(h)
            # delete files with retry
            names = list(os.listdir(LOGS_DIR))
            for fn in names:
                fp = os.path.join(LOGS_DIR, fn)
                for _ in range(3):
                    try:
                        if os.path.isfile(fp):
                            os.remove(fp)
                        break
                    except Exception:
                        time.sleep(0.1)
                        continue
            # attempt to remove the directory entirely and recreate it
            with contextlib.suppress(Exception):
                os.rmdir(LOGS_DIR)
            with contextlib.suppress(Exception):
                os.makedirs(LOGS_DIR, exist_ok=True)
    except Exception as e:
        print(f"Error deleting logs: {e}")
    print("All logs deleted.")

def main_menu():
    global config
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    os.makedirs(SESSION_PROFILES_DIR, exist_ok=True)

    config = load_config()
    # If running with minimal profile, skip the template setup requirement
    if str(config.get('profile_mode','template')).lower() != 'minimal':
        if not config.get('template_created'):
            run_setup_wizard()
            config = load_config() # Reload config after setup
            if not config.get('template_created'):
                print("\nSetup was not completed. Exiting.")
                return

    while True:
        print("\n--- FewFeed Bot - Template Menu ---")
        print("1. Launch Account")
        print("2. List Accounts")
        print("3. Close All Browsers")
        state = 'ON' if config.get('enable_auto_post') else 'OFF'
        print(f"4. Toggle Auto-Post (currently: {state})")
        print("5. Run Setup Wizard Again")
        print("6. Accounts Launch (open Settings only)")
        print("7. Exit")
        choice = input("Enter your choice: ").strip()

        if choice == '1':
            ids = input("Enter account IDs separated by comma, or 'all': ").strip()
            selected = []
            if ids.lower() == 'all':
                selected = [int(os.path.splitext(f)[0].split('_')[0]) for f in os.listdir(ACCOUNTS_DIR) if f.endswith('_cookies.json')]
            else:
                with contextlib.suppress(Exception):
                    selected = [int(x.strip()) for x in ids.split(',') if x.strip().isdigit()]
            # Launch accounts in parallel for simultaneous background operation
            if selected:
                _start_launch_manager_if_needed()
                enqueued = 0
                with launch_queue_lock:
                    for account_id in selected:
                        # skip if a driver already exists for this account
                        is_active = any(data.get('account_id') == account_id for data in active_drivers.values())
                        if is_active:
                            print(f"Account {account_id} is already active.")
                            continue
                        # avoid duplicates already in queue
                        if account_id in launch_queue:
                            print(f"Account {account_id} is already queued.")
                            continue
                        launch_queue.append(account_id)
                        enqueued += 1
                if enqueued:
                    launch_queue_event.set()
                    print(f"Launching {enqueued} account(s) in parallel for simultaneous background operation. Menu remains interactive.")
                    print("All accounts will run independently and continue tracking groups even when you switch desktops.")
                else:
                    print("Nothing to launch.")
            continue
        elif choice == '2':
            list_accounts()
        elif choice == '3':
            close_all_browsers()
        elif choice == '4':
            # toggle auto-post feature
            config['enable_auto_post'] = not config.get('enable_auto_post', False)
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            # reload global config to ensure other functions see change
            config = load_config()
            print(f"Auto-post feature set to {'ON' if config['enable_auto_post'] else 'OFF'}.")
        elif choice == '5':
            run_setup_wizard()
        elif choice == '6':
            ids = input("Enter account IDs separated by comma, or 'all': ").strip()
            selected = []
            if ids.lower() == 'all':
                selected = [int(os.path.splitext(f)[0].split('_')[0]) for f in os.listdir(ACCOUNTS_DIR) if f.endswith('_cookies.json')]
            else:
                with contextlib.suppress(Exception):
                    selected = [int(x.strip()) for x in ids.split(',') if x.strip().isdigit()]
            if selected:
                # Launch all selected accounts in parallel, pausing at Facebook Settings
                threads = []
                enqueued = 0
                for account_id in selected:
                    # Skip if already active
                    is_active = any(data.get('account_id') == account_id for data in active_drivers.values())
                    if is_active:
                        print(f"Account {account_id} is already active.")
                        continue
                    t = threading.Thread(target=launch_account, args=(account_id,), kwargs={"manual_mode": True}, daemon=True)
                    t.start()
                    threads.append(t)
                    enqueued += 1
                    time.sleep(0.2)
                if enqueued:
                    print(f"Launching {enqueued} account(s) to Facebook Settings in parallel. Each will pause for manual steps.")
                else:
                    print("Nothing to launch.")
            continue
        elif choice == '7':
            close_all_browsers()
            delete_logs()
            print("Exiting.")
            break
        else:
            print("Invalid choice.")

if __name__ == "__main__":
    try:
        main_menu()
    except (EOFError, KeyboardInterrupt):
        print("\nExiting program.")
        close_all_browsers()
        delete_logs()