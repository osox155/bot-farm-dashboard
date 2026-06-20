#!/usr/bin/env python3
"""
Soccer goal-detection FUSION engine.

Combines three independent signals into one accurate goal timeline:

  1. OFFICIAL events  (goal minutes + scorers)  -> ground truth of WHAT happened
  2. SCOREBOARD OCR   (score-change timestamps)  -> locks the exact VIDEO SECOND
  3. AUDIO energy      (sustained crowd roar)     -> refines clip in/out, catches
                                                     near-misses / red cards

Output: a clean list of goal events, each with an exact video timestamp and a
confidence score, ready for the ffmpeg clipper.

This module is import-safe: OCR/audio backends are optional and only loaded
when their detector is actually used, so n8n can call just the fusion step.
"""
from __future__ import annotations
import subprocess, re, json, os
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import numpy as np
except Exception:
    np = None

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


# ───────────────────────── data models ─────────────────────────
@dataclass
class GoalEvent:
    video_sec: float                 # exact second in the recording
    match_minute: Optional[int] = None
    scorer: Optional[str] = None
    team: Optional[str] = None
    confidence: float = 0.0          # 0..1
    sources: list = field(default_factory=list)  # ['official','ocr','audio']

    def clip_window(self, pre=8.0, post=12.0):
        return (max(0.0, self.video_sec - pre), self.video_sec + post)


# ───────────────────── 1. official match data ──────────────────
def fetch_official_events_google(query: str) -> list[dict]:
    """
    Returns [{'minute': int, 'scorer': str, 'team': str}, ...].

    Production: the n8n flow calls Firecrawl to scrape the Google match
    summary for `query` (e.g. 'Arsenal vs Chelsea 2026-06-19'), then this
    parser extracts the goal timeline. Kept pluggable on purpose.
    """
    # Placeholder parser contract — real scrape wired in n8n / firecrawl tool.
    # Shape documented so the fusion step is testable without a live call.
    return []


def parse_goal_lines(text: str) -> list[dict]:
    """Parse goal lines like \"23'  H. Kane\" or \"45+2'  Saka\" from scraped text.
    Splits on minute markers so a scorer name can't swallow the next minute."""
    # locate every minute marker (e.g. 23'  or 45+2')
    markers = list(re.finditer(r"(\d{1,3})(?:\+(\d{1,2}))?'", text))
    out = []
    for idx, m in enumerate(markers):
        base, extra = m.group(1), m.group(2)
        minute = int(base) + (int(extra) if extra else 0)
        if not (1 <= minute <= 130):
            continue
        # name = text between this marker and the next marker (or end)
        seg_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        seg = text[m.end():seg_end]
        nm = re.search(r"[A-Z][A-Za-z.\-']*(?:\s+[A-Z][A-Za-z.\-']*)*", seg)
        name = nm.group(0).strip() if nm else None
        if name and len(name) > 1:
            out.append({"minute": minute, "scorer": name, "team": None})
    return out


# ───────────────────── 2. scoreboard OCR ───────────────────────
class ScoreboardOCR:
    """
    Samples frames, OCR-reads the scoreboard region, flags the second the
    score string changes. Backend: easyocr -> pytesseract -> disabled.
    """
    def __init__(self, roi=None, sample_every=2.0):
        # roi = (x, y, w, h) fraction of frame (scoreboard usually top-left)
        self.roi = roi or (0.02, 0.04, 0.28, 0.10)
        self.sample_every = sample_every
        self._reader = None
        self._mode = None
        try:
            import easyocr  # noqa
            self._reader = easyocr.Reader(['en'], gpu=False)
            self._mode = "easyocr"
        except Exception:
            try:
                import pytesseract  # noqa
                self._mode = "pytesseract"
            except Exception:
                self._mode = None

    def available(self):
        return self._mode is not None

    def _read_text(self, img):
        if self._mode == "easyocr":
            return " ".join(self._reader.readtext(img, detail=0))
        if self._mode == "pytesseract":
            import pytesseract
            return pytesseract.image_to_string(img, config="--psm 7 -c tessedit_char_whitelist=0123456789-:")
        return ""

    def detect_score_changes(self, video_path) -> list[float]:
        if not self.available():
            return []
        import cv2
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        step = int(fps * self.sample_every)
        fx, fy, fw, fh = self.roi
        last, changes, i = None, [], 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if i % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    h, w = frame.shape[:2]
                    crop = frame[int(fy*h):int((fy+fh)*h), int(fx*w):int((fx+fw)*w)]
                    digits = re.sub(r"\D", "", self._read_text(crop))
                    if digits and last is not None and digits != last:
                        changes.append(i / fps)
                    if digits:
                        last = digits
            i += 1
        cap.release()
        return changes


# ───────────────────── 3. audio energy ─────────────────────────
def detect_audio_events(video_path, sr=16000, hop=0.5, z_thresh=1.3,
                        min_sustain=2.5, merge_gap=8.0) -> list[float]:
    if np is None:
        return []
    p = subprocess.run([FFMPEG, "-i", video_path, "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    a = np.frombuffer(p.stdout, dtype=np.float32)
    hw = int(sr*hop); n = len(a)//hw
    if n == 0:
        return []
    a = a[:n*hw].reshape(n, hw)
    db = 20*np.log10(np.sqrt((a**2).mean(1)+1e-9)+1e-9)
    z = (db-db.mean())/(db.std()+1e-9)
    loud = z > z_thresh
    minw = max(1, int(min_sustain/hop))
    peaks, i = [], 0
    while i < len(loud):
        if loud[i]:
            j = i
            while j < len(loud) and loud[j]:
                j += 1
            if j-i >= minw:
                peaks.append((i*hop + (j-i)*hop/2))  # center of roar
            i = j
        else:
            i += 1
    # merge close peaks
    merged = []
    for t in peaks:
        if merged and t-merged[-1] <= merge_gap:
            continue
        merged.append(t)
    return merged


# ───────────────────────── FUSION ──────────────────────────────
def fuse(official: list[dict],
         ocr_changes: list[float],
         audio_peaks: list[float],
         kickoff_sec: float = 0.0,
         tol: float = 25.0) -> list[GoalEvent]:
    """
    kickoff_sec: video second when the 1st-half kickoff happens (so match
                 minute M  ~  kickoff_sec + M*60). Set from OCR clock if known.
    tol:         seconds window to associate signals with each other.
    """
    events: list[GoalEvent] = []

    def nearest(t, pool):
        if not pool:
            return None
        c = min(pool, key=lambda x: abs(x-t))
        return c if abs(c-t) <= tol else None

    if official:
        # Anchor every official goal to a real video second via OCR, then audio.
        for g in official:
            approx = kickoff_sec + g["minute"]*60.0
            sec = nearest(approx, ocr_changes)
            srcs = ["official"]
            conf = 0.6
            if sec is not None:
                srcs.append("ocr"); conf = 0.9
            else:
                sec = nearest(approx, audio_peaks)
                if sec is not None:
                    srcs.append("audio"); conf = 0.75
                else:
                    sec = approx  # fall back to estimated second
            a = nearest(sec, audio_peaks)
            if a is not None and "audio" not in srcs:
                srcs.append("audio"); conf = min(1.0, conf+0.1)
            events.append(GoalEvent(video_sec=round(sec, 1),
                                    match_minute=g["minute"],
                                    scorer=g.get("scorer"), team=g.get("team"),
                                    confidence=round(conf, 2), sources=srcs))
    else:
        # No official data: OCR score-change is the goal; audio confirms.
        used_audio = set()
        for c in ocr_changes:
            a = nearest(c, audio_peaks)
            srcs = ["ocr"]; conf = 0.7
            if a is not None:
                srcs.append("audio"); conf = 0.85; used_audio.add(a)
            events.append(GoalEvent(video_sec=round(c, 1),
                                    confidence=round(conf, 2), sources=srcs))
        # Loud roars with no score change = near-miss / card -> low-confidence
        if not ocr_changes:
            for a in audio_peaks:
                events.append(GoalEvent(video_sec=round(a, 1),
                                        confidence=0.4, sources=["audio"]))
    events.sort(key=lambda e: e.video_sec)
    return events


def detect_all(video_path, match_query=None, kickoff_sec=0.0,
               use_ocr=True, use_audio=True):
    official = fetch_official_events_google(match_query) if match_query else []
    ocr = ScoreboardOCR().detect_score_changes(video_path) if use_ocr else []
    audio = detect_audio_events(video_path) if use_audio else []
    goals = fuse(official, ocr, audio, kickoff_sec=kickoff_sec)
    return {"official": official, "ocr_changes": ocr, "audio_peaks": audio,
            "goals": [asdict(g) for g in goals]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--match-query", default=None)
    ap.add_argument("--kickoff", type=float, default=0.0)
    ap.add_argument("--out", default="goals.json")
    a = ap.parse_args()
    res = detect_all(a.video, a.match_query, a.kickoff)
    json.dump(res, open(a.out, "w"), indent=2)
    print(json.dumps(res["goals"], indent=2))
    print(f"\nWrote {a.out}")
