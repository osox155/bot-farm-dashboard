# Architecture

## Components and how they connect

```
                          bot-launcher.config.json
                                   │ (which bots, desktops, accounts, rotation)
                                   ▼
                          start-bots.ps1  ──────────────► telemetry_broker.py  ──► Telegram
                                   │  (stdin: menu opt + accounts)        ▲          (2 rolling msgs)
        ┌──────────────┬──────────┼───────────────┬──────────────┐       │ reads
        ▼              ▼          ▼               ▼              ▼        │ telemetry/*.json
   AutoJoinBot   CommentsReply  FewFeed       ReplyBot        (each bot writes ──┘
   (desktop 1)   (desktop 2)   (desktop 3)   (desktop 4)       telemetry/<Bot>_<acct>.json)
        │              │          │               │
        │  Selenium/Chrome + cookies (local files + Google Sheets)
        │              │          │               │
        └──────────────┴────┬─────┴───────────────┘
                            │ stats_tracker.py  (start_session, log_event, log_login_*)
                            ▼
                        Supabase  (accounts, sessions, events, login_attempts, daily_stats)
                            ▲
                            │ reads (PostgREST)
                       dashboard.py  ──► Bottle web UI ("Dashboard Overview")
                       (Procfile/wsgi.py → PythonAnywhere/Render)
```

## Two independent telemetry channels

This is the single most important architectural fact: **there are two monitoring systems and they do
not share storage.** They can (and do) disagree.

| | Supabase channel | Telegram broker channel |
|---|---|---|
| Producer | bots → `stats_tracker.py` | bots → `telemetry/<Bot>_<acct>.json` files |
| Store | Supabase Postgres tables | transient JSON files on disk |
| Consumer | `dashboard.py` (web UI) | `telemetry_broker.py` (Telegram) |
| Staleness rule | account `active` & `last_active` > **300s** → rendered `offline` | file `last_update` > **120s** → `Inactive / Stopped` |
| Survives restart? | yes (durable) | no (broker wipes `telemetry/` on startup, `/reset`, and parent exit) |

Because the two channels use different sources and different staleness thresholds, the web dashboard
and the Telegram message can show different numbers at the same moment. That's expected, not a bug.

## Data flow, end to end

1. **Launch.** `start-bots.bat` → `start-bots.ps1` reads `bot-launcher.config.json`, starts
   `telemetry_broker.py` (hidden, `--parent-pid`), then for each *enabled* bot: switches to its virtual
   desktop and launches the Python entry point as a child process, feeding menu option(s) + an account
   CSV into the child's **stdin**.
2. **Login.** Each bot loads per-account cookies (local file, falling back to / refreshed from a shared
   Google Sheet) and injects them into Chrome. No password except ReplyBot's optional TOTP fallback.
3. **Work.** Each bot does its job (reply / comment / join / post) in a loop, one Chrome window per
   account, often one thread per account.
4. **Report (durable).** Significant events go to Supabase via `stats_tracker`:
   `start_session` → `log_login_success/failure` → `log_event('reply_sent'|'message_sent'|...)` →
   `end_session`. This drives the **web dashboard**.
5. **Report (transient).** Each loop the bot writes `telemetry/<Bot>_<account>.json` (status, stats,
   `failed_logins`). This drives the **Telegram broker**.
6. **Alerts.** On a failed/expired login the bot marks the account `logged_out` (Supabase) and adds it
   to its `failed_logins` map (telemetry file) → both the dashboard "Attention Required" panel and the
   broker's logout-alerts message surface it.
7. **Shutdown.** The broker's parent-watchdog notices the launcher PID died, kills child bots + Chrome +
   chromedriver, and wipes `telemetry/`. Note it does **not** call `end_session`, so Supabase accounts
   may stay `active` until the dashboard's 300s rule renders them `offline`.

## Conventions shared across all bots

- **Accounts are numbered cookie files**: `<bot>/accounts/<id>_cookies.json` (Selenium cookie-dict
  format). The "account name" everywhere is just that `<id>` (e.g. `2`, `3`, `6`).
- **Cookies are the auth.** The shared **Google Sheet** is the cross-machine source of truth; the
  operator updates cookies there and the bots pull them. ReplyBot can additionally do a
  credentials+TOTP fresh login for accounts that have a `*.credentials.json`.
- **`stats_tracker` is imported from the parent dir** (`../stats_tracker.py`) via a `sys.path` insert,
  tagged with the bot name (`get_tracker('ReplyBot')` etc.). If the import fails it degrades to a no-op
  `_Null` tracker, so stats can silently disappear without crashing the bot.
- **Telemetry files are written atomically** (temp file + `os.replace`).
- **Secrets are committed** in the working tree (Telegram tokens in each `config.json`, the Supabase
  anon key in `supabase_config.json`, Google service-account JSONs, real cookie/credential files).
  See [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## What lives where

```
BOT Py/
├─ start-bots.ps1 / .bat        launcher
├─ bot-launcher.config.json     fleet config (enabled bots, desktops, accounts, rotation)
├─ stats_tracker.py             Supabase writer/reader library (shared by all bots + dashboard)
├─ dashboard.py + templates/    web dashboard (Bottle)
├─ telemetry_broker.py          Telegram broker
├─ reset_account_states.py      maintenance (active → idle)
├─ setup_supabase.sql           the 5-table schema
├─ supabase_config.json         Supabase URL + anon key
├─ telemetry/                   transient per-account JSON + broker_state.json + broker.log
├─ stats_db/                    legacy/local SQLite (Supabase is the live store)
├─ AutoJoinBot/                 bot + its accounts/, config.json, uid.txt
├─ CommentsReplyBot/            bot + its accounts/, config.json, groups.txt, reply_text.txt
├─ ReplyBotv7/                  bot + its accounts/, config.json, reply_message.txt, recipients.txt
├─ fewfeedbotv6/                bot + its accounts/, config.json, prompt/, the fewfeed extension
└─ docs/                        ← you are here
```
