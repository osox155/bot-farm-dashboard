#!/usr/bin/env python3
"""
Recording watcher  (runs on the RDP).

Watches the OBS recording folder. When a new .mp4 finishes writing (size
stable for a few seconds), it fires the n8n webhook with the file path +
the match query, which kicks off the produce→publish flow.

Run as a background service:
    OBS_REC_DIR=/path/to/recordings  N8N_WEBHOOK=https://.../webhook/match \
    python watcher.py
"""
from __future__ import annotations
import os, time, json, glob
import requests

REC_DIR = os.getenv("OBS_REC_DIR", os.path.expanduser("~/Videos"))
WEBHOOK = os.environ.get("N8N_WEBHOOK")          # n8n trigger
POLL = int(os.getenv("WATCH_POLL", "5"))
STABLE_CHECKS = 3                                 # size stable N polls = done
EXTS = (".mp4", ".mkv", ".flv")


def _stable(path):
    last = -1
    for _ in range(STABLE_CHECKS):
        try:
            sz = os.path.getsize(path)
        except OSError:
            return False
        if sz == last:
            return True
        last = sz
        time.sleep(POLL)
    return False


def fire(path, match_query=None):
    payload = {"recording_path": path,
               "match_query": match_query or "",
               "detected_at": time.time()}
    if WEBHOOK:
        r = requests.post(WEBHOOK, json=payload, timeout=30)
        print("fired n8n:", r.status_code, path)
    else:
        # no webhook configured -> just print (n8n exec node can read stdout)
        print(json.dumps(payload))


def main():
    print(f"Watching {REC_DIR} (every {POLL}s)…")
    seen = set(glob.glob(os.path.join(REC_DIR, "*")))
    while True:
        for p in glob.glob(os.path.join(REC_DIR, "*")):
            if p in seen or not p.lower().endswith(EXTS):
                continue
            if _stable(p):
                seen.add(p)
                fire(p, os.getenv("MATCH_QUERY"))
        time.sleep(POLL)


if __name__ == "__main__":
    main()
