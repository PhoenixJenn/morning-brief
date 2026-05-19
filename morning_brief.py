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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import anthropic
from openai import OpenAI

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR       = Path(__file__).parent
OUTPUT_DIR        = PROJECT_DIR / "output"
RSS_FILE          = PROJECT_DIR / "feed.xml"
SEEN_TITLES_FILE  = OUTPUT_DIR / "seen-titles.json"
EVENTS_FILE       = PROJECT_DIR / "events.json"

PODCAST_TITLE       = "Morning Brief"
PODCAST_DESCRIPTION = "AI-curated daily tech briefing — spatial computing, AI, XR, media, and more."
GITHUB_PAGES_URL    = "https://PhoenixJenn.github.io/morning-brief"

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
    "XR, Spatial Computing & Immersive Tech": [
        "https://www.roadtovr.com/feed/",
        "https://uploadvr.com/feed/",
        "https://arinsider.co/feed/",
        "https://www.sciencedaily.com/rss/computers_math/virtual_reality.xml",
    ],
    "3D Scanning, Printing & Spatial Internet": [
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
        start = date.fromisoformat(event["start"]) - timedelta(days=1)
        end   = date.fromisoformat(event.get("end", event["start"])) + timedelta(days=2)
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

    today_pretty = datetime.now().strftime("%A, %B %d, %Y")

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
3. XR, Spatial Computing & Immersive Tech — important professionally; cover substantively
4. 3D Scanning, Printing & Spatial Internet — include anything on Niantic Scaniverse, Creality, xTool, world models, digital twins, spatial internet
5. Autonomous Vehicles, Robotics & Humanoid Robots — 2-3 stories; humanoid robots (Figure, Tesla Optimus, Boston Dynamics, etc.) are of high interest
6. IoT & Connected Devices — 2-3 stories; smart home, industrial IoT, connected devices
7. Media & Entertainment — 2-3 stories

EDITORIAL RULES:
- Skip rumors with no substance, listicles, and "X does something minor" non-stories
- If a section had no meaningful news today, say so in one sentence and move on
- Prioritize stories with real implications over press releases

TODAY'S ARTICLES:
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
    target, cumulative = total // 2, 0
    split_at = len(sentences) // 2
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

# ─── Podcast RSS Feed ─────────────────────────────────────────────────────────

def update_podcast_feed(audio_path: Path, title: str):
    pub_date  = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    file_size = audio_path.stat().st_size
    audio_url = f"{GITHUB_PAGES_URL}/output/{audio_path.name}"
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

def parse_briefing(briefing: str, today: str, today_pretty: str) -> dict:
    """Extract TLDR and action items from the briefing via Claude."""
    client = anthropic.Anthropic()

    prompt = f"""Read this morning news briefing and extract two things:

1. A TLDR — 8 to 12 bullet points covering the most important stories. Be specific and concrete. No vague summaries.

2. Action items across these categories. Only include items that are genuinely actionable — skip anything vague:
   - **Upcoming Events** — conferences, product launches, keynotes, release dates worth putting on the radar
   - **Things to Try** — new features, tools, apps, or functionality worth testing (e.g. Meta's 2D-to-3D photo, a new AI tool, a product update)
   - **Stories to Follow** — ongoing developments worth tracking over the next week or two
   - **Blog Post Ideas** — angles worth writing about on a spatial computing / emerging tech blog
   - **People & Companies to Watch** — names worth adding to a watchlist
   - **Other** — anything actionable that doesn't fit above

Format your response EXACTLY like this (use these exact headers):

## TLDR
- bullet
- bullet

## Action Items

### Upcoming Events
- item (source: where this came from)

### Things to Try
- item

### Stories to Follow
- item

### Blog Post Ideas
- item

### People & Companies to Watch
- item

### Other
- item

If a category has nothing actionable, write "None today." under it.

---

BRIEFING:
{briefing}"""

    print("  Parsing briefing for action items...")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

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
    """Append action items to claude_projects TODO.md."""
    if not TODO_PATH.exists():
        print(f"  ⚠ TODO.md not found at {TODO_PATH}")
        return

    # Extract everything after ## Action Items
    action_block = ""
    if "## Action Items" in tldr_and_actions:
        action_block = tldr_and_actions.split("## Action Items", 1)[1].strip()

    if not action_block:
        print("  No action items to write.")
        return

    # Convert ### headers + bullets into flat TODO format
    entry = f"\n### From Morning Brief — {today_pretty}\n"
    for line in action_block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            entry += f"\n**{stripped[4:]}**\n"
        elif stripped.startswith("- ") and "None today" not in stripped:
            entry += f"- [ ] {stripped[2:]}\n"

    # Insert before the Rainy Day Projects section (or at end)
    content = TODO_PATH.read_text()
    if "## Rainy Day Projects" in content:
        content = content.replace("## Rainy Day Projects", entry + "\n## Rainy Day Projects")
    else:
        content += entry

    TODO_PATH.write_text(content)
    print(f"  ✓ Action items written to TODO.md")

# ─── Publish to GitHub ────────────────────────────────────────────────────────

def push_to_github():
    os.chdir(PROJECT_DIR)
    today = datetime.now().strftime("%Y-%m-%d")
    subprocess.run(["git", "add", "output/", "feed.xml"], check=True)
    subprocess.run(["git", "commit", "-m", f"Morning brief {today}"], check=True)
    subprocess.run(["git", "push"], check=True)
    print("  ✓ Published to GitHub Pages")

# ─── Main ─────────────────────────────────────────────────────────────────────

def produce_episode(text: str, base_name: str, title: str):
    """TTS a briefing (splitting into parts if long) and add to the feed. Returns audio paths."""
    parts = split_briefing(text) if len(text.split()) > SPLIT_WORDS else [text]
    paths = []
    for i, part in enumerate(parts, 1):
        suffix     = f"-part{i}" if len(parts) > 1 else ""
        part_title = f"{title} (Part {i})" if len(parts) > 1 else title
        audio      = text_to_speech(part, OUTPUT_DIR / f"{base_name}{suffix}")
        update_podcast_feed(audio, part_title)
        paths.append(audio)
    return paths

def main():
    today        = datetime.now().strftime("%Y-%m-%d")
    today_pretty = datetime.now().strftime("%A, %B %d")
    title        = f"Morning Brief — {today_pretty}"

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
        print("  No articles found. Check your network connection.")
        return

    save_seen_titles(seen_titles | new_titles)

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
        briefing = generate_briefing(regular_articles)

        (OUTPUT_DIR / f"brief-{today}.txt").write_text(briefing)
        words = len(briefing.split())
        print(f"  ✓ Transcript: {words:,} words (~{words // 145} min)")
        if words > SPLIT_WORDS:
            print(f"  ⚡ Over {SPLIT_WORDS:,} words — will split into two parts")

        print("\n🔍  Parsing briefing for TLDR + action items...")
        parsed = parse_briefing(briefing, today, today_pretty)
        (OUTPUT_DIR / f"brief-{today}-actions.md").write_text(parsed)

        print("\n📓  Writing to daily log and TODO...")
        write_daily_log(parsed, today, today_pretty)
        write_action_items(parsed, today, today_pretty)

        print("\n🔊  Converting to audio...")
        produce_episode(briefing, f"brief-{today}", title)

    # ── Special brief ──
    if event_articles and active_events:
        event_names = ", ".join(e["name"] for e in active_events)
        special_title = f"Special Brief — {event_names}"
        slug = active_events[0]["name"].lower().replace(" ", "-")[:24]

        print(f"\n🎤  Generating Special Brief for {event_names}...")
        special = generate_special_brief(event_articles, active_events, today_pretty)

        (OUTPUT_DIR / f"special-{today}-{slug}.txt").write_text(special)
        words = len(special.split())
        print(f"  ✓ Special Brief: {words:,} words (~{words // 145} min)")
        if words > SPLIT_WORDS:
            print(f"  ⚡ Over {SPLIT_WORDS:,} words — will split into two parts")

        print("\n🔊  Converting Special Brief to audio...")
        produce_episode(special, f"special-{today}-{slug}", special_title)

    print("\n📻  Feed updated")
    print("\n☁️   Publishing to GitHub...")
    push_to_github()

    print(f"\n✅  Done! Subscribe in Apple Podcasts:")
    print(f"    {GITHUB_PAGES_URL}/feed.xml\n")

if __name__ == "__main__":
    main()
