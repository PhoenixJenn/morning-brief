#!/usr/bin/env python3
"""
Weekly Recap — comprehensive digest of the week's Morning Brief transcripts.
Runs Sunday morning. Produces a blog-ready markdown post + email digest.
"""

import os
import re
import smtplib
import subprocess
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
AYX_DIR        = PROJECT_DIR.parent / "augmentyourexperience-www"
AYX_BRIEFS_DIR = AYX_DIR / "weekly-briefs"

# ─── Collect this week's transcripts ─────────────────────────────────────────

def week_dates(ref: date) -> list[date]:
    """Return Mon–Sun dates for the week containing ref."""
    monday = ref - timedelta(days=ref.weekday())
    return [monday + timedelta(days=i) for i in range(7)]  # Mon=0 … Sun=6

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

# ─── Image size check ────────────────────────────────────────────────────────

MAX_WIDTH_PX = 1400
MAX_SIZE_MB  = 0.5
IMAGE_EXTS   = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}

def _image_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) using macOS sips. Returns (0, 0) on failure."""
    try:
        r = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        lines = r.stdout.strip().splitlines()
        width  = int(next(l.split(":")[1].strip() for l in lines if "pixelWidth"  in l))
        height = int(next(l.split(":")[1].strip() for l in lines if "pixelHeight" in l))
        return width, height
    except Exception:
        return 0, 0

def check_staged_images() -> bool:
    """
    Inspect images staged in the AYX repo (after git add, before commit).
    Prints a warning for each oversized image with a resize/compress suggestion.
    Returns True if everything is within limits, False otherwise.
    """
    result = subprocess.run(
        ["git", "-C", str(AYX_DIR), "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    staged = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    images = [f for f in staged if Path(f).suffix.lower() in IMAGE_EXTS]

    if not images:
        return True

    print(f"\n🖼   Checking {len(images)} staged image(s)...")
    all_clear = True

    for rel in images:
        path = AYX_DIR / rel
        if not path.exists():
            continue

        size_mb = path.stat().st_size / (1024 * 1024)
        w, h    = _image_dimensions(path)
        issues  = []

        if w > MAX_WIDTH_PX:
            issues.append(f"width {w}px > {MAX_WIDTH_PX}px")
        if size_mb > MAX_SIZE_MB:
            issues.append(f"{size_mb:.1f}MB > {MAX_SIZE_MB}MB")

        if issues:
            print(f"  ⚠ {rel}: {', '.join(issues)}")
            if w > MAX_WIDTH_PX:
                print(f"      Resize: sips -Z {MAX_WIDTH_PX} \"{path.name}\"")
            if size_mb > MAX_SIZE_MB:
                print(f"      Compress: sips -s format jpeg -s formatOptions 80 \"{path.name}\" --out \"{path.stem}.jpg\"")
                print(f"      Or use ImageOptim (drag & drop): https://imageoptim.com")
            all_clear = False
        else:
            dim = f"{w}×{h}  " if w else ""
            print(f"  ✓ {rel}: {dim}{size_mb:.2f}MB")

    if not all_clear:
        print("  ↳ Committing anyway — optimize these images when you can.")

    return all_clear

# ─── AYX Publishing ──────────────────────────────────────────────────────────

def extract_body_content(full_html: str) -> str:
    """Extract inner body content from a full HTML document."""
    m = re.search(r'<body[^>]*>(.*)</body>', full_html, re.DOTALL)
    return m.group(1).strip() if m else full_html

def strip_email_header(body_content: str) -> str:
    """Remove the email's own WEEK IN REVIEW header — the AYX page has its own."""
    # The email header is the first <div> inside the outer wrapper, containing
    # the WEEK IN REVIEW title, date, and category line. Strip it out.
    # Match the header div (ends before the TOP STORIES section)
    stripped = re.sub(
        r'<!--\s*HEADER\s*-->.*?(?=<!--\s*TOP STORIES|<!--\s*THIS WEEK)',
        '', body_content, flags=re.DOTALL | re.IGNORECASE
    )
    # Fallback: remove the first child div of the outer wrapper if it contains "WEEK IN REVIEW"
    if 'WEEK IN REVIEW' in stripped:
        stripped = re.sub(
            r'<div[^>]*>\s*<div[^>]*>WEEK IN REVIEW</div>.*?</div>\s*</div>',
            '', stripped, count=1, flags=re.DOTALL
        )
    return stripped

def build_ayx_page(body_content: str, week_label: str, week_slug: str) -> str:
    """Wrap email body content in the AYX site template."""
    content = strip_email_header(body_content)
    page_title = f"Week in Review: {week_label}"
    return f"""<!DOCTYPE html>
<!-- 🎨 EASTER EGG: Triple-click the logo to reveal the theme switcher -->
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_title} — Augment Your Experience</title>
  <meta name="description" content="AI, XR, spatial computing, robotics, and media — a practitioner's digest of the week's most important tech developments.">
  <link rel="stylesheet" href="../css/main.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/ScrollTrigger.min.js"></script>
  <style>
    /* Widen brief content to match site full content width */
    article.brief-content {{
      max-width: 1024px !important;
    }}
    /* Override email's inner width constraint */
    article.brief-content > div {{
      max-width: 100% !important;
      padding: 0 !important;
    }}
    /* Override email inline styles to match dark site theme */
    .brief-content, .brief-content * {{
      color: var(--text-body) !important;
    }}
    .brief-content strong {{
      color: var(--text-primary) !important;
    }}
    .brief-content a {{
      color: var(--accent) !important;
    }}
    /* Top stories box */
    .brief-content div[style*="background-color:#f5f5f5"],
    .brief-content div[style*="background-color: #f5f5f5"] {{
      background-color: var(--bg-surface-2) !important;
      border: 1px solid var(--border) !important;
    }}
  </style>
</head>
<body>
  <div id="nav-mount"></div>
  <div class="post-header">
    <span class="hero-tag">AI · XR · Spatial Computing · Robotics · Media</span>
    <h1>Week in Review</h1>
    <div class="post-meta">
      <span class="post-meta-item">{week_label}</span>
      <span class="post-meta-divider">·</span>
      <span class="post-meta-item">Morning Brief Weekly Digest</span>
      <span class="post-meta-divider">·</span>
      <span class="post-meta-item" style="color:var(--accent);">Weekly Brief</span>
    </div>
  </div>
  <article class="prose brief-content">
{content}
  </article>
  <div class="post-nav">
    <a href="index.html">← All Weekly Briefs</a>
    <a href="../index.html">All Posts</a>
  </div>
  <div id="footer-mount"></div>
  <div id="switcher-mount"></div>
  <script src="../js/shared.js" data-base="../"></script>
</body>
</html>"""

def build_index_entry(week_label: str, week_slug: str, top_stories_summary: str) -> str:
    """Return an <a> card for the index page."""
    return f"""
      <a href="{week_slug}.html" style="display:block;text-decoration:none;border:1px solid var(--border, #e5e5e5);border-radius:8px;padding:1.25rem 1.5rem;transition:box-shadow 0.2s;" onmouseover="this.style.boxShadow='0 4px 16px rgba(0,0,0,0.08)'" onmouseout="this.style.boxShadow='none'">
        <div style="font-size:0.75rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted,#888);margin-bottom:0.4rem;">Week of {week_label}</div>
        <div style="font-size:1.1rem;font-weight:700;color:var(--text,#1a1a1a);margin-bottom:0.4rem;">Week in Review: {week_label}</div>
        <div style="font-size:0.9rem;color:var(--muted,#666);line-height:1.5;">{top_stories_summary}</div>
      </a>"""

def extract_top_stories_summary(html: str) -> str:
    """Pull the first few bold terms from the top stories section for the index card."""
    items = re.findall(r'<strong>([^<]{5,60})</strong>', html)
    return ", ".join(items[:5]) + "." if items else "Weekly tech digest."

def update_ayx_index(new_entry: str, week_slug: str):
    """Prepend or update the entry for week_slug in weekly-briefs/index.html."""
    index_path = AYX_BRIEFS_DIR / "index.html"
    content = index_path.read_text()
    if f"{week_slug}.html" in content:
        content = re.sub(
            rf'<a href="{re.escape(week_slug)}\.html"[^>]*>.*?</a>',
            new_entry.strip(),
            content,
            flags=re.DOTALL,
        )
        index_path.write_text(content)
        print(f"  ✓ Index entry updated for {week_slug}")
        return
    marker = "<!-- Most recent first — new entries go at the TOP -->"
    if marker in content:
        content = content.replace(marker, marker + new_entry)
        index_path.write_text(content)

def publish_to_ayx(email_html: str, week_label: str, week_slug: str, monday: date, week_end: date):
    if not AYX_DIR.exists():
        print(f"  ⚠ AYX directory not found at {AYX_DIR} — skipping")
        return

    AYX_BRIEFS_DIR.mkdir(exist_ok=True)

    # Build and save the AYX page
    body_content = extract_body_content(email_html)
    page_html    = build_ayx_page(body_content, week_label, week_slug)
    page_path    = AYX_BRIEFS_DIR / f"{week_slug}.html"
    page_path.write_text(page_html)
    print(f"  ✓ Saved AYX page: {page_path}")

    # Update the index
    summary   = extract_top_stories_summary(email_html)
    new_entry = build_index_entry(week_label, week_slug, summary)
    update_ayx_index(new_entry, week_slug)
    print(f"  ✓ Index updated")

    # Commit and push AYX
    try:
        subprocess.run(["git", "-C", str(AYX_DIR), "add", "weekly-briefs/"], check=True)
        check_staged_images()
        subprocess.run(["git", "-C", str(AYX_DIR), "commit", "-m",
                        f"Weekly Brief {week_slug} — {week_label}"], check=True)
        subprocess.run(["git", "-C", str(AYX_DIR), "push"], check=True)
        print(f"  ✓ Pushed to GitHub")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ Git push failed: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = date.today()

    # Allow --week YYYY-MM-DD to target a specific week
    if "--week" in sys.argv:
        idx = sys.argv.index("--week")
        today = date.fromisoformat(sys.argv[idx + 1])

    dates  = week_dates(today)
    monday = dates[0]
    week_end = dates[-1]
    week_label = f"{monday.strftime('%B %d')} – {week_end.strftime('%B %d, %Y')}"
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

    # Strip markdown code fences if model wraps response
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]

    # Parse SUBJECT / BODY same as daily email
    subject = f"Week in Review — {week_label}"
    html    = raw
    if "SUBJECT:" in raw and "BODY:" in raw:
        subject = raw.split("SUBJECT:")[1].split("BODY:")[0].strip()
        html    = raw.split("BODY:")[1].strip()

    out_html = OUTPUT_DIR / f"weekly-{week_slug}.html"
    out_html.write_text(html)
    print(f"  ✓ Saved email HTML: {out_html}")

    print("\n📧  Sending email digest...")
    send_email(subject, html)

    print("\n🌐  Publishing to Augment Your Experience...")
    publish_to_ayx(html, week_label, week_slug, monday, week_end)

    print(f"\n✅  Done. HTML at:\n    {out_html}\n")

if __name__ == "__main__":
    main()
