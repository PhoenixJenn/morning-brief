#!/usr/bin/env python3
"""
Auto-triage: runs after each morning brief, before items surface in the Inbox.

Two passes:
  Pass 1 — Name match (free): items mentioning a watchlist entity
           → prepend signal to entity note · mark checked · archive
  Pass 2 — Claude Haiku (one call): classify remaining items as
           new_entity → add to watchlist, mark checked, archive
           theme_signal → mark checked, archive
           inbox → leave unchecked (appears in Inbox tab)
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
INBOX_FILE   = CONTEXT_DIR / "brief-inbox.md"
WATCHLIST    = CONTEXT_DIR / "watchlist.md"
ARCHIVE_FILE = CONTEXT_DIR / "intel_archive.json"

VALID_CATEGORIES = [
    "AI Lab", "AI Researcher", "AI / XR / Platform", "AI / Platform / XR",
    "XR Hardware", "XR Optics", "XR Founder",
    "AV", "AV/Robotics", "Robotics",
    "Space/Finance", "3D Capture & Create", "Spatial Media", "Platform & Media",
]

# These subsections are handled by other parts of the pipeline — skip them
SKIP_SUBSECTIONS = {"Things to Try", "Blog Post Ideas", "Blog Ideas", "Upcoming Events"}


# ─── Watchlist readers ────────────────────────────────────────────────────────

def load_watchlist_entities() -> list[dict]:
    if not WATCHLIST.exists():
        return []
    text = WATCHLIST.read_text()
    section = re.search(r'## Companies & People\n([\s\S]+?)(?:\n---|\n## |$)', text)
    if not section:
        return []
    entities = []
    for line in section.group(1).split('\n'):
        if not line.startswith('|'):
            continue
        cols = [c.strip() for c in line.split('|')[1:-1]]
        if len(cols) < 6 or not cols[0] or cols[0] == 'Name' or re.match(r'^[-\s]+$', cols[0]):
            continue
        entities.append({
            'name':      cols[0],
            'category':  cols[1],
            'last_seen': cols[3],
            'note':      cols[5] if len(cols) > 5 else '',
        })
    return entities


def load_theme_names() -> list[str]:
    if not WATCHLIST.exists():
        return []
    return re.findall(r'^### (.+)', WATCHLIST.read_text(), re.MULTILINE)


# ─── Inbox helpers ────────────────────────────────────────────────────────────

def parse_unchecked_items(today: str) -> list[dict]:
    """Return today's unchecked inbox items, skipping auto-migrated subsections."""
    if not INBOX_FILE.exists():
        return []
    text = INBOX_FILE.read_text()
    items = []
    for section in re.split(r'\n---\n', text):
        date_m = re.search(r'### From Morning Brief — (.+)', section)
        if not date_m:
            continue
        brief_date_str = date_m.group(1).strip()
        brief_date_iso = _parse_date(brief_date_str, today)
        if brief_date_iso != today:
            continue
        current_sub = None
        for line in section.split('\n'):
            sub_m = re.match(r'^\*\*(.+)\*\*$', line.strip())
            if sub_m:
                current_sub = sub_m.group(1)
                continue
            m = re.match(r'^- \[ \] (.+)', line)
            if m and current_sub not in SKIP_SUBSECTIONS:
                items.append({
                    'text':           m.group(1).strip(),
                    'subsection':     current_sub,
                    'brief_date':     brief_date_str,
                    'brief_date_iso': brief_date_iso,
                })
    return items


def _parse_date(date_str: str, fallback: str) -> str:
    for fmt in ("%A, %B %d", "%B %d, %Y", "%A, %B %d, %Y"):
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return fallback


def mark_checked(item_text: str):
    if not INBOX_FILE.exists():
        return
    content = INBOX_FILE.read_text()
    INBOX_FILE.write_text(content.replace(f'- [ ] {item_text}', f'- [x] {item_text}', 1))


def append_archive(item: dict, action: str, **extra):
    archive = json.loads(ARCHIVE_FILE.read_text()) if ARCHIVE_FILE.exists() else []
    archive.append({**item, 'action': action,
                    'actioned_at': datetime.now().isoformat()[:10], **extra})
    ARCHIVE_FILE.write_text(json.dumps(archive, indent=2, ensure_ascii=False))


# ─── Watchlist writers ────────────────────────────────────────────────────────

def prepend_entity_note(entity_name: str, signal: str, brief_date_iso: str):
    """Prepend signal phrase to entity note. Backs up watchlist.md first."""
    if not WATCHLIST.exists():
        return
    text = WATCHLIST.read_text()
    WATCHLIST.with_suffix('.md.bak').write_text(text)
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if not line.startswith('|'):
            continue
        cols = line.split('|')
        if len(cols) < 7 or cols[1].strip() != entity_name:
            continue
        if brief_date_iso > cols[4].strip():
            cols[4] = f' {brief_date_iso} '
        existing = cols[6].strip()
        phrase   = signal[:80]
        cols[6]  = f' {phrase}{" · " + existing if existing else ""} '
        lines[i] = '|'.join(cols)
        break
    WATCHLIST.write_text('\n'.join(lines))


def add_entity_to_watchlist(name: str, category: str, note: str, brief_date_iso: str):
    """Append a new row to Companies & People."""
    if not WATCHLIST.exists():
        return
    text = WATCHLIST.read_text()
    WATCHLIST.with_suffix('.md.bak').write_text(text)
    new_row = f'| {name} | {category} | {brief_date_iso} | {brief_date_iso} | 1 | {note[:80]} |'
    marker  = '\n\n---\n\n## Recurring Themes'
    if marker in text:
        WATCHLIST.write_text(text.replace(marker, f'\n{new_row}{marker}', 1))


# ─── Triage passes ────────────────────────────────────────────────────────────

def pass1_name_match(
    items: list[dict], entities: list[dict], verbose: bool
) -> tuple[list[dict], list[dict]]:
    matched, unmatched = [], []
    for item in items:
        found = next(
            (e for e in entities if e['name'].lower() in item['text'].lower()), None
        )
        if found:
            prepend_entity_note(found['name'], item['text'], item['brief_date_iso'])
            mark_checked(item['text'])
            append_archive(item, 'auto_routed', matched_entity=found['name'])
            matched.append(item)
            if verbose:
                print(f'    · [{found["name"]}] {item["text"][:65]}')
        else:
            unmatched.append(item)
    return matched, unmatched


def pass2_haiku_classify(
    items: list[dict], entities: list[dict], themes: list[str], verbose: bool
) -> None:
    if not items:
        return

    client = anthropic.Anthropic()

    entity_list = '\n'.join(f'- {e["name"]} ({e["category"]})' for e in entities)
    theme_list  = '\n'.join(f'- {t}' for t in themes)
    item_lines  = '\n'.join(f'[{i}] {item["text"]}' for i, item in enumerate(items))
    cats        = ' | '.join(VALID_CATEGORIES)

    prompt = f"""You are triaging incoming tech news items for a personal intelligence tracker.

EXISTING WATCHLIST ENTITIES:
{entity_list}

EXISTING THEMES:
{theme_list}

INBOX ITEMS TO CLASSIFY:
{item_lines}

Classify each item as ONE of:
- new_entity: a specific company or person worth adding to the watchlist (not already listed above)
- theme_signal: fits an existing theme or is a broad trend with no single entity to track
- inbox: genuinely ambiguous — leave for human review

For new_entity items, also provide:
  name: the entity name
  category: exactly one of: {cats}
  note: ≤80 char signal phrase capturing why it matters

Return ONLY a JSON array (no markdown fences):
[
  {{"index": 0, "type": "new_entity", "name": "...", "category": "...", "note": "..."}},
  {{"index": 1, "type": "theme_signal"}},
  {{"index": 2, "type": "inbox"}}
]"""

    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = re.sub(r'```(?:json)?\n?', '', resp.content[0].text.strip()).strip('`')
        results = json.loads(raw)
    except Exception as exc:
        print(f'  ✗ Haiku classification failed: {exc}')
        return

    for result in results:
        idx = result.get('index')
        if idx is None or idx >= len(items):
            continue
        item  = items[idx]
        rtype = result.get('type')

        if rtype == 'new_entity':
            name     = result.get('name', '').strip()
            category = result.get('category', 'AI Lab')
            note     = result.get('note', item['text'])
            if name:
                add_entity_to_watchlist(name, category, note, item['brief_date_iso'])
                mark_checked(item['text'])
                append_archive(item, 'auto_added_entity', new_entity=name, category=category)
                if verbose:
                    print(f'    · new entity [{name} / {category}]: {note[:55]}')
        elif rtype == 'theme_signal':
            mark_checked(item['text'])
            append_archive(item, 'auto_theme')
            if verbose:
                print(f'    · theme → archive: {item["text"][:65]}')
        else:
            if verbose:
                print(f'    · inbox: {item["text"][:65]}')


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(today: str = None, verbose: bool = True):
    if not today:
        today = datetime.now().strftime('%Y-%m-%d')

    if verbose:
        print(f'\n🔀  Auto-triage — {today}')

    items = parse_unchecked_items(today)
    if not items:
        if verbose:
            print('  No unchecked items to triage')
        return

    entities = load_watchlist_entities()
    themes   = load_theme_names()

    if verbose:
        print(f'  {len(items)} items · {len(entities)} entities · {len(themes)} themes')
        print('  Pass 1 — name match…')

    matched, unmatched = pass1_name_match(items, entities, verbose)

    if verbose:
        print(f'  Pass 1: {len(matched)} routed')

    if unmatched:
        if verbose:
            print(f'  Pass 2 — Haiku classify ({len(unmatched)} items)…')
        pass2_haiku_classify(unmatched, entities, themes, verbose)

    if verbose:
        print('  ✓ Triage complete')


if __name__ == '__main__':
    date_arg = next((a for a in sys.argv[1:] if re.match(r'\d{4}-\d{2}-\d{2}', a)), None)
    run(date_arg, verbose=True)
