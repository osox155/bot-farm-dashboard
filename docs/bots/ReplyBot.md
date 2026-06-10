# ReplyBot (`ReplyBotv7/new.py`)

The Messenger auto-reply bot — the only bot currently enabled, and the one you've been editing. ~5900
lines. Drives multiple **Facebook Messenger** accounts via Selenium/Chrome, one daemon thread + Chrome
window per account.

## What it does (three jobs)
1. **Auto-accept & reply to message requests** — the "You may know" and "Spam" request tabs.
2. **Main-chat auto-reply** — reply in existing group conversations.
3. **Bulk send** — send a templated message to a recipients list.

## Entry point
Run `python new.py` from `ReplyBotv7/`. `__main__` (`new.py:5887`) → recovers the previous session,
sends an initial status, starts background reporters, then `menu()` (`new.py:4621`). All behavior comes
from `config.json` (relative to CWD). The real per-account worker is **`run_account()` (`new.py:3719`)**,
launched as daemon threads. (`main()` at `new.py:3693` is **dead code** — never called.)

In the farm it's launched by `start-bots.ps1` with `OptionSequence: 2` → menu option **2** (multiple
accounts), accounts chosen by daily rotation.

## Menu options
| # | Action |
|---|--------|
| 1 | Single-account auto-reply (`option1`) |
| 2 | **Multiple-account auto-reply (`option2`) — what the launcher uses** |
| 3 | List accounts |
| 4 | Add account (paste cookie JSON → `accounts/<n>_cookies.json`) |
| 5 | Remove account |
| 6 | Rename account |
| 7 | Edit reply message (`reply_message.txt`) |
| 8 | Pause/Resume (creates/removes `pause.flag`; all loops poll it) |
| 9 | View logs (live tail in a new window) |
| 10 | "Exit" — actually kills Chrome + deletes logs and **returns to the menu** (does not exit the process) |
| 11 | Main-chat auto-reply (`option11`) — group chats only, skips personal `/e2ee/t/` |
| 12 | Bulk send (`option12`) — reads `recipients.txt` |
| 13 | Toggle auto popup closer (label always prints "ON" regardless of real state) |

## ⭐ Login & cookies (the part you changed)

Cookies live one file per account at `accounts/<n>_cookies.json`. The shared **Google Sheet** is the
cross-machine source of truth. Two refresh modes:
- **`api`** (active): `gspread` + `service_account.json`, reads **and writes back** cookies.
- **`published_csv`**: read-only via `requests`+CSV. ⚠️ **currently broken** (references an undefined
  `fieldnames`) — only `api` mode works.

### `run_account()` persistent login loop — *as redesigned this session*
On launch it pre-syncs cookies from the sheet, then enters a **persistent login loop** (`new.py:~3790`):

1. Inject cookies into `messenger.com`, check the URL for `login`/`recover`.
2. **On every retry (attempt > 1) it re-pulls cookies from the Google Sheet** (`refresh_account_cookies`)
   and reloads them — so the operator can fix cookies in the sheet and the next loop picks them up
   **without restarting the bot**.
3. **It never gives up and never closes Chrome on a failed login** — no 3-attempt cap, no `return`. It
   marks the window `[LOGIN RETRY n]`, sets status `🔴 Login failed — retrying`, waits
   `login_retry_delay_seconds` (default 60), and loops.
4. The consolidated Telegram/dashboard failed-login alert is sent **only once** (on the first failure)
   so the "login failures today" counter isn't re-incremented every 60s.
5. On success: save fresh `driver.get_cookies()` locally for all accounts; push back to the Sheet
   **only for accounts that have full credentials** (username+password+totp_secret); non-credentialed
   accounts are "manual mode" (operator updates the sheet by hand). Then enter `process_message_requests`.

> ✅ **Fixed (2026-06-10).** This persistent-login behavior was previously defeated by a latent bug: the
> first-failure alert call (`send_or_update_failed_login_notice`) crashed with `NameError:
> FAILED_LOGIN_LOCK is not defined`, which propagated to `finally: driver.quit()` and closed Chrome. The
> missing module globals are now defined (**[KNOWN_ISSUES.md → BUG-1](../KNOWN_ISSUES.md)**), and ReplyBot
> now **delegates** its login alerts to the central broker (writes telemetry, skips its own Telegram send
> — `telegram.alerts_via_broker`), so the alert path no longer crashes and Chrome stays open across
> retries.

### Credentials + TOTP fallback (mid-run)
`handle_logout_and_refresh_with_credentials` (used by `process_message_requests` when logout is detected
mid-run): tries cookie refresh first, then, for accounts with a `*.credentials.json`,
`login_with_credentials` — email/password + `pyotp` TOTP, up to 5 fresh-code attempts. Uses per-account
exponential backoff (`BASE_BACKOFF_SECONDS=30 × count`, capped 60s).

## Key runtime flows
- **`process_message_requests`** (`new.py:3598`): poll `pause.flag` → check `is_logged_out` **first**
  (before rehoming, to avoid redirect loops) → recover if needed → click Requests icon → "You may know"
  tab → accept & reply → "Spam" tab → accept & reply → sleep `delay_check_requests` (60s) → loop.
- **`accept_and_reply`**: open thread (3 click strategies × 3 retries, caches URL), click Accept,
  detect uncontactable banners → skip, expand `{{RAN_M(a|b|c)}}` template, paste via clipboard + Enter
  (3 send attempts), verify by searching the message text in the DOM → `update_statistics`.

## What it writes
| Target | Content |
|--------|---------|
| `accounts/<n>_cookies.json` | cookie store (overwritten from Sheet + after each login) |
| `accounts/<n>_cookies.credentials.json` | username/password/totp (operator-created; read-only here) |
| `../telemetry/ReplyBot_<account>.json` | status, stats{replies,messages}, failed_logins, recent_events |
| Supabase (via `stats_tracker` `get_tracker('ReplyBot')`) | sessions, login success/failure, reply/message events, account status |
| `logs/<fname>.log` (+ mainchat/bulk/summary variants) | per-account fsync'd logs |
| `pause.flag` | pause toggle |

## Config highlights (`ReplyBotv7/config.json`)
- `google_sheets.{enabled,mode,service_account_json,spreadsheet_id,sheet_name,account_column,json_column,published_csv_url}`
- `auto_login_with_credentials` (TOTP fallback toggle)
- `login_retry_delay_seconds` (persistent-retry interval; **absent from config → code default 60**)
- `delay_retry_login` / `delay_check_requests` / `delay_resend_same_chat` …
- `telegram.{bot_token,chat_id,alerts_only,…}` — `alerts_only` defaults **true**, suppressing routine
  status/report messages so only failed-login alerts go to Telegram (the dashboard replaced routine
  reporting).
- `watchdog.session_limit_hours` — a hard watchdog `os._exit(0)`s after N hours (default 6).

## Notable bugs (see [KNOWN_ISSUES.md](../KNOWN_ISSUES.md) for the full list with refs)
- **BUG-1 (critical):** undefined `FAILED_LOGIN_LOCK` / state-file / `LAST_*_EDIT_TS` globals → crashes
  the failed-login alert path → closes Chrome (defeats the persistent-login fix).
- **BUG-2:** `bot_statistics` sub-dicts (`option1_replies`, …) are never created → `update_statistics`
  raises `KeyError` on the first reply, swallowed → **reply/message counts can silently stay 0** (matches
  the `ReplyBot_3.json` showing `replies:0`).
- **BUG-3:** `load_account_credentials` references an undefined `logger` → NameError whenever a valid
  credentials file is found, defeating the TOTP path (caught by callers).
- **BUG-4:** `published_csv` cookie-refresh branch references undefined `fieldnames` → always throws.
- Several functions are defined twice (the second wins) — harmless.
- Secrets committed: live Telegram token in `config.json`, a real `*.credentials.json`, `service_account.json`.
