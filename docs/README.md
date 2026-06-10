# Bot Farm — Documentation

A Windows-based "bot farm" that runs several Selenium/Chrome Facebook-automation bots in parallel,
each on its own Windows **Virtual Desktop**, with a shared telemetry/observability stack
(a Supabase-backed web dashboard **plus** a Telegram broker).

This `docs/` folder is the canonical written reference for how every part works. It was produced by
reading the actual source (file:line references throughout) — when in doubt, trust the code, but these
docs capture the intent, the data flow, and the **non-obvious behaviors and known bugs** that the code
alone doesn't explain.

## The four bots

| Bot | Entry file | What it does | Desktop | Telemetry it emits |
|-----|-----------|--------------|---------|--------------------|
| **ReplyBot** | `ReplyBotv7/new.py` | Messenger: auto-accept & reply to message requests, main-chat auto-reply, bulk send | 4 | replies + messages |
| **CommentsReplyBot** | `CommentsReplyBot/facebook_bot.py` | Facebook: auto-reply to comments on group posts (DM-bait) | 2 | comments/replies |
| **AutoJoinBot** | `AutoJoinBot/join_groups.py` | Facebook: auto-join groups from a UID list, AI-answer join questions | 1 | groups joined |
| **FewFeed** | `fewfeedbotv6/fewfeed_bot_template.py` | Facebook: bulk-post one prompt to many groups via the fewfeed.online tool | 3 | posts published |

Only **ReplyBot** is currently `Enabled` in `bot-launcher.config.json`.

## The shared infrastructure

| Component | File | Role |
|-----------|------|------|
| **Launcher** | `start-bots.ps1` (+ `start-bots.bat`) | One-click boot of the fleet, virtual-desktop switching, drives each bot's stdin menu, day-based account rotation |
| **Stats tracker** | `stats_tracker.py` | Library the bots call; writes durable metrics/status to **Supabase** via PostgREST |
| **Web dashboard** | `dashboard.py` + `templates/` | Bottle web app reading the same Supabase tables; the "Dashboard Overview" screen |
| **Telegram broker** | `telemetry_broker.py` | Separate channel: reads the bots' local `telemetry/*.json` and relays two rolling Telegram messages |
| **Maintenance** | `reset_account_states.py` | One-off: clean stale `active` rows in Supabase |
| **Deploy glue** | `Procfile`, `wsgi.py` | Host `dashboard.py` on PythonAnywhere/Render |

## Read next

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the pieces fit, the two independent telemetry channels, data flow.
- **[TELEMETRY_AND_DASHBOARD.md](TELEMETRY_AND_DASHBOARD.md)** — Supabase schema, the account-status lifecycle, how the dashboard counts RUNNING/IDLE/FAILED/TOTAL, timezone handling, the broker.
- **[LAUNCHER.md](LAUNCHER.md)** — `start-bots.ps1`, the config file, account rotation math, deployment.
- **Per-bot deep dives:** [bots/ReplyBot.md](bots/ReplyBot.md) · [bots/CommentsReplyBot.md](bots/CommentsReplyBot.md) · [bots/AutoJoinBot.md](bots/AutoJoinBot.md) · [bots/FewFeed.md](bots/FewFeed.md)
- **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)** — confirmed bugs and footguns, prioritized. **Read this before trusting reply counts or the "never close Chrome" behavior.**

## One-paragraph mental model

Each bot is an independent Python process that logs into Facebook/Messenger **with exported cookies**
(no passwords, except ReplyBot's optional TOTP fallback), driven by Selenium/Chrome. Cookies are the
source of truth and live both locally (`<bot>/accounts/<id>_cookies.json`) and in a shared **Google
Sheet**; when a login fails the bot re-pulls cookies from the sheet and retries. Every bot reports in
**two** ways that never share storage: (1) durable rows in **Supabase** via `stats_tracker` — the web
dashboard reads these; (2) transient `telemetry/<Bot>_<account>.json` files — the **Telegram broker**
reads these. The **launcher** (`start-bots.ps1`) boots the whole thing, putting each bot on its own
virtual desktop and feeding menu choices + account IDs into its stdin.
