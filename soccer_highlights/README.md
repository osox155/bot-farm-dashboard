# ⚽ Soccer Live → Recap → Auto-Post SaaS

Go live on a match, record it, auto-detect the goals, build a recap video with
a branded thumbnail + AI captions, and auto-post to YouTube, Facebook and
Instagram — orchestrated by **n8n** running on your always-on **RDP PC**.

```
                         ┌──────────────── RDP PC (Windows, always-on) ───────────────┐
   you click "go live"   │                                                            │
        │                │   obs_control.py ──► OBS Studio ──► Restream.io ──► YouTube │
        ▼                │        (stream)                              └────► Facebook│
   n8n: go-live  ────────►        (record) ──► match_2026..mp4  (local recording)      │
                         │                                │                            │
   match ends            │   obs_control.py stop          ▼                            │
        │                │                       watcher.py (folder watch)             │
        ▼                │                                │ webhook                     │
   n8n: produce ◄────────┼────────────────────────────────┘                            │
                         └────────────────────────────────────────────────────────────┘
        │
        ├─ match_pipeline.py   recording ─► goals.json + clips/ + recap.mp4
        ├─ thumbnail_gen.py    recap peak frame ─► thumbnail.png
        ├─ captions.py         goals.json ─► per-platform titles/descriptions/tags
        └─ poster.py           recap + thumb + captions ─► YouTube / Facebook / Instagram
```

## Files
| File | Stage | Runs on |
|---|---|---|
| `obs_control.py` | Go live / record / stop (obs-websocket v5) | RDP |
| `watcher.py` | Detect finished recording → fire n8n | RDP |
| `match_pipeline.py` | **Core**: detect goals → clip → stitch recap | RDP / sandbox |
| `goal_detection.py` | 3-signal fusion (official + OCR + audio) | — (imported) |
| `thumbnail_gen.py` | Branded thumbnail | RDP / sandbox |
| `captions.py` | Per-platform copy | RDP / sandbox |
| `poster.py` | Upload to YT / FB / IG (official APIs) | RDP |
| `extract_highlights.py` | Standalone audio-only recap (fallback) | — |

## Goal detection — how the 3 signals fuse
1. **Official data** (Firecrawl search → cross-validated goal JSON from ESPN/UEFA/Wikipedia) = *what* happened + minutes.
2. **Scoreboard OCR** = locks each goal to the exact *video second* (score change).
3. **Audio roar** = refines clip in/out + catches near-misses/red cards.

When OCR misses a goal, official-minute + audio still pin it (confidence ~0.75).
Triple-confirmed goals get confidence 1.0. Near-misses (loud, no score change)
are flagged low-confidence and excluded from the recap by default.

---

## Setup on the RDP

```bash
git clone <your repo>   # or copy this folder
cd soccer_highlights
python -m venv .venv && . .venv/Scripts/activate   # Windows RDP
pip install -r requirements.txt
```

### 1. OBS
- OBS Studio 28+ → **Tools ▸ WebSocket Server Settings** → Enable, set password.
- **Stream** output → Restream.io stream key (Restream fans out to YouTube + Facebook).
- **Recording** output → `.mp4`, pointed at a folder `watcher.py` watches.

### 2. Environment variables
```
# OBS
OBS_HOST=localhost  OBS_PORT=4455  OBS_PASSWORD=...
# Watcher
OBS_REC_DIR=C:\obs-recordings   N8N_WEBHOOK=https://<rdp>/webhook/match
# YouTube (Data API v3 OAuth)
YT_CLIENT_ID=...  YT_CLIENT_SECRET=...  YT_REFRESH_TOKEN=...
# Facebook Page
FB_PAGE_ID=...  FB_PAGE_TOKEN=...
# Instagram (Business acct linked to the FB Page)
IG_USER_ID=...  IG_ACCESS_TOKEN=...
```

### 3. n8n master flow (self-hosted on the RDP, free)
1. **Webhook** node `/webhook/match` (triggered by `watcher.py`).
2. **Execute Command**: `python match_pipeline.py "{{recording_path}}" --match-query "{{match_query}}" --kickoff {{kickoff}}`
3. **Execute Command**: `python thumbnail_gen.py --background recap_frame.png --goals-json goals.json`
4. **Execute Command**: `python captions.py goals.json` → parse JSON.
5. **Execute Command**: `python poster.py --recap recap.mp4 --thumbnail thumbnail.png --captions captions.json --public-recap-url {{hosted_url}}`
6. (Instagram) upload `recap.mp4` to cloud storage first → pass its public URL.

To **go live**, a separate n8n flow (manual trigger / schedule) runs
`python obs_control.py go-live --scene "Match"`; to end, `obs_control.py stop`.

---

## ⚠️ Deployment notes (read these)
- **Instagram Live** via OBS/RTMP is **not officially supported**. Plan: stream
  live to **YouTube + Facebook**, and post the **recap + clips** to Instagram
  afterward (where they perform best anyway).
- **Instagram posting** needs an IG **Business/Creator** account linked to a
  Facebook Page, and the recap hosted at a **public https URL** (Graph API can't
  take local files).
- **Lowest-code posting alternative:** instead of `poster.py` + OAuth, use
  **Blotato** or **Make.com**'s native YouTube/Facebook/Instagram modules — feed
  them the recap + captions and skip all the API/token setup.
- **OCR backend:** `easyocr` (no system binary) or `pytesseract` (needs the
  Tesseract binary). Set the scoreboard region via `ScoreboardOCR(roi=(x,y,w,h))`
  as a fraction of the frame (default top-left).

## Quick local test (no OBS, no posting)
```bash
python match_pipeline.py match.mp4 --match-query "Arsenal vs Chelsea 2026-06-19"
python captions.py goals.json
python thumbnail_gen.py --background stadium_bg.jpeg --goals-json goals.json
```
