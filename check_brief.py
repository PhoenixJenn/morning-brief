#!/usr/bin/env python3
"""Check if today's Morning Brief ran; send a macOS notification if not."""

import json
import subprocess
from datetime import date
from pathlib import Path

STATUS_FILE = Path(__file__).parent / "status.json"
TODAY = date.today().isoformat()

def notify(title, message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}" sound name "Basso"'
    ])

if not STATUS_FILE.exists():
    notify("Morning Brief", "status.json missing — brief may never have run.")
else:
    status = json.loads(STATUS_FILE.read_text())
    if status.get("date") != TODAY:
        notify("Morning Brief didn't run", "Network may have been down at 5:30am. Run it manually.")
    else:
        print(f"Morning Brief ran at {status.get('run_at')} — all good.")
