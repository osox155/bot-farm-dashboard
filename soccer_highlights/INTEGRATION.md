# Integrating the Soccer SaaS into bot-farm-dashboard (ephemeral RDP)

Your GitHub-Actions RDP wipes every ~5.5h, but it auto-clones this repo and
installs every `requirements.txt` on each boot. So we just live inside the repo
and we're pre-installed on every fresh box. 🎯

## 1. Where to put it
Copy the whole `soccer_highlights/` folder into your **bot-farm-dashboard** repo root:

```
bot-farm-dashboard/
├── start-bots.bat
├── soccer_highlights/        ← drop this folder here
│   ├── run_match.py
│   ├── run_match.bat         ← you double-click this in the RDP
│   ├── setup_obs.ps1
│   ├── match_pipeline.py
│   ├── goal_detection.py
│   ├── thumbnail_gen.py
│   ├── captions.py
│   ├── poster.py
│   ├── obs_control.py
│   ├── stadium_bg.jpeg
│   └── requirements.txt      ← auto-installed by your YAML
└── ...
```
Commit + push. Your auto-pull watcher will sync it to every running box too.

> ⚠️ Do **NOT** add soccer to `start-bots.bat`. A match is an event you start
> manually — your other bots run continuously, soccer runs per match.

## 2. Add GitHub Secrets (repo ▸ Settings ▸ Secrets ▸ Actions)
| Secret | Purpose |
|---|---|
| `OBS_PASSWORD` | OBS WebSocket password |
| `RESTREAM_KEY` | Restream stream key (fans out to YouTube + Facebook) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | (optional) done-notifications — you already use Telegram |
| `YT_CLIENT_ID`,`YT_CLIENT_SECRET`,`YT_REFRESH_TOKEN` | (optional) auto-post to YouTube |
| `FB_PAGE_ID`,`FB_PAGE_TOKEN` | (optional) auto-post to Facebook |
| `IG_USER_ID`,`IG_ACCESS_TOKEN` | (optional) auto-post to Instagram |

## 3. Make the secrets visible to your RDP desktop session
GitHub step-env doesn't reach the interactive RDP. Add this step to `main.yml`
**before** the "Run bore TCP tunnel" step so the values are written as
persistent machine env vars the desktop session can read:

```yaml
      - name: Provision soccer env + OBS
        shell: pwsh
        env:
          OBS_PASSWORD:       ${{ secrets.OBS_PASSWORD }}
          RESTREAM_KEY:       ${{ secrets.RESTREAM_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID:   ${{ secrets.TELEGRAM_CHAT_ID }}
          # add YT_*/FB_*/IG_* here too if you auto-post
        run: |
          # persist for the interactive RDP user
          setx OBS_PASSWORD       "$env:OBS_PASSWORD"       /M | Out-Null
          setx RESTREAM_KEY       "$env:RESTREAM_KEY"       /M | Out-Null
          setx TELEGRAM_BOT_TOKEN "$env:TELEGRAM_BOT_TOKEN" /M | Out-Null
          setx TELEGRAM_CHAT_ID   "$env:TELEGRAM_CHAT_ID"   /M | Out-Null
          setx OBS_REC_DIR        "C:\obs-recordings"       /M | Out-Null
          setx POST_TARGETS       ""                        /M | Out-Null   # e.g. "youtube,facebook"
          # pre-provision OBS so it's match-ready
          $sc = "C:\Users\runneradmin\Downloads\Bot\soccer_highlights\setup_obs.ps1"
          if (Test-Path $sc) { powershell -ExecutionPolicy Bypass -File $sc }
```

(Use real GitHub Secrets here — don't hardcode values in the YAML.)

## 4. Run a match (each session)
1. Wait for the workflow to boot a fresh RDP, connect via your bore tunnel.
2. Open `C:\Users\runneradmin\Downloads\Bot\soccer_highlights\`.
3. Double-click **`run_match.bat`**.
   - It provisions + launches OBS, then prompts for the match name.
   - It starts streaming (YouTube+Facebook) + recording.
4. Watch the match. **Press ENTER at full-time.**
5. It auto-builds the recap + thumbnail + captions, posts (if creds set), saves
   everything to `Downloads\matches\<timestamp>\`, and pings you on Telegram.

## 5. ⚠️ Survive the wipe
The box dies at session end. Make sure the recap is somewhere permanent:
- **Auto-post = best backup** (it lives on YouTube). Set `POST_TARGETS`.
- Or before the session ends, upload `Downloads\matches\<ts>\` to Google Drive,
  or `git add` the small files (goals.json, thumbnail) and let your auto-pull
  watcher push them. (Don't commit the 70 MB raw recording.)

## 6. Timing reality check
Setup ~10 min + match ~2h + produce ~15 min + post ~5 min ≈ **2.5h** — well
within your 5.5h hold. If a match might run past the hold window, bump
`HOLD_MINUTES` when you dispatch the workflow.

## Notes
- **OBS source:** `setup_obs.ps1` defaults to full-screen Display Capture. If you
  play the match in Chrome, that's captured automatically. Adjust the scene in
  OBS if you use a capture card / browser source.
- **OCR:** add `easyocr` to requirements only if you want automatic scoreboard
  reading (it pulls ~2 GB of torch — slower boot). Audio + official data already
  work without it.
- **No-code posting:** leave `POST_TARGETS` empty and instead point Blotato/Make
  at the `Downloads\matches\<ts>\` outputs.
