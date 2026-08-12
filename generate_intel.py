#!/usr/bin/env python3
"""
generate_intel.py — Regenerates Companies and People rows in intel/index.html
from three sources of truth:
  - context/watchlist.md         (entities + signal counts + notes)
  - context/watchlist_meta.json  (entity_type, slugs, category tags, affiliation)
  - context/entity_tracker.json  (authoritative signal counts)

Run standalone:
  python generate_intel.py

Called automatically by watchlist_curator.py after promotions.
"""

import json
import re
from pathlib import Path

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
AYX_DIR      = PROJECT_DIR.parent / "augmentyourexperience-www"
INTEL_HTML   = AYX_DIR / "intel" / "index.html"
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
    "space/finance":         ["ai"],
    "space & finance":       ["ai"],
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ─── Theme parsing ────────────────────────────────────────────────────────────

def infer_theme_category(name: str, body: str) -> str:
    text = (name + " " + body).lower()
    cats = []
    if any(w in text for w in ["ai", "llm", "model", "openai", "anthropic", "spending", "capex", "coding", "agent", "infrastructure", "publishing", "backlash", "layoff", "productivity"]):
        cats.append("ai")
    if any(w in text for w in ["xr", "glasses", "spatial", "ar ", " ar\n", "vr ", "headset", "optics", "wearable", "smart glasses"]):
        cats.append("xr")
    if any(w in text for w in ["robot", "humanoid", "autonomous", "av ", "waymo", "self-driving"]):
        cats.append("robotics")
    if any(w in text for w in ["3d print", "manufactur", "maker", "filament", "bambu", "prusa"]):
        cats.append("3d")
    if any(w in text for w in ["media", "publish", "content", "studio", "youtube", "streaming"]):
        cats.append("media")
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

        themes.append({"name": name, "appearances": appearances, "body": body})
    return themes


def build_theme_card(theme: dict) -> str:
    name     = esc(theme["name"])
    category = infer_theme_category(theme["name"], theme.get("body", ""))
    meta_html = (
        f'<span style="font-size:0.72rem;color:var(--text-muted,#888);white-space:nowrap;">'
        f'{esc(theme["appearances"])}</span>'
        if theme.get("appearances") else ""
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


def infer_entity_type(category: str) -> str:
    cat = category.lower()
    for kw in PERSON_CATEGORY_KEYWORDS:
        if kw in cat:
            return "person"
    return "company"


def infer_category_tags(category: str) -> list:
    return CATEGORY_TAG_MAP.get(category.lower(), ["ai"])


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


def load_tracker_counts() -> dict:
    if not TRACKER_FILE.exists():
        return {}
    data = json.loads(TRACKER_FILE.read_text())
    return {k: v.get("count", 0) for k, v in data.get("entities", {}).items()}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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
    description = esc(meta.get("description") or entity["note"] or "")
    pills       = render_pills(tag_list)

    return (
        f'            <tr class="intel-row" data-category="{tags}"'
        f' data-entity-type="company" data-company-id="{company_id}">\n'
        f'              <td class="itd" style="font-weight:600;color:var(--text-primary,#fff);white-space:nowrap;">{esc(name)}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);">{esc(category)}{pills}</td>\n'
        f'              <td class="itd" style="color:var(--text-muted,#888);text-align:center;">{count}</td>\n'
        f'              <td class="itd-last" style="color:var(--text-muted,#aaa);line-height:1.5;">{description}</td>\n'
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
    description       = esc(meta.get("description") or entity["note"] or "")
    pills             = render_pills(tag_list)

    if affiliation:
        affil_html = (
            f'<a class="affiliation-badge" href="#"'
            f' data-link-company="{affiliation}">{esc(affiliation_label)}</a>'
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
        f'              <td class="itd-last" style="color:var(--text-muted,#aaa);line-height:1.5;">{description}</td>\n'
        f'            </tr>'
    )


def splice(html: str, marker: str, rows_html: str) -> str:
    start = f"<!-- {marker}-START -->"
    end   = f"<!-- {marker}-END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(html):
        raise ValueError(f"Markers for {marker} not found in intel/index.html")
    replacement = start + "\n" + rows_html + "\n" + end
    return pattern.sub(replacement, html)


def generate(verbose: bool = True) -> bool:
    entities = parse_watchlist()
    if not entities:
        if verbose:
            print("  [intel] No entities in watchlist.md — skipping")
        return False

    meta   = load_meta()
    counts = load_tracker_counts()

    # Prefer authoritative count from entity_tracker
    for e in entities:
        if e["name"] in counts:
            e["count"] = counts[e["name"]]

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

    themes     = parse_themes()
    theme_rows = "\n".join(build_theme_card(t) for t in themes)

    if not INTEL_HTML.exists():
        if verbose:
            print(f"  [intel] {INTEL_HTML} not found — skipping")
        return False

    html = INTEL_HTML.read_text()
    INTEL_HTML.with_suffix(".html.bak").write_text(html)  # backup before every write

    try:
        html = splice(html, "COMPANIES-ROWS", company_rows)
        html = splice(html, "PEOPLE-ROWS",    people_rows)
        html = splice(html, "THEMES",         theme_rows)
    except ValueError as err:
        if verbose:
            print(f"  [intel] {err}")
        return False

    INTEL_HTML.write_text(html)

    if verbose:
        print(f"  [intel] Regenerated: {len(companies)} companies, {len(people)} people, {len(themes)} themes")

    return True


if __name__ == "__main__":
    generate()
