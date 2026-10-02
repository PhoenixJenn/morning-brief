#!/usr/bin/env python3
"""
category_trends.py — Tracks story volume per topic category over time.

For each day's briefing transcript, asks Claude to count how many distinct
stories were covered in each of the 7 topic sections, and appends that to
context/category-trends.json (date -> {category: count}). Powers the
Recurring Themes-adjacent "category volume over time" chart on Dash.

Runs after each brief (called from morning_brief.py), or standalone:
  python category_trends.py                  # today
  python category_trends.py 2026-05-24       # specific date
  python category_trends.py --backfill       # all existing regular briefs
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
OUTPUT_DIR   = PROJECT_DIR / "output"
TRENDS_FILE  = CONTEXT_DIR / "category-trends.json"

CATEGORIES = [
    "General Tech & Industry",
    "AI & Machine Learning",
    "XR, Spatial Computing, Spatial Internet & World Models",
    "3D Capture & Create",
    "Autonomous Vehicles, Robotics & Humanoid Robots",
    "IoT & Connected Devices",
    "Media & Entertainment",
]

client = anthropic.Anthropic()


def load_trends() -> dict:
    if not TRENDS_FILE.exists():
        return {}
    try:
        return json.loads(TRENDS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_trends(trends: dict):
    TRENDS_FILE.write_text(json.dumps(trends, indent=2, ensure_ascii=False, sort_keys=True))


def count_categories(transcript: str) -> dict:
    """Ask Claude how many distinct stories appear under each topic section."""
    categories_list = "\n".join(f"- {c}" for c in CATEGORIES)
    prompt = f"""This is a transcript of a daily spoken news briefing. It covers these topic \
sections, introduced with verbal transitions rather than headers (e.g. "Moving to AI...", \
"On the spatial computing front..."), in this order:

{categories_list}

Count how many distinct stories/topics were covered under each section. If a section says \
"No news for [category]" or equivalent, its count is 0. Do not count the same story twice \
even if it's referenced again later for a different angle.

Return ONLY a JSON object with this exact structure, no markdown fences or extra text:
{{{", ".join(f'"{c}": <integer>' for c in CATEGORIES)}}}

TRANSCRIPT:
{transcript}"""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = re.sub(r"```(?:json)?\n?", "", resp.content[0].text.strip()).strip("`")
    counts = json.loads(raw)
    return {c: int(counts.get(c, 0)) for c in CATEGORIES}


def run(date_str: str = None, verbose: bool = True) -> bool:
    """Process one brief date. Returns True if recorded."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    txt_path = OUTPUT_DIR / f"brief-{date_str}.txt"
    if not txt_path.exists():
        if verbose:
            print(f"  [trends] No transcript for {date_str} — skipping")
        return False

    transcript = txt_path.read_text()
    try:
        counts = count_categories(transcript)
    except Exception as exc:
        if verbose:
            print(f"  [trends] Extraction failed for {date_str}: {exc}")
        return False

    trends = load_trends()
    trends[date_str] = counts
    save_trends(trends)

    if verbose:
        total = sum(counts.values())
        print(f"  [trends] {date_str}: {total} stories → {TRENDS_FILE.name}")
    return True


def backfill():
    """Process all existing brief-YYYY-MM-DD.txt files (regular briefs only) in date order."""
    existing = load_trends()
    files = sorted(OUTPUT_DIR.glob("brief-????-??-??.txt"))
    todo = [f for f in files if re.match(r"brief-(\d{4}-\d{2}-\d{2})\.txt", f.name).group(1) not in existing]
    print(f"Backfilling {len(todo)} of {len(files)} brief(s) (skipping already-recorded dates)...\n")
    for i, path in enumerate(todo):
        m = re.match(r"brief-(\d{4}-\d{2}-\d{2})\.txt", path.name)
        date_str = m.group(1)
        print(f"[{i+1}/{len(todo)}] {date_str}...", end=" ")
        run(date_str, verbose=False)
        counts = load_trends().get(date_str, {})
        print(f"{sum(counts.values())} stories")
    print("\n✓ Backfill complete")


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        backfill()
    else:
        date_arg = next(
            (a for a in sys.argv[1:] if re.match(r"\d{4}-\d{2}-\d{2}", a)), None
        )
        run(date_arg)
