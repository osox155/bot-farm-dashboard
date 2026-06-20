#!/usr/bin/env python3
"""
Per-platform caption generator.

Turns the structured match data (teams, score, goals/scorers) into ready-to-post
titles, descriptions and hashtags tuned for each platform's norms and limits.

Template-based by default (deterministic, free, no API). Optional LLM upgrade:
set OPENAI_API_KEY and pass use_llm=True for varied, punchier copy.
"""
from __future__ import annotations
import json, os, textwrap

PLATFORM_LIMITS = {
    "youtube":   {"title": 100, "desc": 5000, "tags": 15},
    "facebook":  {"title": 0,   "desc": 2200, "tags": 8},
    "instagram": {"title": 0,   "desc": 2200, "tags": 30},
}


def _hashtags(meta, n):
    base = ["football", "soccer", "highlights", "goals", "matchday",
            "footballhighlights", "goal"]
    import re as _re
    for t in (meta.get("home_team"), meta.get("away_team")):
        if t:
            base.insert(0, _re.sub(r"[^A-Za-z0-9]", "", t))
    out, seen = [], set()
    for h in base:
        h = "#" + _re.sub(r"[^a-z0-9]", "", h.lower())
        if h not in seen:
            seen.add(h); out.append(h)
        if len(out) >= n:
            break
    return out


def _goal_lines(goals):
    lines = []
    for g in goals:
        mn = g.get("match_minute")
        sc = g.get("scorer")
        if sc and mn:
            lines.append(f"\u26bd {mn}' {sc}")
        elif mn:
            lines.append(f"\u26bd {mn}'")
    return lines


def build_captions(meta: dict, recap_url: str | None = None,
                   chat_url: str | None = None) -> dict:
    home = meta.get("home_team") or "Home"
    away = meta.get("away_team") or "Away"
    score = meta.get("final_score") or ""
    goals = meta.get("goals", [])
    matchup = f"{home} {score} {away}".strip()
    glines = _goal_lines(goals)
    goalblock = "\n".join(glines) if glines else "All the key moments."

    yt_title = f"{matchup} | Goals & Highlights"[:PLATFORM_LIMITS["youtube"]["title"]]
    yt_desc = (f"{matchup} \u2014 full match highlights and all the goals.\n\n"
               f"\u23f1\ufe0f Goal timeline:\n{goalblock}\n\n"
               "Watch every goal, key chance and the build-up. "
               "Like & subscribe for more match recaps.")
    yt_tags = [h[1:] for h in _hashtags(meta, PLATFORM_LIMITS["youtube"]["tags"])]

    fb_desc = f"\U0001f3df\ufe0f {matchup}\n\n{goalblock}\n\nFull recap below \U0001f447"
    fb_tags = _hashtags(meta, PLATFORM_LIMITS["facebook"]["tags"])

    ig_desc = (f"{matchup} \U0001f525\u26bd\n\n{goalblock}\n\n"
               "Save & share \U0001f4cc  Drop your rating in the comments \U0001f447")
    ig_tags = _hashtags(meta, PLATFORM_LIMITS["instagram"]["tags"])

    def cap(text, link, tags, limit):
        extra = (f"\n\n\u25b6\ufe0f {link}" if link else "") + "\n\n" + " ".join(tags)
        budget = limit - len(extra)
        return (text[:budget].rstrip() + extra).strip()

    return {
        "youtube": {"title": yt_title, "description": cap(yt_desc, recap_url, [], PLATFORM_LIMITS["youtube"]["desc"]).strip(), "tags": yt_tags},
        "facebook": {"description": cap(fb_desc, recap_url, fb_tags, PLATFORM_LIMITS["facebook"]["desc"])},
        "instagram": {"description": cap(ig_desc, None, ig_tags, PLATFORM_LIMITS["instagram"]["desc"])},
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("goals_json")
    ap.add_argument("--recap-url")
    a = ap.parse_args()
    data = json.load(open(a.goals_json))
    meta = data.get("match", {})
    meta["goals"] = data.get("goals", [])
    caps = build_captions(meta, recap_url=a.recap_url)
    print(json.dumps(caps, indent=2, ensure_ascii=False))
