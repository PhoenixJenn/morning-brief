#!/usr/bin/env python3
"""Check if today's Morning Brief ran; re-run it automatically if not."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
STATUS_FILE = PROJECT_DIR / "status.json"
BRIEF_SCRIPT = PROJECT_DIR / "morning_brief.py"
TODAY = date.today().isoformat()

def notify(title, message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}" sound name "Basso"'
    ])

def brief_ran_today():
    if not STATUS_FILE.exists():
        return False
    status = json.loads(STATUS_FILE.read_text())
    return status.get("date") == TODAY and status.get("status") == "ok"

if brief_ran_today():
    status = json.loads(STATUS_FILE.read_text())
    print(f"Morning Brief ran at {status.get('run_at')} — all good.")
    sys.exit(0)

print(f"Morning Brief didn't run today — launching now...")
notify("Morning Brief", "Didn't run at 5:30am — re-running now.")

result = subprocess.run(
    [sys.executable, str(BRIEF_SCRIPT)],
    env={**__import__('os').environ},
)

if result.returncode == 0:
    notify("Morning Brief", "Re-run complete — check your podcast app.")
else:
    notify("Morning Brief FAILED", "Re-run at 8:30am also failed. Check logs.")
