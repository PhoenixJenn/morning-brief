#!/usr/bin/env python3
"""
update_models.py — Update frontier-models.json and sync to AYX.

Usage:
  python update_models.py                        # sync current JSON → AYX, push
  python update_models.py --list                 # show all models + news_date
  python update_models.py --set "model-id" "New news text"   # update one model's news + date
  python update_models.py --stale                # list models flagged needs_update=true
  python update_models.py --mark-stale "model-id"            # flag a model for update
  python update_models.py --clear-stale "model-id"           # clear stale flag

Every sync:
  - Writes a .bak of the current AYX data/frontier-models.json before overwriting
  - Updates meta.last_updated to today
  - git add + commit + push augmentyourexperience-www
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
AYX_DIR      = PROJECT_DIR.parent / "augmentyourexperience-www"
SRC_FILE     = CONTEXT_DIR / "frontier-models.json"
DEST_FILE    = AYX_DIR / "data" / "frontier-models.json"


def load() -> dict:
    return json.loads(SRC_FILE.read_text())


def save(data: dict):
    SRC_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def find_model(data: dict, model_id: str) -> dict | None:
    for m in data["models"]:
        if m["id"] == model_id or m.get("version", "").lower() == model_id.lower():
            return m
    return None


def sync_and_push(data: dict, commit_msg: str, force: bool = False):
    # Guard against reverting an AYX-side update this script doesn't know about.
    # The weekly Saturday cron job (run_frontier_models_refresh.sh) edits
    # DEST_FILE directly and never touches SRC_FILE, so the two can diverge in
    # content even when both claim the same last_updated date -- this bit us
    # 2026-09-27 when weekly_recap.py pushed a stale SRC_FILE over a same-day,
    # much richer AYX refresh. Compare model counts + ids as a cheap divergence
    # signal; on mismatch, stop rather than silently overwrite.
    if DEST_FILE.exists() and not force:
        try:
            dest_data  = json.loads(DEST_FILE.read_text())
            dest_ids   = {m.get("id") for m in dest_data.get("models", [])}
            src_ids    = {m.get("id") for m in data.get("models", [])}
            only_in_dest = dest_ids - src_ids
            if only_in_dest:
                print(f"  ✗ Refusing to sync: AYX's copy has {len(only_in_dest)} model(s) not present in "
                      f"context/'s copy ({', '.join(sorted(only_in_dest)[:5])}{'...' if len(only_in_dest) > 5 else ''}) "
                      f"-- it's likely been updated independently (e.g. by the weekly refresh cron) since "
                      f"context/ was last touched. Pull AYX's version into context/ first "
                      f"(git -C {AYX_DIR} pull, then copy data/frontier-models.json over SRC_FILE), reapply "
                      f"your edit, and re-run. Use --force to override.")
                sys.exit(1)
        except (json.JSONDecodeError, KeyError):
            pass

    today = datetime.now().strftime("%Y-%m-%d")
    data["meta"]["last_updated"] = today
    save(data)

    # Backup before overwrite
    if DEST_FILE.exists():
        DEST_FILE.with_suffix(".json.bak").write_text(DEST_FILE.read_text())

    DEST_FILE.parent.mkdir(exist_ok=True)
    shutil.copy2(SRC_FILE, DEST_FILE)
    print(f"  ✓ Synced → {DEST_FILE.relative_to(AYX_DIR)}")

    cmds = [
        ["git", "-C", str(AYX_DIR), "add", "data/frontier-models.json"],
        ["git", "-C", str(AYX_DIR), "commit", "-m", commit_msg],
        ["git", "-C", str(AYX_DIR), "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        verb = cmd[3]
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                print(f"  · git commit: nothing to commit")
            else:
                print(f"  ✗ git {verb}: {result.stderr.strip()}")
            return
        print(f"  ✓ git {verb}")


def cmd_list(data: dict):
    print(f"\n{'Model':<30} {'Company':<14} {'Updated':<12} {'Stale'}")
    print("-" * 70)
    for m in data["models"]:
        stale = "⚠ stale" if m.get("needs_update") else ""
        date  = m.get("news_date", "—")
        print(f"  {m.get('version', m['id']):<28} {m['company']:<14} {date:<12} {stale}")


def cmd_stale(data: dict):
    stale = [m for m in data["models"] if m.get("needs_update")]
    if not stale:
        print("  No models flagged as stale.")
        return
    print(f"\n  {len(stale)} stale model(s):")
    for m in stale:
        print(f"  · {m.get('version', m['id'])} ({m['company']}) — last updated {m.get('news_date', '?')}")


def main():
    parser = argparse.ArgumentParser(
        description="Update frontier-models.json and sync to AYX.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list",        action="store_true", help="List all models with dates")
    parser.add_argument("--stale",       action="store_true", help="List stale models")
    parser.add_argument("--set",         nargs=2, metavar=("MODEL_ID", "NEWS"),
                        help="Update news text + set news_date to today")
    parser.add_argument("--mark-stale",  metavar="MODEL_ID", help="Flag model needs_update=true")
    parser.add_argument("--clear-stale", metavar="MODEL_ID", help="Clear needs_update flag")
    parser.add_argument("--no-push",     action="store_true", help="Skip git push")
    parser.add_argument("--force",       action="store_true", help="Skip the AYX-divergence safety check")

    args = parser.parse_args()
    data = load()

    if args.list:
        cmd_list(data)
        return

    if args.stale:
        cmd_stale(data)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    commit_msg = f"data: update frontier-models.json ({today})"

    if args.set:
        model_id, news_text = args.set
        m = find_model(data, model_id)
        if not m:
            print(f"  ✗ Model '{model_id}' not found. Run --list to see IDs.")
            sys.exit(1)
        m["news"]      = news_text
        m["news_date"] = today
        m["needs_update"] = False
        name = m.get("version", m["id"])
        print(f"\n  Updated: {name}")
        print(f"  News:    {news_text[:80]}")
        print(f"  Date:    {today}")
        commit_msg = f"data: update {name} — {today}"

    if args.mark_stale:
        m = find_model(data, args.mark_stale)
        if not m:
            print(f"  ✗ Model '{args.mark_stale}' not found.")
            sys.exit(1)
        m["needs_update"] = True
        print(f"  Flagged {m.get('version', m['id'])} as stale")

    if args.clear_stale:
        m = find_model(data, args.clear_stale)
        if not m:
            print(f"  ✗ Model '{args.clear_stale}' not found.")
            sys.exit(1)
        m["needs_update"] = False
        m["news_date"] = today
        print(f"  Cleared stale flag on {m.get('version', m['id'])}")

    if args.no_push:
        save(data)
        print("  (--no-push: saved locally, skipping sync)")
        return

    print(f"\n  Syncing frontier-models.json → AYX…")
    sync_and_push(data, commit_msg, force=args.force)
    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
