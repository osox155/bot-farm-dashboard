#!/usr/bin/env python3
"""
Thumbnail generator.

Builds a YouTube/social thumbnail (1280x720) from:
  - a background (a peak frame grabbed from the recap, or any image)
  - the match data (teams, score, top scorer)

Overlays a darkening gradient + bold matchup text + score + "GOALS" badge so
the thumbnail reads clearly at small sizes.
"""
from __future__ import annotations
import argparse, os, subprocess
from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

W, H = 1280, 720


def grab_frame(video, sec, out="frame.png"):
    subprocess.run([FFMPEG, "-y", "-ss", f"{sec:.2f}", "-i", video,
                    "-frames:v", "1", "-q:v", "2", out], capture_output=True)
    return out


def _font(size, bold=True):
    # Search order: Windows system fonts first (RDP), then Linux, then macOS.
    win = os.environ.get("WINDIR", r"C:\Windows")
    font_dir = os.path.join(win, "Fonts")
    paths = [
        # Windows — prefer Impact (punchy) then Arial Bold then fallbacks
        os.path.join(font_dir, "impact.ttf"),
        os.path.join(font_dir, "arialbd.ttf"),
        os.path.join(font_dir, "arial.ttf"),
        os.path.join(font_dir, "verdanab.ttf"),
        os.path.join(font_dir, "verdana.ttf"),
        os.path.join(font_dir, "calibrib.ttf"),
        os.path.join(font_dir, "calibri.ttf"),
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    # Last-resort: Pillow's built-in bitmap font (tiny but always works)
    return ImageFont.load_default()


def _fit(img):
    img = img.convert("RGB")
    r = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * r), int(img.height * r)))
    x = (img.width - W) // 2
    y = (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def make_thumbnail(background, meta, out="thumbnail.png",
                   accent=(255, 215, 0)):
    bg = _fit(Image.open(background))
    # darken bottom for text legibility
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int(255 * min(1, (y / H) ** 1.6)))
    grad = grad.resize((W, H))
    black = Image.new("RGB", (W, H), (0, 0, 0))
    bg = Image.composite(black, bg, grad.point(lambda v: int(v * 0.8)))
    d = ImageDraw.Draw(bg)

    home = meta.get("home_team") or "HOME"
    away = meta.get("away_team") or "AWAY"
    score = meta.get("final_score") or ""
    goals = meta.get("goals", [])
    top = goals[0].get("scorer") if goals else None

    # "GOALS" badge top-left
    bf = _font(54)
    d.rectangle([40, 40, 250, 110], fill=accent)
    d.text((60, 50), "GOALS", font=bf, fill=(10, 10, 10))

    # matchup (big, bottom)
    mf = _font(72)
    matchup = f"{home}  vs  {away}".upper()
    # auto-shrink to fit width
    while d.textlength(matchup, font=mf) > W - 100 and mf.size > 30:
        mf = _font(mf.size - 4)
    d.text((60, H - 220), matchup, font=mf, fill=(255, 255, 255),
           stroke_width=3, stroke_fill=(0, 0, 0))

    # score (huge, accent)
    if score:
        sf = _font(120)
        d.text((60, H - 150), score, font=sf, fill=accent,
               stroke_width=4, stroke_fill=(0, 0, 0))

    # top scorer chip bottom-right
    if top:
        cf = _font(40)
        lf = _font(24)
        txt = top.upper()
        tw = d.textlength(txt, font=cf)
        lw = d.textlength("SCORER", font=lf)
        chip_w = max(tw, lw) + 50
        d.rounded_rectangle([W - chip_w - 40, H - 130, W - 40, H - 45],
                            radius=18, fill=(0, 0, 0))
        d.text((W - chip_w - 15, H - 124), "SCORER", font=lf, fill=(180, 180, 180))
        d.text((W - chip_w - 15, H - 98), txt, font=cf, fill=accent)

    bg.save(out, quality=92)
    return out


if __name__ == "__main__":
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--background", required=True)
    ap.add_argument("--goals-json")
    ap.add_argument("--home"); ap.add_argument("--away")
    ap.add_argument("--score"); ap.add_argument("--scorer")
    ap.add_argument("--out", default="thumbnail.png")
    a = ap.parse_args()
    if a.goals_json:
        data = json.load(open(a.goals_json))
        meta = data.get("match", {}); meta["goals"] = data.get("goals", [])
    else:
        meta = {"home_team": a.home, "away_team": a.away,
                "final_score": a.score,
                "goals": [{"scorer": a.scorer}] if a.scorer else []}
    print(make_thumbnail(a.background, meta, a.out))
