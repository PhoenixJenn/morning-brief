#!/usr/bin/env python3
"""
add_entity.py — Add a company or person to the intel watchlist in one shot.

Usage:
  python add_entity.py "Name" company ai [--note "signal phrase"] [--description "longer desc"]
  python add_entity.py "Name" person xr [--note "role/context"]
  python add_entity.py "Name" company ai xr   # multi-tag: maps to AI / XR / Platform

Category abbreviations (one or two):
  ai         → AI Lab (company) / AI Researcher (person)
  xr         → XR Hardware (company) / XR Founder (person)
  xr-optics  → XR Optics
  robotics   → Robotics
  av         → AV
  3d         → 3D Capture & Create
  media      → Platform & Media
  space      → Space/Finance

Combo shortcuts (two args):
  ai xr      → AI / XR / Platform, tags: [ai, xr]
  ai media   → AI / Platform / XR (fallback: "AI / Platform / XR"), tags: [ai, media]

What it does:
  1. Appends a row to watchlist.md (Company & People section)
  2. Adds/updates entry in watchlist_meta.json
  3. Runs generate_intel.py (updates AYX intel/index.html)
  4. git add + commit + push in augmentyourexperience-www
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
AYX_DIR      = PROJECT_DIR.parent / "augmentyourexperience-www"
WATCHLIST    = CONTEXT_DIR / "watchlist.md"
META_FILE    = CONTEXT_DIR / "watchlist_meta.json"


# ─── Category mapping ────────────────────────────────────────────────────────

SINGLE_COMPANY = {
    "ai":        ("AI Lab",              ["ai"]),
    "xr":        ("XR Hardware",         ["xr"]),
    "xr-optics": ("XR Optics",           ["xr"]),
    "robotics":  ("Robotics",            ["robotics"]),
    "av":        ("AV",                  ["robotics"]),
    "av-robotics":("AV/Robotics",        ["robotics"]),
    "3d":        ("3D Capture & Create", ["3d"]),
    "media":     ("Platform & Media",    ["media"]),
    "space":     ("Space/Finance",       ["ai"]),
    "spatial":   ("Spatial Media",       ["xr"]),
}

SINGLE_PERSON = {
    "ai":        ("AI Researcher",       ["ai"]),
    "xr":        ("XR Founder",          ["xr"]),
    "robotics":  ("Robotics",            ["robotics"]),
    "av":        ("AV",                  ["robotics"]),
    "space":     ("Space/Finance",       ["ai"]),
}

COMBO = {
    frozenset(["ai", "xr"]):      ("AI / XR / Platform",  ["ai", "xr"]),
    frozenset(["ai", "media"]):   ("AI / Platform / XR",  ["ai", "media"]),
    frozenset(["av", "robotics"]): ("AV/Robotics",        ["robotics"]),
}


def resolve_category(entity_type: str, tags: list[str]) -> tuple[str, list[str]]:
    """Return (watchlist_category_string, tag_list) from raw tag inputs."""
    tags_lower = [t.lower() for t in tags]

    if len(tags_lower) >= 2:
        key = frozenset(tags_lower[:2])
        if key in COMBO:
            return COMBO[key]

    tag = tags_lower[0] if tags_lower else "ai"
    if entity_type == "person":
        if tag in SINGLE_PERSON:
            return SINGLE_PERSON[tag]
        # fall through: treat as company category with person entity_type
    if tag in SINGLE_COMPANY:
        return SINGLE_COMPANY[tag]

    # Raw value passed (e.g. full "AI Lab") — infer tags from it
    known_tags = []
    tl = tag.lower()
    if "ai" in tl:
        known_tags.append("ai")
    if "xr" in tl or "spatial" in tl:
        known_tags.append("xr")
    if "robot" in tl or "av" in tl:
        known_tags.append("robotics")
    if "3d" in tl or "capture" in tl:
        known_tags.append("3d")
    if "media" in tl:
        known_tags.append("media")
    return (tags[0], known_tags or ["ai"])


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ─── Watchlist.md writers ────────────────────────────────────────────────────

def entity_exists(name: str) -> bool:
    if not WATCHLIST.exists():
        return False
    return f"| {name} |" in WATCHLIST.read_text()


def append_to_watchlist(name: str, category: str, note: str, today: str):
    text = WATCHLIST.read_text()
    WATCHLIST.with_suffix(".md.bak").write_text(text)
    new_row = f"| {name} | {category} | {today} | {today} | 1 | {note[:120]} |"
    marker  = "\n\n---\n\n## Recurring Themes"
    if marker not in text:
        print("  ✗ Could not find '## Recurring Themes' marker in watchlist.md")
        sys.exit(1)
    WATCHLIST.write_text(text.replace(marker, f"\n{new_row}{marker}", 1))


# ─── watchlist_meta.json writer ──────────────────────────────────────────────

def update_meta(name: str, entity_type: str, category_tags: list[str],
                description: str):
    meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
    if name in meta:
        print(f"  ⚠  {name} already in watchlist_meta.json — updating description only")
        if description:
            meta[name]["description"] = description
    else:
        entry: dict = {
            "entity_type":   entity_type,
            "company_id":    slugify(name),
            "category_tags": category_tags,
        }
        if entity_type == "person":
            entry["person_id"] = slugify(name)
        if description:
            entry["description"] = description
        meta[name] = entry
    META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


# ─── AYX publish ─────────────────────────────────────────────────────────────

def push_ayx(name: str):
    if not AYX_DIR.exists():
        print(f"  ⚠  AYX directory not found at {AYX_DIR} — skipping push")
        return

    msg = f"intel: add {name}"
    cmds = [
        ["git", "-C", str(AYX_DIR), "add", "intel/index.html"],
        ["git", "-C", str(AYX_DIR), "commit", "-m", msg],
        ["git", "-C", str(AYX_DIR), "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        label = " ".join(cmd[3:] if cmd[2] == "-C" else cmd)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                print(f"  · git commit: nothing to commit (intel/index.html unchanged)")
            else:
                print(f"  ✗ git {cmd[-1]} failed: {result.stderr.strip()}")
            return
        else:
            print(f"  ✓ git {cmd[3]}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add a company or person to the intel watchlist.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("name",        help="Entity name (quoted if spaces)")
    parser.add_argument("entity_type", choices=["company", "person"],
                        help="company or person")
    parser.add_argument("tags",        nargs="+",
                        help="Category abbreviation(s): ai xr robotics av 3d media space")
    parser.add_argument("--note",        default="",
                        help="Short signal phrase for watchlist.md Note column (≤120 chars)")
    parser.add_argument("--description", default="",
                        help="Longer description for watchlist_meta.json (shows on AYX)")
    parser.add_argument("--no-push",   action="store_true",
                        help="Skip git push to AYX")

    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")

    # Resolve category
    category, tag_list = resolve_category(args.entity_type, args.tags)
    print(f"\n  Name:     {args.name}")
    print(f"  Type:     {args.entity_type}")
    print(f"  Category: {category}  tags: {tag_list}")

    # Guard: already on watchlist?
    if entity_exists(args.name):
        print(f"\n  ⚠  '{args.name}' already exists in watchlist.md")
        print("     To update, edit watchlist.md directly or tell Claude.")
        sys.exit(1)

    # 1. Write watchlist.md
    note = args.note or f"Added {today}"
    append_to_watchlist(args.name, category, note, today)
    print(f"\n  ✓ watchlist.md — appended row")

    # 2. Write watchlist_meta.json
    update_meta(args.name, args.entity_type, tag_list, args.description)
    print(f"  ✓ watchlist_meta.json — entry added")

    # 3. Run generate_intel.py
    try:
        import generate_intel
        ok = generate_intel.generate(verbose=False)
        if ok:
            print(f"  ✓ generate_intel.py — AYX intel/index.html regenerated")
        else:
            print(f"  ⚠  generate_intel.py ran but reported no output")
    except Exception as exc:
        print(f"  ✗ generate_intel.py failed: {exc}")
        sys.exit(1)

    # 4. Push AYX
    if not args.no_push:
        print(f"  Pushing AYX…")
        push_ayx(args.name)
    else:
        print(f"  (--no-push: skipping git push)")

    print(f"\n  Done. '{args.name}' is live on the watchlist.\n")


if __name__ == "__main__":
    main()
