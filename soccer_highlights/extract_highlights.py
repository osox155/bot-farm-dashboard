#!/usr/bin/env python3
"""
Soccer highlight extractor.

Goals in soccer broadcasts have a distinct audio signature: a sustained
crowd roar + commentary spike that lasts several seconds (not a brief blip).
We detect *sustained* loud windows, cluster them into events, then clip each
moment with a lead-in (build-up) and lead-out (celebration) and stitch a recap.

Usage:
    python extract_highlights.py MATCH.mp4 --out recap.mp4
"""
import argparse, subprocess, os, json, tempfile, sys
import numpy as np

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(cmd[:3])}...\n{p.stderr[-800:]}")
    return p


def duration(path):
    import re
    # `ffmpeg -i` with no output exits nonzero by design; don't use run()
    p = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def load_audio_rms(path, sr=16000, hop_sec=0.5):
    """Extract mono audio, return per-window loudness (dB) and window times."""
    p = subprocess.run([FFMPEG, "-i", path, "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    audio = np.frombuffer(p.stdout, dtype=np.float32)
    hop = int(sr * hop_sec)
    n = len(audio) // hop
    audio = audio[: n * hop].reshape(n, hop)
    rms = np.sqrt((audio ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms + 1e-9)
    times = np.arange(n) * hop_sec
    return times, db, hop_sec


def detect_events(times, db, hop_sec,
                  z_thresh=1.3, min_sustain_sec=2.5, merge_gap_sec=8.0):
    """Find sustained loud windows = goal/big-moment candidates."""
    mu, sigma = db.mean(), db.std() + 1e-9
    z = (db - mu) / sigma
    loud = z > z_thresh
    # require sustained loudness
    min_win = max(1, int(min_sustain_sec / hop_sec))
    events = []
    i = 0
    while i < len(loud):
        if loud[i]:
            j = i
            while j < len(loud) and loud[j]:
                j += 1
            if (j - i) >= min_win:
                events.append([times[i], times[j - 1] + hop_sec,
                               float(z[i:j].max())])
            i = j
        else:
            i += 1
    # merge events that are close together
    merged = []
    for ev in events:
        if merged and ev[0] - merged[-1][1] <= merge_gap_sec:
            merged[-1][1] = ev[1]
            merged[-1][2] = max(merged[-1][2], ev[2])
        else:
            merged.append(ev)
    return merged


def make_clips(path, events, outdir, pre=8.0, post=10.0, total_dur=None):
    os.makedirs(outdir, exist_ok=True)
    clips = []
    for idx, (start, end, score) in enumerate(events):
        cs = max(0, start - pre)
        ce = end + post
        if total_dur:
            ce = min(ce, total_dur)
        clip = os.path.join(outdir, f"clip_{idx:02d}.mp4")
        run([FFMPEG, "-y", "-ss", f"{cs:.2f}", "-to", f"{ce:.2f}",
             "-i", path, "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-avoid_negative_ts", "make_zero", clip])
        clips.append(clip)
    return clips


def concat(clips, out):
    listfile = out + ".txt"
    with open(listfile, "w") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", out])
    os.remove(listfile)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default="recap.mp4")
    ap.add_argument("--pre", type=float, default=8.0, help="seconds before event")
    ap.add_argument("--post", type=float, default=10.0, help="seconds after event")
    ap.add_argument("--z", type=float, default=1.3, help="loudness z-threshold")
    args = ap.parse_args()

    dur = duration(args.video)
    print(f"Match duration: {dur:.1f}s")
    times, db, hop = load_audio_rms(args.video)
    events = detect_events(times, db, hop, z_thresh=args.z)
    print(f"Detected {len(events)} highlight moments:")
    for s, e, sc in events:
        print(f"  {s:6.1f}s - {e:6.1f}s  (intensity z={sc:.2f})")
    if not events:
        print("No highlights found. Try lowering --z.")
        sys.exit(0)
    clips = make_clips(args.video, events, "clips", args.pre, args.post, dur)
    concat(clips, args.out)
    print(f"Recap written: {args.out}")
    # also emit machine-readable timeline
    with open("highlights.json", "w") as f:
        json.dump([{"start": s, "end": e, "intensity": sc}
                   for s, e, sc in events], f, indent=2)


if __name__ == "__main__":
    main()
