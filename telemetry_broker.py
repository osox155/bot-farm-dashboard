#!/usr/bin/env python3
"""
Central Telegram Telemetry Broker
Aggregates status, logout alerts, and statistics from all active bots and
sends throttled, unified, premium notifications to Telegram.
"""

import os
import sys
import json
import time
import glob
import logging
import threading
from datetime import datetime
import requests

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_DIR = os.path.join(BASE_DIR, 'telemetry')
STATE_FILE = os.path.join(TELEMETRY_DIR, 'broker_state.json')

# Create telemetry folder if not exists
os.makedirs(TELEMETRY_DIR, exist_ok=True)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(TELEMETRY_DIR, 'broker.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("telemetry_broker")

def load_config():
    """Scan all bot config files to find a valid Telegram configuration."""
    paths = [
        os.path.join(BASE_DIR, 'ReplyBotv7', 'config.json'),
        os.path.join(BASE_DIR, 'CommentsReplyBot', 'config.json'),
        os.path.join(BASE_DIR, 'fewfeedbotv6', 'config.json'),
        os.path.join(BASE_DIR, 'AutoJoinBot', 'config.json'),
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    tg = cfg.get('telegram') or {}
                    # Try flat parameters or nested structure
                    token = tg.get('bot_token') or cfg.get('telegram_bot_token')
                    chat_id = tg.get('chat_id') or cfg.get('telegram_chat_id')
                    enabled = tg.get('enabled', True)
                    if token and chat_id:
                        logger.info(f"Loaded valid Telegram config from: {p}")
                        return {
                            "bot_token": str(token).strip(),
                            "chat_id": str(chat_id).strip(),
                            "enabled": bool(enabled),
                            # When true (default), Telegram is used ONLY for account
                            # login-problem alerts — no routine status/stats dashboard.
                            "alerts_only": bool(tg.get('alerts_only', True))
                        }
            except Exception as e:
                logger.error(f"Error reading config {p}: {e}")
    
    logger.warning("No valid Telegram configuration found in any bot config.json files.")
    return None

def load_broker_state():
    """Load persistent message IDs to prevent sending duplicate notifications across restarts."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                logger.info(f"Loaded persistent message IDs: {state}")
                return state
        except Exception as e:
            logger.error(f"Failed to load broker state: {e}")
    return {"status_msg_id": None, "alerts_msg_id": None, "stats_msg_id": None}

def save_broker_state(state):
    """Save persistent message IDs to file."""
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save broker state: {e}")

class CentralTelegramNotifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = cfg['bot_token']
        self.chat_id = cfg['chat_id']
        # Alerts-only: push only account login-problem notifications, never the
        # routine status/stats dashboard. Default on.
        self.alerts_only = bool(cfg.get('alerts_only', True))
        self.state = load_broker_state()
        self.lock = threading.Lock()
        self.last_api_call = 0.0

    def _api_call(self, method, payload, max_retries=3, enforce_rate_limit=True):
        """Throttled and rate-limited Telegram API request handler."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        
        # Enforce rate limit: at least 1.5 seconds between direct Telegram API calls
        if enforce_rate_limit:
            with self.lock:
                elapsed = time.time() - self.last_api_call
                if elapsed < 1.5:
                    time.sleep(1.5 - elapsed)
                self.last_api_call = time.time()
            
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, timeout=12)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"Telegram 429 Rate Limit. Sleeping for {retry_after}s...")
                    time.sleep(retry_after + 1)
                    continue
                if resp.ok:
                    return resp.json()
                else:
                    logger.error(f"Telegram error {resp.status_code}: {resp.text}")
                    return resp.json()
            except Exception as e:
                logger.error(f"Telegram request failed on attempt {attempt+1}: {e}")
                time.sleep(2)
        return None

    def send_or_update(self, text, message_type, reply_markup=None):
        """Update an existing message of message_type, or send a new one and record the message_id."""
        msg_id_key = f"{message_type}_msg_id"
        msg_id = self.state.get(msg_id_key)
        
        # Use HTML parsing for maximum formatting richness
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_markup is not None:
            if isinstance(reply_markup, dict):
                payload["reply_markup"] = json.dumps(reply_markup)
            else:
                payload["reply_markup"] = reply_markup
            
        if msg_id:
            payload["message_id"] = int(msg_id)
            res = self._api_call("editMessageText", payload)
            if res and res.get("ok"):
                logger.info(f"Successfully edited Telegram {message_type} message ({msg_id})")
                return msg_id
            
            # If edit fails (e.g. message deleted or too old), send a fresh one
            desc = (res or {}).get("description", "")
            if "message to edit not found" in desc.lower() or "bad request" in desc.lower():
                logger.warning(f"Failed to edit {message_type} message ({msg_id}). Sending new message.")
                payload.pop("message_id", None)
            else:
                # Other error (e.g. Rate Limit / Transient), skip to avoid duplicate spamming
                return msg_id

        # Send a fresh message
        res = self._api_call("sendMessage", payload)
        if res and res.get("ok"):
            new_id = res["result"]["message_id"]
            self.state[msg_id_key] = new_id
            save_broker_state(self.state)
            logger.info(f"Successfully sent new Telegram {message_type} message ({new_id})")
            return new_id
        return None

    def delete_message(self, message_type):
        """Delete message of message_type and clear its state ID."""
        msg_id_key = f"{message_type}_msg_id"
        msg_id = self.state.get(msg_id_key)
        if not msg_id:
            return
        payload = {"chat_id": self.chat_id, "message_id": int(msg_id)}
        res = self._api_call("deleteMessage", payload)
        if res and res.get("ok"):
            logger.info(f"Successfully deleted Telegram {message_type} message ({msg_id})")
            self.state[msg_id_key] = None
            save_broker_state(self.state)

def compile_telemetry():
    """Read all JSON state files in the telemetry/ directory and aggregate them."""
    files = glob.glob(os.path.join(TELEMETRY_DIR, '*_*.json'))
    # Filter out state, broker, and summary files
    files = [f for f in files if not os.path.basename(f).startswith('broker') and not os.path.basename(f).startswith('failed') and not os.path.basename(f).lower().endswith('_summary.json')]
    
    bots_data = {}
    logout_alerts = {}
    stats_data = {
        "ReplyBot": {"replies": 0, "messages": 0, "accounts": {}},
        "CommentsReplyBot": {"posts": 0, "comments": 0, "replied": 0, "skipped": 0, "accounts": {}},
        "FewFeed": {"posts": 0, "accounts": {}},
        "AutoJoinBot": {"joins": 0, "pending": 0, "errors": 0, "total": 0, "accounts": {}}
    }
    
    now_ts = time.time()
    
    for filepath in files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            bot_name = data.get("bot_name", "Unknown")
            account = data.get("account", "unknown")
            if str(account).lower() == "summary":
                continue
            last_update = data.get("last_update", 0)
            
            # Determine status (stale check: if no update in 2 minutes, treat as Offline/Inactive)
            is_offline = (now_ts - last_update) > 120
            status = "🔴 Inactive / Stopped" if is_offline else data.get("status", "🟢 Active")
            
            bots_data.setdefault(bot_name, {})[account] = {
                "status": status,
                "last_update": last_update,
                "current_group": data.get("current_group", "N/A"),
                "events": data.get("recent_events", [])
            }
            
            # Collect logout/failed logins — only from files that are still active.
            # Stale files (bot not running this session) keep old failed_logins on disk
            # forever, causing ghost accounts to appear in alerts. Skip them.
            if not is_offline:
                failed = data.get("failed_logins") or {}
                for acc_name, reason in failed.items():
                    logout_alerts.setdefault(bot_name, {})[acc_name] = reason
                
            # Collect statistics
            stats = data.get("stats") or {}
            if bot_name == "ReplyBot":
                replies = stats.get("replies", 0)
                msgs = stats.get("messages", 0)
                stats_data["ReplyBot"]["replies"] += replies
                stats_data["ReplyBot"]["messages"] += msgs
                stats_data["ReplyBot"]["accounts"][account] = {"replies": replies, "messages": msgs}
            elif bot_name == "CommentsReplyBot":
                posts = stats.get("posts", 0)
                comments = stats.get("comments", 0)
                replied = stats.get("replied", 0)
                skipped = stats.get("skipped", 0)
                stats_data["CommentsReplyBot"]["posts"] += posts
                stats_data["CommentsReplyBot"]["comments"] += comments
                stats_data["CommentsReplyBot"]["replied"] += replied
                stats_data["CommentsReplyBot"]["skipped"] += skipped
                stats_data["CommentsReplyBot"]["accounts"][account] = {
                    "posts": posts, "comments": comments, "replied": replied, "skipped": skipped
                }
            elif bot_name == "FewFeed":
                posts = stats.get("posts", 0)
                stats_data["FewFeed"]["posts"] += posts
                stats_data["FewFeed"]["accounts"][account] = {"posts": posts}
            elif bot_name == "AutoJoinBot":
                joins = stats.get("joins", 0)
                pending = stats.get("pending", 0)
                errs = stats.get("errors", 0)
                tot = stats.get("total", 0)
                stats_data["AutoJoinBot"]["joins"] += joins
                stats_data["AutoJoinBot"]["pending"] += pending
                stats_data["AutoJoinBot"]["errors"] += errs
                stats_data["AutoJoinBot"]["total"] += tot
                stats_data["AutoJoinBot"]["accounts"][account] = {
                    "joins": joins, "pending": pending, "errors": errs, "total": tot
                }
        except Exception as e:
            logger.error(f"Error reading telemetry file {filepath}: {e}")
            
    return bots_data, logout_alerts, stats_data

def build_status_dashboard(bots_data, stats_data, logout_alerts):
    """Generate a clean, professional, emoji-rich systems dashboard."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        "🖥️ <b>CENTRAL BOT SYSTEM DASHBOARD</b> 🖥️",
        f"🕒 <i>Last Updated: {now_str}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]
    
    # 1. Accounts requiring attention warnings (if any)
    has_alerts = False
    alert_lines = [
        "⚠️ <b>ACCOUNTS REQUIRING ATTENTION</b> ⚠️",
        "────────────────────────────────",
    ]
    for bot_name, accounts in sorted(logout_alerts.items()):
        if not accounts:
            continue
        for acc, reason in sorted(accounts.items()):
            has_alerts = True
            alert_lines.append(f"• 👤 <b>{bot_name} (Acc {acc})</b> — 🔴 {reason}")
            
    if has_alerts:
        alert_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        alert_lines.append("")
        lines = alert_lines + lines
    
    bot_keys = ["FewFeed", "ReplyBot", "CommentsReplyBot", "AutoJoinBot"]
    labels = {
        "FewFeed": "🤖 <b>FB POSTER (FewFeed)</b>",
        "ReplyBot": "💬 <b>AUTO-REPLY (ReplyBot)</b>",
        "CommentsReplyBot": "💬 <b>COMMENTS REPLY (CommentsReplyBot)</b>",
        "AutoJoinBot": "👥 <b>GROUP JOINER (AutoJoinBot)</b>"
    }
    
    for bot in bot_keys:
        lines.append(labels[bot])
        accounts = bots_data.get(bot) or {}
        if not accounts:
            lines.append(" └─ ⚪ No accounts running / disabled")
        else:
            sorted_accs = sorted(accounts.keys(), key=lambda x: (len(x), x))
            for idx, acc in enumerate(sorted_accs):
                info = accounts[acc]
                status = info["status"]
                status_formatted = status
                if "running" in status.lower() or "active" in status.lower() or "watching" in status.lower():
                    status_formatted = f"🟢 {status}"
                elif "idle" in status.lower() or "sleep" in status.lower() or "wait" in status.lower():
                    status_formatted = f"🟡 {status}"
                elif "logged out" in status.lower() or "stop" in status.lower() or "inactive" in status.lower() or "offline" in status.lower():
                    status_formatted = f"🔴 {status}"
                
                connector = " └─" if idx == len(sorted_accs) - 1 else " ├─"
                lines.append(f"{connector} 👤 Account {acc}: {status_formatted}")
        lines.append("")
        
    lines.append("📊 <b>GENERAL PERFORMANCE STATISTICS</b> 📊")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    ff = stats_data["FewFeed"]
    rb = stats_data["ReplyBot"]
    cr = stats_data["CommentsReplyBot"]
    aj = stats_data["AutoJoinBot"]
    
    lines.append(f"🤖 <b>FewFeed:</b> 📦 <b>{ff['posts']}</b> posts published")
    lines.append(f"💬 <b>ReplyBot:</b> ✅ <b>{rb['replies']}</b> replies | 💬 <b>{rb['messages']}</b> messages")
    lines.append(f"💬 <b>CommentsReply:</b> ✅ <b>{cr['replied']}</b> replies | ⏭️ <b>{cr['skipped']}</b> skipped")
    lines.append(f"👥 <b>AutoJoin:</b> ✅ <b>{aj['joins']}</b> groups joined | ⏳ <b>{aj['pending']}</b> pending")
    
    total_actions = ff['posts'] + rb['replies'] + rb['messages'] + cr['replied'] + aj['joins']
    
    lines.append("────────────────────────────────")
    lines.append(f"🏆 <b>GRAND TOTAL ACTIONS:</b> ⭐ <b>{total_actions}</b> successfully executed!")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔄 Dashboard updates automatically in real-time.")
    return "\n".join(lines)

def build_logout_alerts(logout_alerts):
    """Generate consolidated, attention-grabbing alert message for logged-out accounts."""
    if not logout_alerts or not any(logout_alerts.values()):
        return None  # No message needed, delete alert message!
        
    lines = [
        "⚠️ <b>ACCOUNTS REQUIRE ATTENTION</b> ⚠️",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "The following accounts have encountered login failures or expired cookies:",
        ""
    ]
    
    for bot_name, accounts in sorted(logout_alerts.items()):
        if not accounts:
            continue
        lines.append(f"📦 <b>{bot_name}</b>")
        for acc, reason in sorted(accounts.items()):
            lines.append(f" ├─ 👤 <b>Account {acc}</b> — 🔴 {reason}")
        lines.append("")
        
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔧 <b>Action Required:</b> Update the cookie values in the Google Sheet / account directory and restart the respective bot.")
    return "\n".join(lines)

def build_statistics_report(stats_data):
    """Generate beautifully formatted session statistics report."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        "📊 <b>CENTRAL BOT PERFORMANCE REPORT</b> 📊",
        f"🕒 <i>Reporting Time: {now_str}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]
    
    # 1. FewFeed
    ff = stats_data["FewFeed"]
    lines.append("🤖 <b>FB POSTER (FewFeed)</b>")
    for acc, s in sorted(ff["accounts"].items()):
        lines.append(f" ├─ Account {acc}: 📦 <b>{s['posts']}</b> posts confirmed")
    lines.append(f" └─ <b>Total Success:</b> 📦 {ff['posts']} posts successfully made\n")
    
    # 2. ReplyBot
    rb = stats_data["ReplyBot"]
    lines.append("💬 <b>AUTO-REPLIES (ReplyBot)</b>")
    for acc, s in sorted(rb["accounts"].items()):
        lines.append(f" ├─ Account {acc}: ✅ {s['replies']} replies | 💬 {s['messages']} messages")
    lines.append(f" └─ <b>Total Success:</b> ✅ {rb['replies']} replies | 💬 {rb['messages']} messages\n")
    
    # 3. CommentsReplyBot
    cr = stats_data["CommentsReplyBot"]
    lines.append("💬 <b>COMMENTS REPLY (CommentsReplyBot)</b>")
    for acc, s in sorted(cr["accounts"].items()):
        lines.append(f" ├─ Account {acc}: ✅ {s['replied']} replies | ⏭️ {s['skipped']} skipped")
    lines.append(f" └─ <b>Total Success:</b> ✅ {cr['replied']} replies | ⏭️ {cr['skipped']} skipped\n")
    
    # 4. AutoJoinBot
    aj = stats_data["AutoJoinBot"]
    lines.append("👥 <b>GROUP JOINER (AutoJoinBot)</b>")
    for acc, s in sorted(aj["accounts"].items()):
        lines.append(f" ├─ Account {acc}: ✅ {s['joins']} joined | ⏳ {s['pending']} pending | ⚠️ {s['errors']} errors")
    lines.append(f" └─ <b>Total Success:</b> ✅ {aj['joins']} joined | ⏳ {aj['pending']} pending\n")
    
    # Grand Totals
    total_actions = ff['posts'] + rb['replies'] + rb['messages'] + cr['replied'] + aj['joins']
    
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🏆 <b>GRAND TOTAL ACTIONS:</b> ⭐ <b>{total_actions}</b> successfully executed!")
    return "\n".join(lines)

dashboard_keyboard = {
    "inline_keyboard": [
        [
            {"text": "🤖 FewFeed Details", "callback_data": "view_fewfeed"},
            {"text": "💬 ReplyBot Details", "callback_data": "view_replybot"}
        ],
        [
            {"text": "💬 CommentsReply Details", "callback_data": "view_comments"},
            {"text": "👥 AutoJoin Details", "callback_data": "view_autojoin"}
        ],
        [
            {"text": "🔄 Refresh Dashboard", "callback_data": "refresh_dash"}
        ]
    ]
}

def build_help_message():
    """Format and return a helpful system guidelines prompt."""
    return (
        "<b>📋 TELEMETRY BROKER GUIDE & COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Use these standard text commands anywhere in the chat:\n\n"
        "• <b>/status</b>: Forces an instant refresh of the dashboard status.\n"
        "• <b>/errors</b>: Compiles detailed login/stale errors for all accounts.\n"
        "• <b>/reset</b>: Wipes historical counts and restarts statistics from 0.\n\n"
        "<i>Tip: You can also click the attached buttons on the Welcome Message or Dashboard for instant detailed views!</i>"
    )

def send_welcome_message(notifier):
    """Send a gorgeous Welcoming & Explanation message upon broker startup, outlining details and interactive options."""
    welcome_text = (
        "🚀 <b>CENTRAL TELEMETRY BROKER IS ONLINE!</b> 🚀\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Welcome to your unified <b>Bot Farm control center</b>! This system runs automatically in the background to consolidate telemetry, alerts, and performance statistics.\n\n"
        "<b>📂 How It Works:</b>\n"
        "• Your active bots write their live status locally to the <code>telemetry/</code> directory.\n"
        "• The broker scans this directory every 10s and updates exactly <b>2 rolling messages</b> to keep your chat clean and rate-limit-free.\n\n"
        "<b>📱 Persistent Layout:</b>\n"
        "1. 🖥️ <b>Dashboard & Stats:</b> Real-time status of all running accounts + grand totals.\n"
        "2. ⚠️ <b>Logout Alerts:</b> Live list of cookie expirations (deletes itself when healthy!).\n\n"
        "<b>🎮 Interactive Controls:</b>\n"
        "Use the interactive buttons attached to the <b>Dashboard Message</b> below to inspect specific bots in detail, refresh the dashboard instantly, or show helper guidelines!"
    )
    
    res = notifier._api_call("sendMessage", {
        "chat_id": notifier.chat_id,
        "text": welcome_text,
        "parse_mode": "HTML"
    })
    if res and res.get("ok"):
        notifier.state["welcome_msg_id"] = res["result"]["message_id"]
        save_broker_state(notifier.state)

def build_detailed_bot_report(bot_target):
    """Compile and format real-time details, logs, events, and performance of a specific bot."""
    mapping = {
        "fewfeed": ("FewFeed", "🤖 FB POSTER (FewFeed)"),
        "replybot": ("ReplyBot", "💬 AUTO-REPLY (ReplyBot)"),
        "comments": ("CommentsReplyBot", "💬 COMMENTS REPLY (CommentsReplyBot)"),
        "autojoin": ("AutoJoinBot", "👥 AUTO-JOIN GROUPS (AutoJoinBot)")
    }
    
    if bot_target not in mapping:
        return "⚠️ Unknown bot selected."
        
    internal_name, display_name = mapping[bot_target]
    files = glob.glob(os.path.join(TELEMETRY_DIR, f"{internal_name}_*.json"))
    
    lines = [
        f"<b>📊 DETAILED REPORT: {display_name}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    
    if not files:
        lines.append("<i>No active sessions or files recorded for this bot.</i>")
        lines.append("Make sure it is started via start-bots.ps1.")
        return "\n".join(lines)
        
    now = time.time()
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            acc = data.get("account", "unknown")
            status = data.get("status", "unknown")
            last_upd = data.get("last_update", 0)
            stats = data.get("stats") or {}
            events = data.get("recent_events") or []
            failed = data.get("failed_logins") or {}
            
            is_active = (now - last_upd) < 120
            emoji = "🟢" if is_active else "🔴"
            active_text = "Running" if is_active else "Stopped/Inactive"
            
            lines.append(f"{emoji} <b>Account {acc}</b> ({active_text})")
            lines.append(f"   • Status: <i>{status}</i>")
            
            if stats:
                stat_parts = []
                if bot_target == "comments":
                    posts = stats.get("posts", 0)
                    comments = stats.get("comments", 0)
                    replied = stats.get("replied", 0)
                    skipped = stats.get("skipped", 0)
                    stat_parts.append(f"Scan Posts: <b>{posts}</b>")
                    stat_parts.append(f"Comments Scanned: <b>{comments}</b>")
                    stat_parts.append(f"Replies Sent: <b>{replied}</b>")
                    stat_parts.append(f"Skipped: <b>{skipped}</b>")
                elif bot_target == "fewfeed":
                    posts = stats.get("posts", 0)
                    stat_parts.append(f"Posts Published: <b>{posts}</b>")
                elif bot_target == "replybot":
                    replies = stats.get("replies", 0)
                    msgs = stats.get("messages", 0)
                    stat_parts.append(f"Replies Sent: <b>{replies}</b>")
                    stat_parts.append(f"Messages Sent: <b>{msgs}</b>")
                elif bot_target == "autojoin":
                    joins = stats.get("joins", 0)
                    pending = stats.get("pending", 0)
                    errors = stats.get("errors", 0)
                    stat_parts.append(f"Groups Joined: <b>{joins}</b>")
                    stat_parts.append(f"Pending: <b>{pending}</b>")
                    stat_parts.append(f"Errors: <b>{errors}</b>")
                else:
                    for k, v in stats.items():
                        stat_parts.append(f"{k.capitalize()}: <b>{v}</b>")
                lines.append(f"   • Stats: {' | '.join(stat_parts)}")
                
            if failed:
                lines.append(f"   • ⚠️ <b>Issue:</b> {list(failed.values())[0]}")
                
            if events:
                lines.append("   • 📝 <b>Recent activity log:</b>")
                for ev in events[-3:]:
                    lines.append(f"     └ {ev}")
                    
            lines.append("──────────────────────")
        except Exception as e:
            lines.append(f"⚠️ Error reading account file: {e}")
            
    return "\n".join(lines)

def archive_previous_session(notifier):
    """Archiving the last active session message by adding a datetime banner and stripping its keyboard."""
    try:
        old_status_id = notifier.state.get("status_msg_id")
        old_welcome_id = notifier.state.get("welcome_msg_id")
        
        # 1. Archive the old status dashboard
        if old_status_id:
            try:
                bots_data, logout_alerts, stats_data = compile_telemetry()
                from datetime import datetime
                full_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                archive_text = (
                    f"📁 <b>[SESSION HISTORY ARCHIVE — {full_date}]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{build_status_dashboard(bots_data, stats_data, logout_alerts)}"
                )
                archive_text = archive_text.replace("🔄 Dashboard updates automatically in real-time.", "🔒 Session closed and archived.")
                
                notifier._api_call("editMessageText", {
                    "chat_id": notifier.chat_id,
                    "message_id": int(old_status_id),
                    "text": archive_text,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps({"inline_keyboard": []})
                }, enforce_rate_limit=False)
                logger.info(f"Successfully moved final statistics to history archive ({old_status_id})")
            except Exception as e:
                logger.debug(f"Could not edit old dashboard into archive: {e}")
                try:
                    notifier._api_call("editMessageReplyMarkup", {
                        "chat_id": notifier.chat_id,
                        "message_id": int(old_status_id),
                        "reply_markup": json.dumps({"inline_keyboard": []})
                    }, enforce_rate_limit=False)
                except Exception:
                    pass
                    
        # 2. Delete the old welcome guide
        if old_welcome_id:
            try:
                notifier._api_call("deleteMessage", {
                    "chat_id": notifier.chat_id,
                    "message_id": int(old_welcome_id)
                }, enforce_rate_limit=False)
            except Exception:
                pass
                
        notifier.delete_message("alerts")
    except Exception as e:
        logger.error(f"Error during session archiving: {e}")

def parent_monitor_worker(parent_pid, notifier):
    logger.info(f"Starting parent process monitor for PID {parent_pid}...")
    import time
    import ctypes
    import subprocess
    import glob
    
    def is_process_running(pid):
        PROCESS_QUERY_INFORMATION = 0x0400
        SYNCHRONIZE = 0x0010
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, pid)
        if handle == 0:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return exit_code.value == 259 # STILL_ACTIVE
        
    while True:
        if not is_process_running(parent_pid):
            logger.info("Parent launcher process exit detected! Starting clean shutdown sequence...")
            
            # 1. Archive active session dashboard
            try:
                archive_previous_session(notifier)
            except Exception as e:
                logger.error(f"Failed to archive session on parent exit: {e}")
                
            # 2. Kill all bot processes
            try:
                cmd = (
                    f"powershell -Command \""
                    f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {parent_pid} -or $_.CommandLine -like '*facebook_bot*' -or $_.CommandLine -like '*new.py*' -or $_.CommandLine -like '*new.exe*' -or $_.CommandLine -like '*fewfeed*' -or $_.CommandLine -like '*join_groups*' -or $_.CommandLine -like '*ReplyBot*' -or $_.CommandLine -like '*CommentsReplyBot*' -or $_.CommandLine -like '*FewFeedBot*' -or $_.CommandLine -like '*AutoJoinBot*' -or $_.CommandLine -like '*--remote-debugging-port=*' }} | ForEach-Object {{ Stop-Process $_.ProcessId -Force }}"
                    f"\""
                )
                subprocess.run(cmd, shell=True, capture_output=True)
                subprocess.run("taskkill /f /im chromedriver.exe 2>nul", shell=True, capture_output=True)
                logger.info("Successfully terminated child bots and Chrome instances.")
            except Exception as e:
                logger.error(f"Error terminating child processes: {e}")
                
            # 3. Wipe all files in telemetry/
            try:
                for filename in os.listdir(TELEMETRY_DIR):
                    fp = os.path.join(TELEMETRY_DIR, filename)
                    if os.path.isfile(fp):
                        try:
                            os.remove(fp)
                        except Exception:
                            pass
                logger.info("Wiped entire telemetry folder successfully.")
            except Exception as e:
                logger.error(f"Error wiping telemetry: {e}")
                
            logger.info("Shutdown sequence complete. Exiting.")
            os._exit(0)
            
        time.sleep(1.5)

def handle_callback_query(cb_id, cb_data, notifier, msg_id):
    """Handle Telegram Inline Keyboard button click callbacks by editing the message in-place."""
    # Bypassing rate limit for answerCallbackQuery removes clicking lag completely!
    notifier._api_call("answerCallbackQuery", {"callback_query_id": cb_id}, enforce_rate_limit=False)
    
    if cb_data == "refresh_dash":
        notifier.state["current_view"] = "dashboard"
        notifier.state["status_msg_id"] = msg_id
        save_broker_state(notifier.state)
        
        bots_data, logout_alerts, stats_data = compile_telemetry()
        status_text = build_status_dashboard(bots_data, stats_data, logout_alerts)
        notifier._api_call("editMessageText", {
            "chat_id": notifier.chat_id,
            "message_id": msg_id,
            "text": status_text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(dashboard_keyboard)
        }, enforce_rate_limit=False)
        
    elif cb_data.startswith("view_"):
        bot_target = cb_data.split("_")[1]
        notifier.state["current_view"] = bot_target
        notifier.state["status_msg_id"] = msg_id
        save_broker_state(notifier.state)
        
        bot_details = build_detailed_bot_report(bot_target)
        notifier._api_call("editMessageText", {
            "chat_id": notifier.chat_id,
            "message_id": msg_id,
            "text": bot_details,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [
                        {"text": "🔄 Refresh Details", "callback_data": f"view_{bot_target}"},
                        {"text": "🖥️ Back to Dashboard", "callback_data": "refresh_dash"}
                    ]
                ]
            })
        }, enforce_rate_limit=False)
        
    elif cb_data == "show_help":
        notifier.state["current_view"] = "help"
        notifier.state["status_msg_id"] = msg_id
        save_broker_state(notifier.state)
        
        help_text = build_help_message()
        notifier._api_call("editMessageText", {
            "chat_id": notifier.chat_id,
            "message_id": msg_id,
            "text": help_text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [
                        {"text": "🖥️ Back to Dashboard", "callback_data": "refresh_dash"}
                    ]
                ]
            })
        }, enforce_rate_limit=False)

def command_polling_worker(notifier):
    """Poll for Telegram updates and process command requests and callback queries in the chat."""
    update_offset = None
    logger.info("Starting Telegram Command Polling thread...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{notifier.token}/getUpdates"
            params = {"timeout": 15}
            if update_offset is not None:
                params["offset"] = update_offset
            
            resp = requests.get(url, params=params, timeout=20)
            if resp.ok:
                data = resp.json()
                for upd in data.get("result", []):
                    update_offset = upd.get("update_id", 0) + 1
                    
                    if "callback_query" in upd:
                        cb = upd["callback_query"]
                        cb_id = cb["id"]
                        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                        if chat_id != notifier.chat_id:
                            continue
                        cb_data = cb.get("data", "")
                        msg_id = cb.get("message", {}).get("message_id")
                        logger.info(f"Received Callback Query: '{cb_data}' for message {msg_id}")
                        handle_callback_query(cb_id, cb_data, notifier, msg_id)
                        continue
                        
                    msg = upd.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != notifier.chat_id:
                        continue
                    
                    text = (msg.get("text") or "").strip().lower()
                    if not text:
                        continue
                    
                    logger.info(f"Received Telegram command: '{text}'")
                    handle_telegram_command(text, notifier)
            else:
                time.sleep(5)
        except Exception as e:
            logger.error(f"Error in Telegram command polling: {e}")
            time.sleep(5)

def handle_telegram_command(cmd, notifier):
    """Handle incoming Telegram commands in the bot farm chat."""
    if cmd == "/status" or cmd == "/start":
        bots_data, logout_alerts, stats_data = compile_telemetry()
        status_text = build_status_dashboard(bots_data, stats_data, logout_alerts)
        notifier.send_or_update(status_text, "status", reply_markup=dashboard_keyboard)
        notifier._api_call("sendMessage", {"chat_id": notifier.chat_id, "text": "🔄 Dashboard status refreshed instantly!"})
        
    elif cmd.startswith("/errors"):
        files = glob.glob(os.path.join(TELEMETRY_DIR, '*_*.json'))
        errors_found = False
        lines = ["<b>❗ Detailed Bot Errors / Issues</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        
        for fp in files:
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                bot_name = data.get("bot_name", "Unknown")
                acc = data.get("account", "unknown")
                failed = data.get("failed_logins") or {}
                if failed:
                    errors_found = True
                    lines.append(f"❌ <b>{bot_name} (Account {acc})</b>:")
                    for a, msg in failed.items():
                        lines.append(f"   - {msg}")
            except Exception:
                pass
                
        if not errors_found:
            notifier._api_call("sendMessage", {"chat_id": notifier.chat_id, "text": "✅ No active errors recorded on any running bot!"})
        else:
            notifier._api_call("sendMessage", {"chat_id": notifier.chat_id, "text": "\n".join(lines), "parse_mode": "HTML"})
            
    elif cmd == "/reset":
        try:
            for filename in os.listdir(TELEMETRY_DIR):
                fp = os.path.join(TELEMETRY_DIR, filename)
                if os.path.isfile(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
        except Exception:
            pass
        notifier._api_call("sendMessage", {"chat_id": notifier.chat_id, "text": "🗑️ Telemetry statistics reset! Bots will regenerate data on next loop."})



def main():
    logger.info("Initializing Central Telegram Telemetry Broker...")
    
    # 1. Load configuration
    cfg = load_config_with_retry()
    if not cfg:
        logger.error("Could not initialize Telegram settings. Exiting.")
        sys.exit(1)
        
    notifier = CentralTelegramNotifier(cfg)
    
    # Parse parent process ID to monitor for clean shutdown on exit
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, default=None)
    args, unknown = parser.parse_known_args()
    
    parent_pid = args.parent_pid or os.getppid()
    if parent_pid:
        t_monitor = threading.Thread(target=parent_monitor_worker, args=(parent_pid, notifier), daemon=True)
        t_monitor.start()
        logger.info(f"Started parent process monitor for PID: {parent_pid}")
        
    # Clean up legacy persistent messages permanently and archive the previous session's dashboard
    archive_previous_session(notifier)
        
    # Reset states for fresh interactive session panels
    notifier.state["welcome_msg_id"] = None
    notifier.state["status_msg_id"] = None
    notifier.state["current_view"] = "dashboard"
    save_broker_state(notifier.state)
        
    # Clean up previous session telemetry files on startup so we start completely from 0!
    try:
        for filename in os.listdir(TELEMETRY_DIR):
            fp = os.path.join(TELEMETRY_DIR, filename)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
        logger.info("Cleared all historical telemetry files on startup successfully.")
    except Exception as e:
        logger.error(f"Failed to clear historical stats on startup: {e}")
    
    # 2. Start Telegram Commands Polling Thread
    t = threading.Thread(target=command_polling_worker, args=(notifier,), daemon=True)
    t.start()
    
    # 3. Send Welcoming Message (disabled - no longer sends on every startup)
    # try:
    #     send_welcome_message(notifier)
    # except Exception as e:
    #     logger.error(f"Failed to send welcome message: {e}")
    
    logger.info("Broker running successfully. Entering main update loop...")
    
    # 4. Main Update Loop
    last_status_hash = ""

    while True:
        try:
            # Aggregate all telemetry files
            bots_data, logout_alerts, stats_data = compile_telemetry()

            if notifier.alerts_only:
                # Telegram is used ONLY for account login-problem notifications,
                # across ALL bots. compile_telemetry() already aggregates every
                # bot's failed_logins, so this one message covers the whole farm.
                # Push the consolidated alert when one or more accounts have a
                # login failure / expired cookie; remove the message entirely when
                # everything is healthy. No routine status/stats dashboard is sent.
                alert_text = build_logout_alerts(logout_alerts)
                alert_hash = hash(alert_text or "")
                if alert_hash != last_status_hash:
                    if alert_text:
                        notifier.send_or_update(alert_text, "alerts")
                    else:
                        notifier.delete_message("alerts")
                    last_status_hash = alert_hash
                time.sleep(10)
                continue

            # Check the active view mode (dashboard or specific bot details)
            view_mode = notifier.state.get("current_view", "dashboard")

            if view_mode == "dashboard":
                # Message 1: Status Dashboard
                status_text = build_status_dashboard(bots_data, stats_data, logout_alerts)
                status_hash = hash(status_text)
                if status_hash != last_status_hash:
                    notifier.send_or_update(status_text, "status", reply_markup=dashboard_keyboard)
                    last_status_hash = status_hash
            elif view_mode == "help":
                # Static help view, skip auto-refresh to prevent lag
                pass
            else:
                # Dynamic auto-refresh for bot details!
                bot_details = build_detailed_bot_report(view_mode)
                details_hash = hash(bot_details)
                if details_hash != last_status_hash:
                    notifier.send_or_update(bot_details, "status", reply_markup={
                        "inline_keyboard": [
                            [
                                {"text": "🔄 Refresh Details", "callback_data": f"view_{view_mode}"},
                                {"text": "🖥️ Back to Dashboard", "callback_data": "refresh_dash"}
                            ]
                        ]
                    })
                    last_status_hash = details_hash

        except Exception as e:
            logger.error(f"Error in broker main loop: {e}")

        time.sleep(10)

def load_config_with_retry():
    """Attempt loading configuration, retrying every 10s if not found initially."""
    for attempt in range(12):  # Try for 2 minutes
        cfg = load_config()
        if cfg:
            return cfg
        logger.info(f"Attempt {attempt+1}: waiting for valid config.json in directories...")
        time.sleep(10)
    return None

if __name__ == "__main__":
    main()
