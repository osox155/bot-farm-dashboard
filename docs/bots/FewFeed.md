# FewFeed (`fewfeedbotv6/fewfeed_bot_template.py`)

A Facebook **group-posting** bot (NOT a reply bot). It drives the third-party **fewfeed.online** "Auto
Post" web tool (plus a bundled Chrome extension `fewfeedv2`) via Selenium to broadcast **one prompt to
many groups** per account. ~230KB.

## Entry point
`python fewfeed_bot_template.py`. `__main__` (line 4877) → `main_menu()` (4768), an interactive stdin
menu. In the farm `start-bots.ps1` → `Start-FewFeedInstance` sends `MenuOptionToLaunch=1` then the
accounts CSV over stdin. Runs on **DesktopIndex 3**, `Enabled:false`. Per-account cookies in
`accounts/<id>_cookies.json`; per-account prompt in `prompt/<id>.txt` (`default.txt` fallback).

## Menu
| # | Action |
|---|--------|
| 1 | **Launch Account** — enqueue selected accounts onto a background launch manager; each opens Chrome, injects cookies, logs into FewFeed, auto-posts (if `enable_auto_post`), then keeps a results-watcher running. *(the launcher uses this)* |
| 2 | List accounts |
| 3 | Close all browsers (quit drivers, delete session profiles) |
| 4 | Toggle auto-post (persists to `config.json`) |
| 5 | Run setup wizard (build the `template_chrome_profile` with the extension) |
| 6 | Accounts launch (open Settings only) — manual fixups, no posting |
| 7 | Exit |

## Two logins
1. **Facebook** = cookie-only (no password). Cookies from `accounts/<id>_cookies.json` else a published
   **Google Sheet CSV** (`cookies_sheet_csv_url`), normalized (domain forced to `.facebook.com`) and
   injected via **CDP** `Network.setCookies`. Verified at `facebook.com/settings` (`c_user` cookie).
2. **FewFeed tool** = **hard-coded** email/password (a dedicated FewFeed account; the literal
   credentials live in `fewfeed_bot_template.py` ~line 3437 — see KNOWN_ISSUES) entered on
   the fewfeed.online sign-in page; handles the "Upgrade Your Plan" paywall by going back and re-logging
   in (max 3 restarts).

## Posting flow
1. Click "Use this tool" → Auto Post.
2. Paste the per-account prompt (clipboard Ctrl+V), set `thread_value` + `delay_value`.
3. "Select all groups", then (if Sheets enabled) `gs_fetch_posted_groups` → uncheck already-posted groups.
4. Optionally paste images (CF_HDROP clipboard).
5. Click Post, wait for the "Posting started"/"successfully" toast.
6. Spawn `_watch_results_loop` (detached thread).

### Results watch / success recording
Polls the results list until the Stop button disappears (+60s) or `post_watch_seconds`. For each
non-error row it clicks "View Post" to harvest the real FB post URL, extracts the `group_id`, and appends
`(group_id, post_url)` to the **Google Sheet `posted_groups`** worksheet (the cross-run dedupe ledger).
`save_telemetry('FewFeed', id, stats={'posts': len(session_seen)})`.

### FB session monitor
`start_fb_monitor` loops ~10s (non-intrusive `c_user` check). On logout: Telegram-alert, close FewFeed
tabs, refresh cookies from the Sheet, re-inject; on restore, resume. Exponential backoff to 300s. **No
TOTP/2FA** — recovery is purely cookie-refresh from the Sheet.

## ⭐ The telemetry fix
FewFeed is a posting bot, but its status pings carry a cumulative `posts` count. **Old bug:** it logged a
fake `reply_sent` on every ping → `stats_tracker` incremented `total_replies`, inflating the dashboard's
**reply** counters. **Now:** `save_telemetry` (line ~298) calls **only** `log_login_failure` /
`log_login_success` and **never** `log_event` for posts/replies. Verified: the only two `_ff_tracker`
call sites in the whole file are the login ones (lines ~310/312). The `posts` count lives **only** in the
local telemetry JSON (`stats.posts`); the broker reports it as "posts published," not replies.

## What it writes
| Target | Content |
|--------|---------|
| `../telemetry/FewFeed_<account>.json` | status, failed_logins, `stats={'posts': len(session_seen)}` |
| Supabase (via `get_tracker('FewFeed')`) | **login state only** — no post/reply events ever |
| Google Sheet `posted_groups` | per-account `Account N`/`Posts N` columns appended on each success |
| `accounts/<id>_cookies.json` | overwritten by `refresh_cookies_from_sheet` |
| `session_profiles/session_<id>` | disposable Chrome profile per launch |

## Config highlights (`fewfeedbotv6/config.json`)
`enable_auto_post`, `thread_value`/`delay_value`, `post_with_images`/`images_path`,
`extension_trigger_mode` (`web` vs `hotkey`), `profile_mode` (`template`/`minimal`),
`post_watch_seconds`, `results_min_age`, `background_check_interval`,
`google_sheets.{enabled,spreadsheet_id,worksheet_name,service_account_json}`, `cookies_sheet_csv_url`,
`telegram.{bot_token,chat_id}`.

## Notable gotchas (see [KNOWN_ISSUES.md](../KNOWN_ISSUES.md))
- The **first** `load_config` (line 272) has **no return** → returns `None`; it's shadowed by the second,
  correct `load_config` (line 2932) that's actually used. The module-level `config = load_config()` runs
  while only the buggy def exists, so the initial global `config` is `None` until `main_menu()` reassigns it.
- Hard-coded FewFeed credentials and a Google service-account JSON are committed.
- The watcher runs detached; it signals "needs restart" to the main thread via a `threading.Event` (the
  "Upgrade Your Plan" page is the main mid-post failure mode → triggers a full restart, max 2).
