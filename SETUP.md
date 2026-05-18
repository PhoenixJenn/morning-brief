# Morning Brief — Setup Guide

## Step 1 — Install dependencies

```bash
cd ~/Projects/morning-brief
pip3 install -r requirements.txt
```

## Step 2 — Set your Anthropic API key

The script uses the same API key as Claude Code, so this is probably already set.
Check with:

```bash
echo $ANTHROPIC_API_KEY
```

If it's empty, add this to your `~/.zshrc`:

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

Then run `source ~/.zshrc`.

## Step 3 — Create the GitHub repo

1. Go to github.com → New repository
2. Name it `morning-brief`
3. Set it to **Public** (required for free GitHub Pages)
4. Don't add any files yet

Then in Terminal:

```bash
cd ~/Projects/morning-brief
git init
git remote add origin https://github.com/YOURUSERNAME/morning-brief.git
git add .
git commit -m "Initial setup"
git push -u origin main
```

## Step 4 — Enable GitHub Pages

1. Go to your repo on github.com
2. Settings → Pages
3. Source: **Deploy from a branch** → `main` → `/ (root)`
4. Save — your site will be at `https://YOURUSERNAME.github.io/morning-brief`

## Step 5 — Update the script with your GitHub URL

Open `morning_brief.py` and update this line:

```python
GITHUB_PAGES_URL = "https://YOURUSERNAME.github.io/morning-brief"
```

Replace `YOURUSERNAME` with your actual GitHub username.

## Step 6 — Test it

```bash
cd ~/Projects/morning-brief
python3 morning_brief.py
```

First run takes ~5-8 minutes (audio conversion is slow for 45 min of content).

## Step 7 — Subscribe in Apple Podcasts

Once GitHub Pages is live (can take up to 10 minutes after first push):

1. Open Apple Podcasts
2. File → Add a Show by URL...
3. Paste: `https://YOURUSERNAME.github.io/morning-brief/feed.xml`

## Step 8 — Schedule it to run every morning

Open Terminal and run:

```bash
crontab -e
```

Add this line (runs at 6:30am daily):

```
30 6 * * * cd /Users/jennlee/Projects/morning-brief && /usr/bin/python3 morning_brief.py >> /Users/jennlee/Projects/morning-brief/output/cron.log 2>&1
```

Save and exit (`:wq` in vim).

## Step 9 — Optional: Upgrade the voice

For a much better voice quality:
1. System Settings → Accessibility → Spoken Content → System Voice → Manage Voices
2. Download **Ava (Enhanced)** or **Zoe (Enhanced)**
3. In `morning_brief.py`, change:
   ```python
   TTS_VOICE = "Ava (Enhanced)"
   ```

## Step 10 — iPhone Shortcut (NFC trigger)

1. Open **Shortcuts** on iPhone
2. Create a new shortcut: **Open URL** → `podcast://YOURUSERNAME.github.io/morning-brief/feed.xml`
3. Or simpler: **Open App** → Podcasts (opens to last played)
4. Go to **Automation** → add NFC trigger → tap your MagicBand by the door → run the shortcut

---

## Troubleshooting

**`say` voice not found:** Run `say -v '?'` to list available voices. Use any voice name exactly as shown.

**GitHub push fails:** Make sure you've run `git remote add origin ...` with your actual username.

**No articles showing up:** Some feeds block automated fetching. The script skips those silently.

**Audio file too large for GitHub:** GitHub has a 100MB limit per file. A 45-min M4A at reasonable quality is ~25-40MB — should be fine.
