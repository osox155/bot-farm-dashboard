# Known Issues & Footguns

Confirmed by reading the source (and, for BUG-1, by a runtime log). Prioritized. File:line refs are
approximate where the file is large — search the symbol if a line has drifted.

---

## ✅ BUG-1 (FIXED) — undefined globals crashed the failed-login alert → Chrome closed

**File:** `ReplyBotv7/new.py` · **Status: fixed (2026-06-10).**

`send_or_update_failed_login_notice` (and the status/report senders, and `_load/_save_failed_login_state`)
referenced module globals that were **never assigned anywhere**:

| Symbol | Used at | Was defined? |
|--------|---------|--------------|
| `FAILED_LOGIN_LOCK` | `new.py:487`, `:701` (`with FAILED_LOGIN_LOCK:`) | ❌ → ✅ now defined |
| `FAILED_LOGIN_STATE_FILE` | `:336`, `:353` | ❌ → ✅ |
| `STATUS_STATE_FILE`, `REPORT_STATE_FILE`, `STATS_CHECKPOINT_FILE` | various | ❌ → ✅ |
| `LAST_FAILED_ALERT_EDIT_TS`, `LAST_STATUS_EDIT_TS`, `LAST_REPORT_EDIT_TS` | read before first assign | ❌ → ✅ |

**Effect (before the fix):** `send_or_update_failed_login_notice` ran `log_login_failure` (line ~501 —
which is why failed accounts *do* show `logged_out` in Supabase) and then hit `with FAILED_LOGIN_LOCK:`
→ `NameError`. The error propagated out of `run_account`'s `try` → `finally: driver.quit()` → **the Chrome
window closed.** A real log confirmed it: `Unexpected error for 3_cookies.json: NameError: name
FAILED_LOGIN_LOCK is not defined` followed by `Browser closed`. This was the *actual* reason "Chrome
closes when an account fails to log in," and it **defeated the persistent-login retry loop**.

**The fix:** all eight globals are now defined at module scope (a `threading.Lock()`, four `Path`
state-files under `ReplyBotv7/telemetry_state/`, and three `0.0` edit-timestamps) right after the other
module globals near the top of `new.py`. Verified: the module compiles and all eight names resolve as
module-level assignments. The failed-login alert path can now run to completion, so the persistent-login
loop stays alive and Chrome stays open across retries.

> Note: with the Telegram change below (BUG-1 is now moot for the *alert send* because ReplyBot delegates
> alerts to the broker), the `FAILED_LOGIN_LOCK` block is usually skipped anyway — but the globals are
> still needed for the status/report/clear paths and for robustness, so the definitions stay.

---

## 🟠 BUG-2 — reply/message counts silently stay 0

**File:** `ReplyBotv7/new.py`

`bot_statistics` is initialized as `{}` (`new.py:37`); the sub-dicts `option1_replies`, `option2_replies`,
`option11_messages`, `option12_messages` are **never created**. `update_statistics` does
`bot_statistics['option1_replies'][acct] = …` (`~:4053`) → `KeyError` on the first reply, swallowed by the
surrounding `try/except`. So per-account reply/message counts can stay **0** even when replies are sent
(matches `telemetry/ReplyBot_3.json` showing `replies:0`). `recover_and_report_previous_session` only
populates keys that already exist, so a cold start doesn't fix it.

**Fix:** initialize the sub-dicts (`bot_statistics.setdefault('option1_replies', {})`, etc.) at startup,
or use `defaultdict`.

---

## 🟠 BUG-3 — TOTP/credentials fallback broken by undefined `logger`

**File:** `ReplyBotv7/new.py:3406` `load_account_credentials`

References `logger` at `~:3435-3436` but `logger` is **not** a parameter → `NameError` whenever a valid
credentials file is found, defeating the credentials/TOTP login path (caught by callers, so it silently
falls back to cookie-only).

**Fix:** pass `logger` into `load_account_credentials` (or use a module logger) and remove the bare refs.

---

## 🟠 BUG-4 — `published_csv` cookie refresh always throws

**File:** `ReplyBotv7/new.py:~2548`

The `published_csv` branch of `refresh_account_cookies` references an undefined `fieldnames` (should be
`reader.fieldnames`) → that branch always raises. Only `api` mode works (the active config uses `api`).
Also: `run_account`'s pre-login cookie sync is gated on `gs_cfg.get('published_csv_url')` even though the
active mode is `api` — it only works because the config happens to include *both* keys; removing
`published_csv_url` would silently skip the first-login pre-sync.

---

## 🟡 BUG-5 — CommentsReplyBot: two reply-count sources disagree

**File:** `CommentsReplyBot/facebook_bot.py`

The Supabase reply counters are now session-accurate (logged once at the real send moment), but the
**local telemetry JSON** `stats.replied` and `show_stats()` are still derived from
`len(processed_comments)` (cumulative). So the web dashboard (Supabase) and the Telegram broker
(telemetry JSON) show different reply numbers. Also `final_status=='unconfirmed'` counts as success for
control flow but is **not** logged as `reply_sent`, so submitted-but-unverified replies don't appear in
dashboard counts. `self.session_replies/session_failures` are incremented but never read.

---

## 🟡 BUG-6 — AutoJoinBot: stubbed notifier + brittle regex + buggy local mode

**File:** `AutoJoinBot/join_groups.py`

- The Telegram notifier is intentionally stubbed (no routine messages) — fine, but `send_or_update`
  writes telemetry only if a regex matches the exact emoji format of `format_progress`; change that
  string and telemetry silently stops.
- `local_accounts` multi-account path (`:1334-1337`) passes the wrong thread args → `TypeError` if used
  (empty in shipped config).
- The single-account `do_join` path writes no state/telemetry/stats and doesn't skip processed groups.
- `accounts_numbers` heuristic: `[1,7]` is auto-expanded to the **range** 1-7, not two discrete indices.
- AutoJoin never calls `start_session`/`end_session` → Supabase session left `running`.

---

## 🟡 BUG-7 — FewFeed: first `load_config` returns `None`

**File:** `fewfeedbotv6/fewfeed_bot_template.py:272`

The first `load_config` has no `return` → returns `None`; shadowed by the correct one at line 2932. The
module-level `config = load_config()` runs while only the buggy def exists, so the initial global `config`
is `None` until `main_menu()` reassigns it. Any code reading module-level `config` before `main_menu`
runs would see `None`.

---

## 🟡 BUG-8 — Launcher footguns

**File:** `start-bots.ps1`

- `Resolve-ReturnDesktopIndex` regex `comments?replybot` matches **both** `CommentsReplyBot` and
  `ReplyBot`; a bare `ReplyBot` return target wrongly resolves to desktop 2 instead of 4.
- A fallback path calls `Ensure-DesktopSwitch`, **a function that does not exist** → would throw if the
  module-based desktop return fails.
- Keep-alive `Wait-Process` only waits on FewFeed + ReplyBot PIDs; with only AutoJoin/Comments enabled
  the launcher wouldn't block on them.
- `ReplyBot.ManualAccounts.AccountsForOption2="6"` is ignored while `RotationEnabled=true` (rotation
  wins) — a common point of confusion about "which accounts run today."

---

## 🟡 BUG-9 — dashboard / tracker sharp edges

- `daily_stats` increments are **read-then-write, not atomic** — safe only within one process; multiple
  bot processes hitting the same row can lose increments.
- The 300s "offline" threshold is **hardcoded in 3 places** (`dashboard.py:91,164,199`).
- `reset_today` deletes events `created_at >= today T00:00:00Z` (**UTC** midnight) — can drop events that
  are "today" in the operator's local timezone.
- The broker's parent-watchdog kills Chrome by broad `CommandLine` match (any `--remote-debugging-port`,
  `new.py`, `fewfeed`, …) — can kill unrelated Chrome instances on the machine.

---

## 🔴 Security — committed secrets

Across the repo (and ignored only partially by `.gitignore`): live **Telegram bot tokens** in each
`config.json`, the **Supabase anon key** in `supabase_config.json` (with permissive RLS → full
read/write/delete), Google **service-account JSONs**, hard-coded **FewFeed tool credentials** in source,
and real **cookie/credential** files. `DASHBOARD_PASSWORD` only protects the web UI, not Supabase. Rotate
these and move them out of version control.
