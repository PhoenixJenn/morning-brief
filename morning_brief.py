#!/usr/bin/env python3
"""
Morning Brief — AI-curated daily news briefing
Fetches RSS feeds, curates with Claude, converts to audio, publishes as podcast.
"""

import re
import os
import json
import hashlib
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
import feedparser
import anthropic
from openai import OpenAI

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR       = Path(__file__).parent
OUTPUT_DIR        = PROJECT_DIR / "output"
LOG_DIR           = PROJECT_DIR / "logs"
ERROR_LOG         = LOG_DIR / "errors.log"
RSS_FILE          = PROJECT_DIR / "feed.xml"
SEEN_TITLES_FILE  = OUTPUT_DIR / "seen-titles.json"
EVENTS_FILE       = PROJECT_DIR.parent / "augmentyourexperience-www" / "data" / "events.json"

PODCAST_TITLE       = "Morning Brief"
PODCAST_DESCRIPTION = "AI-curated daily tech briefing — spatial computing, AI, XR, media, and more."
GITHUB_PAGES_URL    = "https://PhoenixJenn.github.io/morning-brief"

# ─── Cloudflare R2 (audio hosting) ───────────────────────────────────────────
# Credentials come from env vars — add to crontab: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
R2_ACCOUNT_ID  = "00b8f5d4c66b807a2396f13949ddc8ff"
R2_BUCKET      = "morning-brief"
R2_ENDPOINT    = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
# After enabling "Allow Public Access" on the bucket in the R2 dashboard,
# paste the pub-XXXX.r2.dev URL here:
R2_PUBLIC_URL  = "https://pub-f3e7015d6403424da2cfcf8b1f9060ac.r2.dev"  # e.g. "https://pub-abc123.r2.dev"
PURGE_DAYS     = 14    # delete R2 episodes and feed entries older than this

TARGET_WORDS  = 7000   # ~48 min at 145 wpm — aim high so we land near 45
SPLIT_WORDS   = 8500   # split into Part 1 / Part 2 if briefing exceeds this
LOOKBACK_HRS  = 26     # slightly more than 24 to catch late-night posts
MAX_PER_FEED  = 10     # max articles pulled per feed
TTS_VOICE     = "nova"    # OpenAI voices: alloy, echo, fable, onyx, nova, shimmer
TTS_MODEL     = "tts-1"  # tts-1 (~$13/mo daily) or tts-1-hd (higher quality, ~$26/mo)
TTS_SPEED     = 1.4      # 1.0 = normal, 1.25 = slightly faster, 1.5 = fast
OPENAI_CHUNK  = 4000     # OpenAI TTS max chars per request

# ─── RSS Feeds by Topic ───────────────────────────────────────────────────────

FEEDS = {
    "General Tech & Industry": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.engadget.com/rss.xml",
        "https://thenextweb.com/feed/",
        "https://www.cnet.com/rss/news/",
        "https://www.digitaltrends.com/feed/",
        "https://feeds.macrumors.com/MacRumors-All",
        "https://www.macworld.com/feed",
        "https://9to5mac.com/feed/",
        "https://appleinsider.com/rss/news/",
        "https://www.slashgear.com/feed/",
        "https://www.techradar.com/rss",
        "https://slashdot.org/rss/slashdot.rss",
        "https://www.forbes.com/innovation/feed/",
        "https://mashable.com/feeds/rss/all",
        "https://www.tomsguide.com/feeds/all",
        "https://www.tomshardware.com/feeds/all",
    ],
    "AI & Machine Learning": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
        "https://www.technologyreview.com/feed/",
        "https://openai.com/news/rss.xml",
        "https://aws.amazon.com/blogs/machine-learning/feed/",
        "https://9to5google.com/feed/",
        "https://www.androidauthority.com/feed/",
        "https://spectrum.ieee.org/feeds/feed.rss",
    ],
    "XR, Spatial Computing & Spatial Internet": [
        "https://www.roadtovr.com/feed/",
        "https://uploadvr.com/feed/",
        "https://arinsider.co/feed/",
        "https://about.fb.com/news/feed/",
        "https://9to5google.com/tag/android-xr/feed/",
        "https://mixed-news.com/en/feed/",
        "https://www.wareable.com/feed/",
    ],
    "3D Capture & Create": [
        "https://spectrum.ieee.org/feeds/feed.rss",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ],
    "Autonomous Vehicles, Robotics & Humanoid Robots": [
        "https://techcrunch.com/category/transportation/feed/",
        "https://techcrunch.com/category/robotics/feed/",
        "https://www.theverge.com/transportation/rss/index.xml",
        "https://www.technologyreview.com/feed/",
        "https://spectrum.ieee.org/feeds/feed.rss",
    ],
    "IoT & Connected Devices": [
        "https://staceyoniot.com/feed/",
        "https://iotanalytics.com/feed/",
        "https://www.iotworldtoday.com/feed/",
        "https://www.iotforall.com/feed",
    ],
    "Media & Entertainment": [
        "https://techcrunch.com/category/media-entertainment/feed/",
        "https://www.fiercevideo.com/rss/xml",
        "https://www.theverge.com/entertainment/rss/index.xml",
    ],
}

# ─── Seen-title deduplication ────────────────────────────────────────────────

def load_seen_titles() -> set:
    if SEEN_TITLES_FILE.exists():
        return set(json.loads(SEEN_TITLES_FILE.read_text()))
    return set()

def save_seen_titles(seen: set):
    OUTPUT_DIR.mkdir(exist_ok=True)
    SEEN_TITLES_FILE.write_text(json.dumps(sorted(seen), indent=2))

# ─── Events ──────────────────────────────────────────────────────────────────

def get_active_events(today: str) -> list:
    """Return events whose coverage window includes today (1 day before start through 2 days after end)."""
    if not EVENTS_FILE.exists():
        return []
    from datetime import date
    today_dt = date.fromisoformat(today)
    active = []
    for event in json.loads(EVENTS_FILE.read_text()):
        start = date.fromisoformat(event["date"]) - timedelta(days=1)
        end   = date.fromisoformat(event.get("date_end") or event["date"]) + timedelta(days=2)
        if start <= today_dt <= end:
            active.append(event)
    return active

def partition_articles(articles: dict, events: list) -> tuple[dict, dict]:
    """Split articles into event-specific and regular buckets by keyword matching."""
    keywords = [kw.lower() for e in events for kw in e.get("keywords", [])]
    regular, event_articles = {}, {}
    for topic, items in articles.items():
        reg, evt = [], []
        for item in items:
            text = (item["title"] + " " + item.get("summary", "")).lower()
            (evt if any(kw in text for kw in keywords) else reg).append(item)
        if reg:
            regular[topic] = reg
        if evt:
            event_articles[topic] = evt
    return regular, event_articles

# ─── Fetch Articles ───────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())

def fetch_articles(seen_titles: set) -> tuple[dict, set]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HRS)
    results = {}
    new_titles = set()

    for topic, urls in FEEDS.items():
        articles = []
        seen_this_run = set()

        for url in urls:
            try:
                feed = feedparser.parse(url, agent="MorningBrief/1.0")
                for entry in feed.entries[:MAX_PER_FEED]:
                    title = entry.get("title", "").strip()
                    if not title or title in seen_this_run or title in seen_titles:
                        continue
                    seen_this_run.add(title)

                    raw = ""
                    if hasattr(entry, "content"):
                        raw = entry.content[0].value
                    elif hasattr(entry, "summary"):
                        raw = entry.summary

                    summary = strip_html(raw)[:600]
                    source  = feed.feed.get("title", url)

                    articles.append({
                        "title":   title,
                        "summary": summary,
                        "source":  source,
                    })
                    new_titles.add(title)
            except Exception as e:
                print(f"    ⚠ Skipped {url}: {e}")

        if articles:
            results[topic] = articles
            print(f"  ✓ {topic}: {len(articles)} articles")

    return results, new_titles

# ─── Watch Context ────────────────────────────────────────────────────────────

_CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
THINGS_FILE   = _CONTEXT_DIR / "things_to_try.json"
WATCHLIST_MD  = _CONTEXT_DIR / "watchlist.md"

def load_watch_context() -> str:
    """Build a watch-context block to inject into the briefing prompt.

    Pulls: (1) entity names from watchlist.md Companies & People table,
           (2) HIGH-priority stories from things_to_try.json.
    Returns a formatted string ready to drop into the prompt, or "" if nothing found.
    """
    lines = []

    # Watchlist entities
    if WATCHLIST_MD.exists():
        text    = WATCHLIST_MD.read_text()
        section = re.search(r'## Companies & People\n(.+?)(?=\n---\n)', text, re.DOTALL)
        if section:
            names = []
            for row in section.group(1).split('\n'):
                m = re.match(r'\|\s*(.+?)\s*\|', row)
                if m:
                    name = m.group(1).strip()
                    if name and name != 'Name' and not re.match(r'^[-\s]+$', name):
                        names.append(name)
            if names:
                lines.append("ENTITIES TO WATCH (flag when these appear in any story):")
                lines.append(", ".join(names))
                lines.append("")

    # HIGH-priority stories
    if THINGS_FILE.exists():
        things = json.loads(THINGS_FILE.read_text())
        high_stories = [
            t["text"] for t in things
            if t.get("type") == "stories_to_follow" and t.get("priority") == "high"
        ]
        if high_stories:
            lines.append("ONGOING STORIES TO PRIORITIZE (cover any new developments on these):")
            for s in high_stories[:20]:
                lines.append(f"- {s}")
            lines.append("")

    if not lines:
        return ""

    return (
        "WATCH CONTEXT — use this to prioritize coverage and make connections:\n"
        + "\n".join(lines)
        + "\n"
    )


# ─── Generate Briefing ────────────────────────────────────────────────────────

def generate_briefing(articles: dict) -> str:
    client = anthropic.Anthropic()

    article_text = ""
    for topic, items in articles.items():
        article_text += f"\n\n## {topic}\n"
        for a in items:
            article_text += f"- **{a['title']}** ({a['source']})\n"
            if a["summary"]:
                article_text += f"  {a['summary']}\n"

    today_pretty  = datetime.now().strftime("%A, %B %d, %Y")
    watch_context = load_watch_context()

    prompt = f"""You are writing a spoken audio news briefing for a senior tech leader working in AI and spatial computing. She listens during her ~1 hour morning commute.

Write this as natural spoken audio — not an article, not a list. She will hear this, not read it.

TONE:
- Smart, conversational, like a knowledgeable colleague catching you up
- Get to the substance immediately — no "In today's fast-moving tech landscape..." filler
- When relevant, connect dots between stories
- Occasional dry wit is welcome
- Treat her as a peer who already knows the basics

STRUCTURE:
- Open with: "Good morning. Here's your briefing for {today_pretty}."
- Use natural verbal transitions between topics: "Moving to AI..." / "On the spatial computing front..." / "A few things in media and entertainment..."
- Write approximately {TARGET_WORDS} words — this is a firm target, not a suggestion. If a topic has limited news, go deeper on analysis and context rather than cutting length. The listener has a 45-minute commute and wants it filled.
- Close with: "That's your briefing. Have a great commute."

TOPIC SECTIONS (cover each — weight toward AI and XR which are her core focus):
1. General Tech & Industry — 3-4 top stories
2. AI & Machine Learning — give this section the most depth; it's central to her work
3. XR, Spatial Computing, Spatial Internet & World Models — important professionally; cover substantively; include spatial internet, digital twins, world models
4. 3D Capture & Create — include anything on Niantic Scaniverse, Creality, xTool, 3D Gaussian splatting, photogrammetry
5. Autonomous Vehicles, Robotics & Humanoid Robots — 2-3 stories; humanoid robots (Figure, Tesla Optimus, Boston Dynamics, etc.) are of high interest
6. IoT & Connected Devices — 2-3 stories; smart home, industrial IoT, connected devices
7. Media & Entertainment — 2-3 stories

EDITORIAL RULES:
- Skip rumors with no substance, listicles, and "X does something minor" non-stories
- If a section had no meaningful news today, say so in one sentence and move on
- Prioritize stories with real implications over press releases

{watch_context}TODAY'S ARTICLES:
{article_text}

Write the full briefing script now:"""

    print("  Calling Claude API...")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

# ─── Generate Special Brief ───────────────────────────────────────────────────

def generate_special_brief(articles: dict, events: list, today_pretty: str) -> str:
    client = anthropic.Anthropic()
    event_names = ", ".join(e["name"] for e in events)

    article_text = ""
    for topic, items in articles.items():
        article_text += f"\n\n## {topic}\n"
        for a in items:
            article_text += f"- **{a['title']}** ({a['source']})\n"
            if a["summary"]:
                article_text += f"  {a['summary']}\n"

    prompt = f"""You are writing a Special Brief — a focused spoken audio episode covering breaking announcements from {event_names}.

The listener is a senior tech leader in AI and spatial computing. She wants comprehensive coverage of everything announced — be thorough, specific, and concrete. Don't summarize; report.

TONE: Smart, insider energy. Connect announcements to broader trends where it's obvious. Occasional dry wit welcome.

STRUCTURE:
- Open with: "This is your Special Brief for {event_names}, {today_pretty}."
- Work through announcements category by category — group related things together
- Be as long as the material demands. Do not cut news to fit a length target.
- If something is minor, say so in one sentence and move on
- Close with: "That's everything from {event_names}. Back to your regular brief."

TODAY'S ARTICLES:
{article_text}

Write the full Special Brief now:"""

    print(f"  Calling Claude API for Special Brief ({event_names})...")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

# ─── Split long briefings ─────────────────────────────────────────────────────

def split_briefing(text: str) -> list[str]:
    """Split a briefing into two halves at the nearest sentence boundary to the midpoint."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    total = len(text.split())
    target, cumulative, split_at = total // 2, 0, len(sentences) // 2
    for i, s in enumerate(sentences):
        cumulative += len(s.split())
        if cumulative >= target:
            split_at = i + 1
            break
    return [" ".join(sentences[:split_at]), " ".join(sentences[split_at:])]

# ─── Text-to-Speech ───────────────────────────────────────────────────────────

def split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks at sentence boundaries."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= max_chars:
            current = (current + " " + sentence).strip()
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks

def text_to_speech(text: str, base_path: Path) -> Path:
    client   = OpenAI()
    mp3_path = base_path.with_suffix(".mp3")
    chunks   = split_text(text, OPENAI_CHUNK)

    print(f"  Converting {len(chunks)} chunks via OpenAI TTS ({TTS_VOICE})...")
    audio_bytes = b""
    for i, chunk in enumerate(chunks, 1):
        print(f"    Chunk {i}/{len(chunks)}...", end="\r")
        response = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=chunk,
            response_format="mp3",
            speed=TTS_SPEED,
        )
        audio_bytes += response.content

    mp3_path.write_bytes(audio_bytes)
    print(f"\n  ✓ Audio: {mp3_path.name}")
    return mp3_path

# ─── Cloudflare R2 upload + purge ────────────────────────────────────────────

def purge_old_episodes():
    """Delete R2 objects and feed.xml entries older than PURGE_DAYS days."""
    cutoff  = datetime.now(timezone.utc) - timedelta(days=PURGE_DAYS)
    date_re = re.compile(r'(\d{4}-\d{2}-\d{2})')

    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (R2_PUBLIC_URL and access_key and secret_key):
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )

    purged_keys = set()
    resp = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix="output/")
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        m = date_re.search(key)
        if m and datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc) < cutoff:
            s3.delete_object(Bucket=R2_BUCKET, Key=key)
            purged_keys.add(Path(key).name)
            print(f"  🗑  Purged from R2: {key}")

    if not purged_keys:
        print(f"  ✓ Nothing to purge (all episodes within {PURGE_DAYS} days)")
        return

    if RSS_FILE.exists():
        content = RSS_FILE.read_text()
        parts   = re.split(r'(<item>.*?</item>)', content, flags=re.DOTALL)
        removed = 0
        filtered = []
        for part in parts:
            if part.startswith('<item>'):
                url_match = re.search(r'url="([^"]+)"', part)
                if url_match and Path(url_match.group(1)).name in purged_keys:
                    removed += 1
                    continue
            filtered.append(part)
        if removed:
            RSS_FILE.write_text(''.join(filtered))
            print(f"  ✓ Removed {removed} old entries from feed.xml")

def upload_to_r2(path: Path) -> str:
    """Upload an MP3 to Cloudflare R2. Returns the public URL.

    Audio is R2-only — push_to_github() never commits mp3s — so there is no
    working fallback URL. Raise instead of writing a link that will 404.
    """
    if not R2_PUBLIC_URL:
        raise RuntimeError(
            "R2_PUBLIC_URL not set — cannot publish audio (GitHub Pages does not host mp3s)"
        )

    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise RuntimeError(
            "R2 credentials not in environment — cannot publish audio. "
            "If running manually outside cron, export R2_ACCESS_KEY_ID and "
            "R2_SECRET_ACCESS_KEY first (see crontab for values)."
        )

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )
    key = f"output/{path.name}"
    s3.upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": "audio/mpeg"})
    url = f"{R2_PUBLIC_URL}/{key}"
    print(f"  ✓ Uploaded to R2: {url}")
    return url

# ─── Podcast RSS Feed ─────────────────────────────────────────────────────────

def update_podcast_feed(audio_path: Path, title: str, audio_url: str):
    if not audio_url:
        raise RuntimeError(
            f"update_podcast_feed called without an audio_url for {audio_path.name} — "
            "GitHub Pages does not host mp3s, so there is no safe fallback."
        )
    pub_date  = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    file_size = audio_path.stat().st_size
    guid      = hashlib.md5(title.encode()).hexdigest()

    new_item = f"""
    <item>
      <title>{title}</title>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{file_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{guid}</guid>
      <itunes:duration>2700</itunes:duration>
    </item>"""

    if RSS_FILE.exists():
        content = RSS_FILE.read_text()
        content = content.replace("</channel>", new_item + "\n  </channel>")
    else:
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{PODCAST_TITLE}</title>
    <description>{PODCAST_DESCRIPTION}</description>
    <language>en-us</language>
    <link>{GITHUB_PAGES_URL}</link>
    <itunes:image href="{R2_PUBLIC_URL}/morningbrief.jpg"/>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
    {new_item}
  </channel>
</rss>"""

    RSS_FILE.write_text(content)
    print(f"  ✓ RSS feed updated")

# ─── Parse Briefing → TLDR + Action Items ────────────────────────────────────

CLAUDE_PROJECTS_DIR = Path("/Users/jennlee/Projects/claude_projects")
DAILY_LOG_DIR       = CLAUDE_PROJECTS_DIR / "context" / "daily"
TODO_PATH           = CLAUDE_PROJECTS_DIR / "context" / "TODO.md"
BRIEF_INBOX_PATH    = CLAUDE_PROJECTS_DIR / "context" / "brief-inbox.md"

AYX_DIR             = PROJECT_DIR.parent / "augmentyourexperience-www"
AYX_EVENTS_PATH     = EVENTS_FILE  # same file — AYX is the single source of truth

def parse_briefing(briefing: str, today: str, today_pretty: str) -> str:
    """Extract TLDR and action items — two focused calls with separate token budgets.

    Split rationale: a single 2048-token response couldn't reliably fit both TLDR
    (8-12 bullets) and all six action categories on long briefs, causing the last
    sections (esp. People & Companies) to be silently truncated.

    Refactoring notes for next pass:
      - anthropic.Anthropic() is re-instantiated in every function; move to module level
      - main() is ~170 lines and could be decomposed into a pipeline of named steps
      - watchlist_curator is imported inline inside main(); hoist to top-level import
    """
    client = anthropic.Anthropic()
    print("  Parsing briefing (TLDR + action items)...")

    # ── Call 1: TLDR (800 tokens is plenty for 8-12 bullets) ──────────────────
    tldr_msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Read this morning news briefing and write a TLDR.

8 to 12 bullet points. Specific and concrete — cite company names, numbers, and facts. No vague summaries.

Format EXACTLY like this:

## TLDR
- bullet
- bullet

---

BRIEFING:
{briefing}""",
        }],
    )
    tldr_section = tldr_msg.content[0].text.strip()

    # ── Call 2: Action items (2500 tokens for six full categories) ─────────────
    actions_msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2500,
        messages=[{
            "role": "user",
            "content": f"""Read this morning news briefing and extract action items across six categories.
Only include genuinely actionable items — skip anything vague.

## Action Items

### Upcoming Events
Conferences, product launches, keynotes, release dates. Include source in parentheses.
- item (source: ...)

### Things to Try
New features, tools, apps, or functionality worth testing hands-on.
- item

### Stories to Follow
Ongoing developments worth tracking over the next 1-2 weeks.
- item

### Blog Post Ideas
Angles worth writing about on a spatial computing / emerging tech blog.
- item

### People & Companies to Watch
Names worth adding to a watchlist. Include a 1-line reason in parentheses.
- Name (reason)

### Other
Anything actionable that doesn't fit above.
- item

Use these exact headers. If a category has nothing actionable, write "None today." under it.

---

BRIEFING:
{briefing}""",
        }],
    )
    actions_section = actions_msg.content[0].text.strip()

    return f"{tldr_section}\n\n{actions_section}"

def write_daily_log(tldr_and_actions: str, today: str, today_pretty: str):
    """Append TLDR to today's daily log in claude_projects."""
    log_path = DAILY_LOG_DIR / f"{today}.md"
    today_full = datetime.now().strftime("%B %d, %Y")

    # Extract just the TLDR section
    lines = tldr_and_actions.split("\n")
    tldr_lines = []
    in_tldr = False
    for line in lines:
        if line.strip() == "## TLDR":
            in_tldr = True
            continue
        if in_tldr and line.startswith("## "):
            break
        if in_tldr:
            tldr_lines.append(line)
    tldr = "\n".join(tldr_lines).strip()

    entry = f"""
---

## Morning Brief TLDR — {today_pretty}

{tldr}
"""

    if log_path.exists():
        with open(log_path, "a") as f:
            f.write(entry)
        print(f"  ✓ TLDR appended to {log_path.name}")
    else:
        log_path.write_text(f"# Daily Log — {today_full}\n{entry}")
        print(f"  ✓ Daily log created with TLDR: {log_path.name}")

def write_action_items(tldr_and_actions: str, today: str, today_pretty: str):
    """Append action items to the brief inbox."""
    if not BRIEF_INBOX_PATH.exists():
        print(f"  ⚠ brief-inbox.md not found at {BRIEF_INBOX_PATH}")
        return

    # Extract everything after ## Action Items
    action_block = ""
    if "## Action Items" in tldr_and_actions:
        action_block = tldr_and_actions.split("## Action Items", 1)[1].strip()

    if not action_block:
        print("  No action items to write.")
        return

    # Convert ### headers + bullets into flat inbox format
    entry = f"\n---\n\n### From Morning Brief — {today_pretty}\n"
    for line in action_block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            entry += f"\n**{stripped[4:]}**\n"
        elif stripped.startswith("- ") and "None today" not in stripped:
            entry += f"- [ ] {stripped[2:]}\n"

    with open(BRIEF_INBOX_PATH, "a") as f:
        f.write(entry)

    print(f"  ✓ Action items written to brief-inbox.md")

# ─── Event Extraction ────────────────────────────────────────────────────────

def extract_new_events(briefing: str) -> list:
    """Ask Claude to pull new trackable events from today's briefing."""
    if not AYX_EVENTS_PATH.exists():
        existing_names = []
    else:
        existing = json.loads(AYX_EVENTS_PATH.read_text())
        existing_names = [e["name"] for e in existing]

    client = anthropic.Anthropic()
    prompt = f"""Read this morning briefing and identify any upcoming events worth tracking publicly — conferences, product launches, IPOs, major regulatory milestones, or government decisions with hard deadlines.

ALREADY TRACKED (do not re-add these):
{json.dumps(existing_names, indent=2)}

Rules:
- Only include events with a specific date or a reasonable estimate (within ~3 months)
- Skip vague future references with no timeframe
- Skip duplicates or minor updates to already-tracked events
- If no new events qualify, return an empty array

Return ONLY a valid JSON array (no explanation, no markdown fences). Each object:
{{
  "id": "kebab-case-name-year",
  "name": "Event Name",
  "date": "YYYY-MM-DD",
  "date_end": "YYYY-MM-DD or null",
  "date_approximate": true or false,
  "event_type": "conference | launch | finance | regulatory",
  "location": "City, State / Online — or null",
  "categories": ["ai", "xr", "robotics", "space", "iot", "3d", "media"],
  "description": "1-2 sentence factual description.",
  "why_watching": "1 sentence on why this matters to AI/XR/spatial computing practitioners.",
  "new": true
}}

BRIEFING:
{briefing}"""

    print("  Checking briefing for new events...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        events = json.loads(raw)
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        print(f"  ⚠ Could not parse event JSON: {raw[:200]}")
        return []


def update_ayx_events(new_events: list, today: str):
    """Append new events to data/events.json and push to AYX."""
    if not new_events:
        print("  No new events found.")
        return
    if not AYX_EVENTS_PATH.exists():
        print(f"  ⚠ AYX events file not found at {AYX_EVENTS_PATH} — skipping")
        return

    existing = json.loads(AYX_EVENTS_PATH.read_text())
    existing_ids = {e["id"] for e in existing}
    added = [e for e in new_events if e["id"] not in existing_ids]

    if not added:
        print("  No new events to add (all already tracked).")
        return

    existing.extend(added)
    existing.sort(key=lambda e: e["date"])
    AYX_EVENTS_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"  ✓ Added {len(added)} new event(s): {[e['name'] for e in added]}")

    try:
        subprocess.run(["git", "-C", str(AYX_DIR), "add", "data/events.json"], check=True)
        subprocess.run(["git", "-C", str(AYX_DIR), "commit", "-m",
                        f"Events update {today} — {len(added)} new"], check=True)
        subprocess.run(["git", "-C", str(AYX_DIR), "push"], check=True)
        print("  ✓ Events pushed to AYX")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ Git push failed: {e}")


# ─── Error Logging & Alerting ─────────────────────────────────────────────────

EMAIL_RECIPIENT = "phoenixjenn@gmail.com"
EMAIL_SENDER    = "ClaudeCode9000@gmail.com"

def send_alert_email(step: str, exc: Exception, tb: str = "") -> None:
    """Send an urgent failure alert via Gmail. Never raises — only prints on failure."""
    import smtplib
    import traceback
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace("\xa0", "").replace(" ", "")
    if not password:
        print("  ⚠ GMAIL_APP_PASSWORD not set — skipping alert email")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"🚨 Morning Brief FAILED — {step} ({ts[:10]})"
    tb_escaped = tb.replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<html><body style="font-family:monospace;background:#1a1a1a;color:#e0e0e0;padding:20px;max-width:680px;margin:auto;">
<h2 style="color:#f87171;margin-top:0;">Morning Brief Job Failed</h2>
<table style="border-collapse:collapse;margin-bottom:16px;">
  <tr><td style="color:#888;padding-right:12px;">Step</td><td style="color:#fbbf24;">{step}</td></tr>
  <tr><td style="color:#888;padding-right:12px;">Error</td><td>{exc}</td></tr>
  <tr><td style="color:#888;padding-right:12px;">Time</td><td>{ts}</td></tr>
</table>
<pre style="background:#111;padding:12px;border-radius:6px;font-size:0.8em;color:#f97316;overflow-x:auto;">{tb_escaped}</pre>
<p style="color:#555;font-size:0.8em;">Log: {ERROR_LOG}<br>
Retry: <code style="color:#888;">python morning_brief.py</code></p>
</body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECIPIENT
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, password)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        print(f"  ✓ Alert email sent to {EMAIL_RECIPIENT}")
    except Exception as mail_err:
        print(f"  ⚠ Could not send alert email: {mail_err}")


def log_error(step: str, exc: Exception, today: str = "") -> None:
    """Append to errors.log, update status.json to 'error', and send alert email."""
    import traceback
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tb = traceback.format_exc()
    entry = f"\n{'='*52}\n[{ts}] STEP: {step}\nERROR: {exc}\n\nTRACEBACK:\n{tb}\n"
    with open(ERROR_LOG, "a") as f:
        f.write(entry)
    print(f"  ✗ ERROR in '{step}': {exc}")

    status_path = PROJECT_DIR / "status.json"
    try:
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
    except Exception:
        status = {}
    status.update({"status": "error", "error_step": step, "error": str(exc), "error_at": ts})
    if today:
        status["date"] = today
    try:
        status_path.write_text(json.dumps(status, indent=2))
    except Exception:
        pass

    send_alert_email(step, exc, tb)


# ─── Email Digest ─────────────────────────────────────────────────────────────

def generate_email_digest(briefing: str, today_pretty: str) -> tuple[str, str]:
    """Reformat the audio transcript into a scannable HTML email digest."""
    client = anthropic.Anthropic()

    prompt = f"""You are reformatting a spoken audio news briefing into a clean email newsletter digest.

The briefing was written to be *heard*, not read — no headers, flowing prose. Your job is to restructure it into a scannable digest with clear sections, bullet points, and bold key terms.

CRITICAL RULES — these override everything else:
1. COVER EVERY STORY. Do not drop any story from the transcript. If a section has 8 stories, include all 8. The reader listens to the podcast AND reads the email — omissions are noticed.
2. PRESERVE ALL SPECIFIC FACTS. Every dollar amount, percentage, user count, date, company name, and product name must appear in the email exactly as stated in the transcript. Never round, approximate, or omit a number.
3. KEEP THE ANALYTICAL CONCLUSIONS. The transcript's "so what" framing, competitive implications, and connective observations are the most valuable part. Do not strip them for brevity.
4. PRESERVE DIRECT QUOTES when the transcript includes them — they carry meaning that paraphrasing loses.
5. Do not pad — but do not compress at the cost of substance. Length is fine. Dropping facts is not.

OUTPUT FORMAT — return exactly this structure:

SUBJECT: [one-line subject, e.g. "Morning Brief — Monday, May 18"]

BODY:
[Clean HTML email body. Use inline styles only. Design guidelines:
- Max width 620px, centered, font-family: -apple-system, Arial, sans-serif, color: #1a1a1a
- Header: large bold title "MORNING BRIEF" + date in smaller gray text below + one line of coverage categories in small gray text: "General Tech · AI & ML · XR, Spatial & Spatial Internet · 3D Capture & Create · Robotics & AVs · IoT · Media"
- "TODAY'S TOP STORIES" section: gray background box (#f5f5f5), 4-5 must-read bullets, each starting with a bold term and including the key specific facts/numbers from the transcript
- Topic sections with ALL-CAPS headers in small gray text (AI & INDUSTRY, SPATIAL COMPUTING, ROBOTICS & AVs, 3D CAPTURE & CREATE, IOT, MEDIA & ENTERTAINMENT)
- Each section: ALL stories from that section as <p> tags. Each item: <strong>Company or Topic</strong> — 2-3 sentence summary preserving specific numbers and the analytical "so what"
- Closing "BIG PICTURE" section: 3-4 connective observations from the transcript, keeping the original editorial framing
- Footer: small gray text "Morning Brief · AI-curated · {today_pretty}"]

BRIEFING TRANSCRIPT:
{briefing}

Return SUBJECT line first, then BODY with full HTML."""

    print("  Generating email digest...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text

    subject = ""
    html    = ""
    if "SUBJECT:" in raw and "BODY:" in raw:
        subject = raw.split("SUBJECT:")[1].split("BODY:")[0].strip()
        html    = raw.split("BODY:")[1].strip()
    else:
        subject = f"Morning Brief — {today_pretty}"
        html    = raw

    return subject, html

def send_email_digest(subject: str, html_body: str):
    """Send the email digest via Gmail SMTP."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

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
    print(f"  ✓ Email digest sent to {EMAIL_RECIPIENT}")

# ─── Publish to GitHub ────────────────────────────────────────────────────────

def push_to_github():
    os.chdir(PROJECT_DIR)
    today = datetime.now().strftime("%Y-%m-%d")
    # MP3s live on R2 now — only push feed.xml and status.json
    subprocess.run(["git", "add", "feed.xml", "status.json"], check=True)
    subprocess.run(["git", "commit", "-m", f"Morning brief {today}"], check=True)
    subprocess.run(["git", "push"], check=True)
    print("  ✓ Published to GitHub Pages")

# ─── Main ─────────────────────────────────────────────────────────────────────

def upload_transcript_to_r2(path: Path):
    """Upload a .txt transcript to R2 for archiving."""
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (R2_PUBLIC_URL and access_key and secret_key):
        return
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )
    key = f"output/{path.name}"
    s3.upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": "text/plain"})
    print(f"  ✓ Transcript archived to R2: {key}")

def produce_episode(text: str, base_name: str, title: str):
    """TTS a briefing (splitting into parts if long), upload to R2, and add to the feed."""
    parts = split_briefing(text) if len(text.split()) > SPLIT_WORDS else [text]
    paths = []
    for i, part in enumerate(parts, 1):
        suffix     = f"-part{i}" if len(parts) > 1 else ""
        part_title = f"{title} (Part {i})" if len(parts) > 1 else title
        audio      = text_to_speech(part, OUTPUT_DIR / f"{base_name}{suffix}")
        print("\n☁️   Uploading to R2...")
        audio_url  = upload_to_r2(audio)
        update_podcast_feed(audio, part_title, audio_url)
        paths.append(audio)
    return paths

def main():
    today        = datetime.now().strftime("%Y-%m-%d")
    today_pretty = datetime.now().strftime("%A, %B %d")
    title        = f"Morning Brief — {today_pretty}"

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'─' * 52}")
    print(f"▶  Run started: {run_ts}")
    print(f"\n🎙  {title}")
    print("=" * 52)

    OUTPUT_DIR.mkdir(exist_ok=True)

    if (OUTPUT_DIR / f"brief-{today}.mp3").exists():
        print(f"  Brief for {today} already exists. Delete it to regenerate.")
        return

    # Check for active events
    active_events = get_active_events(today)
    if active_events:
        names = ", ".join(e["name"] for e in active_events)
        print(f"\n🎪  Active events: {names}")

    print("\n📡  Fetching articles...")
    seen_titles = load_seen_titles()
    all_articles, new_titles = fetch_articles(seen_titles)

    if not all_articles:
        for attempt in range(1, 6):
            print(f"  No articles found — retrying in 90s (attempt {attempt}/5)...")
            time.sleep(90)
            all_articles, new_titles = fetch_articles(seen_titles)
            if all_articles:
                break

    if not all_articles:
        print("  No articles found after retries. Check your network connection.")
        return

    # Partition articles on event days
    if active_events:
        regular_articles, event_articles = partition_articles(all_articles, active_events)
        if event_articles:
            total = sum(len(v) for v in event_articles.values())
            print(f"  → {total} articles routed to Special Brief")
    else:
        regular_articles, event_articles = all_articles, {}

    # ── Regular brief ──
    if regular_articles:
        print("\n✍️   Generating briefing with Claude...")
        try:
            briefing = generate_briefing(regular_articles)
        except Exception as e:
            log_error("generate_briefing", e, today)
            raise

        txt_path = OUTPUT_DIR / f"brief-{today}.txt"
        txt_path.write_text(briefing)
        try:
            upload_transcript_to_r2(txt_path)
        except Exception as e:
            log_error("upload_transcript_to_r2", e, today)
            print("  ⚠ R2 upload failed — continuing without cloud backup")

        words = len(briefing.split())
        print(f"  ✓ Transcript: {words:,} words (~{words // 145} min)")
        if words > SPLIT_WORDS:
            print(f"  ⚡ Over {SPLIT_WORDS:,} words — will split into two parts")

        print("\n🔍  Parsing briefing for TLDR + action items...")
        try:
            parsed = parse_briefing(briefing, today, today_pretty)
        except Exception as e:
            log_error("parse_briefing", e, today)
            raise
        (OUTPUT_DIR / f"brief-{today}-actions.md").write_text(parsed)

        print("\n📓  Writing to daily log and TODO...")
        try:
            write_daily_log(parsed, today, today_pretty)
            write_action_items(parsed, today, today_pretty)
        except Exception as e:
            log_error("write_daily_log/write_action_items", e, today)
            print("  ⚠ Log/inbox write failed — continuing")

        try:
            import watchlist_curator
            watchlist_curator.run(today)
        except Exception as e:
            log_error("watchlist_curator", e, today)
            print("  ⚠ Watchlist curator failed — continuing")

        print("\n🔀  Auto-triaging inbox items...")
        try:
            import auto_triage
            auto_triage.run(today)
        except Exception as e:
            log_error("auto_triage", e, today)
            print("  ⚠ Auto-triage failed — continuing")

        print("\n📅  Scanning for new events...")
        try:
            new_events = extract_new_events(briefing)
            update_ayx_events(new_events, today)
        except Exception as e:
            log_error("extract_new_events/update_ayx_events", e, today)
            print("  ⚠ Event extraction failed — continuing")

        print("\n📧  Generating and sending email digest...")
        try:
            subject, html_body = generate_email_digest(briefing, today_pretty)
            (OUTPUT_DIR / f"brief-{today}-email.html").write_text(html_body)
            send_email_digest(subject, html_body)
        except Exception as e:
            log_error("generate_email_digest/send_email_digest", e, today)
            print("  ⚠ Email digest failed — continuing")

        print("\n🔊  Converting to audio...")
        try:
            produce_episode(briefing, f"brief-{today}", title)
        except Exception as e:
            log_error("produce_episode", e, today)
            print(f"  ✗ Episode NOT published — {e}")
            print("  ⚠ Email digest was already sent; continuing to GitHub push without this episode")

    # ── Special brief ──
    if event_articles and active_events:
        event_names = ", ".join(e["name"] for e in active_events)
        special_title = f"Special Brief — {event_names}"
        slug = re.sub(r'[^a-z0-9-]', '', active_events[0]["name"].lower().replace(" ", "-"))[:24]

        print(f"\n🎤  Generating Special Brief for {event_names}...")
        try:
            special = generate_special_brief(event_articles, active_events, today_pretty)
        except Exception as e:
            log_error("generate_special_brief", e, today)
            raise

        special_txt = OUTPUT_DIR / f"special-{today}-{slug}.txt"
        special_txt.write_text(special)
        try:
            upload_transcript_to_r2(special_txt)
        except Exception as e:
            log_error("upload_transcript_to_r2 (special)", e, today)
            print("  ⚠ R2 upload failed — continuing")

        words = len(special.split())
        print(f"  ✓ Special Brief: {words:,} words (~{words // 145} min)")
        if words > SPLIT_WORDS:
            print(f"  ⚡ Over {SPLIT_WORDS:,} words — will split into two parts")

        print("\n🔊  Converting Special Brief to audio...")
        try:
            produce_episode(special, f"special-{today}-{slug}", special_title)
        except Exception as e:
            log_error("produce_episode (special)", e, today)
            print(f"  ✗ Special Brief episode NOT published — {e}")
            print("  ⚠ Continuing to GitHub push without this episode")

    print("\n📻  Feed updated")
    save_seen_titles(seen_titles | new_titles)

    # Write status.json for remote monitoring
    status = {
        "date": today,
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regular_brief": bool(regular_articles),
        "special_brief": bool(event_articles and active_events),
        "active_events": [e["name"] for e in active_events],
        "status": "ok",
    }
    (PROJECT_DIR / "status.json").write_text(json.dumps(status, indent=2))

    print("\n🧹  Purging old episodes...")
    try:
        purge_old_episodes()
    except Exception as e:
        log_error("purge_old_episodes", e, today)
        print("  ⚠ Purge failed — continuing")

    print("\n☁️   Publishing to GitHub...")
    try:
        push_to_github()
    except Exception as e:
        log_error("push_to_github", e, today)
        raise

    done_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n✅  Done at {done_ts}")
    print(f"    {GITHUB_PAGES_URL}/feed.xml\n")
    print(f"{'─' * 52}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # log_error already called inside main for known steps;
        # this catches any unhandled crash and ensures a final alert
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n{'='*52}\n[{ts}] UNHANDLED CRASH\n{tb}\n")
        send_alert_email("unhandled crash", e, tb)
        raise SystemExit(1)
