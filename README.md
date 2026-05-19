# Morning Brief

A personal AI news podcast that runs every morning before you wake up.

Every day at 6:30am, this script fetches hundreds of RSS articles, has Claude write a spoken briefing tailored to your interests, converts it to audio via OpenAI TTS, and publishes it as a private podcast you can subscribe to in Apple Podcasts (or any podcast app).

The result: a fresh 20-45 minute episode waiting in your feed every morning. No apps. No algorithms. No ads. Just the news you actually care about, in your chosen voice.

---

## How it works

```
RSS feeds → Claude (briefing script) → OpenAI TTS → MP3
                                                       ↓
                                         GitHub Pages (RSS feed)
                                                       ↓
                                         Apple Podcasts / Overcast / etc.
```

1. Pulls articles from ~30 RSS feeds across 7 topic categories
2. Filters out any article that appeared in a previous episode (deduplication via `output/seen-titles.json`)
3. Checks `events.json` for active conferences — if one is running, event articles are routed to a separate **Special Brief** episode
4. Sends remaining articles to Claude with a prompt tuned for spoken audio (not lists, not articles)
5. If either the regular or Special Brief exceeds ~8,500 words, it automatically splits into Part 1 and Part 2 as separate episodes so nothing gets cut
6. Converts to MP3 via OpenAI's TTS API and updates the RSS feed
7. Pushes everything to GitHub Pages — your podcast app picks it up automatically

---

## Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/) (Claude)
- An [OpenAI API key](https://platform.openai.com/) (text-to-speech)
- A GitHub account with a public repo (for GitHub Pages hosting)
- macOS with cron (for scheduled runs — Linux works too)

**Estimated cost:** ~$1-2/day using Claude Opus + OpenAI TTS at standard pricing. Roughly $30-45/month for a daily 30-45 minute episode.

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOURUSERNAME/morning-brief.git
cd morning-brief
pip3 install -r requirements.txt
```

### 2. Set your API keys

Add both keys to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export ANTHROPIC_API_KEY="your-anthropic-key"
export OPENAI_API_KEY="your-openai-key"
```

Then reload: `source ~/.zshrc`

### 3. Create a GitHub repo and enable GitHub Pages

1. Go to github.com → New repository → name it `morning-brief` → **Public**
2. Push this project to it:

```bash
git remote add origin https://github.com/YOURUSERNAME/morning-brief.git
git push -u origin main
```

3. In your repo: **Settings → Pages → Source: Deploy from branch → main → / (root)**

Your feed will live at `https://YOURUSERNAME.github.io/morning-brief/feed.xml`

### 4. Update the script with your GitHub URL

Open `morning_brief.py` and update this line near the top:

```python
GITHUB_PAGES_URL = "https://YOURUSERNAME.github.io/morning-brief"
```

### 5. Customize your topic feeds

The `FEEDS` dictionary in `morning_brief.py` defines what gets pulled. Edit it to match your interests — add, remove, or swap RSS URLs for any topic area.

The default categories are: General Tech, AI & Machine Learning, XR & Spatial Computing, 3D Scanning & Printing, Autonomous Vehicles & Robotics, IoT, and Media & Entertainment.

### 6. Customize the briefing prompt

Around line 160 in `morning_brief.py`, there's a `prompt` that tells Claude how to write the briefing. Update it to describe your role, interests, and what you want emphasized. This is where the personalization lives.

### 7. (Optional) Remove the personal log integration

Lines 284-412 write a TLDR and action items to a separate personal notes directory. If you don't use that setup, you can safely remove the `parse_briefing`, `write_daily_log`, and `write_action_items` functions and their calls in `main()`.

### 8. Test it

```bash
python3 morning_brief.py
```

First run takes 5-10 minutes (fetching + Claude API + TTS conversion for a long episode). You should see progress output as it goes.

### 9. Subscribe in Apple Podcasts

Once GitHub Pages is live (can take a few minutes after the first push):

1. Open Apple Podcasts
2. **File → Add a Show by URL**
3. Paste: `https://YOURUSERNAME.github.io/morning-brief/feed.xml`

### 10. Schedule it to run every morning

```bash
crontab -e
```

Add these lines (the first two pass your API keys into the cron environment):

```
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
30 6 * * * /usr/bin/python3 /path/to/morning-brief/morning_brief.py >> /path/to/morning-brief/output/cron.log 2>&1
```

Change `30 6` to whatever time you want (24-hour format). The API keys must be set here explicitly — cron doesn't read your shell profile.

---

## Special Briefs and the event calendar

On days when a major conference is active, the script produces two episodes instead of one:

- **Regular Brief** — all the day's news, minus conference coverage
- **Special Brief** — a dedicated episode covering only that event's announcements, with a focused prompt that prioritizes completeness over length. No word cap. If there's a lot to cover, it splits into Part 1 and Part 2 rather than leaving anything out.

Both land in your podcast feed automatically. On a big keynote day like Google I/O or WWDC, you might wake up to three episodes: the regular brief, and a two-part Special Brief covering every announcement.

### Adding and editing events

`events.json` in the project root defines the conference calendar. Each entry needs a name, start/end dates, and keywords used to match articles:

```json
{
  "name": "WWDC 2026",
  "start": "2026-06-08",
  "end": "2026-06-12",
  "keywords": ["WWDC", "Apple developer", "visionOS"]
}
```

The script watches a window of 1 day before the start date through 2 days after the end date, so pre-event coverage and recaps are included.

The repo ships pre-populated with CES, MWC, GDC, SXSW, Nvidia GTC, Google I/O, WWDC, AWE, NAB, SIGGRAPH, Qualcomm Snapdragon Summit, Adobe MAX, Samsung Unpacked, Meta Connect, and AWS re:Invent. **Dates beyond mid-2026 are approximate placeholders** — update them each fall when schedules are confirmed.

---

## Configuration options

All tunable settings are at the top of `morning_brief.py`:

| Setting | Default | Notes |
|---|---|---|
| `TARGET_WORDS` | 7000 | ~45 min at 1.4x speed. Lower for a shorter episode. |
| `LOOKBACK_HRS` | 26 | How far back to pull articles. 26 catches late-night posts. |
| `MAX_PER_FEED` | 10 | Max articles per RSS feed. |
| `TTS_VOICE` | `nova` | OpenAI voices: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |
| `TTS_MODEL` | `tts-1` | `tts-1-hd` is higher quality at ~2x the cost |
| `TTS_SPEED` | 1.4 | 1.0 = normal pace. 1.4 fills a commute without feeling rushed. |

---

## Troubleshooting

**Authentication error at runtime:** Your API keys aren't in the environment. Double-check `echo $ANTHROPIC_API_KEY` returns a value. In cron, set the keys explicitly at the top of your crontab (see step 10).

**GitHub push fails:** Make sure the remote is set correctly: `git remote -v`. Also confirm GitHub Pages is enabled in your repo settings.

**No articles showing up:** Some feeds block automated fetching. The script skips failures silently — this is normal. Check the feed URLs work in a browser.

**Episode already exists:** The script won't overwrite an existing episode. Delete `output/brief-YYYY-MM-DD.mp3` to regenerate.

**Too many articles being skipped:** The deduplication file (`output/seen-titles.json`) grows over time. If you want to reset it and allow all articles through again, delete the file — it will be recreated on the next run.

**Cron ran but nothing happened:** Check `output/cron.log` for error output.

---

## Bonus: NFC trigger for your morning commute

Once your podcast is set up, you can make it launch automatically when you walk out the door — no tapping, no unlocking your phone.

**What you need:** Any NFC-enabled object. Old hotel keycards work perfectly. So do Disney MagicBands, transit cards, or cheap NFC stickers from Amazon (~$10 for a pack of 20).

### Step 1 — Create a Morning Commute shortcut

1. Open the **Shortcuts** app on your iPhone
2. Tap **+** to create a new shortcut, name it "Morning Commute"
3. Add actions in order:
   - **Get Current Location** (optional — makes the next step smarter)
   - **Open App** → Maps (or add a **Get Directions** action to your commute destination)
   - **Open App** → Podcasts
4. Save it

When you run this shortcut, Maps opens with your commute and Podcasts opens to whatever was last playing — which will be that morning's episode.

### Step 2 — Set up the NFC automation

1. In Shortcuts, go to the **Automation** tab
2. Tap **+** → **Personal Automation** → **NFC**
3. Tap **Scan** and hold your keycard or MagicBand to the top of your iPhone
4. Name it (e.g. "Morning Commute Tag")
5. Add action: **Run Shortcut** → select "Morning Commute"
6. Turn off **Ask Before Running** so it fires instantly

### Step 3 — Place your tag

Stick the keycard or NFC sticker somewhere you'll naturally brush your phone past on the way out — by the door, on your bag, on the car dashboard. Tap once, commute starts.

---

## License

MIT. Build your own, make it yours.
