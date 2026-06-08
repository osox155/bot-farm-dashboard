#!/usr/bin/env python3
"""
One-off / on-demand maintenance for the dashboard's Supabase data.

Usage:
  python reset_account_states.py            # set every 'active' account -> 'idle'
  python reset_account_states.py --bot ReplyBot   # only that bot

Why: before the per-session tracking fix, ReplyBot's start_session marked the
whole cookie roster 'active' even when only a couple were launched, leaving
stale 'active' rows. This resets them so the dashboard reflects reality; real
logins will flip accounts back to 'active' on the next run.
"""
import json
import os
import sys
import urllib.request
import urllib.parse

_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase_config.json")


def _conn():
    cfg = json.load(open(_CFG))
    base = cfg["supabase_url"].rstrip("/") + "/rest/v1"
    key = cfg["supabase_key"]
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    return base, headers


def reset_active_to_idle(bot=None):
    base, headers = _conn()
    params = {"status": "eq.active"}
    if bot:
        params["bot_name"] = f"eq.{bot}"
    url = base + "/accounts?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=json.dumps({"status": "idle"}).encode(),
                                 headers={**headers, "Prefer": "return=representation"}, method="PATCH")
    rows = json.load(urllib.request.urlopen(req, timeout=20))
    print(f"Reset {len(rows)} account(s) from 'active' -> 'idle'"
          + (f" for bot {bot}" if bot else ""))
    for r in rows:
        print("  ", r.get("bot_name"), r.get("name"))


if __name__ == "__main__":
    bot = None
    if "--bot" in sys.argv:
        bot = sys.argv[sys.argv.index("--bot") + 1]
    reset_active_to_idle(bot)
