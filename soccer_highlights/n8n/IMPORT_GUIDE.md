# n8n import guide

Two importable workflows for the Soccer SaaS.

| File | What it does | Trigger |
|---|---|---|
| `soccer_go_live.json` | Start / stop OBS stream + recording | Manual (2 buttons) |
| `soccer_produce_publish.json` | recording → goals → recap → thumbnail → captions → post | Webhook (fired by `watcher.py`) |

## How to import
1. In n8n: **Workflows ▸ Import from File** → pick each `.json`.
2. Open **Config** (in Produce & Publish) and set `base` to your project path
   on the RDP (default `/opt/soccer_highlights`). Same for the two nodes in the
   Go-Live workflow if your path differs.
3. Make sure these env vars are available to n8n (Settings ▸ Variables or the
   shell n8n runs in): OBS_*, YT_*, FB_*, IG_* (see main README).

## Wire the trigger
- Activate **Produce & Publish** → copy its Webhook **Production URL**.
- Set that URL as `N8N_WEBHOOK` for `watcher.py` on the RDP.
- When OBS finishes a recording, `watcher.py` POSTs
  `{ recording_path, match_query, public_recap_url }` → the flow runs end-to-end.

## Daily use
1. Run **▶ Go Live** before kickoff.
2. Run **⏹ Stop** at full time.
3. `watcher.py` detects the saved file → Produce & Publish runs automatically.

## Notes
- **Instagram** step needs a public recap URL. The "Host recap for Instagram"
  node is a placeholder — wire your S3/GCS/CDN upload there, or drop the
  `instagram` target and post the Reel manually / via Blotato.
- Prefer **no-code posting?** Replace the final "Post to…" node with n8n's
  native YouTube node + HTTP nodes for Meta, or call Blotato/Make.
- Each step is an **Execute Command** node running the Python modules from your
  project dir, so you can test any node in isolation.
