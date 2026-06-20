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
│   ├── run_match.py          ← all-in-one orchestrator
│   ├── run_match.bat         ← you double-click this in the RDP
│   ├── watcher.py            ← watches OBS_REC_DIR, fires n8n webhook
│   ├── setup_obs.ps1
│   ├── match_pipeline.py
│   ├── goal_detection.py
│   ├── thumbnail_gen.py
│   ├── captions.py
│   ├── poster.py
│   ├── obs_control.py
│   ├── extract_highlights.py
│   ├── stadium_bg.jpeg
│   └── requirements.txt      ← auto-installed by your YAML
└── ...
```
Commit + push. Your auto-pull watcher will sync it to every running box too.

> ⚠️ Do **NOT** add soccer to `start-bots.bat`. A match is an event you start
> manually — your other bots run continuously, soccer runs per match.

---

## 2. Add GitHub Secrets (repo ▸ Settings ▸ Secrets ▸ Actions)

### Required
| Secret | Value |
|---|---|
| `GH_PAT` | A GitHub Personal Access Token with `repo` scope (replaces the hardcoded PAT) |
| `OBS_PASSWORD` | OBS WebSocket password (e.g. `osajiox`) |
| `RESTREAM_KEY` | Your Restream stream key (fans out to YouTube + Facebook live) |

### Telegram (done-notifications — you already have a bot)
| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token (e.g. `8679941554:AAFGV_...`) |
| `TELEGRAM_CHAT_ID` | Your personal chat ID (e.g. `1670086536`) |

### Social auto-posting (optional — leave blank to skip; `POST_TARGETS` is set automatically)
| Secret | Value |
|---|---|
| `YT_CLIENT_ID` | YouTube OAuth 2.0 client ID |
| `YT_CLIENT_SECRET` | YouTube OAuth 2.0 client secret |
| `YT_REFRESH_TOKEN` | Long-lived refresh token (see § YouTube OAuth below) |
| `FB_PAGE_ID` | Facebook Page ID |
| `FB_PAGE_TOKEN` | Page access token (never-expiring via Business Suite) |
| `IG_USER_ID` | Instagram Business/Creator account ID |
| `IG_ACCESS_TOKEN` | Long-lived IG access token (linked to the FB Page token) |

`POST_TARGETS` is computed automatically in `main.yaml`: it is set to
`youtube,facebook,instagram` for each platform whose secret is non-empty.
You don't need to set it manually.

---

## 3. How `watcher.py` works

`watcher.py` watches `OBS_REC_DIR` for a new video file. When OBS writes one
and the file size stops growing (recording finished), it POSTs to `N8N_WEBHOOK`
with the recording path and match query so the n8n produce/publish pipeline
starts automatically — no manual step needed.

```
OBS finishes recording  →  watcher.py  →  POST /webhook/match  →  n8n pipeline
```

`run_match.bat` starts `watcher.py` in the background automatically. You can
also run it standalone:

```bat
REM In the RDP, from soccer_highlights\:
set N8N_WEBHOOK=http://localhost:5678/webhook/match
set MATCH_QUERY=Al-Ittihad vs Al-Nassr 2026-06-20
python watcher.py
```

Environment variables:
| Var | Default | Purpose |
|---|---|---|
| `OBS_REC_DIR` | `C:\obs-recordings` | Folder OBS records into |
| `N8N_WEBHOOK` | _(empty)_ | n8n webhook URL — if blank, path is printed but no POST is made |
| `MATCH_QUERY` | _(empty)_ | Forwarded to n8n so `match_pipeline.py` can fetch official goals |

---

## 4. Run a match (each session)
1. Wait for the workflow to boot a fresh RDP, connect via your bore tunnel.
2. Open `C:\Users\runneradmin\Downloads\Bot\soccer_highlights\`.
3. Double-click **`run_match.bat`**.
   - It provisions + launches OBS, then prompts for the match name.
   - It starts streaming (YouTube + Facebook) + recording.
   - `watcher.py` starts in the background waiting for the recording to finish.
4. Watch the match. **Press ENTER at full-time.**
5. It auto-builds the recap + thumbnail + captions, posts (if credentials set),
   saves everything to `Downloads\matches\<timestamp>\`, and pings you on Telegram.

---

## 5. ⚠️ Survive the wipe
The box dies at session end. Make sure the recap is somewhere permanent:
- **Auto-post = best backup** (it lives on YouTube). Set the social posting secrets.
- Or before the session ends, upload `Downloads\matches\<ts>\` to Google Drive,
  or `git add` the small files (goals.json, thumbnail) and let your auto-pull
  watcher push them. (Don't commit the raw recording — it's too large.)

---

## 6. YouTube OAuth — get your refresh token

This is the trickiest part. Do it once on your own machine; the tokens are
long-lived.

### Step 1 — Create OAuth credentials in Google Cloud Console
1. Go to [console.cloud.google.com](https://console.cloud.google.com) →
   **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app** (or Web, but Desktop is easiest).
3. Download the JSON — note `client_id` and `client_secret`.
4. Enable the **YouTube Data API v3** under **APIs & Services → Library**.

### Step 2 — Get a refresh token via OAuth Playground
1. Go to [developers.google.com/oauthplayground](https://developers.google.com/oauthplayground).
2. Click the gear ⚙ (top right) → ✅ "Use your own OAuth credentials" →
   paste your `client_id` and `client_secret`.
3. In the left panel, find **YouTube Data API v3** → select
   `https://www.googleapis.com/auth/youtube.upload` and
   `https://www.googleapis.com/auth/youtube` → **Authorize APIs**.
4. Sign in with the YouTube channel account → allow access.
5. Click **Exchange authorization code for tokens**.
6. Copy the `refresh_token` from the response.

### Step 3 — Add to repo secrets
```
YT_CLIENT_ID     =  <client_id from JSON>
YT_CLIENT_SECRET =  <client_secret from JSON>
YT_REFRESH_TOKEN =  <refresh_token from step 2>
```

### Note on quota
YouTube Data API v3 has a default quota of 10,000 units/day. A video upload
costs 1,600 units. You can post ~6 videos/day for free. If you need more,
request a quota increase in Google Cloud Console.

---

## 7. Facebook & Instagram tokens

### Facebook Page token (long-lived)
1. In [Meta Business Suite](https://business.facebook.com) → **Settings →
   Integrations → Meta Business Suite App** (or create one in
   [developers.facebook.com](https://developers.facebook.com)).
2. **Graph API Explorer** → select your app → generate a User Token with
   `pages_manage_posts`, `pages_read_engagement`, `publish_video` scopes.
3. Exchange for a **long-lived Page token** (60-day, renewable):
   ```
   GET https://graph.facebook.com/v20.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id={app_id}
     &client_secret={app_secret}
     &fb_exchange_token={short_lived_user_token}
   ```
   Then get the Page token:
   ```
   GET https://graph.facebook.com/v20.0/me/accounts?access_token={long_lived_user_token}
   ```
   The `access_token` in that response for your page is effectively **permanent**.

### Instagram (linked to the Page)
Your IG Business/Creator account must be linked to the Facebook Page.

```
GET https://graph.facebook.com/v20.0/{page_id}?fields=instagram_business_account&access_token={page_token}
```
The `instagram_business_account.id` is your `IG_USER_ID`.
Your `IG_ACCESS_TOKEN` is the same long-lived Page token.

---

## 8. Timing reality check
Setup ~10 min + match ~2h + produce ~15 min + post ~5 min ≈ **2.5h** — well
within your 5.5h hold. If a match might run past the hold window, bump
`HOLD_MINUTES` when you dispatch the workflow.

---

## Notes
- **OBS source:** `setup_obs.ps1` defaults to full-screen Display Capture. If you
  play the match in Chrome, that's captured automatically. Adjust the scene in
  OBS if you use a capture card / browser source.
- **OCR:** add `easyocr` to requirements only if you want automatic scoreboard
  reading (it pulls ~2 GB of torch — slower boot). Audio + official data already
  work without it.
- **No-code posting:** leave all social posting secrets empty and instead point
  **Blotato** or **Make.com**'s native YouTube/Facebook/Instagram modules at the
  `Downloads\matches\<ts>\` outputs (recap.mp4, thumbnail.png, captions.json).
