import json
import os
import time
import threading
from datetime import datetime, date, timezone

try:
    import requests as _requests
except ImportError:
    _requests = None

_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase_config.json")

_SUPABASE_URL = None
_SUPABASE_KEY = None

if os.path.isfile(_CONFIG_FILE):
    try:
        with open(_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        _SUPABASE_URL = cfg.get("supabase_url", "").rstrip("/")
        _SUPABASE_KEY = cfg.get("supabase_key", "")
    except Exception:
        pass

if not _SUPABASE_URL or not _SUPABASE_KEY:
    _SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    _SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_API_BASE = f"{_SUPABASE_URL}/rest/v1" if _SUPABASE_URL else None

_HEADERS = {
    "apikey": _SUPABASE_KEY,
    "Authorization": f"Bearer {_SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_lock = threading.Lock()

def _iso_now():
    return datetime.utcnow().isoformat() + "Z"

def _today_str():
    return date.today().isoformat()

def _fmt_ts(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float)):
        return datetime.utcfromtimestamp(val).isoformat() + "Z"
    return str(val)

def _parse_ts(val):
    if val is None:
        return None
    if isinstance(val, str):
        try:
            # Timestamps are stored in UTC (suffixed with "Z"). Parse them as
            # timezone-aware UTC so .timestamp() yields the correct epoch on any
            # machine; otherwise a naive parse is treated as local time and the
            # value is wrong by the local UTC offset (breaks offline detection).
            s = val.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return val
    return val

def _supa_get(table, params=None):
    if not _API_BASE:
        return []
    try:
        r = _requests.get(f"{_API_BASE}/{table}", headers=_HEADERS, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception:
        return []

def _supa_post(table, data, headers_ext=None):
    if not _API_BASE:
        return None
    try:
        h = dict(_HEADERS)
        if headers_ext:
            h.update(headers_ext)
        r = _requests.post(f"{_API_BASE}/{table}", headers=h, json=data, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        return None
    except Exception:
        return None

def _supa_patch(table, data, query):
    if not _API_BASE:
        return
    try:
        _requests.patch(f"{_API_BASE}/{table}", headers=_HEADERS, json=data, params=query, timeout=10)
    except Exception:
        pass

def _supa_delete(table, query):
    if not _API_BASE:
        return {"error": "No API base URL"}
    headers = dict(_HEADERS)
    if not query:
        headers["Prefer"] = "count=planned"
    r = _requests.delete(f"{_API_BASE}/{table}", headers=headers, params=query, timeout=10)
    if r.status_code not in (200, 204):
        raise Exception(f"DELETE {table} failed: {r.status_code} {r.text[:200]}")
    return r.json() if r.status_code == 200 and r.text.strip() else []

def _supa_upsert(table, data, on_conflict):
    if not _API_BASE:
        return None
    try:
        h = dict(_HEADERS)
        h["Prefer"] = f"resolution=merge-duplicates,return=representation"
        params = {"on_conflict": on_conflict} if on_conflict else None
        r = _requests.post(f"{_API_BASE}/{table}", headers=h, json=data, params=params, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        return None
    except Exception:
        return None

class StatsTracker:
    def __init__(self, bot_name="Bot"):
        self._bot_name = bot_name
        self._session_id = None
        self._accounts = []
        self._running = False

    def start_session(self, bot_name=None, accounts=None):
        if bot_name:
            self._bot_name = bot_name
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + str(int(time.time() * 1000))[-6:]
        self._accounts = accounts or []
        self._running = True

        ts = _iso_now()
        with _lock:
            # Do NOT pre-register the account roster here. Only accounts that
            # are actually launched should appear, and they create their own
            # rows the moment they act: log_login_success -> "active",
            # log_login_failure -> "logged_out", end_session flips active ->
            # "idle". Pre-seeding every known cookie file as "idle" made the
            # dashboard report accounts that were never launched this run.
            _supa_post("sessions", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "started_at": ts,
                "accounts_count": len(self._accounts),
                "status": "running"
            })
        return self._session_id

    def end_session(self, status="completed"):
        ts = _iso_now()
        self._running = False
        with _lock:
            _supa_patch("sessions", {"ended_at": ts, "status": status},
                       {"session_id": f"eq.{self._session_id}"})
            # Flip this bot's currently-running accounts back to "idle" so the
            # dashboard stops showing them "running" the moment the bot stops,
            # instead of waiting ~5 min for the stale-timeout rule. Leave
            # "logged_out" rows untouched so failed accounts still surface.
            _supa_patch("accounts", {"status": "idle", "last_active": ts},
                       {"bot_name": f"eq.{self._bot_name}", "status": "in.(active,running)"})

    def _ensure_session(self):
        if not self._session_id:
            self.start_session()

    def log_event(self, event_type, account_name=None, option_type=None, details=None):
        self._ensure_session()
        ts = _iso_now()
        today = _today_str()
        details_json = json.dumps(details) if details else None

        with _lock:
            _supa_post("events", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "account_name": account_name,
                "event_type": event_type,
                "option_type": option_type,
                "details": details_json,
                "created_at": ts
            })

            if account_name:
                # Ensure daily_stats row exists without overwriting counters
                _supa_upsert("daily_stats", {
                    "date": today, "bot_name": self._bot_name,
                    "account_name": account_name
                }, on_conflict="date,bot_name,account_name")

                if event_type in ("reply_sent", "message_sent"):
                    field = "total_replies" if event_type == "reply_sent" else "total_messages"
                    self._increment_daily(today, account_name, field)
                elif event_type in ("reply_failed", "message_failed"):
                    self._increment_daily(today, account_name, "total_failures")

                if event_type == "reply_sent":
                    self._increment_session("total_replies")
                elif event_type == "message_sent":
                    self._increment_session("total_messages")
                elif event_type in ("reply_failed", "message_failed", "login_failed"):
                    self._increment_session("total_failures")

            if account_name:
                _supa_upsert("accounts", {
                    "name": account_name, "bot_name": self._bot_name,
                    "last_active": ts, "status": "active"
                }, on_conflict="name,bot_name")

    def _increment_daily(self, today, account_name, field):
        if not _API_BASE:
            return
        try:
            rows = _supa_get("daily_stats", {
                "select": field,
                "date": f"eq.{today}",
                "bot_name": f"eq.{self._bot_name}",
                "account_name": f"eq.{account_name}"
            })
            if rows:
                cur = rows[0].get(field, 0) or 0
                _supa_patch("daily_stats", {field: cur + 1},
                           {"date": f"eq.{today}", "bot_name": f"eq.{self._bot_name}",
                            "account_name": f"eq.{account_name}"})
        except Exception:
            pass

    def _increment_session(self, field):
        if not _API_BASE:
            return
        try:
            rows = _supa_get("sessions", {
                "select": field,
                "session_id": f"eq.{self._session_id}"
            })
            if rows:
                cur = rows[0].get(field, 0) or 0
                _supa_patch("sessions", {field: cur + 1},
                           {"session_id": f"eq.{self._session_id}"})
        except Exception:
            pass

    def log_login_failure(self, account_name, reason=None):
        self._ensure_session()
        ts = _iso_now()
        today = _today_str()
        with _lock:
            _supa_post("login_attempts", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "account_name": account_name,
                "success": 0, "reason": reason,
                "attempted_at": ts
            })
            _supa_post("events", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "account_name": account_name,
                "event_type": "login_failed",
                "details": json.dumps({"reason": reason}) if reason else None,
                "created_at": ts
            })

            _supa_upsert("daily_stats", {
                "date": today, "bot_name": self._bot_name,
                "account_name": account_name
            }, on_conflict="date,bot_name,account_name")

            self._increment_daily(today, account_name, "login_failures")
            self._increment_daily(today, account_name, "total_failures")
            self._increment_session("total_failures")
            _supa_upsert("accounts", {
                "name": account_name, "bot_name": self._bot_name,
                "last_active": ts, "status": "logged_out"
            }, on_conflict="name,bot_name")

    def log_login_success(self, account_name):
        self._ensure_session()
        ts = _iso_now()
        with _lock:
            _supa_post("login_attempts", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "account_name": account_name,
                "success": 1,
                "attempted_at": ts
            })
            _supa_post("events", {
                "session_id": self._session_id,
                "bot_name": self._bot_name,
                "account_name": account_name,
                "event_type": "login_success",
                "created_at": ts
            })
            _supa_upsert("accounts", {
                "name": account_name, "bot_name": self._bot_name,
                "last_active": ts, "status": "active"
            }, on_conflict="name,bot_name")

    def set_account_status(self, account_name, status, bot_name=None):
        ts = _iso_now()
        bn = bot_name or self._bot_name
        with _lock:
            _supa_upsert("accounts", {
                "name": account_name, "bot_name": bn,
                "status": status, "last_active": ts
            }, on_conflict="name,bot_name")
        self.log_event("status_change", account_name=account_name, details={"status": status})

    def get_active_accounts(self, bot_name=None):
        params = {"select": "name,bot_name,status,last_active", "order": "last_active.desc.nullslast"}
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        rows = _supa_get("accounts", params)
        out = []
        for r in rows:
            out.append({
                "name": r["name"],
                "bot_name": r.get("bot_name", ""),
                "status": r.get("status", "unknown"),
                "last_active": _parse_ts(r.get("last_active"))
            })
        return out

    def get_account_status(self, account_name):
        """Return the stored status of an account across every bot it belongs to."""
        rows = _supa_get("accounts", {
            "select": "bot_name,status,last_active",
            "name": f"eq.{account_name}",
            "order": "last_active.desc.nullslast"
        })
        out = []
        for r in rows:
            out.append({
                "bot_name": r.get("bot_name", ""),
                "status": r.get("status", "unknown"),
                "last_active": _parse_ts(r.get("last_active"))
            })
        return out

    def get_bots_list(self):
        rows = _supa_get("accounts", {
            "select": "bot_name",
            "bot_name": "neq.",
            "order": "bot_name.asc"
        })
        seen = set()
        result = []
        for r in rows:
            bn = r.get("bot_name", "")
            if bn and bn not in seen:
                seen.add(bn)
                result.append(bn)
        return result

    def get_today_stats(self, bot_name=None):
        today = _today_str()
        params = {"select": "total_replies,total_messages,total_failures,login_failures", "date": f"eq.{today}"}
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        rows = _supa_get("daily_stats", params)
        replies = sum(r.get("total_replies") or 0 for r in rows)
        messages = sum(r.get("total_messages") or 0 for r in rows)
        failures = sum(r.get("total_failures") or 0 for r in rows)
        login_failures = sum(r.get("login_failures") or 0 for r in rows)
        return {"replies": replies, "messages": messages, "failures": failures, "login_failures": login_failures}

    def get_bot_today_stats(self):
        today = _today_str()
        rows = _supa_get("daily_stats", {
            "select": "bot_name,total_replies,total_messages,total_failures,login_failures",
            "date": f"eq.{today}",
            "bot_name": "neq.",
            "order": "bot_name.asc"
        })
        grouped = {}
        for r in rows:
            bn = r.get("bot_name", "")
            if not bn:
                continue
            if bn not in grouped:
                grouped[bn] = {"bot_name": bn, "replies": 0, "messages": 0, "failures": 0, "login_failures": 0}
            grouped[bn]["replies"] += r.get("total_replies") or 0
            grouped[bn]["messages"] += r.get("total_messages") or 0
            grouped[bn]["failures"] += r.get("total_failures") or 0
            grouped[bn]["login_failures"] += r.get("login_failures") or 0
        return list(grouped.values())

    def get_recent_events(self, limit=20, account_name=None, bot_name=None):
        params = {"select": "*", "order": "created_at.desc", "limit": limit}
        filters = []
        if account_name:
            filters.append(f"account_name=eq.{account_name}")
        if bot_name:
            filters.append(f"bot_name=eq.{bot_name}")
        if filters:
            params["or"] = ",".join(filters)
        rows = _supa_get("events", params)
        return rows

    def get_account_history(self, account_name, bot_name=None):
        if bot_name:
            events = _supa_get("events", {
                "select": "*",
                "account_name": f"eq.{account_name}",
                "bot_name": f"eq.{bot_name}",
                "order": "created_at.desc",
                "limit": 50
            })
            logins = _supa_get("login_attempts", {
                "select": "*",
                "account_name": f"eq.{account_name}",
                "bot_name": f"eq.{bot_name}",
                "order": "attempted_at.desc",
                "limit": 20
            })
        else:
            events = _supa_get("events", {
                "select": "*",
                "account_name": f"eq.{account_name}",
                "order": "created_at.desc",
                "limit": 50
            })
            logins = _supa_get("login_attempts", {
                "select": "*",
                "account_name": f"eq.{account_name}",
                "order": "attempted_at.desc",
                "limit": 20
            })
        return {"events": events, "logins": logins}

    def get_daily_stats(self, days=14, bot_name=None):
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        params = {
            "select": "date,total_replies,total_messages,total_failures,login_failures",
            "date": f"gte.{cutoff}",
            "order": "date.asc"
        }
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        rows = _supa_get("daily_stats", params)
        grouped = {}
        for r in rows:
            d = r.get("date", "")
            if not d:
                d = str(r.get("date", ""))
            if isinstance(d, datetime):
                d = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10]
            if d not in grouped:
                grouped[d] = {"date": d, "replies": 0, "messages": 0, "failures": 0, "login_failures": 0}
            grouped[d]["replies"] += r.get("total_replies") or 0
            grouped[d]["messages"] += r.get("total_messages") or 0
            grouped[d]["failures"] += r.get("total_failures") or 0
            grouped[d]["login_failures"] += r.get("login_failures") or 0
        return list(grouped.values())

    def get_sessions(self, limit=10, bot_name=None):
        params = {"select": "*", "order": "started_at.desc", "limit": limit}
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        rows = _supa_get("sessions", params)
        return rows

    def get_current_session(self):
        if not self._session_id:
            return None
        rows = _supa_get("sessions", {"select": "*", "session_id": f"eq.{self._session_id}"})
        return rows[0] if rows else None

    def reset_today(self, bot_name=None):
        today = _today_str()
        params = {"date": f"eq.{today}"}
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        _supa_delete("daily_stats", params)
        # Also delete today's events
        ev_params = {"created_at": f"gte.{today}T00:00:00Z"}
        if bot_name:
            ev_params["bot_name"] = f"eq.{bot_name}"
        _supa_delete("events", ev_params)

    def reset_bot(self, bot_name):
        q = {"bot_name": f"eq.{bot_name}"}
        _supa_delete("accounts", q)
        _supa_delete("events", q)
        _supa_delete("daily_stats", q)
        _supa_delete("login_attempts", q)
        _supa_delete("sessions", q)

    def reset_account(self, account_name, bot_name=None):
        params = {"name": f"eq.{account_name}"}
        if bot_name:
            params["bot_name"] = f"eq.{bot_name}"
        _supa_delete("accounts", params)
        ev_p = {"account_name": f"eq.{account_name}"}
        if bot_name:
            ev_p["bot_name"] = f"eq.{bot_name}"
        _supa_delete("events", ev_p)
        _supa_delete("daily_stats", dict(ev_p))
        _supa_delete("login_attempts", dict(ev_p))

    def reset_all(self):
        for table in ("login_attempts", "events", "daily_stats", "accounts", "sessions"):
            _supa_delete(table, {"id": "gte.0"})

    def get_summary_report(self, bot_name=None):
        today = self.get_today_stats(bot_name=bot_name)
        accounts = self.get_active_accounts(bot_name=bot_name)
        active_count = sum(1 for a in accounts if a["status"] in ("active", "running"))
        failed_count = sum(1 for a in accounts if a["status"] == "logged_out")
        label = f" ({bot_name})" if bot_name else " (All Bots)"

        report = f"\ud83d\udcca **Daily Report{label}**\n"
        report += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        report += f"\ud83d\udc64 Active Accounts: {active_count}\n"
        report += f"\u274c Failed Accounts: {failed_count}\n"
        report += f"\ud83d\udcac Replies Today: {today['replies']}\n"
        report += f"\u2709\ufe0f Messages Today: {today['messages']}\n"
        report += f"\u26a0\ufe0f Failures Today: {today['failures']}\n"
        report += f"\ud83d\udd11 Login Failures: {today['login_failures']}\n"

        if failed_count > 0:
            report += "\n\ud83d\udea8 **Accounts with Issues:**\n"
            for a in accounts:
                if a["status"] == "logged_out":
                    report += f"  - [{a['bot_name']}] `{a['name']}`\n"
        return report

    # ------------------------------------------------------------------
    # Remote control command queue
    #
    # The web dashboard runs in the cloud and cannot touch the operator's PC,
    # so a direct subprocess/taskkill from dashboard.py would try to kill
    # processes on the cloud host (the wrong machine) and silently do nothing.
    # Instead the dashboard enqueues a command here and the broker, running on
    # the actual PC, polls + executes it locally. This guarantees control hits
    # the correct machine where the bots really run.
    # ------------------------------------------------------------------
    def enqueue_command(self, action, bot_name=None):
        """Dashboard side: queue a control command for the local broker."""
        row = _supa_post("bot_commands", {
            "action": action,
            "bot_name": bot_name,
            "status": "pending",
            "created_at": _iso_now(),
        })
        return row

    def fetch_pending_commands(self, limit=20):
        """Broker side: read pending commands oldest-first."""
        return _supa_get("bot_commands", {
            "status": "eq.pending",
            "order": "created_at.asc",
            "limit": str(limit),
        })

    def mark_command_done(self, command_id, status="done", result=None):
        """Broker side: mark a command executed (or errored)."""
        _supa_patch("bot_commands", {
            "status": status,
            "result": result,
            "executed_at": _iso_now(),
        }, {"id": f"eq.{command_id}"})

    def clear_old_commands(self, keep_pending=True):
        """Maintenance: delete finished commands so the queue stays small."""
        if keep_pending:
            _supa_delete("bot_commands", {"status": "neq.pending"})
        else:
            _supa_delete("bot_commands", {"id": "gte.0"})

tracker = StatsTracker()

def get_tracker(bot_name="Bot"):
    return StatsTracker(bot_name=bot_name)
