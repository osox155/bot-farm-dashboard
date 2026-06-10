# CommentsReplyBot (`CommentsReplyBot/facebook_bot.py`)

A Selenium-driven **Facebook** bot that auto-replies to **comments on group posts** (DM-bait — the
reply text says "DM me / message me for stickers", aimed at Monopoly-sticker-trading groups). ~200KB.

## Entry point
Run `python facebook_bot.py` from `CommentsReplyBot/`. `__main__` → `main()` (`facebook_bot.py:4028`)
shows a text menu. Account number typed at the prompt (`1` → `1_cookies.json`). In the farm it's
launched by `start-bots.ps1` with `OptionSequence: 1` (currently `Enabled:false`).

## Menu / modes
| # | Action |
|---|--------|
| 1 | **Start Bot (group-feed mode)** → `run_bot()`: loop groups from `groups.txt`/Sheet, scrape each feed for post permalinks, open each new post, reply to its comments. `continuous_mode=true` → cycles forever every `continuous_cycle_minutes` (30), hot-reloading groups, never quitting Chrome. |
| 2 | Show statistics (local counts from the JSON de-dup stores) |
| 3 | View logs (last 20 lines of `logs/bot.log`) |
| 4 | Exit |
| 5 | **Multi-tab posts mode** → opens a fixed set of specific post URLs (from `posts.txt` or a per-account sheet column) each in its own tab, round-robin rescanning for new comments. Loops forever. |

## Login & cookies
**Cookie-only — no password/TOTP.** `load_cookies(n)` tries the **Google Sheets CSV** (`accounts_sheet`,
matching `<n>_cookies.json` → `cookies_json`, validating `c_user`+`xs`) **first**, then local
`accounts/<n>_cookies.json`. Success = URL contains `facebook.com` and not `login`.
`handle_logout_and_refresh` retries **effectively forever** (`max_retries=999999`, 30s apart): re-pull
cookies from the Sheet, reload into driver, retest. **No credential fallback** — if cookies are dead it
just keeps retrying and surfacing the logged-out alert (so the bot can appear hung on one account).

## Reply flow
1. `process_group_posts`: scroll the group feed, `collect_post_permalinks_on_page` canonicalizes hrefs
   to `…/groups/{gid}/posts/{pid}` (rejecting photo galleries, comment links, etc.).
2. Skip posts whose `pid` is in `processed_posts` or seen this session.
3. `process_comments_on_current_post_page`: expand "View more comments", collect **top-level** comments
   (text length > 5), de-dup by `get_comment_id` or `sha1(post_id|username|text)`.
4. Skip if already replied, if `reply_only_once_per_user_per_post` and the user was already replied to,
   or if `should_skip_comment` (min length, `skip_keywords` spam/bot/fake, `skip_users`).
5. `reply_to_comment`: click the comment's Reply, find the composer bound to **that** comment, append a
   random reply (BMP-sanitized — emoji > 0xFFFF are stripped to avoid a ChromeDriver crash), submit.
6. `detect_and_handle_rate_limit_dialog` → on a block, start a 30-min cooldown.
7. Verify (`_verify_reply_posted`, 5 DOM strategies) → `final_status` = sent/unconfirmed/declined/pending;
   persist to `processed_comments`.

## ⭐ The telemetry fix (per-reply counting)
**Old bug:** counts were derived from `len(processed_comments)` and a `reply_sent` was logged on **every
periodic status ping**, inflating the dashboard.
**Now:** inside `reply_to_comment`, exactly once, after `final_status` is known
(`facebook_bot.py:3463-3490`):
- `final_status == 'sent'` → `session_replies += 1` and `log_event('reply_sent')`.
- `final_status in ('failed','declined')` (and the not-submitted branch) → `log_event('reply_failed')`.

`_save_telemetry` now **only logs login state** to Supabase, never reply counts (comment at
`facebook_bot.py:116-119`).

> ⚠️ **Inconsistency that remains:** the *local telemetry JSON* `stats` block (posts/comments/replied/
> skipped) and `show_stats()` are still derived from `len(processed_comments)`. So Supabase reply
> counts are now session-accurate, but the number the **broker** displays comes from the cumulative
> persisted count — two different numbers from two different sources. Also `final_status=='unconfirmed'`
> counts as success for control flow but is **not** logged as `reply_sent`, so submitted-but-unverified
> replies won't appear in dashboard counts.

## What it writes
| Target | Content |
|--------|---------|
| `logs/processed_posts.json` | `post_id → {processed_at, group_url, post_url}` (post de-dup) |
| `logs/processed_comments.json` | `comment_key → {action, username, post_id, text, reply_status, …}` |
| `../telemetry/CommentsReplyBot_<account>.json` | status + stats{posts,comments,replied,skipped} + failed_logins |
| Supabase (via `get_tracker('CommentsReply')`) | reply_sent/reply_failed events + login state |
| `accounts/<n>_cookies.json` | cookies pulled from the Sheet (local backup) |

## Telegram
**Indirect.** The in-class `send_telegram_message`/`delete_telegram_message` are **stubs**
(return `999999`/`True`). This bot only writes telemetry JSON + Supabase; the actual Telegram push is
done by `telemetry_broker.py`, which reads the telemetry files.

## Config highlights (`CommentsReplyBot/config.json`)
`bot_settings.{continuous_mode,continuous_cycle_minutes,max_posts_per_group,max_comments_per_post,
reply_only_once_per_user_per_post,delay_between_comments/posts/groups,logout_check_interval}`,
`filters.{skip_keywords,skip_users,min_comment_length}`, `anti_block.cooldown_minutes_on_block`,
`accounts_sheet`, `sheets_csv` (groups col A / posts col B, hot-reloaded), `posts_per_account_sheet`,
`posts_multitab.{max_tabs,refresh_seconds,max_rounds}`.
