#!/usr/bin/env python3
"""
watcher.py  —  folder-watch → n8n webhook when OBS recording finishes.

Watches OBS_REC_DIR for new video files. When a file appears and its size
stops growing (i.e. OBS has finished writing), fires a POST to the n8n
webhook so the produce/publish pipeline starts automatically.

You can run this two ways:
  • Automatically: started by run_match.bat before OBS (runs in background)
  • Manually:      python watcher.py --match-query "Arsenal vs Chelsea"

Environment variables
  OBS_REC_DIR      folder OBS records to   (default: C:\\obs-recordings)
  N8N_WEBHOOK      full webhook URL        (e.g. http://localhost:5678/webhook/match)
  MATCH_QUERY      pre-fill match query    (optional; n8n can also receive it via body)

The watcher fires at most ONCE per session (it exits after the first complete
recording so it doesn't re-trigger on old files). Re-run for each match.
"""
from __future__ import annotations
import os, sys, time, glob, json, datetime, argparse, threading

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

REC_DIR     = os.getenv("OBS_REC_DIR",  r"C:\obs-recordings")
N8N_WEBHOOK = os.getenv("N8N_WEBHOOK",  "")
MATCH_QUERY = os.getenv("MATCH_QUERY",  "")

VIDEO_EXTS  = {".mp4", ".mkv", ".flv", ".ts"}
# How long the file size must be stable before we consider it "done"
STABLE_SECS = 6
# How often to poll (seconds)
POLL_SECS   = 2
# Max time to wait for a recording to appear after watcher starts (seconds)
APPEAR_TIMEOUT = 60 * 60 * 6   # 6 hours (a full session)


def _log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] watcher: {msg}", flush=True)


def _video_files(directory: str) -> list[str]:
    out = []
    for ext in VIDEO_EXTS:
        out.extend(glob.glob(os.path.join(directory, f"*{ext}")))
    return out


def _stable_size(path: str, stable_secs: float = STABLE_SECS,
                 poll: float = POLL_SECS) -> int:
    """Poll until file size is unchanged for `stable_secs`. Returns final size."""
    last_size = -1
    stable_since = None
    while True:
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(poll)
            continue
        if size != last_size:
            last_size = size
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stable_secs:
            return size
        time.sleep(poll)


def _fire_webhook(webhook_url: str, recording_path: str,
                  match_query: str) -> bool:
    """POST to the n8n webhook. Returns True on success."""
    payload = {
        "recording_path": recording_path,
        "match_query":    match_query,
        "fired_at":       datetime.datetime.utcnow().isoformat() + "Z",
    }
    if not _HAS_REQUESTS:
        # Fallback: use urllib (stdlib)
        import urllib.request, urllib.error
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(webhook_url, data=data,
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                _log(f"webhook HTTP {r.status}")
                return r.status < 300
        except urllib.error.URLError as e:
            _log(f"webhook error: {e}")
            return False

    for attempt in range(1, 4):
        try:
            r = requests.post(webhook_url, json=payload, timeout=30)
            _log(f"webhook HTTP {r.status_code} (attempt {attempt})")
            if r.status_code < 300:
                return True
        except Exception as e:
            _log(f"webhook attempt {attempt} failed: {e}")
        time.sleep(5)
    return False


def watch(rec_dir: str, webhook_url: str, match_query: str,
          known_files: set[str] | None = None) -> str | None:
    """
    Block until a NEW video appears in rec_dir AND finishes writing.
    Returns the absolute path of the completed recording, or None on timeout.

    `known_files`: set of video paths that already existed when we started
    (so we don't re-fire on a previous recording that's still sitting there).
    """
    os.makedirs(rec_dir, exist_ok=True)
    if known_files is None:
        known_files = set(_video_files(rec_dir))

    _log(f"Watching {rec_dir} for new recordings…")
    _log(f"  webhook : {webhook_url or '(not set — will print path only)'}")
    _log(f"  match   : {match_query or '(none)'}")

    deadline = time.monotonic() + APPEAR_TIMEOUT
    while time.monotonic() < deadline:
        current = set(_video_files(rec_dir))
        new = current - known_files
        if new:
            path = max(new, key=os.path.getmtime)  # pick newest if multiple
            _log(f"New recording detected: {path}")
            _log("Waiting for OBS to finish writing…")
            final_size = _stable_size(path)
            _log(f"Recording complete ({final_size / 1_048_576:.1f} MB): {path}")

            if webhook_url:
                ok = _fire_webhook(webhook_url, os.path.abspath(path), match_query)
                if ok:
                    _log("✅ Webhook fired — n8n pipeline started.")
                else:
                    _log("⚠️  Webhook failed after 3 attempts. Trigger n8n manually.")
            else:
                _log("N8N_WEBHOOK not set — skipping webhook. Recording path:")
                _log(f"  {os.path.abspath(path)}")
            return os.path.abspath(path)

        time.sleep(POLL_SECS)

    _log("Timeout: no new recording appeared. Exiting watcher.")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Watch OBS recordings folder and fire n8n webhook when done.")
    ap.add_argument("--rec-dir",     default=REC_DIR,
                    help=f"OBS recording directory (default: {REC_DIR})")
    ap.add_argument("--webhook",     default=N8N_WEBHOOK,
                    help="n8n webhook URL (default: N8N_WEBHOOK env var)")
    ap.add_argument("--match-query", default=MATCH_QUERY,
                    help="Match name passed to the pipeline (default: MATCH_QUERY env var)")
    ap.add_argument("--background",  action="store_true",
                    help="Run in a daemon thread (for import by run_match.py)")
    a = ap.parse_args()

    if a.background:
        # Used when imported by run_match.py
        t = threading.Thread(
            target=watch,
            args=(a.rec_dir, a.webhook, a.match_query),
            daemon=True,
        )
        t.start()
        return t

    watch(a.rec_dir, a.webhook, a.match_query)


if __name__ == "__main__":
    main()
