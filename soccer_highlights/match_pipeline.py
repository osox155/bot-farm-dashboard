#!/usr/bin/env python3
"""
MATCH PIPELINE  —  one callable service for the SaaS backend.

    recording.mp4  (+ optional match query)
          │
          ├─ 1. fetch official goal events   (Firecrawl search -> JSON)
          ├─ 2. scoreboard OCR  +  audio roar detection
          ├─ 3. fuse all signals -> exact goal seconds + confidence
          ├─ 4. ffmpeg: clip each goal (build-up + celebration)
          └─ 5. stitch recap.mp4
          ↓
    { recap.mp4, clips/clip_NN.mp4, goals.json }

Designed to be invoked by n8n as a single step (webhook/exec node).
Official-data fetch is pluggable: pass --events-json (n8n already scraped) OR
let the script call Firecrawl itself via the Gumloop SDK.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import goal_detection as gd

FFMPEG = gd.FFMPEG


# ───────────── 1. official events via Firecrawl (Gumloop SDK) ─────────────
GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "home_team": {"type": "string"},
        "away_team": {"type": "string"},
        "final_score": {"type": "string"},
        "goals": {"type": "array", "items": {"type": "object", "properties": {
            "minute": {"type": "integer"},
            "scorer": {"type": "string"},
            "team": {"type": "string"}}}},
    },
}


def fetch_events_firecrawl(match_query: str) -> dict:
    """Search authoritative sources and extract a cross-validated goal list.
    Returns {'home_team','away_team','final_score','goals':[...]}.
    Picks the result with the most goals (most complete) and de-dupes."""
    from gumloop import Gumloop
    client = Gumloop()
    args = {
        "query": f"{match_query} goals minute scorer full time",
        "limit": 3,
        "scrape_options": {"onlyMainContent": True, "formats": [{
            "type": "json",
            "prompt": ("Extract the football match goal events. Return final "
                       "score and a list of goals, each with minute (integer), "
                       "scorer name, and which team scored."),
            "schema": GOAL_SCHEMA}]},
    }
    res = client.mcp.execute("firecrawl", "search", args).results[0]
    if res.status != "success":
        raise RuntimeError(res.error)
    data = res.decoded_content
    candidates = [r.get("json") for r in data.get("web", []) if r.get("json")]
    if not candidates:
        return {"goals": []}
    best = max(candidates, key=lambda c: len(c.get("goals") or []))
    # de-dupe goals by (minute, scorer)
    seen, goals = set(), []
    for g in best.get("goals") or []:
        key = (g.get("minute"), (g.get("scorer") or "").lower())
        if key not in seen and g.get("minute") is not None:
            seen.add(key)
            goals.append(g)
    best["goals"] = sorted(goals, key=lambda g: g["minute"])
    return best


# ───────────── 4-5. clip + stitch ─────────────
def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...\n{p.stderr[-600:]}")
    return p


def clip_and_stitch(video, goals, outdir, recap, pre=8.0, post=12.0,
                    min_conf=0.5):
    os.makedirs(outdir, exist_ok=True)
    dur = gd_duration(video)
    clips = []
    kept = [g for g in goals if g["confidence"] >= min_conf]
    for i, g in enumerate(kept):
        cs = max(0.0, g["video_sec"] - pre)
        ce = min(dur, g["video_sec"] + post)
        path = os.path.join(outdir, f"clip_{i:02d}.mp4")
        _run([FFMPEG, "-y", "-ss", f"{cs:.2f}", "-to", f"{ce:.2f}", "-i", video,
              "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
              "-avoid_negative_ts", "make_zero", path])
        clips.append(path)
    if not clips:
        return [], None
    lst = recap + ".txt"
    with open(lst, "w") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", recap])
    os.remove(lst)
    return clips, recap


def gd_duration(path):
    import re
    p = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


# ───────────── orchestration ─────────────
def run_pipeline(video, match_query=None, events_json=None, kickoff_sec=0.0,
                 use_ocr=True, use_audio=True, outdir="clips",
                 recap="recap.mp4", min_conf=0.5):
    # 1. official events
    meta = {}
    if events_json:
        meta = json.loads(events_json) if isinstance(events_json, str) else events_json
    elif match_query:
        meta = fetch_events_firecrawl(match_query)
    official = meta.get("goals", []) if meta else []

    # 2. signals
    ocr = gd.ScoreboardOCR().detect_score_changes(video) if use_ocr else []
    audio = gd.detect_audio_events(video) if use_audio else []

    # 3. fuse
    goals = [gd.asdict(g) for g in
             gd.fuse(official, ocr, audio, kickoff_sec=kickoff_sec)]

    # 4-5. clip + stitch
    clips, recap_path = clip_and_stitch(video, goals, outdir, recap,
                                        min_conf=min_conf)

    out = {"match": {k: meta.get(k) for k in
                     ("home_team", "away_team", "final_score")},
           "goals": goals, "clips": clips, "recap": recap_path,
           "signals": {"official": len(official), "ocr": len(ocr),
                       "audio": len(audio)}}
    json.dump(out, open("goals.json", "w"), indent=2)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--match-query")
    ap.add_argument("--events-json", help="pre-fetched events JSON (from n8n)")
    ap.add_argument("--kickoff", type=float, default=0.0)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--min-conf", type=float, default=0.5)
    a = ap.parse_args()
    res = run_pipeline(a.video, a.match_query, a.events_json, a.kickoff,
                       use_ocr=not a.no_ocr, use_audio=not a.no_audio,
                       min_conf=a.min_conf)
    print(json.dumps({k: v for k, v in res.items() if k != "clips"}, indent=2))
    print(f"\nClips: {len(res['clips'])}  ->  Recap: {res['recap']}")
