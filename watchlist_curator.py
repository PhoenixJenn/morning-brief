#!/usr/bin/env python3
"""
Watchlist Curator — tracks entity mention frequency across Morning Briefs
and auto-promotes to watchlist.md when signal crosses the threshold.

Runs after each brief (called from morning_brief.py), or standalone:
  python watchlist_curator.py                  # today
  python watchlist_curator.py 2026-05-24       # specific date
  python watchlist_curator.py --backfill       # all existing briefs
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
TRACKER_FILE = CONTEXT_DIR / "entity_tracker.json"
WATCHLIST    = CONTEXT_DIR / "watchlist.md"

PROMOTE_THRESHOLD = 3  # briefs mentioning an entity before auto-watchlist

client = anthropic.Anthropic()


# ─── Persistence ──────────────────────────────────────────────────────────────

def load_tracker() -> dict:
    if not TRACKER_FILE.exists():
        return {"entities": {}}
    try:
        return json.loads(TRACKER_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"entities": {}}


def save_tracker(tracker: dict):
    TRACKER_FILE.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))


def load_watchlist_names() -> set:
    """Names already in the Companies & People table in watchlist.md."""
    if not WATCHLIST.exists():
        return set()
    text = WATCHLIST.read_text()
    section = re.search(r'## Companies & People\n(.+?)(?=\n---\n)', text, re.DOTALL)
    if not section:
        return set()
    names = set()
    for line in section.group(1).split('\n'):
        m = re.match(r'\|\s*(.+?)\s*\|', line)
        if m:
            name = m.group(1).strip()
            if name and name != 'Name' and not re.match(r'^[-\s]+$', name):
                names.add(name)
    return names


# ─── Extraction ───────────────────────────────────────────────────────────────

def parse_entities_from_actions(actions_path: Path) -> list[dict]:
    """Pull raw entity names + context from the People & Companies section."""
    if not actions_path.exists():
        return []
    text = actions_path.read_text()
    section = re.search(
        r'### People & Companies to Watch\n(.+?)(?=\n###|\Z)', text, re.DOTALL
    )
    if not section:
        return []

    entities = []
    for line in section.group(1).strip().split('\n'):
        item = re.match(r'^- (.+)', line)
        if not item:
            continue
        raw = item.group(1).strip()
        if raw.lower().startswith('none'):
            continue
        # Split on first ( or — to get clean name regardless of truncation
        name    = re.split(r'[\(—]', raw)[0].strip().rstrip(',').rstrip('/')
        context = re.sub(r'^[^(—]+[\(—]?\s*', '', raw).strip().strip(')')
        if not name:
            name = raw[:40].strip()
        entities.append({"name": name, "raw_context": context or raw})

    return entities


def enrich_entities(entities: list[dict], date_str: str) -> list[dict]:
    """Single Haiku call: assign category + signal strength to each entity."""
    if not entities:
        return []

    lines = "\n".join(f"- {e['name']}: {e['raw_context']}" for e in entities)
    prompt = f"""Categorize and assess these entities flagged in today's ({date_str}) tech news brief.

{lines}

Valid categories — use ONLY these exact strings:
AI | XR & Spatial | Robotics & AV | IoT & Connected Devices | 3D Capture & Create | Media & Entertainment | Space & Finance

For each entity return a JSON array element:
{{
  "name": "<exact name as given>",
  "category": "<valid category from the list above>",
  "signal": "<high | medium | low>",
  "why": "<one sentence — why this matters strategically; omit if signal=low>"
}}

Return only the JSON array, no markdown or extra text."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        # Strip markdown code fences if present
        raw = re.sub(r'```(?:json)?\n?', '', raw).strip().strip('`')
        enriched = json.loads(raw)
        by_name = {e["name"]: e for e in enriched if isinstance(e, dict)}
    except Exception:
        by_name = {}

    result = []
    for entity in entities:
        e = by_name.get(entity["name"], {})
        result.append({
            "name":        entity["name"],
            "raw_context": entity["raw_context"],
            "category":    e.get("category", "AI"),
            "signal":      e.get("signal", "medium"),
            "why":         e.get("why") or entity["raw_context"],
        })
    return result


# ─── Tracker updates ──────────────────────────────────────────────────────────

def update_tracker(tracker: dict, entities: list[dict], date_str: str) -> dict:
    all_ents = tracker.setdefault("entities", {})

    for entity in entities:
        name = entity["name"]
        if name not in all_ents:
            all_ents[name] = {
                "name":         name,
                "category":     entity.get("category", ""),
                "first_seen":   date_str,
                "last_seen":    date_str,
                "count":        0,
                "on_watchlist": False,
                "mentions":     [],
            }

        e = all_ents[name]
        if e["last_seen"] == date_str and e["count"] > 0:
            continue  # already processed for this brief

        e["last_seen"] = date_str
        e["count"]    += 1
        e["category"]  = entity.get("category") or e["category"]
        e["mentions"].append({
            "date":    date_str,
            "context": entity.get("why") or entity.get("raw_context", ""),
            "signal":  entity.get("signal", "medium"),
        })
        e["mentions"] = e["mentions"][-60:]  # rolling 2-month window

    tracker["last_updated"] = date_str
    return tracker


def update_existing_rows(tracker: dict, watchlist_names: set, date_str: str):
    """Update Last Seen and Count in-place for entities already on the watchlist."""
    if not WATCHLIST.exists():
        return

    text  = WATCHLIST.read_text()
    lines = text.split('\n')
    changed = False

    for i, line in enumerate(lines):
        m = re.match(r'\|\s*(.+?)\s*\|', line)
        if not m:
            continue
        name = m.group(1).strip()
        if name not in watchlist_names:
            continue
        entity = tracker.get("entities", {}).get(name)
        if not entity or entity.get("last_seen") != date_str:
            continue

        cols = line.split('|')
        if len(cols) >= 7:
            cols[4] = f" {date_str} "
            cols[5] = f" {entity['count']} "
            lines[i] = '|'.join(cols)
            changed = True

    if changed:
        WATCHLIST.write_text('\n'.join(lines))


# ─── Promotion ────────────────────────────────────────────────────────────────

def promote_candidates(tracker: dict, watchlist_names: set, date_str: str) -> list[str]:
    """Auto-add entities to watchlist.md when count >= PROMOTE_THRESHOLD."""
    if not WATCHLIST.exists():
        return []

    candidates = [
        e for e in tracker["entities"].values()
        if e["name"] not in watchlist_names
        and not e.get("on_watchlist", False)
        and e["count"] >= PROMOTE_THRESHOLD
        and any(m.get("signal") in ("high", "medium") for m in e.get("mentions", []))
    ]
    if not candidates:
        return []

    text     = WATCHLIST.read_text()
    promoted = []

    for e in candidates:
        latest  = e["mentions"][-1] if e["mentions"] else {}
        note    = latest.get("context", "")
        new_row = (
            f"| {e['name']} | {e['category']} | {e['first_seen']} "
            f"| {date_str} | {e['count']} | {note} |"
        )
        marker = "\n\n---\n\n## Recurring Themes"
        if marker in text:
            text = text.replace(marker, f"\n{new_row}{marker}", 1)
            e["on_watchlist"] = True
            promoted.append(e["name"])

    if promoted:
        WATCHLIST.write_text(text)

    return promoted


# ─── Entry points ─────────────────────────────────────────────────────────────

def run(date_str: str = None, verbose: bool = True) -> list[str]:
    """Process one brief date. Returns list of newly promoted entity names."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    actions_path = OUTPUT_DIR / f"brief-{date_str}-actions.md"
    if not actions_path.exists():
        if verbose:
            print(f"  [curator] No actions file for {date_str} — skipping")
        return []

    if verbose:
        print(f"\n🔭  Watchlist Curator — {date_str}")

    tracker        = load_tracker()
    watchlist_names = load_watchlist_names()

    raw_entities = parse_entities_from_actions(actions_path)
    if not raw_entities:
        if verbose:
            print("  No People & Companies found — nothing to track")
        return []

    entities = enrich_entities(raw_entities, date_str)
    tracker  = update_tracker(tracker, entities, date_str)
    save_tracker(tracker)

    update_existing_rows(tracker, watchlist_names, date_str)
    promoted = promote_candidates(tracker, watchlist_names, date_str)

    if verbose:
        print(f"  {len(entities)} entities tracked")

        # Show entities building toward threshold
        building = sorted(
            [
                e for e in tracker["entities"].values()
                if not e.get("on_watchlist")
                and e["name"] not in watchlist_names
                and 1 <= e["count"] < PROMOTE_THRESHOLD
            ],
            key=lambda x: -x["count"],
        )
        if building:
            print(f"  Building signal (below {PROMOTE_THRESHOLD}-mention threshold):")
            for e in building[:8]:
                bar = "█" * e["count"] + "░" * (PROMOTE_THRESHOLD - e["count"])
                print(f"    {bar}  {e['name']}  ({e['count']}/{PROMOTE_THRESHOLD})")

        if promoted:
            print(f"  ★  Auto-promoted: {', '.join(promoted)}")

    return promoted


def backfill():
    """Process all existing brief-*-actions.md files in date order."""
    files = sorted(OUTPUT_DIR.glob("brief-????-??-??-actions.md"))
    print(f"Backfilling {len(files)} brief(s)...\n")
    for path in files:
        m = re.search(r'brief-(\d{4}-\d{2}-\d{2})-actions', path.name)
        if m:
            run(m.group(1), verbose=True)
    print("\n✓ Backfill complete")


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        backfill()
    else:
        date_arg = next(
            (a for a in sys.argv[1:] if re.match(r'\d{4}-\d{2}-\d{2}', a)), None
        )
        run(date_arg)
