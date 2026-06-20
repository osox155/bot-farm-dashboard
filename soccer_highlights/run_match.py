#!/usr/bin/env python3
"""
run_match.py  —  one-shot in-session orchestrator for the ephemeral RDP.

Designed for the GitHub-Actions RDP that gets wiped every ~5.5h. Because the
box auto-clones bot-farm-dashboard and installs requirements on every boot,
this script is already present + ready. You run it once per match.

Flow:
  1. go live  -> OBS starts streaming (Restream -> YouTube+Facebook) + recording
  2. you watch the match; press ENTER when full-time
  3. stop     -> OBS stops; we grab the saved recording
  4. produce  -> detect goals -> recap.mp4 + clips + goals.json
  5. brand    -> thumbnail + per-platform captions
  6. publish  -> post to YouTube/Facebook/Instagram (if creds set) else skip
  7. backup   -> copy outputs to Downloads\\matches\\<timestamp>\\ and (optional)
                 push goals.json+thumbnail to the repo so they survive the wipe
  8. notify   -> Telegram message with the summary

Everything is best-effort: a missing optional dependency (posting creds,
Telegram) is logged and skipped, never fatal.
"""
from __future__ import annotations
import os, sys, json, time, shutil, subprocess, datetime, glob

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REC_DIR = os.getenv("OBS_REC_DIR", r"C:\obs-recordings")
MATCH_QUERY = os.getenv("MATCH_QUERY", "")
TARGETS = os.getenv("POST_TARGETS", "")            # e.g. "youtube,facebook"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
PUBLIC_RECAP_URL = os.getenv("PUBLIC_RECAP_URL", "")


def log(msg): print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text}, timeout=20)
    except Exception as e:
        log(f"telegram notify failed: {e}")


def newest_recording():
    files = [f for f in glob.glob(os.path.join(REC_DIR, "*"))
             if f.lower().endswith((".mp4", ".mkv", ".flv"))]
    return max(files, key=os.path.getmtime) if files else None


def main():
    import obs_control, match_pipeline, captions as capmod, thumbnail_gen as tg

    match_query = MATCH_QUERY or input("Match (e.g. 'Al-Ittihad vs Al-Nassr'): ").strip()

    # 1. go live
    log("Starting OBS stream + recording…")
    try:
        obs_control.go_live(scene=os.getenv("OBS_SCENE"))
        telegram(f"🔴 LIVE: {match_query} — streaming + recording started")
    except Exception as e:
        log(f"❌ OBS go-live failed: {e}")
        log("Check OBS is open and WebSocket is enabled (run setup_obs.ps1).")
        return

    # 2. wait for full-time
    input("\n▶ Streaming. Press ENTER at FULL-TIME to stop & publish…\n")

    # 3. stop
    log("Stopping OBS…")
    info = obs_control.stop()
    time.sleep(3)
    recording = info.get("recording_path") or newest_recording()
    if not recording or not os.path.exists(recording):
        log(f"❌ Could not find the recording in {REC_DIR}.")
        telegram("⚠️ Match recorded but file not found — check OBS recording path.")
        return
    log(f"Recording saved: {recording}")
    telegram("⏹ Full-time. Building recap…")

    # 4-5. produce + brand
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(os.path.expanduser(r"~\Downloads\matches"), ts)
    os.makedirs(outdir, exist_ok=True)
    os.chdir(outdir)
    log("Detecting goals + building recap…")
    res = match_pipeline.run_pipeline(recording, match_query=match_query or None,
                                      outdir=os.path.join(outdir, "clips"),
                                      recap=os.path.join(outdir, "recap.mp4"))
    meta = res.get("match", {}) or {}
    meta["goals"] = res.get("goals", [])
    ngoals = len([g for g in meta["goals"] if g.get("confidence", 0) >= 0.5])
    log(f"Goals detected: {ngoals}  | signals: {res.get('signals')}")

    # thumbnail from a recap peak frame
    recap = res.get("recap") or os.path.join(outdir, "recap.mp4")
    frame = os.path.join(outdir, "thumb_frame.png")
    FF = match_pipeline.FFMPEG
    subprocess.run([FF, "-y", "-ss", "12", "-i", recap, "-frames:v", "1", frame],
                   capture_output=True)
    thumb = os.path.join(outdir, "thumbnail.png")
    bg = frame if os.path.exists(frame) else os.path.join(HERE, "stadium_bg.jpeg")
    try:
        tg.make_thumbnail(bg, meta, out=thumb)
    except Exception as e:
        log(f"thumbnail failed ({e}); using stadium fallback")
        tg.make_thumbnail(os.path.join(HERE, "stadium_bg.jpeg"), meta, out=thumb)

    caps = capmod.build_captions(meta, recap_url=PUBLIC_RECAP_URL or None)
    json.dump(caps, open(os.path.join(outdir, "captions.json"), "w"),
              ensure_ascii=False, indent=2)

    # 6. publish (optional)
    posted = "skipped (no POST_TARGETS set)"
    if TARGETS:
        try:
            import poster
            r = poster.publish_all(recap, thumb, caps,
                                   public_recap_url=PUBLIC_RECAP_URL or None,
                                   targets=tuple(TARGETS.split(",")))
            posted = json.dumps(r)
        except Exception as e:
            posted = f"FAILED: {e}"
    log(f"Publish: {posted}")

    # 7. backup note (recording + outputs already in outdir)
    log(f"All outputs saved to: {outdir}")

    # 8. notify
    title = caps.get("youtube", {}).get("title", match_query)
    telegram(f"✅ Recap ready: {title}\nGoals: {ngoals}\nOutput: {outdir}\nPublish: {posted}")
    print("\n" + "=" * 60)
    print(f"DONE. Recap: {recap}\nThumbnail: {thumb}\nCaptions: captions.json")
    print("⚠️ This box gets wiped — download/upload these now if not auto-posted.")
    print("=" * 60)


if __name__ == "__main__":
    main()
