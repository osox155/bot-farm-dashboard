# AutoJoinBot (`AutoJoinBot/join_groups.py`)

A Selenium-driven **Facebook group auto-joiner**. Logs in with cookies on the **mobile site**
(`m.facebook.com`), iterates a list of group IDs from `uid.txt`, clicks Join / Request-to-join for each,
and **auto-answers join questions** (optionally with an LLM). ~62KB.

## Entry point
`python join_groups.py [flags]`. `main()` at `join_groups.py:695`; argparse at `:1116`. Reads
`config.json` and merges keys into args. Dispatch:
- **Interactive menu** (default / `--menu`, when `auto_start=false`): pick accounts by number, comma
  list, range (`2-5`), filename, or `all`.
- **Google Sheet CSV multi-account** (`:1199`): active when `cookies_sheet_csv_url` is set —
  `fetch_accounts_from_csv` downloads a published Sheet (`account_file`, `cookies_json`), narrowed by
  `accounts_filter`/`accounts_numbers`, runs all selected as parallel daemon threads.
- **Local multi-account** (`config.local_accounts`): ⚠️ **buggy** — passes the wrong thread args
  (`progress, lock` instead of `progress, errors_registry, lock`) → would crash. Empty in shipped config.
- **Single-account local cookies** (`do_join`, `:696`): one Chrome, sequential. ⚠️ This path does **not**
  write state/telemetry/stats and does **not** skip already-processed groups (only the threaded path does).

In the farm it runs on **DesktopIndex 1**, `Enabled:false`, `RotationEnabled:false` (so it would use the
manual `Accounts:"3,4"`, not the rotation pool).

## Join flow
1. `GET m.facebook.com/groups/<gid>`, sleep `delay` (1.0s).
2. Detect content-unavailable / already-member / pending (Requested/Cancel) → classify.
3. Try multiple Join-button selectors (multi-locale), `scrollIntoView`, click.
4. Wait ≤3s for a questions dialog → `answer_join_questions`.
5. Wait ≤5s for Pending/Joined confirmation → classify joined / request_pending / clicked_no_confirm /
   join_button_not_found.

### Answering join questions (optional AI)
Detects desktop modal or inline mobile question blocks; prefers yes/agree/accept for
checkboxes/radios/selects; for text inputs, `ai_answer` uses `OPENAI_API_KEY`/`OPENAI_MODEL` via the
`openai` SDK, **falling back to canned polite text** if no key or on error.

> ⚠️ The shipped `config.json` `ai.api_key` is an **OpenRouter-style** `sk-or-...` key, but the code
> uses the `openai` SDK with its default `api.openai.com` base URL (never overridden) — so that key
> would **not** actually work without a `base_url` change.

## Login & cookies
Cookie-based, **no password**. `ensure_logged_in_with_cookies` (`:115`) navigates to
`m.facebook.com/settings`, clears cookies, then adds each cookie under several **domain variants**
(provided, none, `m.facebook.com`, `.facebook.com`, `facebook.com`), and considers login successful when
the `c_user` cookie is present. On failure, if `cookies_sheet_csv_url` is set it loops **forever** (10s
apart) re-fetching fresh cookies from the Sheet until login succeeds (matching by label in the threaded
path). No TOTP/2FA.

## Telemetry — Telegram is deliberately stubbed
`TelegramNotifier.send_message`/`poll_commands` are **no-ops**, and `send_or_update` **never calls the
Telegram API** — it parses the formatted progress string with a regex and writes local telemetry JSON +
a `stats_tracker` event. **So despite `telegram.enabled=true` and a real token in config, AutoJoinBot
sends no routine Telegram messages.**

> ⚠️ The regex depends on the exact emoji/format of `format_progress` (✅ ⏳ 🙋 ⚠️ 📦); if that string
> changes, telemetry silently stops being written.

## What it writes
| Target | Content |
|--------|---------|
| `../telemetry/AutoJoinBot_<label>.json` | status `Running - Joined j/total`, stats{joined,pending,left,errors,total} |
| Supabase (via `get_tracker('AutoJoin')`) | one `group_joined` event per progress update + account upsert. **Never calls `start_session`/`end_session`** → session created lazily, left `running`. |
| `logs/<label>_<timestamp>.log` | per-group line log incl. login `c_user` |
| `logs/state/<label>.json` | `{"processed_ok": [gid,…]}` — non-error groups recorded so reruns skip them |

## Config highlights (`AutoJoinBot/config.json`)
`auto_start`, `uids` (default `uid.txt`), `cookies_sheet_csv_url`, `accounts_numbers`/`accounts_filter`,
`headless`, `delay` (per-group sleep), `local_accounts`, `ai.{enabled,api_key,model}`,
`telegram.{enabled,bot_token,chat_id}` (parsed but unused for messaging).

> Rate limiting is minimal: a fixed `delay` per group + 0.3s stagger between thread launches; all
> selected accounts open their own Chrome and hit Facebook in parallel. The `left` counter is overloaded
> — it counts both already-member and skipped-already-processed groups.
