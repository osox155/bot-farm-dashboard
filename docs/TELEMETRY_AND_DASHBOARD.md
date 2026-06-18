# Telemetry, Dashboard & Data Layer

This covers `stats_tracker.py`, `dashboard.py` + `templates/`, `telemetry_broker.py`,
`setup_supabase.sql`, and `reset_account_states.py` — the observability stack.

Remember: there are **two independent channels** (see [ARCHITECTURE.md](ARCHITECTURE.md)). The
**Supabase** channel feeds the web dashboard; the **Telegram broker** channel is a separate file-based
system. They don't share storage.

---

## 1. Supabase data model (`setup_supabase.sql`)

Seven tables. RLS is enabled but with **permissive anon policies** (`USING true` / `CHECK true`), so the
committed anon key can read+write+delete everything.

> **First-time setup:** paste the full `setup_supabase.sql` into the Supabase SQL Editor
> (https://supabase.com/dashboard/project/stfrmlgckxnzlmvietcx/sql/new) and click **Run**.
> All `CREATE TABLE IF NOT EXISTS` statements are idempotent — safe to re-run; existing data is
> preserved. The `bot_commands` and `machines` tables **must** exist before the remote-control
> buttons in the dashboard will work.

| Table | Grain | Key columns |
|-------|-------|-------------|
| `accounts` | one row per **(account, bot)** | `name`, `bot_name`, `last_active` (TIMESTAMPTZ), `status` (default `unknown`); `UNIQUE(name, bot_name)` |
| `sessions` | one row per **bot run** | `session_id` (TEXT UNIQUE), `bot_name`, `started_at`, `ended_at`, `accounts_count`, `total_replies/total_messages/total_failures`, `status` (default `running`) |
| `events` | append-only **activity log** | `session_id`, `bot_name`, `account_name`, `event_type`, `option_type`, `details` (JSONB), `created_at` |
| `login_attempts` | one row per **login attempt** | `account_name`, `success` (0/1), `reason`, `attempted_at` |
| `daily_stats` | per **(date, account, bot)** | `total_replies/total_messages/total_failures/login_failures`; `UNIQUE(date, bot_name, account_name)` |
| `bot_commands` | one row per **remote-control command** | `action`, `bot_name`, `machine_id` (NULL = broadcast), `status` (`pending`/`done`/`error`), `result`, `created_at`, `executed_at` |
| `machines` | one row per **active PC** | `machine_id` (TEXT PK = `socket.gethostname()`), `last_seen` (TIMESTAMPTZ) — upserted every 60 s by the broker |

Indexes on `events(session_id, bot_name, account_name, created_at)` and `daily_stats(date)`.

`event_type` values seen in practice: `login_success`, `login_failed`, `reply_sent`, `message_sent`,
`reply_failed`, `message_failed`, `status_change`, `group_joined`.

---

## 2. `stats_tracker.py` — the writer/reader library

Config: reads `supabase_config.json` (`supabase_url`, `supabase_key`); falls back to env
`SUPABASE_URL` / `SUPABASE_KEY`. If neither is present, `_API_BASE` is `None` and **every `_supa_*`
helper becomes a silent no-op** (returns `[]`/`None`) — the tracker is inert and the dashboard shows
nothing, with no error.

### REST helpers (`_supa_*`)
All hit `{url}/rest/v1/<table>` (PostgREST) with `apikey` + `Bearer` headers, 10s timeout.
- `_supa_get` → GET, returns rows or `[]` (swallows errors).
- `_supa_post` → INSERT.
- `_supa_patch` → UPDATE by filter, fire-and-forget.
- `_supa_delete` → DELETE; **the only helper that raises** on failure (so reset endpoints surface errors).
- `_supa_upsert` → POST with `Prefer: resolution=merge-duplicates` + `on_conflict` (used for `accounts`
  on `name,bot_name` and `daily_stats` on `date,bot_name,account_name`).

> ⚠️ Counter increments (`_increment_daily`, `_increment_session`) are **read-then-write, not atomic**.
> They're only safe because each tracker call holds an in-process `threading.Lock`. Two *separate bot
> processes* incrementing the same `daily_stats` row can lose increments.

### Public API
- **Writers:** `start_session`, `end_session`, `log_event`, `log_login_success`, `log_login_failure`,
  `set_account_status`.
- **Readers:** `get_active_accounts`, `get_today_stats`, `get_daily_stats`, `get_recent_events`,
  `get_account_history`, `get_sessions`, `get_bots_list`, `get_summary_report`.
- **Resets:** `reset_today`, `reset_bot`, `reset_account`, `reset_all`.

---

## 3. Account status lifecycle ⭐

This is the part that drives the dashboard cards and has been the subject of recent fixes. The DB
default is `unknown`; the meaningful runtime statuses are:

| Status | Meaning | Written by |
|--------|---------|-----------|
| `active` | currently launched & alive | `log_login_success`, and any `log_event` with an account name |
| `running` | synonym for `active` in all **readers** | only ever via `set_account_status` (the tracker itself only writes `active`) |
| `logged_out` | login/cookie failure | `log_login_failure` |
| `idle` | **launched then stopped this run** | `end_session` (flips this bot's `active`/`running` → `idle`) |
| `paused` | operator-paused | `set_account_status` (dashboard action) |
| `offline` | **stale `active`** — *render-only* | NOT stored; computed by the dashboard's 300s rule |

### ⭐ DECISION 1 — `start_session` no longer pre-registers the roster
Previously `start_session(accounts=[...])` upserted **every** known cookie file as `idle` (originally
`active`). That made accounts that were never launched show up on the dashboard. **Now `start_session`
only creates the `sessions` row** — an account materializes its `accounts` row the first time it
actually acts. (`stats_tracker.py:~148`.)

**Consequence:** `idle` now means *"this account launched and then stopped"*, never
*"known-but-never-launched."* This is why the dashboard's IDLE count dropped — the phantom roster rows
no longer get created. `reset_account_states.py` exists to clean up stale rows left by the old behavior.

### `end_session`
`PATCH accounts SET status='idle' WHERE bot_name=eq.<bot> AND status in (active,running)`. Flips a
bot's running accounts to `idle` immediately on stop (so the dashboard doesn't wait ~5 min for the
stale rule). Leaves `logged_out` rows untouched so failures still surface.

---

## 4. The web dashboard (`dashboard.py`)

Bottle app. Auto-opens a browser; host `0.0.0.0`, port from env `PORT` (default `8765`).

### Routes
- Pages: `/` (overview), `/bot/<name>`, `/account/<name>`, `/history`, `/login`.
- JSON APIs: `/api/overview`, `/api/bot/<name>`, `/api/account/<name>`, `/api/sessions`, `/api/events`,
  `/api/daily`.
- POST: `/api/reset/{today,bot/<name>,account/<name>,all}`, `/api/action/account` (set status).

### Auth
If env `DASHBOARD_PASSWORD` is **empty → auth disabled, every route open.** If set, a `before_request`
hook gates all non-`/login`, non-`/static/` routes. The token is `HMAC-SHA256(key=password,
msg="password:<utc_day_number>")[:16]` — so it **rotates daily** even though the cookie max-age is 7
days (operator must re-login each day).

### ⭐ How the overview cards are computed (`/api/overview`)
Per account, first apply the **stale rule**: if `status in (active, running)` and
`now - last_active > 300s`, override to `offline`. Then:
- **RUNNING** = `status in (active, running)`
- **FAILED** = `status == logged_out`
- **IDLE** = `status in (idle, offline)` — *both mean "not currently launched," reported together*
- **PAUSED** = `status == paused`
- ⭐ **DECISION 3 — TOTAL** = accounts whose status is **NOT** in `(idle, offline)` — i.e. only
  running/failed/paused count toward TOTAL. Idle/offline accounts are **excluded** from the total. So
  the "Total Accounts" card can read lower than the number of account rows that actually exist.

The per-bot grid (`bot_accounts`) mirrors this: active/failed count into the bot's total; idle/offline
only go into the bot's `idle` field.

> The 300s threshold is **hardcoded in three places** (`dashboard.py:91, 164, 199`) and must be kept in
> sync if you ever change it.

### ⭐ DECISION 2 — Timezone handling (don't "simplify" this)
- **Storage:** `stats_tracker._iso_now()` = `datetime.utcnow().isoformat() + "Z"` — always UTC.
- **Read back:** `_parse_ts` treats `Z`/naive strings as UTC → correct epoch, so the 300s offline
  check is correct on any server timezone.
- **Output:** `dashboard._fmt_ts` emits `datetime.utcfromtimestamp(ts).isoformat() + "Z"`. The code
  comment explicitly warns **not** to use `fromtimestamp()` (that would bake in the server's local
  offset and then mislabel it `Z`).
- **Browser:** `templates/shared.js` `fmtDateTime()` parses the `Z` string with `new Date()` and renders
  in the **viewer's** local time.

### Front-end
`templates/dashboard.html` (+ `shared.css`/`shared.js` injected via `/*SHARED_CSS*/` / `/*SHARED_JS*/`
placeholders) consumes `/api/overview`. Cards use the `*_count` fields; the 14-day chart uses
`daily_stats`/`bot_daily` (Chart.js from CDN); theme is stored in `localStorage['botfarm-theme']`.

---

## 5. Remote control & machine tracking

The cloud dashboard (PythonAnywhere/Render) **cannot run local processes** — so stop/restart buttons
work through an indirection:

```
Dashboard → enqueue_command() → bot_commands (Supabase) ← broker polls every 10 s → executes locally
```

### Machine identification

When the broker starts (`telemetry_broker.py`), it calls `socket.gethostname()` to get a **machine_id**
and immediately upserts that name into the `machines` table (`stats_tracker.register_machine`). It
re-upserts every **60 seconds** as a heartbeat.

The dashboard `/api/overview` and `/api/machines` endpoints query `machines` for rows with
`last_seen` within the last 5 minutes. The **Bot Control** panel shows:
- **"✓ DESKTOP-ABC123 (last seen 12:34:56)"** when the broker is online.
- **"⚠ No active PC broker detected"** when nothing has checked in.

### Command targeting

| `machine_id` value in `bot_commands` | Which brokers pick it up |
|--------------------------------------|--------------------------|
| `NULL` | **All** running brokers (broadcast) |
| `"DESKTOP-ABC123"` | Only the broker on that specific PC |

Dashboard currently enqueues commands with `machine_id=NULL` (broadcast), which is correct for a
single-PC setup. If you run brokers on two PCs simultaneously, both will execute every command.

### Stop / restart flow

1. Operator clicks "Stop All" in dashboard → `POST /api/control/stop-all` → `enqueue_command("stop-all")`.
2. Broker's main loop calls `process_remote_commands(machine_id=...)` every 10 s.
3. Broker finds the pending row → calls `_force_kill_all_bots()` (taskkill + PowerShell) → marks command `done`.
4. Dashboard's `pollCommandStatus()` polls `/api/control/commands` and shows the PC's result in a toast.

**Prerequisite:** the `bot_commands` and `machines` tables must exist. Run `setup_supabase.sql` once.
Without them, commands silently fail: `_supa_post` returns `None` (the table 404 is swallowed) and
the dashboard incorrectly shows "queued" while nothing was written.

---

## 6. Telegram broker (`telemetry_broker.py`)

A **separate** process (started by the launcher). It does **not** touch Supabase or `stats_tracker`.

### ⭐ Alerts-only policy (2026-06-10) — Telegram = account login problems, for all bots
By design the broker now runs in **alerts-only** mode (`telegram.alerts_only`, default **true**, read
from the first bot `config.json` with a token — `ReplyBotv7/config.json`). In this mode the auto-loop
pushes **only** the consolidated **login-problem** alert and **never** the routine status/stats
dashboard:
- Each loop it calls `build_logout_alerts(logout_alerts)`. `compile_telemetry()` already aggregates the
  `failed_logins` map from **every** bot's telemetry file, so this one message covers the whole farm.
- If one or more accounts have a login failure / expired cookie → send/update the alert (on the `alerts`
  message channel).
- If everything is healthy → `delete_message("alerts")` (the message disappears).
- Set `telegram.alerts_only: false` to restore the old full stats dashboard.

**Single voice:** the bots feed the broker via telemetry and do **not** message Telegram themselves —
CommentsReplyBot/AutoJoinBot stub their Telegram methods, FewFeed's `tg_alert_add/remove` only write
telemetry, and **ReplyBot now delegates** (`telegram.alerts_via_broker`, default true): its
`send_or_update_failed_login_notice` / `clear_failed_login_notice` still write telemetry + stats but skip
the direct Telegram send, so the operator gets exactly one consolidated message instead of duplicates.

### Mechanics
- `compile_telemetry()` globs `telemetry/*_*.json` (excluding `broker*`, `failed*`, `*_summary.json`),
  reads each bot's `{status, last_update, stats, failed_logins, recent_events, ...}`. A file with no
  update in **>120s** is treated as `Inactive / Stopped`.
- Rolling messages are kept by editing a stored `message_id` (`telemetry/broker_state.json`), so they
  survive broker restarts. Sends are hashed so an unchanged alert isn't re-sent every 10s.
- Config: `load_config()` reads the **first** bot `config.json` that has a Telegram token+chat_id. If
  multiple bots have different Telegram configs, only the first wins.
- Interactive (still available on demand, not auto-pushed): inline-keyboard callbacks + text commands
  (`/status`, `/errors`, `/reset`) via `getUpdates` long-poll.
- **Parent watchdog** (`parent_monitor_worker`): polls the launcher PID; on exit it archives the Telegram
  message, kills child bots + Chrome + chromedriver (broad `CommandLine` match — can hit unrelated
  Chrome), wipes `telemetry/`, and `os._exit(0)`. It does **not** call `end_session`.

---

## 7. `reset_account_states.py`

Standalone `urllib` maintenance script. `PATCH accounts SET status='idle' WHERE status=eq.active`
(optionally `--bot NAME`). Prints affected rows. Use it to clean up stale `active` rows left by the
old pre-seeding behavior, or after a hard crash where `end_session` never ran.

> To delete phantom `idle` rows entirely (not just flip them), you have to issue a PostgREST DELETE
> directly — this script only flips `active → idle`.
