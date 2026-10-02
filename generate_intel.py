#!/usr/bin/env python3
"""
generate_intel.py — Regenerates the four Intel pages in augmentyourexperience-www/intel/
from four sources of truth:
  - context/watchlist.md         (entities + signal counts + notes; recurring themes)
  - context/watchlist_meta.json  (entity_type, slugs, category tags, affiliation)
  - context/entity_tracker.json  (authoritative signal counts, last_seen, per-mention history)
  - deep-dive-feed.xml + output/deepdive-*.txt  (Deep Dive podcast episodes + transcripts)

Pages:
  - intel/index.html      Recurring Themes (short cards)
  - intel/deep-dive.html  Deep Dive — podcast episodes + full dated history per theme
  - intel/companies.html  Companies — list + click-through mention timeline
  - intel/people.html     People    — list + click-through mention timeline

Commits and pushes all four files to AYX after every successful regeneration.

Run standalone:
  python generate_intel.py

Called automatically by watchlist_curator.py after promotions.
"""

import hashlib
import json
import re
import subprocess
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import anthropic

PROJECT_DIR     = Path(__file__).parent
CONTEXT_DIR     = PROJECT_DIR.parent / "claude_projects" / "context"
AYX_DIR         = PROJECT_DIR.parent / "augmentyourexperience-www"
INTEL_DIR       = AYX_DIR / "intel"
OUTPUT_DIR      = PROJECT_DIR / "output"
DEEPDIVE_FEED   = PROJECT_DIR / "deep-dive-feed.xml"
CLASSIFICATION_CACHE_FILE = PROJECT_DIR / "deepdive_classification_cache.json"

THEMES_HTML    = INTEL_DIR / "index.html"
DEEPDIVE_HTML  = INTEL_DIR / "deep-dive.html"
COMPANIES_HTML = INTEL_DIR / "companies.html"
PEOPLE_HTML    = INTEL_DIR / "people.html"

WATCHLIST_MD = CONTEXT_DIR / "watchlist.md"
META_FILE    = CONTEXT_DIR / "watchlist_meta.json"
TRACKER_FILE = CONTEXT_DIR / "entity_tracker.json"

PERSON_CATEGORY_KEYWORDS = {
    "researcher", "founder", "ceo", "cto", "executive", "director",
    "vp", "president", "scientist", "lead", "officer",
}

CATEGORY_TAG_MAP = {
    "ai":                    ["ai"],
    "ai lab":                ["ai"],
    "ai / xr / platform":    ["ai", "xr"],
    "ai researcher":         ["ai"],
    "xr":                    ["xr"],
    "xr hardware":           ["xr"],
    "xr optics":             ["xr"],
    "xr founder":            ["xr"],
    "spatial media":         ["xr"],
    "av":                    ["robotics"],
    "av/robotics":           ["robotics"],
    "robotics":              ["robotics"],
    "3d capture & create":   ["3d"],
    "iot":                   ["iot"],
    "media & entertainment": ["media"],
}

# Single-word fallback for combo/reordered category labels that don't exactly
# match CATEGORY_TAG_MAP above (e.g. "AI / Platform / XR" — same words as the
# "ai / xr / platform" key but reordered, so the exact-match lookup misses it;
# "Platform & Media" was never a whole-phrase key at all). "Platform", "space",
# and "finance" intentionally have no target — there's no dedicated category
# for them in this 6-tag scheme, so they're dropped rather than guessed at.
CATEGORY_TOKEN_MAP = {
    "ai": "ai", "xr": "xr", "robotics": "robotics", "av": "robotics",
    "3d": "3d", "iot": "iot", "media": "media",
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ─── Theme parsing ────────────────────────────────────────────────────────────

def classify_categories(text: str) -> list[str]:
    """Keyword-based category tags for a block of text, no fallback — an empty
    list means nothing matched (used for Deep Dive podcast paragraphs, where
    forcing unmatched boilerplate like an intro/outro line into "ai" would
    pollute the bucket)."""
    text = text.lower()

    def has_word(*words):
        # Word-boundary match for short/ambiguous tokens ("ai", "ar", "vr", "av")
        # that would otherwise false-positive as substrings of ordinary words —
        # e.g. "ar " naively matches "tabular ", "similar ", "familiar ".
        return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)

    cats = []
    if has_word("ai") or any(w in text for w in ["llm", "model", "openai", "anthropic", "spending", "capex", "coding", "agent", "infrastructure", "publishing", "backlash", "layoff", "productivity"]):
        cats.append("ai")
    if has_word("xr", "ar", "vr") or any(w in text for w in ["glasses", "spatial", "headset", "optics", "wearable", "smart glasses"]):
        cats.append("xr")
    if has_word("av") or any(w in text for w in ["robot", "humanoid", "autonomous", "waymo", "self-driving"]):
        cats.append("robotics")
    if any(w in text for w in ["3d print", "manufactur", "maker", "filament", "bambu", "prusa"]):
        cats.append("3d")
    if any(w in text for w in ["media", "publish", "content", "studio", "youtube", "streaming"]):
        cats.append("media")
    return cats


def infer_theme_category(name: str, body: str) -> str:
    cats = classify_categories(name + " " + body)
    return " ".join(cats) if cats else "ai"


def parse_themes() -> list[dict]:
    if not WATCHLIST_MD.exists():
        return []
    text = WATCHLIST_MD.read_text()
    m = re.search(r"## Recurring Themes\n(.*)", text, re.DOTALL)
    if not m:
        return []

    themes = []
    for block in re.split(r"\n(?=### )", m.group(1).strip()):
        block = block.strip()
        if not block.startswith("### "):
            continue
        lines  = block.split("\n")
        name   = lines[0][4:].strip()
        meta   = lines[1] if len(lines) > 1 else ""
        body   = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""

        appearances = ""
        m2 = re.search(r"\*\*Appearances:\*\*\s*([^\s|]+\s*(?:briefs)?)", meta)
        if m2:
            appearances = m2.group(1).strip()

        first_seen = ""
        m3 = re.search(r"\*\*First seen:\*\*\s*(\d{4}-\d{2}-\d{2})", meta)
        if m3:
            first_seen = m3.group(1)

        # Last updated = most recent "YYYY-MM-DD: ..." note prepended to the body
        # by an applied theme-suggestion update, or First Seen if it's never had one.
        dated_notes = re.findall(r"(?m)^(\d{4}-\d{2}-\d{2}):", body)
        last_updated = max(dated_notes) if dated_notes else first_seen

        themes.append({
            "name": name, "appearances": appearances, "body": body,
            "last_updated": last_updated,
        })
    return themes


def build_theme_card(theme: dict) -> str:
    name     = esc(theme["name"])
    category = infer_theme_category(theme["name"], theme.get("body", ""))
    meta_html = (
        f'<span style="font-size:0.72rem;color:var(--text-muted,#888);white-space:nowrap;">'
        f'{esc(theme["appearances"])}</span>'
        if theme.get("appearances") else ""
    )
    if theme.get("last_updated"):
        meta_html += (
            f'\n            <span style="font-size:0.72rem;color:var(--text-muted,#666);white-space:nowrap;">'
            f'Last updated: {esc(theme["last_updated"])}</span>'
        )

    # Split body on blank lines → separate <p> tags
    paras = [p.strip().replace("\n", " ") for p in re.split(r"\n{2,}", theme.get("body", "")) if p.strip()]
    if not paras:
        paras = [""]
    para_html = "\n".join(
        f'          <p style="font-size:0.875rem;color:var(--text-muted,#aaa);line-height:1.65;'
        f'margin:{"0" if i == len(paras)-1 else "0 0 0.65rem"};">{esc(p)}</p>'
        for i, p in enumerate(paras)
    )

    return (
        f'        <div class="intel-card" data-category="{category}" '
        f'style="border:1px solid var(--border,#222);border-radius:8px;padding:1.25rem 1.5rem;">\n'
        f'          <div style="display:flex;align-items:baseline;gap:1rem;margin-bottom:0.5rem;">\n'
        f'            <span style="font-size:1rem;font-weight:700;color:var(--text-primary,#fff);">{name}</span>\n'
        f'            {meta_html}\n'
        f'          </div>\n'
        f'{para_html}\n'
        f'        </div>'
    )


def split_dated_entries(body: str) -> tuple[list[dict], str]:
    """Split a theme body into dated entries (one per prepended "YYYY-MM-DD: ..."
    line, already newest-first since updates are prepended) plus the trailing
    undated background/synthesis text, if any."""
    entries = []
    rest = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2}):\s*(.*)", line)
        if m:
            entries.append({"date": m.group(1), "text": m.group(2).strip()})
        else:
            rest.append(line)
    return entries, " ".join(rest).strip()


# ─── Deep Dive podcast episodes ────────────────────────────────────────────────

def parse_deepdive_episodes() -> list[dict]:
    """Reads deep-dive-feed.xml for episode metadata (title, date, audio url, guid)
    and pairs each with its transcript from output/<slug>.txt, where <slug> is the
    mp3 filename stem (e.g. deepdive-2026-09-30, deepdive-catchup-2026-09-30)."""
    if not DEEPDIVE_FEED.exists():
        return []

    root = ElementTree.fromstring(DEEPDIVE_FEED.read_text())
    episodes = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        pubdate_el = item.find("pubDate")
        enclosure_el = item.find("enclosure")
        guid_el = item.find("guid")
        if title_el is None or enclosure_el is None or guid_el is None:
            continue

        audio_url = enclosure_el.get("url", "")
        m = re.search(r"/([^/]+)\.mp3$", audio_url)
        slug = m.group(1) if m else slugify(title_el.text or "")

        transcript_path = OUTPUT_DIR / f"{slug}.txt"
        transcript_text = transcript_path.read_text() if transcript_path.exists() else ""
        transcript = [p.strip().replace("\n", " ") for p in re.split(r"\n{2,}", transcript_text) if p.strip()]

        date = ""
        if pubdate_el is not None and pubdate_el.text:
            try:
                date = parsedate_to_datetime(pubdate_el.text).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                date = ""

        episodes.append({
            "id":        guid_el.text,
            "title":     title_el.text or slug,
            "date":      date,
            "audio_url": audio_url,
            "transcript": transcript,
        })

    episodes.sort(key=lambda e: e["date"], reverse=True)
    return episodes


# ─── Deep Dive page: topics (category) → subcategories → dated stories ────────
# Groups both Recurring Theme notes and Deep Dive podcast paragraphs under a
# fixed two-level taxonomy. Each story (a theme's dated note, its undated
# background paragraph, or one podcast paragraph) is classified into exactly
# one category + one subcategory by a cached Haiku call — keyword matching was
# tried first and consistently misclassified nuanced prose (e.g. "tabular"
# matching the "ar" glasses keyword), so this needs real judgment, not regex.

CATEGORY_ORDER = ["ai", "xr", "robotics", "iot", "3d", "media", "misc"]
CATEGORY_LABELS = {
    "ai":       "AI",
    "xr":       "XR & Spatial",
    "robotics": "Robotics & AV",
    "iot":      "IoT & Connected Devices",
    "3d":       "3D Capture & Create",
    "media":    "Media & Entertainment",
    "misc":     "Misc",
}
SUBCATEGORY_TAXONOMY = {
    "ai": [
        "Model Releases", "Architecture & Patterns", "Memory & Context", "Frameworks & Tooling",
        "Infrastructure & Compute", "Safety & Security", "Business & Economics", "Policy & Regulation", "Misc",
    ],
    "xr": ["Hardware & Form Factor", "World Models & Spatial AI", "Platform & Ecosystem", "Business & Deals", "Misc"],
    "robotics": ["Humanoid Robotics", "Autonomous Vehicles", "World Models for Robotics", "Business & Deployment", "Misc"],
    "iot": ["Smart Home & Wearables", "Industrial IoT", "Connectivity & Protocols", "Misc"],
    "3d": ["Capture & Scanning", "Creation Tools", "Printing & Fabrication", "Misc"],
    "media": ["Content & Publishing", "Entertainment & Gaming", "Creator Tools", "Misc"],
    "misc": ["Misc"],
}


def collect_classifiable_items(themes: list, episodes: list) -> list[dict]:
    """Flattens theme dated entries/context and episode paragraphs into a single
    list of {id, text, hint, date, source_type, source_label, source_url?,
    is_context?} — id is a stable hash/key so repeat runs only classify new
    content."""
    items = []
    for theme in themes:
        entries, context = split_dated_entries(theme.get("body", ""))
        for e in entries:
            iid = "theme:" + hashlib.md5(f'{theme["name"]}|{e["date"]}|{e["text"]}'.encode()).hexdigest()[:16]
            items.append({
                "id": iid, "text": e["text"], "hint": theme["name"], "date": e["date"],
                "source_type": "theme", "source_label": theme["name"],
            })
        if context:
            iid = "theme-ctx:" + hashlib.md5(f'{theme["name"]}|{context}'.encode()).hexdigest()[:16]
            items.append({
                "id": iid, "text": context, "hint": theme["name"], "date": theme.get("last_updated") or "",
                "source_type": "theme", "source_label": theme["name"], "is_context": True,
            })
    for ep in episodes:
        for idx, para in enumerate(ep.get("transcript", [])):
            items.append({
                "id": f"ep:{ep['id']}:{idx}", "text": para, "hint": ep["title"], "date": ep.get("date", ""),
                "source_type": "episode", "source_label": ep["title"], "source_url": ep.get("audio_url", ""),
            })
    return items


def load_classification_cache() -> dict:
    if not CLASSIFICATION_CACHE_FILE.exists():
        return {}
    return json.loads(CLASSIFICATION_CACHE_FILE.read_text())


def classify_stories(items: list[dict], cache: dict, verbose: bool = True) -> dict:
    """Classifies any items not already in `cache` into {category, subcategory}
    via batched Haiku calls, persists the updated cache, and returns it."""
    todo = [it for it in items if it["id"] not in cache]
    if not todo:
        return cache

    client = anthropic.Anthropic()
    taxonomy_desc = "\n".join(
        f'{c} ({CATEGORY_LABELS[c]}): ' + ", ".join(SUBCATEGORY_TAXONOMY[c])
        for c in CATEGORY_ORDER
    )

    BATCH = 25
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        lines = "\n".join(f'{j}. [{b["hint"]}] {b["text"][:400]}' for j, b in enumerate(batch))
        prompt = f"""Classify each numbered story below into exactly one category and one subcategory.

Valid categories and their subcategories (format: category_key (Label): subcategory, subcategory, ...):
{taxonomy_desc}

Use the "Misc" subcategory only when nothing else genuinely fits, and the "misc" category only when a story
doesn't belong in AI, XR & Spatial, Robotics & AV, IoT & Connected Devices, 3D Capture & Create, or Media & Entertainment at all.

Stories:
{lines}

Return only a JSON array, one element per story, in the same order, using the exact category_key and subcategory string:
[{{"category": "ai", "subcategory": "Model Releases"}}, ...]"""

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = re.sub(r"```(?:json)?\n?", "", resp.content[0].text).strip().strip("`")
            results = json.loads(raw)
        except Exception as exc:
            if verbose:
                print(f"  [intel] classify_stories batch failed: {exc}")
            results = []

        for j, b in enumerate(batch):
            r = results[j] if j < len(results) and isinstance(results[j], dict) else {}
            cat = r.get("category") if r.get("category") in CATEGORY_ORDER else "misc"
            sub = r.get("subcategory") if r.get("subcategory") in SUBCATEGORY_TAXONOMY.get(cat, []) else "Misc"
            cache[b["id"]] = {"category": cat, "subcategory": sub}

    CLASSIFICATION_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    if verbose:
        print(f"  [intel] Classified {len(todo)} new deep dive stories ({len(items) - len(todo)} cached)")
    return cache


def build_category_deepdive_data(themes: list, episodes: list, verbose: bool = True) -> dict:
    items = collect_classifiable_items(themes, episodes)
    cache = classify_stories(items, load_classification_cache(), verbose)

    buckets = {c: {s: [] for s in SUBCATEGORY_TAXONOMY[c]} for c in CATEGORY_ORDER}
    for it in items:
        cls = cache.get(it["id"]) or {"category": "misc", "subcategory": "Misc"}
        c, s = cls["category"], cls["subcategory"]
        if c not in buckets or s not in buckets[c]:
            c, s = "misc", "Misc"
        entry = {
            "date": it["date"], "text": it["text"],
            "source_type": it["source_type"], "source_label": it["source_label"],
        }
        if it.get("source_url"):
            entry["source_url"] = it["source_url"]
        if it.get("is_context"):
            entry["is_context"] = True
        buckets[c][s].append(entry)

    data = {}
    for c in CATEGORY_ORDER:
        subcats = []
        for s in SUBCATEGORY_TAXONOMY[c]:
            entries = sorted(buckets[c][s], key=lambda x: x.get("date", ""), reverse=True)
            if entries:
                subcats.append({"name": s, "entries": entries})
        subcats.sort(key=lambda sc: sc["entries"][0]["date"] if sc["entries"] else "", reverse=True)
        story_count = sum(len(sc["entries"]) for sc in subcats)
        data[c] = {"label": CATEGORY_LABELS[c], "subcategories": subcats, "story_count": story_count}
    return data


def build_category_row(slug: str, bucket: dict) -> str:
    subcats = bucket["subcategories"]
    last_updated = esc(subcats[0]["entries"][0]["date"]) if subcats and subcats[0]["entries"] else "—"

    return (
        f'            <tr class="intel-row" data-row-id="{slug}">\n'
        f'              <td class="itd" style="font-weight:600;color:var(--text-primary,#fff);">{esc(bucket["label"])}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);text-align:center;">{len(subcats)}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);text-align:center;">{bucket["story_count"]}</td>\n'
        f'              <td class="itd-last" style="color:var(--text-muted,#aaa);white-space:nowrap;">{last_updated}</td>\n'
        f'            </tr>'
    )


def infer_entity_type(category: str) -> str:
    cat = category.lower()
    for kw in PERSON_CATEGORY_KEYWORDS:
        if kw in cat:
            return "person"
    return "company"


def infer_category_tags(category: str) -> list:
    cat = category.lower().strip()
    if cat in CATEGORY_TAG_MAP:
        return CATEGORY_TAG_MAP[cat]

    tags = []
    for token in re.split(r"[/&,]", cat):
        slug = CATEGORY_TOKEN_MAP.get(token.strip())
        if slug and slug not in tags:
            tags.append(slug)

    return tags if tags else ["ai"]


def parse_watchlist() -> list:
    if not WATCHLIST_MD.exists():
        return []
    text    = WATCHLIST_MD.read_text()
    section = re.search(r"## Companies & People\n(.+?)(?=\n---\n)", text, re.DOTALL)
    if not section:
        return []
    entities = []
    for line in section.group(1).strip().split("\n"):
        m = re.match(
            r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|",
            line,
        )
        if not m:
            continue
        name = m.group(1).strip()
        if name.lower() in ("name", "----", "---") or re.match(r"^[-\s]+$", name):
            continue
        entities.append({
            "name":     name,
            "category": m.group(2).strip(),
            "count":    int(m.group(5)),
            "note":     m.group(6).strip(),
        })
    return entities


def load_meta() -> dict:
    if not META_FILE.exists():
        return {}
    return json.loads(META_FILE.read_text())


def load_tracker() -> dict:
    """Full entity_tracker.json entities dict, keyed by name: count, last_seen,
    mentions[] ({date, context, signal}), etc."""
    if not TRACKER_FILE.exists():
        return {}
    return json.loads(TRACKER_FILE.read_text()).get("entities", {})


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def to_json_script(data: dict) -> str:
    """JSON-serialize for safe embedding inside a <script type="application/json">
    tag — escapes '<' so a stray '</script' in mention text can't close the tag early."""
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def render_pills(tag_list: list) -> str:
    pills = "".join(f'<span class="cat-pill">{t}</span>' for t in tag_list)
    return f'<div class="cat-pills">{pills}</div>'


def build_company_row(entity: dict, meta: dict) -> str:
    name        = entity["name"]
    category    = entity["category"]
    tag_list    = meta.get("category_tags") or infer_category_tags(category)
    tags        = " ".join(tag_list)
    company_id  = meta.get("company_id") or slugify(name)
    count       = entity["count"]
    last_seen   = esc(entity.get("last_seen") or "—")
    pills       = render_pills(tag_list)

    return (
        f'            <tr class="intel-row" data-category="{tags}"'
        f' data-entity-type="company" data-company-id="{company_id}">\n'
        f'              <td class="itd" style="font-weight:600;color:var(--text-primary,#fff);white-space:nowrap;">{esc(name)}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);">{esc(category)}{pills}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);text-align:center;">{count}</td>\n'
        f'              <td class="itd-last" style="color:var(--text-muted,#aaa);">{last_seen}</td>\n'
        f'            </tr>'
    )


def build_person_row(entity: dict, meta: dict) -> str:
    name              = entity["name"]
    role              = meta.get("role") or entity["category"]
    affiliation       = meta.get("affiliation") or ""
    affiliation_label = meta.get("affiliation_label") or affiliation.replace("-", " ").title()
    tag_list          = meta.get("category_tags") or infer_category_tags(entity["category"])
    tags              = " ".join(tag_list)
    company_id        = meta.get("company_id") or slugify(name)
    person_id         = meta.get("person_id") or slugify(name)
    count             = entity["count"]
    last_seen         = esc(entity.get("last_seen") or "—")
    pills             = render_pills(tag_list)

    if affiliation:
        affil_html = (
            f'<a class="affiliation-badge" href="companies.html#{affiliation}">{esc(affiliation_label)}</a>'
        )
    else:
        affil_html = '<span style="color:var(--text-muted,#555);">—</span>'

    return (
        f'            <tr class="intel-row" data-category="{tags}"'
        f' data-entity-type="person" data-company-id="{company_id}" data-person-id="{person_id}">\n'
        f'              <td class="itd" style="font-weight:600;color:var(--text-primary,#fff);white-space:nowrap;">{esc(name)}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);">{esc(role)}{pills}</td>\n'
        f'              <td class="itd" style="white-space:nowrap;">{affil_html}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);text-align:center;">{count}</td>\n'
        f'              <td class="itd-last" style="color:var(--text-muted,#aaa);">{last_seen}</td>\n'
        f'            </tr>'
    )


def build_entity_mentions_data(pairs: list, tracker: dict, id_field: str) -> dict:
    """pairs: list of (entity, meta) tuples. Returns {slug: {name, mentions}},
    mentions sorted newest-first, sourced from entity_tracker.json."""
    data = {}
    for e, m in pairs:
        slug = m.get(id_field) or slugify(e["name"])
        t = tracker.get(e["name"], {})
        mentions = sorted(t.get("mentions", []), key=lambda x: x.get("date", ""), reverse=True)
        data[slug] = {"name": e["name"], "mentions": mentions}
    return data


def splice(html: str, marker: str, content: str) -> str:
    start = f"<!-- {marker}-START -->"
    end   = f"<!-- {marker}-END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(html):
        raise ValueError(f"Markers for {marker} not found")
    replacement = start + "\n" + content + "\n" + end
    return pattern.sub(replacement, html)


def write_file(path: Path, sections: dict, verbose: bool = True) -> bool:
    if not path.exists():
        if verbose:
            print(f"  [intel] {path} not found — skipping")
        return False

    html = path.read_text()
    path.with_suffix(".html.bak").write_text(html)  # backup before every write

    try:
        for marker, content in sections.items():
            html = splice(html, marker, content)
    except ValueError as err:
        if verbose:
            print(f"  [intel] {path.name}: {err}")
        return False

    path.write_text(html)
    return True


def generate(verbose: bool = True) -> bool:
    entities = parse_watchlist()
    if not entities:
        if verbose:
            print("  [intel] No entities in watchlist.md — skipping")
        return False

    meta    = load_meta()
    tracker = load_tracker()

    # Prefer authoritative count + last_seen from entity_tracker
    for e in entities:
        t = tracker.get(e["name"], {})
        if "count" in t:
            e["count"] = t["count"]
        e["last_seen"] = t.get("last_seen", "")

    companies, people = [], []
    for e in entities:
        m           = meta.get(e["name"], {})
        entity_type = m.get("entity_type") or infer_entity_type(e["category"])
        if entity_type == "person":
            people.append((e, m))
        else:
            companies.append((e, m))

    # Sort by count desc, then name asc
    companies.sort(key=lambda x: (-x[0]["count"], x[0]["name"]))
    people.sort(key=lambda x:    (-x[0]["count"], x[0]["name"]))

    company_rows = "\n".join(build_company_row(e, m) for e, m in companies)
    people_rows  = "\n".join(build_person_row(e, m)  for e, m in people)

    company_data   = build_entity_mentions_data(companies, tracker, "company_id")
    people_data    = build_entity_mentions_data(people,    tracker, "person_id")
    company_script = f'      <script type="application/json" id="company-data">{to_json_script(company_data)}</script>'
    people_script  = f'      <script type="application/json" id="person-data">{to_json_script(people_data)}</script>'

    themes = parse_themes()
    theme_rows = "\n".join(build_theme_card(t) for t in themes)

    episodes = parse_deepdive_episodes()

    category_data   = build_category_deepdive_data(themes, episodes, verbose)
    category_rows   = "\n".join(build_category_row(c, category_data[c]) for c in CATEGORY_ORDER)
    category_script = f'      <script type="application/json" id="deepdive-data">{to_json_script(category_data)}</script>'

    ok = True
    ok &= write_file(THEMES_HTML, {"THEMES": theme_rows}, verbose)
    ok &= write_file(DEEPDIVE_HTML, {
        "DEEPDIVE-ROWS": category_rows, "DEEPDIVE-DATA": category_script,
    }, verbose)
    ok &= write_file(COMPANIES_HTML, {"COMPANIES-ROWS": company_rows, "COMPANIES-DATA": company_script}, verbose)
    ok &= write_file(PEOPLE_HTML,    {"PEOPLE-ROWS": people_rows, "PEOPLE-DATA": people_script}, verbose)

    if not ok:
        return False

    if verbose:
        print(f"  [intel] Regenerated: {len(companies)} companies, {len(people)} people, {len(themes)} themes, {len(episodes)} deep dive episodes")

    push_to_ayx(verbose=verbose)

    return True


def push_to_ayx(verbose: bool = True):
    """Commit + push the regenerated Intel pages to AYX. Never raises —
    a failed push here shouldn't take down the caller's brief/curator run."""
    today = datetime.now().strftime("%Y-%m-%d")
    files = [
        "intel/index.html", "intel/deep-dive.html",
        "intel/companies.html", "intel/people.html",
    ]
    cmds = [
        ["git", "-C", str(AYX_DIR), "add", *files],
        ["git", "-C", str(AYX_DIR), "commit", "-m", f"Intel: auto-regenerate ({today})"],
        ["git", "-C", str(AYX_DIR), "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                if verbose:
                    print("  [intel] git commit: nothing to commit")
            else:
                if verbose:
                    print(f"  [intel] ⚠ git {cmd[3]} failed: {result.stderr.strip()}")
            return
    if verbose:
        print("  [intel] ✓ Pushed to AYX")


if __name__ == "__main__":
    generate()
