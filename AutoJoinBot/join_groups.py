import argparse
import json
import os
import sys
import time
import csv
import io
import threading
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# Cross-bot stats tracker (SQLite)
try:
    _aj_tracker_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if _aj_tracker_dir not in sys.path:
        sys.path.insert(0, _aj_tracker_dir)
    from stats_tracker import get_tracker
    _aj_tracker = get_tracker("AutoJoin")
    del _aj_tracker_dir
except Exception:
    class _Null:
        def log_event(self, *a, **kw): pass
    _aj_tracker = _Null()

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import requests


FB_MOBILE = "https://m.facebook.com/"
SETTINGS_URL = "https://m.facebook.com/settings"
GROUP_URL_TMPL = "https://m.facebook.com/groups/{gid}"


def read_group_ids(path: str) -> List[str]:
    if not os.path.exists(path):
        print(f"[ERROR] Group id file not found: {path}")
        return []
    gids: List[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            # accept URLs or raw ids
            if "/groups/" in s:
                try:
                    # extract after /groups/
                    after = s.split("/groups/")[-1]
                    gid = after.split("/")[0].split("?")[0]
                    if gid:
                        gids.append(gid)
                except Exception:
                    continue
            else:
                gids.append(s)
    # de-dup while preserving order
    seen = set()
    unique = []
    for g in gids:
        if g not in seen:
            unique.append(g)
            seen.add(g)
    return unique


def load_cookies(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Cookie file must be a JSON list of cookie objects.")
    norm = []
    for c in data:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        cookie: Dict = {"name": name, "value": value}
        # Selenium expects 'expiry' not 'expirationDate'
        if "expirationDate" in c:
            try:
                cookie["expiry"] = int(float(c["expirationDate"]))
            except Exception:
                pass
        # Optional fields
        if c.get("path"):
            cookie["path"] = c["path"]
        if c.get("domain"):
            # Keep domain as provided; browsers ignore leading dot today, but selenium accepts it
            cookie["domain"] = c["domain"]
        if c.get("secure") is not None:
            cookie["secure"] = bool(c["secure"])
        if c.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if c.get("sameSite"):
            # Selenium uses 'sameSite' with values: 'Lax', 'Strict', 'None'
            s = str(c["sameSite"]).lower()
            if "lax" in s:
                cookie["sameSite"] = "Lax"
            elif "strict" in s:
                cookie["sameSite"] = "Strict"
            elif "no_restriction" in s or "none" in s:
                cookie["sameSite"] = "None"
        norm.append(cookie)
    return norm


def ensure_logged_in_with_cookies(driver: webdriver.Chrome, cookies: List[Dict], wait: WebDriverWait) -> bool:
    # Navigate first to settings to set the correct domain context without hitting home
    driver.get(SETTINGS_URL)
    time.sleep(1.5)
    # Clear any existing cookies
    try:
        driver.delete_all_cookies()
    except WebDriverException:
        pass
    # Add cookies for facebook domains
    def sanitize_cookie(c: Dict) -> Dict:
        cc = dict(c)
        # Ensure name/value are strings
        if "name" in cc:
            cc["name"] = str(cc["name"])
        if "value" in cc and cc["value"] is not None:
            cc["value"] = str(cc["value"])
        # sameSite must be one of Strict/Lax/None
        if "sameSite" in cc:
            val = str(cc["sameSite"]).strip()
            mapping = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
            v = mapping.get(val.lower())
            if v:
                cc["sameSite"] = v
            else:
                cc.pop("sameSite", None)
        # expiry must be int seconds
        if "expiry" in cc:
            try:
                cc["expiry"] = int(float(cc["expiry"]))
            except Exception:
                cc.pop("expiry", None)
        # Domain empty => remove so Selenium sets for current domain
        if not cc.get("domain"):
            cc.pop("domain", None)
        return cc
    added = 0
    for c in cookies:
        base = {k: v for k, v in c.items()}
        d = base.get("domain")
        if d and "facebook.com" not in d:
            continue
        variants = []
        # Try provided domain first
        if d:
            variants.append(base)
        # Also try without domain (let browser set for current m.facebook.com)
        variants.append({k: v for k, v in base.items() if k != "domain"})
        # Also try explicit m.facebook.com and facebook.com
        for dom in ("m.facebook.com", ".facebook.com", "facebook.com"):
            v = {k: v for k, v in base.items()}
            v["domain"] = dom
            variants.append(v)
        for v in variants:
            try:
                driver.add_cookie(sanitize_cookie(v))
                added += 1
            except WebDriverException:
                pass
    # Reload to apply cookies (stay on settings to avoid home)
    driver.get(SETTINGS_URL)
    # Check for c_user cookie which indicates authenticated session
    cu = driver.get_cookie("c_user")
    if cu and cu.get("value"):
        return True
    # Heuristic UI checks as fallback
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/menu/']")))
        return True
    except TimeoutException:
        pass
    # If login form present, it's not logged in
    try:
        if driver.find_elements(By.CSS_SELECTOR, "input[name='email'], input[name='m_login_email']"):
            return False
    except Exception:
        pass
    # Last attempt: navigate to settings and re-check cookie
    try:
        driver.get(SETTINGS_URL)
        time.sleep(1.0)
        cu = driver.get_cookie("c_user")
        if cu and cu.get("value"):
            return True
    except Exception:
        pass
    return False


def attempt_join_group(driver: webdriver.Chrome, gid: str, wait: WebDriverWait) -> str:
    url = GROUP_URL_TMPL.format(gid=gid)
    driver.get(url)
    # Let page load
    time.sleep(1.2)

    # Detect content unavailable and treat as error
    try:
        if driver.find_elements(By.XPATH, "//div[contains(.,""This content isn't available right now"") or contains(.,'content is unavailable')]|//span[contains(.,""This content isn't available right now"")]|//h2[contains(.,'not available')]"):
            return "error:content_unavailable"
    except Exception:
        pass

    # Helper: detect PENDING (request already sent)
    def is_pending_page() -> bool:
        try:
            pending_x = (
                "//span[contains(.,'Your membership is pending')]|"
                "//span[contains(.,'Requested')]|"
                "//span[contains(.,'request sent')]|"
                "//div[@role='button' and .//span[normalize-space()='Cancel request']]|"
                "//button[normalize-space()='Cancel request']"
            )
            return bool(driver.find_elements(By.XPATH, pending_x))
        except Exception:
            return False

    # Check if already a member indicators
    joined_indicators = [
        (By.XPATH, "//span[contains(text(),'Joined')]"),
        (By.XPATH, "//span[contains(text(),'عضو')]"),
        (By.XPATH, "//span[contains(text(),'Đã tham gia')]"),
        (By.XPATH, "//span[contains(text(),'Membre')]"),
        (By.XPATH, "//span[contains(text(),'عضوة')]"),
    ]
    for by, sel in joined_indicators:
        try:
            if driver.find_elements(by, sel):
                return "already_member"
        except Exception:
            pass

    # If already pending at load
    if is_pending_page():
        return "pending:already_requested"

    # Possible join button selectors (m.facebook.com varies by locale and UI)
    candidates = [
        (By.XPATH, "//a[contains(@href,'join') and (contains(.,'Join') or contains(.,'انضم') or contains(.,'Rejoindre') or contains(.,'Bergabung') or contains(.,'Gabung'))]"),
        (By.XPATH, "//div[contains(@role,'button') and (contains(.,'Join') or contains(.,'انضم') or contains(.,'Rejoindre') or contains(.,'Bergabung') or contains(.,'Gabung'))]"),
        (By.XPATH, "//span[(text()='Join group' or text()='Join Group' or text()='طلب الانضمام' or text()='Rejoindre le groupe')]/ancestor::*[self::a or self::div][1]"),
        (By.CSS_SELECTOR, "a[href*='/join']"),
    ]

    clicked = False
    for by, sel in candidates:
        try:
            el = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.3)
            el.click()
            clicked = True
            # After clicking Join, wait briefly to see if a questions popup appears
            try:
                WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@role='dialog' and (.//span[contains(.,'Answer')]|.//h2[contains(.,'questions')]) ]"))
                )
            except TimeoutException:
                pass
            # Some UIs open a modal to answer questions; handle it immediately
            try:
                answer_join_questions(driver, wait)
            except Exception:
                pass
            break
        except TimeoutException:
            continue
        except WebDriverException:
            continue

    if not clicked:
        # Sometimes the group is private and only shows 'Request to join'
        # After loading, check if a request already sent indicator exists
        pending_indicators = [
            (By.XPATH, "//span[contains(text(),'Pending') or contains(text(),'في الانتظار') or contains(text(),'En attente')]")
        ]
        for by, sel in pending_indicators:
            try:
                if driver.find_elements(by, sel):
                    return "request_pending"
            except Exception:
                pass
        return "join_button_not_found"

    # After clicking, wait for confirmation of pending/request sent or joined
    post_indicators = [
        (By.XPATH, "//span[contains(text(),'Pending') or contains(text(),'في الانتظار') or contains(text(),'En attente') or contains(text(),'Requested')]") ,
        (By.XPATH, "//span[contains(text(),'Joined') or contains(text(),'عضو') or contains(text(),'Đã tham gia') or contains(text(),'Membre')]")
    ]
    try:
        WebDriverWait(driver, 5).until(
            lambda d: any(d.find_elements(by, sel) for by, sel in post_indicators)
        )
        # Determine which state
        for by, sel in post_indicators:
            found = driver.find_elements(by, sel)
            if found:
                txt = found[0].text.lower()
                if any(k in txt for k in ["pending", "en attente", "requested", "انتظار"]):
                    return "request_pending"
                return "joined"
    except TimeoutException:
        pass
    return "clicked_no_confirm"


def answer_join_questions(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """Detect and answer group join questions heuristically.
    If OPENAI_API_KEY is set and openai is installed, use AI for text answers.
    Works for both m.facebook.com flows and desktop modal on web.facebook.com.
    """
    def ai_answer(prompt: str) -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return "I agree and will follow the rules. I joined to learn and contribute positively."
        try:
            # import lazily to keep dependency optional
            from openai import OpenAI  # type: ignore
            client = OpenAI(api_key=key)
            completion = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo"),
                messages=[
                    {"role": "system", "content": "Answer briefly (max 1-2 sentences). Be polite, agree to rules, no links."},
                    {"role": "user", "content": prompt[:4000]},
                ],
                temperature=0.3,
                max_tokens=80,
            )
            return completion.choices[0].message.content.strip() if completion.choices else "I agree to follow the rules."
        except Exception:
            return "I agree to follow the rules and will participate respectfully."

    def click_all_checkboxes(scope):
        boxes = scope.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        for b in boxes:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                if not b.is_selected() and b.is_enabled():
                    label_text = ""
                    try:
                        label = b.find_element(By.XPATH, "ancestor::label[1]")
                        label_text = label.text.lower()
                    except Exception:
                        pass
                    if any(k in label_text for k in ["yes", "agree", "i agree", "accept"]):
                        b.click()
            except Exception:
                pass

    def choose_radios(scope):
        # Prefer positive answers
        radios = scope.find_elements(By.CSS_SELECTOR, "input[type='radio']")
        for r in radios:
            try:
                label_text = ""
                try:
                    label = r.find_element(By.XPATH, "ancestor::label[1]")
                    label_text = label.text.lower()
                except Exception:
                    pass
                if any(k in label_text for k in ["yes", "agree", "i agree", "accept"]):
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", r)
                    if r.is_enabled():
                        r.click()
                # If nothing clearly positive, select first option in each group
            except Exception:
                pass
        # Ensure at least one radio per group selected
        # Fallback: just click the first radio in each fieldset-like container
        groups = scope.find_elements(By.XPATH, "//fieldset|//div[contains(@role,'radiogroup')]")
        for g in groups:
            try:
                selected = g.find_elements(By.CSS_SELECTOR, "input[type='radio']:checked")
                if selected:
                    continue
                opts = g.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if opts:
                    opts[0].click()
            except Exception:
                pass

    def choose_selects(scope):
        selects = scope.find_elements(By.TAG_NAME, "select")
        for s in selects:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", s)
                from selenium.webdriver.support.ui import Select  # local import
                sel = Select(s)
                # Prefer option containing yes/agree/accept; else first non-empty
                chosen = False
                for opt in sel.options:
                    txt = opt.text.strip().lower()
                    if any(k in txt for k in ["yes", "agree", "accept", "oui", "sí", "ok"]):
                        sel.select_by_visible_text(opt.text)
                        chosen = True
                        break
                if not chosen:
                    for opt in sel.options:
                        if opt.get_attribute("value") or opt.text.strip():
                            sel.select_by_visible_text(opt.text)
                            break
            except Exception:
                pass

    def fill_text_inputs(scope):
        # Capture visible question texts near textareas/inputs
        textareas = scope.find_elements(By.CSS_SELECTOR, "textarea")
        inputs = scope.find_elements(By.CSS_SELECTOR, "input[type='text'], input:not([type])[role='textbox']")
        for el in textareas + inputs:
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                # Find nearby question label
                q = ""
                try:
                    lab = el.find_element(By.XPATH, "ancestor::div[1]")
                    q = lab.text.strip()
                    if not q:
                        lbl2 = el.find_element(By.XPATH, "preceding::*[self::label or self::span or self::div][normalize-space()][1]")
                        q = lbl2.text.strip()
                except Exception:
                    pass
                group = ""
                try:
                    h = driver.find_element(By.XPATH, "//h1|//h2")
                    group = h.text.strip()
                except Exception:
                    pass
                prompt = (q or "Please provide a short reason for joining and confirm agreement to the rules.")
                if group:
                    prompt = f"Group: {group}\nQuestion: {prompt}\nAnswer briefly and positively."
                ans = ai_answer(prompt)
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                el.clear()
                el.send_keys(ans[:400])
            except Exception:
                pass

    def try_submit(scope) -> bool:
        xpath = (
            "//div[@role='dialog']//span[text()='Submit']/ancestor::div[@role='button']|"
            "//div[@role='dialog']//span[text()='Send']/ancestor::div[@role='button']|"
            "//div[@role='dialog']//span[contains(text(),'Submit')]/ancestor::*[self::div or self::button][@role='button']|"
            "//button[normalize-space()='Submit']|//button[normalize-space()='Send']|//button[contains(.,'Submit')]|//button[contains(.,'Send')]|"
            "//a[contains(.,'Submit')]|//a[contains(.,'Send')]"
        )
        buttons = scope.find_elements(By.XPATH, xpath)
        if not buttons:
            # Mobile flow submit buttons
            xpath_mobile = (
                "//button[contains(.,'Submit') or contains(.,'Send') or contains(.,'Done')]|"
                "//div[@role='button' and (contains(.,'Submit') or contains(.,'Send') or contains(.,'Done'))]"
            )
            buttons = scope.find_elements(By.XPATH, xpath_mobile)
        for b in buttons:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                b.click()
                time.sleep(0.8)
                return True
            except Exception:
                continue
        return False

    # Try to click entry points like "Answer questions" with scrolling retries
    def try_open_questions_entry(max_scrolls: int = 5) -> None:
        entry_x = (
            "//span[normalize-space()='Answer questions']/ancestor::*[self::div or self::a or self::button][1]|"
            "//div[@role='button' and .//span[contains(.,'Answer questions')]]|"
            "//a[contains(.,'Answer questions')]|"
            "//span[contains(.,'Répondre aux questions')]/ancestor::*[self::div or self::a or self::button][1]|"
            "//span[contains(.,'إجابة') or contains(.,'الأسئلة')]/ancestor::*[self::div or self::a or self::button][1]"
        )
        for _ in range(max_scrolls):
            try:
                entries = driver.find_elements(By.XPATH, entry_x)
                if entries:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", entries[0])
                    entries[0].click()
                    time.sleep(0.8)
                    return
            except Exception:
                pass
            try:
                driver.execute_script("window.scrollBy(0, Math.max(400, window.innerHeight/2));")
            except Exception:
                break
            time.sleep(0.2)

    try_open_questions_entry()

    # Detect dialog/modal or question form
    # Desktop modal
    dialog = None
    try:
        dialog = WebDriverWait(driver, 2).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[@role='dialog' and (.//span[contains(.,'Answer questions')] or .//h2[contains(.,'Answer questions')])]")
            )
        )
    except TimeoutException:
        pass

    # Mobile page (no dialog)
    if not dialog:
        try:
            # Look for question prompts on page
            xpath_q = (
                "//div[contains(.,'Answer questions')]|"
                "//div[contains(.,'Group rules')]|"
                "//label|//textarea"
            )
            question_blocks = driver.find_elements(By.XPATH, xpath_q)
            scope = driver
            if not question_blocks:
                # No dialog and no inline blocks found assume no questions
                return
        except Exception:
            return
    else:
        scope = dialog

    # Interact within scope
    click_all_checkboxes(scope)
    choose_radios(scope)
    choose_selects(scope)
    fill_text_inputs(scope)

    # Try to submit
    if not try_submit(scope):
        # Sometimes submit appears after scrolling
        try:
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", scope)
        except Exception:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.4)
        try_submit(scope)

    # Wait briefly for pending state to confirm
    try:
        WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(.,'Pending') or contains(.,'request sent') or contains(.,'Requested')]"))
        )
    except TimeoutException:
        pass



class TelegramNotifier:
    def __init__(self, enabled: bool, token: Optional[str], chat_id: Optional[str]):
        self.enabled = True
        self.token = token
        self.chat_id = chat_id
        self.message_id = None
        self.update_offset = None

    def send_or_update(self, text: str) -> None:
        try:
            import re
            import os
            import json
            import time
            
            telemetry_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "telemetry"))
            os.makedirs(telemetry_dir, exist_ok=True)
            
            lines = text.split("\n")
            for line in lines:
                match = re.search(r"<b>(.*?)</b>\s*—\s*✅\s*(\d+)\s*\|\s*⏳\s*(\d+)\s*\|\s*🙋\s*(\d+)\s*\|\s*⚠️\s*(\d+)\s*\|\s*📦\s*(\d+)", line)
                if match:
                    label = match.group(1).strip()
                    joined = int(match.group(2))
                    pending = int(match.group(3))
                    left = int(match.group(4))
                    errors = int(match.group(5))
                    total = int(match.group(6))

                    # Log to shared SQLite database
                    try:
                        _aj_tracker.log_event("group_joined", account_name=label, details={"joined": joined, "pending": pending, "left": left, "errors": errors, "total": total})
                    except Exception:
                        pass
                    
                    filename = f"AutoJoinBot_{label}.json"
                    filepath = os.path.join(telemetry_dir, filename)
                    
                    data = {}
                    if os.path.exists(filepath):
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                        except Exception:
                            pass
                            
                    data["bot_name"] = "AutoJoinBot"
                    data["account"] = label
                    data["last_update"] = time.time()
                    data["status"] = f"Running - Joined {joined}/{total}"
                    data["stats"] = {
                        "joined": joined,
                        "pending": pending,
                        "left": left,
                        "errors": errors,
                        "total": total
                    }
                    
                    temp_path = filepath + ".tmp"
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                    os.replace(temp_path, filepath)
        except Exception as e:
            print(f"AutoJoinBot telemetry error: {e}")

    def send_message(self, text: str) -> None:
        pass

    def poll_commands(self, handler) -> None:
        pass


def fetch_accounts_from_csv(csv_url: str) -> List[Tuple[str, List[Dict]]]:
    """Download a published Google Sheet CSV with columns: account_file, cookies_json.
    Returns list of (account_label, cookies_list).
    """
    try:
        resp = requests.get(csv_url, timeout=20)
        resp.raise_for_status()
        data = resp.content.decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(data))
        out: List[Tuple[str, List[Dict]]] = []
        for row in reader:
            # Make headers case-insensitive and trim whitespace
            lowered = { ((k.lower().strip()) if k else k): (v if v is not None else "") for k, v in row.items() }
            label = (lowered.get("account_file") or lowered.get("label") or "account").strip()
            cj = lowered.get("cookies_json") or lowered.get("cookies") or ""
            cj = cj.strip()
            if not cj:
                # Fallback: find any field that looks like a JSON array
                for v in lowered.values():
                    if isinstance(v, str) and v.strip().startswith('[') and ']' in v:
                        cj = v.strip()
                        break
                if not cj:
                    continue
            try:
                # Try direct JSON
                cookies = json.loads(cj)
                if isinstance(cookies, list):
                    out.append((label, cookies))
            except Exception:
                # Try extracting JSON array between first '[' and last ']'
                try:
                    start = cj.find('[')
                    end = cj.rfind(']')
                    if start != -1 and end != -1 and end > start:
                        repaired = cj[start:end+1]
                        cookies = json.loads(repaired)
                        if isinstance(cookies, list):
                            out.append((label, cookies))
                            continue
                except Exception:
                    pass
                continue
        return out
    except Exception:
        return []


def find_cookie_file(accounts_dir: str, account_number: Optional[int], cookie_path: Optional[str]) -> Optional[str]:
    if cookie_path:
        return cookie_path if os.path.exists(cookie_path) else None
    if account_number is not None:
        guess = os.path.join(accounts_dir, f"{account_number}_cookies.json")
        return guess if os.path.exists(guess) else None
    # If neither provided, pick the first *_cookies.json
    for name in sorted(os.listdir(accounts_dir)):
        if name.endswith("_cookies.json"):
            return os.path.join(accounts_dir, name)
    return None


def main():
    def do_join(gids: List[str], cookie_file: str, headless: bool, delay: float):
        print(f"[INFO] Using cookies: {cookie_file}")
        cookies = load_cookies(cookie_file)
        if not cookies:
            print("[ERROR] Cookie file contained no valid cookies.")
            sys.exit(1)

        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--lang=en-US")

        try:
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        except WebDriverException as e:
            print(f"[ERROR] Failed to start Chrome WebDriver: {e}")
            sys.exit(1)

        wait = WebDriverWait(driver, 10)

        try:
            ok = ensure_logged_in_with_cookies(driver, cookies, wait)
            if not ok:
                print("[WARN] Login via local cookies failed. Will retry with cookies from sheet if configured…")
                # Keep window and keep retrying by fetching new cookies from sheet
                retry_url = args.cookies_sheet_csv_url
                if not retry_url and 'cookies_sheet_csv_url' in (cfg or {}):
                    retry_url = cfg.get('cookies_sheet_csv_url')
                if retry_url:
                    # Continuous retry until success
                    attempt = 0
                    while True:
                        attempt += 1
                        try:
                            accounts = fetch_accounts_from_csv(retry_url)
                        except Exception:
                            accounts = []
                        if not accounts:
                            print(f"[WARN] Attempt {attempt}: No cookies available from sheet. Retrying in 10s…")
                            time.sleep(10)
                            continue
                        success = False
                        for sheet_label, sheet_cookies in accounts:
                            print(f"[INFO] Attempt {attempt}: trying sheet cookies: {sheet_label}")
                            try:
                                if ensure_logged_in_with_cookies(driver, sheet_cookies, wait):
                                    print(f"[INFO] Login successful with sheet account: {sheet_label}")
                                    cookies = sheet_cookies
                                    success = True
                                    break
                            except Exception:
                                continue
                        if success:
                            break
                        print(f"[WARN] Attempt {attempt}: all sheet cookies failed. Retrying in 10s…")
                        time.sleep(10)
                else:
                    print("[ERROR] No cookies_sheet_csv_url configured. Cannot retry. Exiting.")
                    sys.exit(2)
            else:
                print("[INFO] Login successful.")

            results = []
            for i, gid in enumerate(gids, 1):
                try:
                    status = attempt_join_group(driver, gid, wait)
                    print(f"[{i}/{len(gids)}] {gid}: {status}")
                    results.append((gid, status))
                except Exception as e:
                    print(f"[{i}/{len(gids)}] {gid}: error {e}")
                    results.append((gid, f"error:{e}"))
                time.sleep(max(0.0, delay))

            joined = sum(1 for _, s in results if s == "joined")
            pending = sum(1 for _, s in results if "pending" in s)
            print("\n=== Summary ===")
            print(f"Joined: {joined}")
            print(f"Pending: {pending}")
            print(f"Total processed: {len(results)}")

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def list_accounts(accounts_dir: str) -> List[str]:
        files = []
        try:
            for name in os.listdir(accounts_dir):
                if name.endswith("_cookies.json"):
                    files.append(name)
        except FileNotFoundError:
            return []
        return sorted(files, key=lambda x: (len(x), x))

    def interactive_menu(default_uids: str, accounts_dir: str):
        print("=== Auto Join Menu ===")
        gids = read_group_ids(default_uids)
        print(f"Group IDs loaded from {default_uids}: {len(gids)}")
        if not gids:
            print("No group IDs found. Please add IDs to uid.txt and run again.")
            return

        files = list_accounts(accounts_dir)
        if not files:
            print(f"No cookie files found in {accounts_dir}.")
            return
        for idx, fname in enumerate(files, 1):
            print(f"{idx}. {fname}")
        sel = input("Type numbers/filenames separated by commas, ranges like 2-5, or 'all' (default 1): ").strip()
        selected_paths: List[str] = []
        if not sel:
            selected_paths = [os.path.join(accounts_dir, files[0])]
        else:
            tokens = [t.strip() for t in sel.split(',') if t.strip()]
            choose_all = any(t.lower() == 'all' for t in tokens)
            if choose_all:
                selected_paths = [os.path.join(accounts_dir, f) for f in files]
            else:
                chosen: List[str] = []
                for t in tokens:
                    if t.isdigit():
                        idx = int(t)
                        if 1 <= idx <= len(files):
                            chosen.append(files[idx - 1])
                        else:
                            print(f"Ignoring out-of-range index: {t}")
                    elif '-' in t:
                        a, b = t.split('-', 1)
                        if a.strip().isdigit() and b.strip().isdigit():
                            start = int(a)
                            end = int(b)
                            if start > end:
                                start, end = end, start
                            for i in range(start, end + 1):
                                if 1 <= i <= len(files):
                                    chosen.append(files[i - 1])
                                else:
                                    print(f"Ignoring out-of-range index in range: {i}")
                        else:
                            print(f"Ignoring invalid range: {t}")
                    else:
                        name = t if t in files else (t if t.endswith("_cookies.json") else f"{t}_cookies.json")
                        if name in files:
                            chosen.append(name)
                        else:
                            print(f"Ignoring unknown filename: {t}")
                # de-duplicate while preserving order as listed above
                seen = set()
                for n in chosen:
                    if n not in seen:
                        selected_paths.append(os.path.join(accounts_dir, n))
                        seen.add(n)

        if not selected_paths:
            print("No valid accounts selected.")
            return

        print("\nLaunching accounts:")
        for p in selected_paths:
            print(f"- {os.path.basename(p)}")

        # Launch in parallel like the CSV flow
        notifier = TelegramNotifier(args.telegram_enabled, args.telegram_bot_token, args.telegram_chat_id)
        errors_registry: Dict[str, List[str]] = {}
        if args.telegram_enabled:
            def on_cmd(text: str):
                t = text.strip().lower()
                if t.startswith('/errors'):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        lbl = parts[1].strip()
                        items = errors_registry.get(lbl) or []
                        notifier.send_message((f"❗ Errors for <b>{lbl}</b> (count={len(items)}):\n" + "\n".join(items[:200])) if items else (f"No errors recorded for <b>{lbl}</b> yet."))
                    else:
                        lines = ["<b>❗ Error Groups</b>"]
                        total = 0
                        for lbl, arr in errors_registry.items():
                            if arr:
                                total += len(arr)
                                lines.append(f"- <b>{lbl}</b> ({len(arr)}): " + ", ".join(arr[:20]) + (" …" if len(arr) > 20 else ""))
                        notifier.send_message("No error groups recorded yet." if total == 0 else "\n".join(lines))
            notifier.poll_commands(on_cmd)

        progress: Dict[str, Dict[str, int]] = {}
        lock = threading.Lock()
        threads: List[threading.Thread] = []
        for p in selected_paths:
            label = os.path.basename(p)
            try:
                cookies = load_cookies(p)
            except Exception:
                print(f"[WARN] Skipping invalid cookies file: {label}")
                continue
            t = threading.Thread(
                target=do_join_with_cookies,
                args=(gids, cookies, args.headless, args.delay, label, notifier, args.logs_dir, progress, errors_registry, lock),
                daemon=True,
            )
            threads.append(t)
            t.start()
            time.sleep(0.3)
        for t in threads:
            t.join()
        if args.telegram_enabled:
            notifier.send_or_update(format_progress(progress))
        return

    def do_join_with_cookies(gids: List[str], cookies: List[Dict], headless: bool, delay: float,
                              label: str, notifier: Optional[TelegramNotifier], logs_dir: str,
                              progress: Dict[str, Dict[str, int]], errors_registry: Dict[str, List[str]], lock: threading.Lock) -> None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, f"{label}_{ts}.log")
        # Processed state per account to skip next time
        state_dir = os.path.join(logs_dir, "state")
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, f"{label}.json")
        processed_ok: set[str] = set()
        try:
            if os.path.exists(state_path):
                with open(state_path, 'r', encoding='utf-8') as sf:
                    data = json.load(sf)
                    if isinstance(data, list):
                        # legacy format: list of IDs
                        processed_ok = set(str(x) for x in data)
                    elif isinstance(data, dict) and isinstance(data.get('processed_ok'), list):
                        processed_ok = set(str(x) for x in data.get('processed_ok'))
        except Exception:
            processed_ok = set()
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--lang=en-US")
        try:
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        except WebDriverException as e:
            with open(log_path, 'a', encoding='utf-8') as lf:
                lf.write(f"[ERROR] {label}: WebDriver start failed: {e}\n")
            return
        wait = WebDriverWait(driver, 10)
        try:
            ok = ensure_logged_in_with_cookies(driver, cookies, wait)
            if not ok:
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[WARN] {label}: login via cookies failed. Will retry with cookies from sheet if configured…\n")
                retry_url = args.cookies_sheet_csv_url
                if not retry_url and 'cookies_sheet_csv_url' in (cfg or {}):
                    retry_url = cfg.get('cookies_sheet_csv_url')
                if retry_url:
                    attempt = 0
                    while True:
                        attempt += 1
                        try:
                            accounts = fetch_accounts_from_csv(retry_url)
                        except Exception:
                            accounts = []
                        if not accounts:
                            with open(log_path, 'a', encoding='utf-8') as lf:
                                lf.write(f"[WARN] {label}: Attempt {attempt}: no cookies from sheet. Retrying in 10s…\n")
                            time.sleep(10)
                            continue
                        success = False
                        # Only retry with the account that matches our current label
                        sheet_account = next((pair for pair in accounts if pair[0] == label), None)
                        if sheet_account:
                            sheet_label, sheet_cookies = sheet_account
                            try:
                                if ensure_logged_in_with_cookies(driver, sheet_cookies, wait):
                                    cu = driver.get_cookie("c_user")
                                    uid = cu.get("value") if cu else "unknown"
                                    with open(log_path, 'a', encoding='utf-8') as lf:
                                        lf.write(f"[INFO] {label}: login successful with fresh sheet cookies (User ID: {uid})\n")
                                    cookies = sheet_cookies
                                    success = True
                            except Exception as e:
                                with open(log_path, 'a', encoding='utf-8') as lf:
                                    lf.write(f"[ERROR] {label}: Error during retry login: {e}\n")
                        else:
                            with open(log_path, 'a', encoding='utf-8') as lf:
                                lf.write(f"[WARN] {label}: Account not found in sheet during retry.\n")
                        
                        if success:
                            break
                        
                        with open(log_path, 'a', encoding='utf-8') as lf:
                            lf.write(f"[WARN] {label}: Attempt {attempt}: sheet cookies failed for this account. Retrying in 10s…\n")
                        time.sleep(10)

                else:
                    with open(log_path, 'a', encoding='utf-8') as lf:
                        lf.write(f"[ERROR] {label}: No cookies_sheet_csv_url configured. Cannot retry.\n")
                    return
            else:
                cu = driver.get_cookie("c_user")
                uid = cu.get("value") if cu else "unknown"
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[INFO] {label}: login successful (User ID: {uid})\n")

            local = {"joined": 0, "pending": 0, "left": 0, "errors": 0, "total": len(gids)}
            for i, gid in enumerate(gids, 1):
                # Skip if already processed for this account
                if str(gid) in processed_ok:
                    with open(log_path, 'a', encoding='utf-8') as lf:
                        lf.write(f"[{i}/{len(gids)}] {gid}: skipped_already_processed\n")
                    local["left"] += 1  # count as skipped
                    with lock:
                        progress[label] = local.copy()
                        if notifier:
                            notifier.send_or_update(format_progress(progress))
                    continue
                status = ""
                try:
                    status = attempt_join_group(driver, gid, wait)
                except Exception as e:
                    status = f"error:{e}"
                # Reclassify statuses based on current page indicators
                try:
                    def is_pending_page_local() -> bool:
                        try:
                            pending_x = (
                                "//span[contains(.,'Your membership is pending')]|"
                                "//span[contains(.,'Requested')]|"
                                "//span[contains(.,'request sent')]|"
                                "//div[@role='button' and .//span[normalize-space()='Cancel request']]|"
                                "//button[normalize-space()='Cancel request']"
                            )
                            return bool(driver.find_elements(By.XPATH, pending_x))
                        except Exception:
                            return False
                    if status == "join_button_not_found" and is_pending_page_local():
                        status = "pending:cancel_request_present"
                    if status == "clicked_no_confirm":
                        status = "pending:clicked_no_confirm"
                except Exception:
                    pass
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{i}/{len(gids)}] {gid}: {status}\n")
                if status == "joined":
                    local["joined"] += 1
                elif "pending" in status:
                    local["pending"] += 1
                elif status == "already_member":
                    local["left"] += 1
                elif status == "join_button_not_found":
                    local["errors"] += 1
                    try:
                        errors_registry.setdefault(label, []).append(str(gid))
                    except Exception:
                        pass
                else:
                    local["errors"] += 1
                    try:
                        errors_registry.setdefault(label, []).append(str(gid))
                    except Exception:
                        pass
                # Mark as processed only for non-error outcomes so error groups can retry next time
                try:
                    non_error_statuses = {"joined", "already_member"}
                    if status in non_error_statuses or ("pending" in status):
                        processed_ok.add(str(gid))
                        with open(state_path, 'w', encoding='utf-8') as sf:
                            json.dump({"processed_ok": sorted(list(processed_ok))}, sf, ensure_ascii=False)
                except Exception:
                    pass
                # Update shared progress
                with lock:
                    progress[label] = local.copy()
                    if notifier:
                        notifier.send_or_update(format_progress(progress))
                time.sleep(max(0.0, delay))
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def format_progress(progress: Dict[str, Dict[str, int]]) -> str:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = [
            "<b>🚀 Auto Join Status</b>",
            f"🕒 <i>{now}</i>",
            "",
            "<b>Legend</b>:",
            "✅ Joined | ⏳ Pending | 🙋 Skipped | ⚠️ Errors | 📦 Total | 📈 Progress",
            ""
        ]
        total_joined = total_pending = total_left = total_errors = total = 0
        for label, stats in sorted(progress.items()):
            j = stats.get('joined', 0)
            p = stats.get('pending', 0)
            l = stats.get('left', 0)
            e = stats.get('errors', 0)
            t = max(1, stats.get('total', 0))
            pct = int((j + p + l + e) * 100 / t)
            lines.append(
                f"🧑‍💻 <b>{label}</b> — ✅ {j} | ⏳ {p} | 🙋 {l} | ⚠️ {e} | 📦 {t} | 📈 {pct}%"
            )
            total_joined += j
            total_pending += p
            total_left += l
            total_errors += e
            total += t
        lines.append("")
        if total > 0:
            total_pct = int((total_joined + total_pending + total_left + total_errors) * 100 / total)
        else:
            total_pct = 0
        lines.append(
            f"<b>📊 Overall</b>: ✅ {total_joined} | ⏳ {total_pending} | 🙋 {total_left} | ⚠️ {total_errors} | 📦 {total} | 📈 {total_pct}%"
        )
        return "\n".join(lines)

    parser = argparse.ArgumentParser(description="Auto-join Facebook groups using cookies on m.facebook.com")
    parser.add_argument("--uids", default="uid.txt", help="Path to file containing group IDs or URLs (one per line)")
    parser.add_argument("--accounts", default="accounts", help="Directory containing *_cookies.json files")
    parser.add_argument("--account", type=int, default=None, help="Account number to use, e.g., 1 for accounts/1_cookies.json")
    parser.add_argument("--cookies", default=None, help="Explicit path to cookies JSON file (overrides --account)")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between group joins in seconds")
    parser.add_argument("--menu", action="store_true", help="Show interactive menu")
    parser.add_argument("--config", default="config.json", help="Path to JSON config file")
    # Google Sheet CSV source for cookies
    parser.add_argument("--cookies_sheet_csv_url", default=None, help="Published Google Sheet CSV URL containing columns: account_file,cookies_json")
    # Telegram settings
    parser.add_argument("--telegram_bot_token", default=None, help="Telegram bot token")
    parser.add_argument("--telegram_chat_id", default=None, help="Telegram chat id")
    parser.add_argument("--telegram_enabled", action="store_true", help="Enable Telegram updates")
    # Logs directory
    parser.add_argument("--logs_dir", default="logs", help="Directory to write per-account log files")

    args = parser.parse_args()

    # Load config JSON and merge into args if present
    def load_config(path: str) -> Dict:
        try:
            if path and os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    cfg = load_config(args.config)
    if cfg:
        # Flat keys
        if args.uids == "uid.txt" and isinstance(cfg.get("uids"), str):
            args.uids = cfg["uids"]
        if args.accounts == "accounts" and isinstance(cfg.get("accounts"), str):
            args.accounts = cfg["accounts"]
        if args.account is None and isinstance(cfg.get("account"), int):
            args.account = cfg["account"]
        if args.cookies is None and isinstance(cfg.get("cookies"), str):
            args.cookies = cfg["cookies"]
        if args.delay == 1.0 and isinstance(cfg.get("delay"), (int, float)):
            args.delay = float(cfg["delay"])
        if not args.headless and isinstance(cfg.get("headless"), bool):
            args.headless = cfg["headless"]
        if args.cookies_sheet_csv_url is None and isinstance(cfg.get("cookies_sheet_csv_url"), str):
            args.cookies_sheet_csv_url = cfg["cookies_sheet_csv_url"]
        if args.logs_dir == "logs" and isinstance(cfg.get("logs_dir"), str):
            args.logs_dir = cfg["logs_dir"]
        # Telegram nested
        tgc = cfg.get("telegram") or {}
        if not args.telegram_enabled and isinstance(tgc.get("enabled"), bool):
            args.telegram_enabled = tgc["enabled"]
        if args.telegram_bot_token is None and isinstance(tgc.get("bot_token"), str):
            args.telegram_bot_token = tgc["bot_token"]
        if args.telegram_chat_id is None and isinstance(tgc.get("chat_id"), (str, int)):
            args.telegram_chat_id = str(tgc["chat_id"]) 
        # AI settings
        ai = cfg.get("ai") or {}
        if isinstance(ai.get("model"), str) and not os.environ.get("OPENAI_MODEL"):
            os.environ["OPENAI_MODEL"] = ai["model"]
        if isinstance(ai.get("api_key"), str) and ai.get("api_key"):
            # Set for this process; prefer env vars for security in general
            os.environ["OPENAI_API_KEY"] = ai["api_key"]
        if ai.get("enabled") is False:
            # Explicitly disable by clearing key if set via config
            os.environ.pop("OPENAI_API_KEY", None)

    # Auto-start mode (non-interactive) from config
    auto_start = False
    if cfg and isinstance(cfg.get("auto_start"), bool):
        auto_start = cfg.get("auto_start") is True

    if (args.menu or len(sys.argv) == 1) and not auto_start:
        interactive_menu(args.uids, args.accounts)
        return

    gids = read_group_ids(args.uids)
    if not gids:
        print(f"[WARN] No group IDs found in {args.uids}. Add one ID or URL per line.")
        sys.exit(1)

    # Multi-account mode via Google Sheet CSV
    if args.cookies_sheet_csv_url:
        accounts = fetch_accounts_from_csv(args.cookies_sheet_csv_url)
        # Optional filter from config: list of labels to include
        filt = []
        if cfg and isinstance(cfg.get("accounts_filter"), list):
            filt = [str(x).strip() for x in cfg.get("accounts_filter") if str(x).strip()]
        if filt:
            accounts = [(label, cks) for (label, cks) in accounts if label in filt]
        # Optional numeric selection by 1-based index or range
        numbers: List[int] = []
        if args.account is not None:
            numbers = [args.account]
        elif cfg and isinstance(cfg.get("accounts_numbers"), list):
            raw_nums = cfg.get("accounts_numbers")
            for item in raw_nums:
                if isinstance(item, int):
                    numbers.append(item)
                elif isinstance(item, str) and '-' in item:
                    try:
                        a, b = item.split('-', 1)
                        start, end = int(a), int(b)
                        if start > end: start, end = end, start
                        numbers.extend(range(start, end + 1))
                    except Exception: pass
                else:
                    try: numbers.append(int(item))
                    except Exception: pass
        
        # If numbers has exactly two elements and the user might mean a range (e.g. [1, 7] -> 1 to 7)
        # and it's from the config list, we can be smart, but let's stick to explicit or range strings.
        # However, to be extra helpful for the user's current config [1, 7]:
        if not args.account and cfg and isinstance(cfg.get("accounts_numbers"), list) and len(cfg.get("accounts_numbers")) == 2:
            # If they provided [1, 7] and there are many accounts, they likely mean a range
            a, b = cfg.get("accounts_numbers")[0], cfg.get("accounts_numbers")[1]
            if isinstance(a, int) and isinstance(b, int) and a < b and b > a + 1:
                print(f"[INFO] Interpreting accounts_numbers [{a}, {b}] as range {a}-{b}")
                numbers = list(range(a, b + 1))

        if numbers:
            sel = []
            for idx, pair in enumerate(accounts, start=1):
                if idx in numbers:
                    sel.append(pair)
            accounts = sel

        if not accounts:
            print("[ERROR] No accounts matched the selection criteria or parsed from CSV.")
            sys.exit(1)

        # Detect duplicate accounts (same c_user)
        user_id_to_labels: Dict[str, List[str]] = {}
        for lbl, cks in accounts:
            cu = next((c.get("value") for c in cks if c.get("name") == "c_user"), None)
            if cu:
                user_id_to_labels.setdefault(str(cu), []).append(lbl)
        
        duplicates = {uid: lbls for uid, lbls in user_id_to_labels.items() if len(lbls) > 1}
        if duplicates:
            print("[WARN] Duplicate accounts detected in selection (multiple labels for same c_user):")
            for uid, lbls in duplicates.items():
                print(f"  - User ID {uid} is used by: {', '.join(lbls)}")
            print("[WARN] These accounts will run in parallel but act as the same user.")

        notifier = TelegramNotifier(args.telegram_enabled, args.telegram_bot_token, args.telegram_chat_id)
        errors_registry: Dict[str, List[str]] = {}
        # Start command polling to handle /errors
        if args.telegram_enabled:
            def on_cmd(text: str):
                t = text.strip().lower()
                if t.startswith('/errors'):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        lbl = parts[1].strip()
                        items = errors_registry.get(lbl) or []
                        if items:
                            notifier.send_message(f"❗ Errors for <b>{lbl}</b> (count={len(items)}):\n" + "\n".join(items[:200]))
                        else:
                            notifier.send_message(f"No errors recorded for <b>{lbl}</b> yet.")
                    else:
                        # All accounts summary
                        lines = ["<b>❗ Error Groups</b>"]
                        total = 0
                        for lbl, arr in errors_registry.items():
                            if arr:
                                total += len(arr)
                                lines.append(f"- <b>{lbl}</b> ({len(arr)}): " + ", ".join(arr[:20]) + (" …" if len(arr) > 20 else ""))
                        if total == 0:
                            notifier.send_message("No error groups recorded yet.")
                        else:
                            notifier.send_message("\n".join(lines))
            notifier.poll_commands(on_cmd)

        progress: Dict[str, Dict[str, int]] = {}
        lock = threading.Lock()
        threads: List[threading.Thread] = []
        for label, cookies in accounts:
            t = threading.Thread(
                target=do_join_with_cookies,
                args=(gids, cookies, args.headless, args.delay, label, notifier, args.logs_dir, progress, errors_registry, lock),
                daemon=True,
            )
            threads.append(t)
            t.start()
            time.sleep(0.3)
        for t in threads:
            t.join()
        # Final update
        if args.telegram_enabled:
            notifier.send_or_update(format_progress(progress))

        return

    # Multi-account local cookies specified in config.local_accounts
    if cfg and isinstance(cfg.get("local_accounts"), list) and cfg.get("local_accounts"):
        labels_and_cookies: List[Tuple[str, List[Dict]]] = []
        for entry in cfg.get("local_accounts"):
            if not entry:
                continue
            p = str(entry)
            if not os.path.isabs(p):
                p = os.path.join(args.accounts, p)
            if not os.path.exists(p):
                continue
            try:
                cookies = load_cookies(p)
                labels_and_cookies.append((os.path.basename(p), cookies))
            except Exception:
                continue
        if not labels_and_cookies:
            print("[ERROR] No valid local_accounts cookie files found.")
            sys.exit(1)
        notifier = TelegramNotifier(args.telegram_enabled, args.telegram_bot_token, args.telegram_chat_id)
        progress: Dict[str, Dict[str, int]] = {}
        lock = threading.Lock()
        threads: List[threading.Thread] = []
        for label, cookies in labels_and_cookies:
            t = threading.Thread(
                target=do_join_with_cookies,
                args=(gids, cookies, args.headless, args.delay, label, notifier, args.logs_dir, progress, lock),
                daemon=True,
            )
            threads.append(t)
            t.start()
            time.sleep(0.3)
        for t in threads:
            t.join()
        if args.telegram_enabled:
            notifier.send_or_update(format_progress(progress))
        return

    # Single-account local cookies mode
    cookie_file = find_cookie_file(args.accounts, args.account, args.cookies)
    if not cookie_file:
        print("[ERROR] Could not find cookies file. Provide --account N or --cookies path.")
        sys.exit(1)
    do_join(gids, cookie_file, args.headless, args.delay)


if __name__ == "__main__":
    main()
