#!/usr/bin/env python3
"""
One-time check: did today's Deep Dive episode publish? Email the subscribe
link if so. Self-removes its own crontab line after running, either way,
so it only ever fires once.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import morning_brief as mb

PROJECT_DIR = mb.PROJECT_DIR
STATUS_FILE = PROJECT_DIR / "status.json"
SELF_NAME   = "check_deep_dive_and_notify.py"


def build_email(today: str) -> tuple[str, str]:
    if not STATUS_FILE.exists():
        return (
            "Deep Dive check — no status.json found",
            f"<p>Checked at {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
            f"couldn't find <code>status.json</code> at all, so the morning "
            f"run likely didn't complete. Check <code>cron.log</code>.</p>",
        )

    status = json.loads(STATUS_FILE.read_text())

    if status.get("date") != today:
        return (
            "Deep Dive check — today's run hasn't completed yet",
            f"<p>status.json is still showing {status.get('date')}, not {today}. "
            f"The morning run may have failed or not started. Check "
            f"<code>cron.log</code> and <code>logs/errors.log</code>.</p>",
        )

    if status.get("deep_dive"):
        feed_url = mb.DEEP_DIVE_URL
        subject = "🎧 Deep Dive is live — subscribe from your phone"
        body = f"""
        <div style="font-family:-apple-system,Arial,sans-serif;max-width:560px;margin:0 auto;">
          <h2 style="margin-bottom:0.25rem;">Deep Dive published its first episode</h2>
          <p style="color:#666;">AI Research &amp; World Models — technical daily show</p>
          <p>Subscribe with this feed URL:</p>
          <p style="background:#f5f5f5;padding:0.75rem 1rem;border-radius:6px;word-break:break-all;">
            <a href="{feed_url}">{feed_url}</a>
          </p>
          <p><strong>Apple Podcasts</strong> — File → Add a Show by URL → paste the link above<br>
          <strong>Overcast / Pocket Casts / Castro</strong> — Add by RSS feed → paste the link above</p>
        </div>
        """
        return subject, body

    return (
        "Deep Dive check — no episode today",
        f"<p>Today's run completed but no Deep Dive episode published "
        f"(likely no research-significant news in the last 24 hours — this "
        f"is expected behavior, not a failure). Feed will go live the next "
        f"day there's qualifying content.</p>",
    )


def remove_self_from_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return
    lines = [l for l in result.stdout.splitlines() if SELF_NAME not in l]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    subject, body = build_email(today)
    try:
        mb.send_email_digest(subject, body)
    finally:
        remove_self_from_crontab()


if __name__ == "__main__":
    main()
