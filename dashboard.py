import json
import os
import sys
import threading
import time
import hashlib
import hmac
from datetime import datetime
from bottle import Bottle, run, route, request, response, static_file, HTTPResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stats_tracker import tracker, get_tracker

app = Bottle()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

def _make_token():
    raw = f"{DASHBOARD_PASSWORD}:{int(time.time() // 86400)}"
    return hmac.new(DASHBOARD_PASSWORD.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]

def _check_auth():
    if not DASHBOARD_PASSWORD:
        return True
    token = request.get_cookie("dash_token") or request.query.get("token") or ""
    return hmac.compare_digest(token, _make_token())

def load_template(filename):
    path = os.path.join(TEMPLATES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if "/*SHARED_CSS*/" in content:
        shared_css_path = os.path.join(TEMPLATES_DIR, "shared.css")
        with open(shared_css_path, "r", encoding="utf-8") as f_css:
            content = content.replace("/*SHARED_CSS*/", f_css.read())
            
    if "/*SHARED_JS*/" in content:
        shared_js_path = os.path.join(TEMPLATES_DIR, "shared.js")
        with open(shared_js_path, "r", encoding="utf-8") as f_js:
            content = content.replace("/*SHARED_JS*/", f_js.read())
            
    return content

@app.hook('before_request')
def _auth_hook():
    if not DASHBOARD_PASSWORD:
        return
    if request.path == '/login' or request.path.startswith('/static/'):
        return
    if not _check_auth():
        response.set_cookie("redirect_to", request.fullpath or "/")
        if request.path.startswith('/api/'):
            raise HTTPResponse(body=json.dumps({"ok": False, "error": "Authentication required"}), status=401, headers={"Content-Type": "application/json"})
        raise HTTPResponse(body=load_template("login.html"), status=200, headers={"Content-Type": "text/html; charset=utf-8"})

@app.route('/login', method=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        pwd = request.forms.get("password", "")
        if pwd == DASHBOARD_PASSWORD:
            response.set_cookie("dash_token", _make_token(), max_age=86400*7)
            redirect_to = request.get_cookie("redirect_to") or "/"
            response.delete_cookie("redirect_to")
            raise HTTPResponse(status=302, headers={"Location": redirect_to})
        return "<html><body style='font-family:sans-serif;background:#0f0f13;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;'><div style='text-align:center'><h2>Wrong password</h2><a href='/login' style='color:#6366f1'>Try again</a></div></body></html>"
    if _check_auth():
        raise HTTPResponse(status=302, headers={"Location": "/"})
    response.content_type = 'text/html; charset=utf-8'
    return load_template("login.html")

@app.route('/api/overview')
def api_overview():
    response.content_type = 'application/json'
    try:
        accounts = tracker.get_active_accounts()
        today = tracker.get_today_stats()
        bots_today = tracker.get_bot_today_stats()
        session = tracker.get_current_session()
        recent = tracker.get_recent_events(limit=20)
        daily = tracker.get_daily_stats(days=14)
        bots = tracker.get_bots_list()
        machines = tracker.get_active_machines()

        now_ts = time.time()
        for a in accounts:
            la = a.get("last_active")
            status = a.get("status", "unknown")
            if status in ("active", "running"):
                if la and (now_ts - la) > 300:
                    a["status"] = "offline"

        active_count = sum(1 for a in accounts if a["status"] in ("active", "running"))
        failed_count = sum(1 for a in accounts if a["status"] == "logged_out")
        # "idle" = registered but not running; "offline" = was active, now stale.
        # Both mean "not currently launched", so report them together.
        idle_count = sum(1 for a in accounts if a["status"] in ("idle", "offline"))
        paused_count = sum(1 for a in accounts if a["status"] == "paused")
        # Idle/offline accounts are "not currently launched" — don't count them
        # toward the total. Total reflects only running/failed/paused accounts.
        total_count = sum(1 for a in accounts if a["status"] not in ("idle", "offline"))

        for e in recent:
            e["created_at"] = _fmt_ts(e["created_at"])
        for a in accounts:
            a["last_active"] = _fmt_ts(a["last_active"])

        bot_daily = {}
        for b in bots:
            bot_daily[b] = tracker.get_daily_stats(days=14, bot_name=b)

        bot_accounts = {}
        for b in bots:
            bot_accounts[b] = {"active": 0, "failed": 0, "idle": 0, "total": 0}
        for a in accounts:
            bn = a.get("bot_name", "")
            if bn in bot_accounts:
                if a["status"] in ("active", "running"):
                    bot_accounts[bn]["active"] += 1
                    bot_accounts[bn]["total"] += 1
                elif a["status"] == "logged_out":
                    bot_accounts[bn]["failed"] += 1
                    bot_accounts[bn]["total"] += 1
                elif a["status"] in ("idle", "offline"):
                    bot_accounts[bn]["idle"] += 1

        for m in machines:
            m["last_seen"] = _fmt_ts(m.get("last_seen"))

        return json.dumps({
            "ok": True,
            "active_count": active_count,
            "failed_count": failed_count,
            "idle_count": idle_count,
            "paused_count": paused_count,
            "total_count": total_count,
            "accounts": accounts,
            "today": today,
            "bots_today": bots_today,
            "bots": bots,
            "recent_events": recent,
            "daily_stats": daily,
            "bot_daily": bot_daily,
            "bot_accounts": bot_accounts,
            "session": dict(session) if session else None,
            "machines": machines,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/bot/<name>')
def api_bot(name):
    response.content_type = 'application/json'
    try:
        bt = get_tracker(bot_name=name)
        accounts = bt.get_active_accounts(bot_name=name)
        today = bt.get_today_stats(bot_name=name)
        events = bt.get_recent_events(limit=30, bot_name=name)
        daily = bt.get_daily_stats(days=14, bot_name=name)

        now_ts = time.time()
        for a in accounts:
            la = a.get("last_active")
            status = a.get("status", "unknown")
            if status in ("active", "running"):
                if la and (now_ts - la) > 300:
                    a["status"] = "offline"
            a["last_active"] = _fmt_ts(a["last_active"])

        for e in events:
            e["created_at"] = _fmt_ts(e["created_at"])

        return json.dumps({
            "ok": True,
            "bot": name,
            "accounts": accounts,
            "today": today,
            "events": events,
            "daily_stats": daily
        })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/account/<name>')
def api_account(name):
    response.content_type = 'application/json'
    try:
        data = tracker.get_account_history(name)
        for e in data["events"]:
            e["created_at"] = _fmt_ts(e["created_at"])
        for l in data["logins"]:
            l["attempted_at"] = _fmt_ts(l["attempted_at"])

        # Real stored status per bot (with the same stale->offline rule the
        # rest of the dashboard uses), so the page shows the true state instead
        # of guessing from the last login attempt.
        statuses = tracker.get_account_status(name)
        now_ts = time.time()
        for s in statuses:
            la = s.get("last_active")
            if s.get("status") in ("active", "running") and la and (now_ts - la) > 300:
                s["status"] = "offline"
            s["last_active"] = _fmt_ts(s["last_active"])

        return json.dumps({"ok": True, "account": name, "data": data, "statuses": statuses})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/sessions')
def api_sessions():
    response.content_type = 'application/json'
    try:
        bot = request.query.get("bot") or None
        run = request.query.get("run") or None
        from stats_tracker import _supa_get
        params = {"order": "started_at.desc", "limit": "100"}
        if bot:
            params["bot_name"] = f"eq.{bot}"
        if run:
            params["run_id"] = f"eq.{run}"
        sessions = _supa_get("sessions", params)
        for s in sessions:
            s["started_at"] = _fmt_ts(s["started_at"])
            s["ended_at"] = _fmt_ts(s["ended_at"])
        return json.dumps({"ok": True, "sessions": sessions})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/events')
def api_events():
    response.content_type = 'application/json'
    try:
        limit = int(request.query.get("limit", 30))
        bot = request.query.get("bot") or None
        events = tracker.get_recent_events(limit=limit, bot_name=bot)
        for e in events:
            e["created_at"] = _fmt_ts(e["created_at"])
        return json.dumps({"ok": True, "events": events})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

_BOT_LOG_DIRS = {
    "ReplyBot":         os.path.join(os.path.dirname(os.path.abspath(__file__)), "ReplyBotv7", "logs"),
    "CommentsReplyBot": os.path.join(os.path.dirname(os.path.abspath(__file__)), "CommentsReplyBot", "logs"),
    "FewFeed":          os.path.join(os.path.dirname(os.path.abspath(__file__)), "fewfeedbotv6", "logs"),
    "AutoJoinBot":      os.path.join(os.path.dirname(os.path.abspath(__file__)), "AutoJoinBot", "logs"),
}

@app.route('/api/control/stop-all', method='POST')
def api_control_stop_all():
    response.content_type = 'application/json'
    try:
        tracker.enqueue_command("stop-all")
        return json.dumps({"ok": True, "message": "Stop-all command queued — the PC broker will stop all bots."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/start-all', method='POST')
def api_control_start_all():
    response.content_type = 'application/json'
    try:
        tracker.enqueue_command("start-all")
        return json.dumps({"ok": True, "message": "Start-all command queued — the PC broker will launch the fleet."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/restart-all', method='POST')
def api_control_restart_all():
    response.content_type = 'application/json'
    try:
        tracker.enqueue_command("restart-all")
        return json.dumps({"ok": True, "message": "Restart-all command queued — the PC broker will restart the fleet."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

_KNOWN_BOTS = ("ReplyBot", "CommentsReplyBot", "FewFeed", "AutoJoinBot")

@app.route('/api/control/stop/<name>', method='POST')
def api_control_stop(name):
    response.content_type = 'application/json'
    try:
        if name not in _KNOWN_BOTS:
            return json.dumps({"ok": False, "error": f"Unknown bot: {name}"})
        tracker.enqueue_command("stop", bot_name=name)
        return json.dumps({"ok": True, "message": f"Stop command for {name} queued — waiting for the PC broker."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/start/<name>', method='POST')
def api_control_start(name):
    response.content_type = 'application/json'
    try:
        if name not in _KNOWN_BOTS:
            return json.dumps({"ok": False, "error": f"Unknown bot: {name}"})
        tracker.enqueue_command("start", bot_name=name)
        return json.dumps({"ok": True, "message": f"Start command for {name} queued — waiting for the PC broker."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/restart/<name>', method='POST')
def api_control_restart(name):
    response.content_type = 'application/json'
    try:
        if name not in _KNOWN_BOTS:
            return json.dumps({"ok": False, "error": f"Unknown bot: {name}"})
        tracker.enqueue_command("restart", bot_name=name)
        return json.dumps({"ok": True, "message": f"Restart command for {name} queued — waiting for the PC broker."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/commands')
def api_control_commands():
    """Recent control commands + their execution status (so the UI can tell the
    operator whether the PC broker actually picked the command up)."""
    response.content_type = 'application/json'
    try:
        from stats_tracker import _supa_get
        rows = _supa_get("bot_commands", {"order": "created_at.desc", "limit": "15"})
        for r in rows:
            r["created_at"] = _fmt_ts(r.get("created_at"))
            r["executed_at"] = _fmt_ts(r.get("executed_at"))
        pending = sum(1 for r in rows if r.get("status") == "pending")
        return json.dumps({"ok": True, "commands": rows, "pending": pending})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/machines')
def api_machines():
    """Active broker machines (PCs where start-bots.bat is running)."""
    response.content_type = 'application/json'
    try:
        machines = tracker.get_active_machines()
        for m in machines:
            m["last_seen"] = _fmt_ts(m.get("last_seen"))
            m["screenshot_at"] = _fmt_ts(m.get("screenshot_at"))
            # Omit screenshot bytes from the list endpoint to keep payloads small
            m.pop("screenshot", None)
        return json.dumps({"ok": True, "machines": machines})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/runs')
def api_runs():
    """All RDP runs, most recent first."""
    response.content_type = 'application/json'
    try:
        limit = int(request.query.get("limit", 30))
        machine = request.query.get("machine") or None
        runs = tracker.get_runs(machine_id=machine, limit=limit)
        for r in runs:
            r["started_at"] = _fmt_ts(r.get("started_at"))
            r["ended_at"] = _fmt_ts(r.get("ended_at"))
        return json.dumps({"ok": True, "runs": runs})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/git-pull', method='POST')
def api_control_git_pull():
    response.content_type = 'application/json'
    try:
        body = json.loads(request.body.read().decode() or "{}")
        machine_id = body.get("machine_id") or None
        if not machine_id:
            # target whichever machine is online
            machines = tracker.get_active_machines()
            machine_id = machines[0]["machine_id"] if machines else None
        tracker.enqueue_command("git-pull", machine_id=machine_id)
        return json.dumps({"ok": True, "message": "git pull command queued — broker will run it on the PC."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/screenshot', method='POST')
def api_control_screenshot():
    response.content_type = 'application/json'
    try:
        body = json.loads(request.body.read().decode() or "{}")
        machine_id = body.get("machine_id") or None
        if not machine_id:
            machines = tracker.get_active_machines()
            machine_id = machines[0]["machine_id"] if machines else None
        tracker.enqueue_command("screenshot", machine_id=machine_id)
        return json.dumps({"ok": True, "message": "Screenshot command queued — broker will capture and upload it."})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/screenshot/<machine_id>')
def api_get_screenshot(machine_id):
    """Return the latest screenshot stored for a machine."""
    response.content_type = 'application/json'
    try:
        from stats_tracker import _supa_get
        rows = _supa_get("machines", {"machine_id": f"eq.{machine_id}",
                                       "select": "machine_id,screenshot,screenshot_at"})
        if not rows:
            return json.dumps({"ok": False, "error": "Machine not found"})
        m = rows[0]
        return json.dumps({
            "ok": True,
            "machine_id": m.get("machine_id"),
            "screenshot": m.get("screenshot"),
            "screenshot_at": _fmt_ts(m.get("screenshot_at")),
        })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/control/logs/<name>')
def api_control_logs(name):
    response.content_type = 'application/json'
    try:
        log_dir = _BOT_LOG_DIRS.get(name)
        if not log_dir:
            return json.dumps({"ok": False, "error": f"Unknown bot: {name}"})
        lines_n = int(request.query.get("lines", 50))
        try:
            candidates = sorted(
                [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")],
                key=os.path.getmtime, reverse=True
            )
        except Exception:
            candidates = []
        if not candidates:
            return json.dumps({"ok": True, "log": f"No log files found in {log_dir}"})
        with open(candidates[0], "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-lines_n:])
        return json.dumps({"ok": True, "log": tail or "(empty)", "file": os.path.basename(candidates[0])})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/reset/today', method='POST')
def api_reset_today():
    response.content_type = 'application/json'
    try:
        bot = request.query.get("bot") or None
        tracker.reset_today(bot_name=bot)
        return json.dumps({"ok": True, "message": "Today's stats reset" + (f" for {bot}" if bot else " (all bots)")})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/reset/bot/<name>', method='POST')
def api_reset_bot(name):
    response.content_type = 'application/json'
    try:
        tracker.reset_bot(name)
        return json.dumps({"ok": True, "message": f"All data for '{name}' has been wiped"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/reset/account/<name>', method='POST')
def api_reset_account(name):
    response.content_type = 'application/json'
    try:
        bot = request.query.get("bot") or None
        tracker.reset_account(name, bot_name=bot)
        return json.dumps({"ok": True, "message": f"Account '{name}' reset"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/reset/all', method='POST')
def api_reset_all():
    response.content_type = 'application/json'
    try:
        tracker.reset_all()
        return json.dumps({"ok": True, "message": "All history wiped"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/action/account', method='POST')
def api_action_account():
    response.content_type = 'application/json'
    try:
        import json as _json
        body = _json.loads(request.body.read().decode())
        name = body.get("name", "")
        status = body.get("status", "active")
        bot = body.get("bot", None)
        if not name:
            return json.dumps({"ok": False, "error": "Missing account name"})
        tracker.set_account_status(name, status, bot_name=bot)
        return json.dumps({"ok": True, "message": f"Account '{name}' set to {status}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/api/daily')
def api_daily():
    response.content_type = 'application/json'
    try:
        days = int(request.query.get("days", 14))
        bot = request.query.get("bot") or None
        data = tracker.get_daily_stats(days=days, bot_name=bot)
        return json.dumps({"ok": True, "daily": data})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@app.route('/')
def dashboard():
    response.content_type = 'text/html; charset=utf-8'
    return load_template("dashboard.html")

@app.route('/bot/<name>')
def bot_page(name):
    response.content_type = 'text/html; charset=utf-8'
    return load_template("bot.html").replace("__BOT_NAME__", name)

@app.route('/account/<name>')
def account_page(name):
    response.content_type = 'text/html; charset=utf-8'
    return load_template("account.html").replace("__ACCOUNT_NAME__", name)

@app.route('/history')
def history_page():
    response.content_type = 'text/html; charset=utf-8'
    return load_template("history.html")

def _fmt_ts(ts):
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            # ts is a UTC epoch; emit a UTC ISO string so the browser converts
            # it to the viewer's local time. Using fromtimestamp() here would
            # bake in the server's local offset and then mislabel it "Z".
            return datetime.utcfromtimestamp(ts).isoformat() + "Z"
        if isinstance(ts, str):
            if "T" in ts:
                return ts
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    return datetime.strptime(ts, fmt).isoformat() + "Z"
                except ValueError:
                    continue
            return datetime.fromisoformat(ts.replace("Z", "")).isoformat() + "Z"
        return str(ts)
    except Exception:
        return str(ts)

def start_dashboard(host="0.0.0.0", port=None, open_browser=True):
    import os as _os
    if port is None:
        try:
            port = int(_os.environ.get("PORT", 8765))
        except (ValueError, TypeError):
            port = 8765
    if open_browser:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  Bot Farm Dashboard: http://127.0.0.1:{port}  (local)")
    try:
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and not local_ip.startswith("127."):
            print(f"  Network access:       http://{local_ip}:{port}")
    except Exception:
        pass
    print(f"  Press Ctrl+C to stop\n")
    app.run(host=host, port=port, quiet=True)

if __name__ == '__main__':
    start_dashboard()
