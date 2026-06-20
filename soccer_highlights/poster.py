#!/usr/bin/env python3
"""
Multi-platform POSTER  (runs on the RDP, called by n8n).

⚠️ Posting requires each platform's official API + OAuth credentials configured
   on the RDP as environment variables. This module is the production poster the
   n8n "publish" step executes; it is NOT runnable from the Gumloop sandbox
   because the connected YouTube/Instagram integrations there are read-only.

Platforms
  • YouTube   — Data API v3 resumable upload + set custom thumbnail
  • Facebook  — Graph API Page video upload (resumable)
  • Instagram — Graph API: create media container from a PUBLIC video URL,
                then publish (Reels). Requires an IG Business/Creator account
                linked to a Facebook Page, and the recap hosted at a public URL.

Required env vars
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
  FB_PAGE_ID, FB_PAGE_TOKEN
  IG_USER_ID, IG_ACCESS_TOKEN
"""
from __future__ import annotations
import os, time, json, mimetypes
import requests


# ─────────────────────────── YouTube ───────────────────────────
def _yt_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def post_youtube(video_path, title, description, tags=None,
                 thumbnail_path=None, privacy="public",
                 category_id="17"):  # 17 = Sports
    token = _yt_access_token()
    meta = {"snippet": {"title": title[:100], "description": description,
                        "tags": tags or [], "categoryId": category_id},
            "status": {"privacyStatus": privacy,
                       "selfDeclaredMadeForKids": False}}
    fsize = os.path.getsize(video_path)
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {token}",
                 "X-Upload-Content-Type": "video/*",
                 "X-Upload-Content-Length": str(fsize),
                 "Content-Type": "application/json; charset=UTF-8"},
        data=json.dumps(meta), timeout=60)
    init.raise_for_status()
    upload_url = init.headers["Location"]
    with open(video_path, "rb") as f:
        up = requests.put(upload_url, headers={"Content-Type": "video/*",
                          "Content-Length": str(fsize)}, data=f, timeout=None)
    up.raise_for_status()
    vid = up.json()["id"]
    # custom thumbnail
    if thumbnail_path and os.path.exists(thumbnail_path):
        with open(thumbnail_path, "rb") as t:
            requests.post(
                f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                params={"videoId": vid},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "image/png"},
                data=t, timeout=60)
    return {"platform": "youtube", "id": vid,
            "url": f"https://youtu.be/{vid}"}


# ─────────────────────────── Facebook ──────────────────────────
def post_facebook(video_path, description):
    page = os.environ["FB_PAGE_ID"]; token = os.environ["FB_PAGE_TOKEN"]
    fsize = os.path.getsize(video_path)
    base = f"https://graph-video.facebook.com/v20.0/{page}/videos"
    # start
    start = requests.post(base, data={"access_token": token,
                          "upload_phase": "start", "file_size": fsize}).json()
    sid = start["upload_session_id"]; so = int(start["start_offset"])
    eo = int(start["end_offset"])
    with open(video_path, "rb") as f:
        while so < eo:
            f.seek(so)
            chunk = f.read(eo - so)
            tr = requests.post(base, data={"access_token": token,
                    "upload_phase": "transfer", "upload_session_id": sid,
                    "start_offset": so}, files={"video_file_chunk": chunk}).json()
            so = int(tr["start_offset"]); eo = int(tr["end_offset"])
    fin = requests.post(base, data={"access_token": token,
            "upload_phase": "finish", "upload_session_id": sid,
            "description": description}).json()
    vid = start.get("video_id")
    return {"platform": "facebook", "id": vid, "ok": fin.get("success")}


# ─────────────────────────── Instagram ─────────────────────────
def post_instagram(public_video_url, caption, cover_url=None, share_to_feed=True):
    """Requires the recap hosted at a PUBLIC https URL (e.g. the recap uploaded
    to cloud storage / your CDN). IG cannot ingest local files."""
    ig = os.environ["IG_USER_ID"]; token = os.environ["IG_ACCESS_TOKEN"]
    create = requests.post(
        f"https://graph.facebook.com/v20.0/{ig}/media",
        data={"media_type": "REELS", "video_url": public_video_url,
              "caption": caption, "share_to_feed": str(share_to_feed).lower(),
              **({"cover_url": cover_url} if cover_url else {}),
              "access_token": token}, timeout=60).json()
    cid = create["id"]
    # poll until the container finishes processing
    for _ in range(30):
        st = requests.get(f"https://graph.facebook.com/v20.0/{cid}",
                          params={"fields": "status_code",
                                  "access_token": token}).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"IG container error: {st}")
        time.sleep(5)
    pub = requests.post(f"https://graph.facebook.com/v20.0/{ig}/media_publish",
            data={"creation_id": cid, "access_token": token}, timeout=60).json()
    return {"platform": "instagram", "id": pub.get("id")}


# ─────────────────────────── orchestration ─────────────────────
def publish_all(recap_path, thumbnail_path, captions, public_recap_url=None,
                public_thumb_url=None, targets=("youtube", "facebook", "instagram")):
    results = []
    if "youtube" in targets:
        c = captions["youtube"]
        results.append(post_youtube(recap_path, c["title"], c["description"],
                                    c.get("tags"), thumbnail_path))
    if "facebook" in targets:
        results.append(post_facebook(recap_path,
                                     captions["facebook"]["description"]))
    if "instagram" in targets:
        if not public_recap_url:
            results.append({"platform": "instagram", "skipped":
                            "needs public_recap_url (host the recap first)"})
        else:
            results.append(post_instagram(public_recap_url,
                           captions["instagram"]["description"], public_thumb_url))
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--recap", required=True)
    ap.add_argument("--thumbnail", required=True)
    ap.add_argument("--captions", required=True, help="captions JSON file")
    ap.add_argument("--public-recap-url")
    ap.add_argument("--public-thumb-url")
    ap.add_argument("--targets", default="youtube,facebook,instagram")
    a = ap.parse_args()
    caps = json.load(open(a.captions))
    out = publish_all(a.recap, a.thumbnail, caps, a.public_recap_url,
                      a.public_thumb_url, tuple(a.targets.split(",")))
    print(json.dumps(out, indent=2))
