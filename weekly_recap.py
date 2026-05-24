#!/usr/bin/env python3
"""
Weekly Recap — comprehensive digest of the week's Morning Brief transcripts.
Runs Sunday morning. Produces a blog-ready markdown post + email digest.
"""

import os
import smtplib
import sys
from datetime import datetime, timedelta, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR    = Path(__file__).parent
OUTPUT_DIR     = PROJECT_DIR / "output"
EMAIL_SENDER   = "ClaudeCode9000@gmail.com"
EMAIL_RECIPIENT = "phoenixjenn@gmail.com"

# ─── Collect this week's transcripts ─────────────────────────────────────────

def week_dates(ref: date) -> list[date]:
    """Return Mon–Sat dates for the week containing ref."""
    monday = ref - timedelta(days=ref.weekday())
    return [monday + timedelta(days=i) for i in range(6)]  # Mon=0 … Sat=5

def load_transcripts(dates: list[date]) -> list[dict]:
    """Load regular + special brief transcripts for the given dates."""
    transcripts = []
    for d in dates:
        ds = d.isoformat()
        regular = OUTPUT_DIR / f"brief-{ds}.txt"
        if regular.exists():
            transcripts.append({"date": ds, "type": "regular", "text": regular.read_text()})
        for special in sorted(OUTPUT_DIR.glob(f"special-{ds}-*.txt")):
            label = special.stem.replace(f"special-{ds}-", "").replace("-", " ").title()
            transcripts.append({"date": ds, "type": f"special:{label}", "text": special.read_text()})
    return transcripts

# ─── Generate weekly recap ────────────────────────────────────────────────────

def generate_weekly_recap(transcripts: list[dict], week_label: str) -> str:
    client = anthropic.Anthropic()

    combined = ""
    for t in transcripts:
        day = datetime.strptime(t["date"], "%Y-%m-%d").strftime("%A, %B %d")
        label = f"[{day} — {t['type'].upper()}]"
        combined += f"\n\n{'='*60}\n{label}\n{'='*60}\n\n{t['text']}"

    prompt = f"""You are reformatting a week's worth of spoken audio news briefings into a clean weekly email digest — the same format as the daily Morning Brief email, but spanning the full week.

FORMAT EXACTLY like the daily email digest:

SUBJECT: Week in Review — {week_label}

BODY:
[Clean HTML email body. Use inline styles only. Design guidelines:
- Max width 620px, centered, font-family: -apple-system, Arial, sans-serif, color: #1a1a1a
- Header: large bold title "WEEK IN REVIEW" + date range in smaller gray text below + one line of coverage categories in small gray text: "General Tech · AI & ML · XR & Spatial · 3D Scanning & Printing · Robotics & AVs · IoT · Media"
- "THIS WEEK'S TOP STORIES" section: gray background box (#f5f5f5), 5-6 must-read bullets — the single most important development from the week, each starting with a bold term and including key specific facts/numbers
- Topic sections with ALL-CAPS headers in small gray text (AI & INDUSTRY, SPATIAL COMPUTING & XR, ROBOTICS & AVs, 3D SCANNING & PRINTING, IOT, MEDIA & ENTERTAINMENT)
- Each section: key stories as <p> tags. Each item: <strong>Company or Topic</strong> — 2-3 sentences. If a story ran across multiple days, synthesize into one entry.
- Closing "BIG PICTURE" section: 3-4 connective observations about the week's themes
- Footer: small gray text "Morning Brief · Week of {week_label} · AI-curated daily podcast · Subscribe: https://PhoenixJenn.github.io/morning-brief/feed.xml"]

CRITICAL RULES:
1. This is a TLDR digest — not a blog post. Aim for scannable, not comprehensive.
2. Prioritize: pick the most important 2-3 stories per section. Stories that recurred across multiple days get priority — they were significant enough to follow up on.
3. PRESERVE ALL SPECIFIC FACTS in what you do include — every dollar amount, percentage, date, and company name must be accurate. Never drop a number from a story you're covering.
4. If a story appeared multiple days, synthesize it into one sharp entry with the final status/outcome.
5. Keep the analytical "so what" — the implications matter more than the headline.

TRANSCRIPTS FOR THE WEEK OF {week_label}:
{combined}

Return SUBJECT line first, then BODY with full HTML."""

    print("  Calling Claude API for weekly recap...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

# ─── Email ────────────────────────────────────────────────────────────────────


def send_email(subject: str, html_body: str):
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace("\xa0", "").replace(" ", "")
    if not password:
        print("  ⚠ GMAIL_APP_PASSWORD not set — skipping email")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_SENDER, password)
        smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    print(f"  ✓ Email sent to {EMAIL_RECIPIENT}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = date.today()

    # Allow --week YYYY-MM-DD to target a specific week
    if "--week" in sys.argv:
        idx = sys.argv.index("--week")
        today = date.fromisoformat(sys.argv[idx + 1])

    dates  = week_dates(today)
    monday = dates[0]
    saturday = dates[-1]
    week_label = f"{monday.strftime('%B %d')} – {saturday.strftime('%B %d, %Y')}"
    week_slug  = monday.strftime("%Y-W%V")

    print(f"\n📅  Weekly Recap — {week_label}")
    print("=" * 52)

    print("\n📂  Loading transcripts...")
    transcripts = load_transcripts(dates)
    if not transcripts:
        print("  No transcripts found for this week.")
        return
    print(f"  ✓ {len(transcripts)} transcript(s) loaded ({', '.join(t['date'] for t in transcripts)})")

    print("\n✍️   Generating weekly recap with Claude...")
    raw = generate_weekly_recap(transcripts, week_label)

    # Parse SUBJECT / BODY same as daily email
    subject = f"Week in Review — {week_label}"
    html    = raw
    if "SUBJECT:" in raw and "BODY:" in raw:
        subject = raw.split("SUBJECT:")[1].split("BODY:")[0].strip()
        html    = raw.split("BODY:")[1].strip()

    out_html = OUTPUT_DIR / f"weekly-{week_slug}.html"
    out_html.write_text(html)
    print(f"  ✓ Saved: {out_html}")

    print("\n📧  Sending email digest...")
    send_email(subject, html)

    print(f"\n✅  Done. HTML at:\n    {out_html}\n")

if __name__ == "__main__":
    main()
