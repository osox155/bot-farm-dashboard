#!/usr/bin/env python3
"""
OBS control  (runs on the RDP)  — obs-websocket v5 (OBS Studio 28+).

Lets the SaaS go live + record + stop programmatically.

  • go_live()     -> starts streaming (OBS streams to Restream -> YT + FB)
                     and simultaneously starts a local recording.
  • stop()        -> stops streaming + recording, returns the saved file path.

Setup on the RDP:
  1. OBS Studio 28+  (Tools ▸ WebSocket Server Settings ▸ Enable, set a password)
  2. pip install obsws-python
  3. env: OBS_HOST=localhost  OBS_PORT=4455  OBS_PASSWORD=...
  4. Configure OBS Stream output to Restream.io (fans out to YouTube + Facebook).
     Configure Recording output (mp4) to a watched folder.
"""
from __future__ import annotations
import os, time
import obsws_python as obs


def _client():
    return obs.ReqClient(host=os.getenv("OBS_HOST", "localhost"),
                         port=int(os.getenv("OBS_PORT", "4455")),
                         password=os.environ.get("OBS_PASSWORD", ""),
                         timeout=10)


def go_live(scene: str | None = None):
    cl = _client()
    if scene:
        cl.set_current_program_scene(scene)
    st = cl.get_stream_status()
    if not st.output_active:
        cl.start_stream()
    rec = cl.get_record_status()
    if not rec.output_active:
        cl.start_record()
    return {"streaming": True, "recording": True, "ts": time.time()}


def stop():
    cl = _client()
    out_path = None
    rec = cl.get_record_status()
    if rec.output_active:
        resp = cl.stop_record()           # returns output path in v5
        out_path = getattr(resp, "output_path", None)
    st = cl.get_stream_status()
    if st.output_active:
        cl.stop_stream()
    return {"streaming": False, "recording": False, "recording_path": out_path}


def status():
    cl = _client()
    s = cl.get_stream_status(); r = cl.get_record_status()
    return {"streaming": s.output_active, "stream_secs": s.output_duration // 1000,
            "recording": r.output_active}


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["go-live", "stop", "status"])
    ap.add_argument("--scene")
    a = ap.parse_args()
    if a.action == "go-live":
        print(json.dumps(go_live(a.scene)))
    elif a.action == "stop":
        print(json.dumps(stop()))
    else:
        print(json.dumps(status()))
