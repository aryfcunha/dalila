"""HTML digest renderer.

Produces a single self-contained HTML file from the same items the Markdown
editor draws on. Distinct from the Telegram digest: the HTML version is the
office-deployable channel and the "high-fidelity" reading surface.

Design brief: Bloomberg-terminal density with a high-end editorial finish.
Same brand palette as the leadership pitch deck (navy / cream / gold) for
visual consistency. Inline styles, no external CSS — opens anywhere, prints
cleanly, works behind a corporate firewall.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable

import pytz

from dalila.config import get_config


# Brand palette — same as the leadership pitch deck and the future brand book.
NAVY = "#0B2545"
NAVY_LIGHT = "#13315C"
NAVY_DEEP = "#06182E"
CREAM = "#F4F1EA"
CREAM_DEEP = "#EAE4D5"
GOLD = "#C9A961"
GOLD_LIGHT = "#E8D4A0"
INK = "#1A1A1A"
MUTED = "#6B7280"
RULE = "#D8CFB8"

# Categorical order + display labels and small Unicode glyphs (no SVG dependency,
# renders everywhere). Mirrors the Markdown editor's section order.
SECTIONS = [
    ("humanitarian",             "Humanitarian",            "△"),
    ("aid_commitments",          "Aid commitments",         "$"),
    ("reports_evidence",         "Reports & evidence",      "≡"),
    ("conferences_events",       "Conferences & events",    "◇"),
    ("uae_foreign_policy_signals", "UAE foreign-policy signals", "→"),
    ("uae_leadership_doctrine",  "UAE doctrine",            "★"),
    ("uae_ecosystem_moves",      "UAE ecosystem",           "⌬"),
]

DOCTRINE_TAG_COLOR = {
    "reinforcing":    "#5B7A99",
    "refining":       "#7AA67A",
    "evolving":       GOLD,
    "contradicting":  "#C25A4F",
    "new":            "#3D6FB3",
}


def render(items: list[dict], *, when: datetime | None = None, total_ingested: int | None = None) -> str:
    """Render a complete HTML digest. Items list is the same shape as items_for_digest()."""
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)
    now = (when or datetime.now(tz)).astimezone(tz)
    date_label = f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}"
    time_label = now.strftime("%H:%M %Z")

    # Number items globally so #N references in the HTML match what would
    # appear in a Markdown digest of the same dataset.
    numbered = []
    for n, it in enumerate(items, start=1):
        numbered.append({**it, "n": n})

    # Top 3 = highest combined score
    def _score(it: dict) -> float:
        return float(it.get("uae_relevance") or 0) * (float(it.get("severity") or 0.5) or 0.5)
    top3 = sorted(numbered, key=_score, reverse=True)[:3]

    # Bucket the rest by category, preserving the priority order from items_for_digest
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for it in numbered:
        by_cat[it.get("category") or "other"].append(it)

    # Compose
    parts: list[str] = []
    parts.append(_html_head(date_label))
    parts.append(_masthead(date_label, time_label, len(numbered), total_ingested or len(numbered)))
    parts.append(_top3_block(top3))
    for cat_key, label, glyph in SECTIONS:
        rows = by_cat.get(cat_key) or []
        if not rows:
            continue
        parts.append(_section_block(label, glyph, rows))
    parts.append(_footer(now))
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _html_head(date_label: str) -> str:
    title = f"Dalila — {date_label}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  /* Brand palette — navy/cream/gold. Same as the leadership deck. */
  :root {{
    --navy: {NAVY};
    --navy-light: {NAVY_LIGHT};
    --navy-deep: {NAVY_DEEP};
    --cream: {CREAM};
    --cream-deep: {CREAM_DEEP};
    --gold: {GOLD};
    --gold-light: {GOLD_LIGHT};
    --ink: {INK};
    --muted: {MUTED};
    --rule: {RULE};
  }}
  html {{
    background: var(--cream);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}
  body {{
    margin: 0;
    font-family: Georgia, "Source Serif Pro", "PT Serif", "Times New Roman", serif;
    font-size: 17px;
    line-height: 1.5;
    background: var(--cream);
  }}
  /* Monospace strip for stats, source citations, item numbers */
  .mono, code {{
    font-family: "JetBrains Mono", "IBM Plex Mono", "SF Mono", Consolas, monospace;
    font-size: 0.78em;
    letter-spacing: 0.04em;
  }}
  .wrap {{
    max-width: 760px;
    margin: 0 auto;
    padding: 56px 48px 96px;
  }}
  /* ---------- masthead ---------- */
  .masthead {{
    border-bottom: 2px solid var(--navy);
    padding-bottom: 18px;
    margin-bottom: 28px;
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: end;
    gap: 24px;
  }}
  .masthead-title {{
    margin: 0;
    font-size: 38px;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--navy);
    line-height: 1;
  }}
  .masthead-title .ar {{
    font-style: italic;
    color: var(--gold);
    font-weight: 400;
    margin-left: 12px;
    font-size: 0.65em;
    vertical-align: 4px;
  }}
  .masthead-date {{
    font-family: "JetBrains Mono", "IBM Plex Mono", Consolas, monospace;
    font-size: 11px;
    color: var(--navy);
    text-align: right;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .masthead-stats {{
    margin: 0 0 0 0;
    color: var(--muted);
    font-family: "JetBrains Mono", "IBM Plex Mono", Consolas, monospace;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .masthead-stats b {{ color: var(--navy); }}
  /* ---------- top 3 ---------- */
  .top3 {{
    background: var(--navy);
    color: var(--cream);
    padding: 26px 28px 28px;
    margin: 0 0 36px;
    border-left: 4px solid var(--gold);
  }}
  .top3-eyebrow {{
    font-family: "JetBrains Mono", "IBM Plex Mono", Consolas, monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--gold);
    margin: 0 0 14px;
  }}
  .top3 ol {{ margin: 0; padding-left: 0; list-style: none; counter-reset: top3; }}
  .top3 li {{
    counter-increment: top3;
    padding: 12px 0;
    border-top: 1px solid rgba(244,241,234, 0.18);
    display: grid;
    grid-template-columns: 28px 1fr;
    gap: 18px;
    align-items: baseline;
  }}
  .top3 li:first-child {{ border-top: none; padding-top: 0; }}
  .top3 li::before {{
    content: counter(top3);
    font-family: Georgia, serif;
    font-size: 22px;
    color: var(--gold);
    font-weight: 700;
    line-height: 1;
  }}
  .top3-headline {{ font-size: 19px; line-height: 1.35; }}
  .top3 a, .top3 a:visited {{
    color: var(--cream);
    text-decoration: none;
    border-bottom: 1px solid rgba(244, 241, 234, 0.18);
    transition: border-color 0.15s ease, color 0.15s ease;
  }}
  .top3 a:hover {{
    color: var(--gold-light);
    border-bottom-color: var(--gold);
  }}
  .top3-ref {{
    color: var(--gold-light);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11px;
    margin-left: 6px;
  }}
  /* ---------- section blocks ---------- */
  .section {{ margin: 0 0 32px; }}
  .section-head {{
    display: flex;
    align-items: baseline;
    gap: 14px;
    border-bottom: 1px solid var(--rule);
    padding-bottom: 8px;
    margin-bottom: 18px;
  }}
  .section-glyph {{
    color: var(--gold);
    font-size: 14px;
    width: 16px;
    display: inline-block;
    text-align: center;
  }}
  .section-title {{
    margin: 0;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--navy);
    font-weight: 700;
    font-family: "JetBrains Mono", Consolas, monospace;
  }}
  .section-count {{
    margin-left: auto;
    color: var(--muted);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11px;
  }}
  /* ---------- item ---------- */
  .item {{
    padding: 14px 0 18px;
    border-bottom: 1px dashed var(--rule);
  }}
  .item:last-child {{ border-bottom: none; }}
  .item-head {{
    display: grid;
    grid-template-columns: 36px 1fr;
    gap: 12px;
    align-items: baseline;
    margin-bottom: 6px;
  }}
  .item-n {{
    color: var(--muted);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 12px;
    text-align: right;
  }}
  .item-title {{
    margin: 0;
    font-size: 17px;
    line-height: 1.35;
    color: var(--navy);
    font-weight: 600;
  }}
  .item-title a {{ color: inherit; text-decoration: none; border-bottom: 1px solid transparent; transition: border-color 0.15s; }}
  .item-title a:hover {{ border-bottom-color: var(--gold); }}
  .item-summary {{
    margin: 0 0 8px 48px;
    color: var(--ink);
    line-height: 1.55;
  }}
  .item-meta {{
    margin: 0 0 0 48px;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    color: var(--muted);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}
  .item-meta .src {{ color: var(--navy); }}
  .item-meta .rel {{ color: var(--gold); font-weight: 700; }}
  .item-meta .sev {{ color: var(--muted); }}
  .item-meta a {{
    color: var(--navy-light);
    text-decoration: none;
    border-bottom: 1px solid var(--rule);
  }}
  .item-meta a:hover {{ border-bottom-color: var(--gold); color: var(--navy); }}
  .doctrine-tag {{
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    color: var(--cream);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }}
  /* ---------- footer ---------- */
  .footer {{
    margin-top: 56px;
    padding-top: 18px;
    border-top: 2px solid var(--navy);
    color: var(--muted);
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }}
  .footer b {{ color: var(--navy); }}
  /* ---------- print ---------- */
  @media print {{
    body {{ background: white; }}
    .top3 {{ background: var(--navy-deep) !important; -webkit-print-color-adjust: exact; }}
    .wrap {{ max-width: none; padding: 24px 32px; }}
    .item, .section {{ page-break-inside: avoid; }}
  }}
  /* ---------- small screens ---------- */
  @media (max-width: 600px) {{
    .wrap {{ padding: 32px 22px 64px; }}
    .masthead {{ grid-template-columns: 1fr; }}
    .masthead-date {{ text-align: left; }}
    .masthead-title {{ font-size: 30px; }}
    .item-head, .item-summary, .item-meta {{ margin-left: 0; }}
    .item-head {{ grid-template-columns: 1fr; }}
    .item-n {{ text-align: left; }}
    .item-summary, .item-meta {{ margin-left: 0; }}
  }}
</style>
</head>
<body>
<div class="wrap">
"""


def _masthead(date_label: str, time_label: str, n_items: int, total: int) -> str:
    return f"""
<header class="masthead">
  <div>
    <h1 class="masthead-title">Dalila<span class="ar">دليلة</span></h1>
    <p class="masthead-stats">
      <b>{n_items}</b> items above threshold &nbsp;·&nbsp;
      <b>{total}</b> reviewed in 24h &nbsp;·&nbsp;
      composed {html.escape(time_label)}
    </p>
  </div>
  <div class="masthead-date">{html.escape(date_label)}</div>
</header>
"""


def _top3_block(top3: list[dict]) -> str:
    if not top3:
        return ""
    lis = []
    for it in top3:
        title = html.escape(it.get("title") or "")
        url = it.get("url") or ""
        n = it.get("n") or 0
        headline = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
        lis.append(
            f'<li><span class="top3-headline">{headline}'
            f'<span class="top3-ref">#{n}</span></span></li>'
        )
    return (
        '<section class="top3">'
        '<p class="top3-eyebrow">Top 3 today</p>'
        '<ol>' + "\n".join(lis) + '</ol>'
        '</section>'
    )


def _section_block(label: str, glyph: str, rows: list[dict]) -> str:
    return (
        f'<section class="section">'
        f'<div class="section-head">'
        f'  <span class="section-glyph">{glyph}</span>'
        f'  <h2 class="section-title">{html.escape(label)}</h2>'
        f'  <span class="section-count">{len(rows)} item{"s" if len(rows) != 1 else ""}</span>'
        f'</div>'
        + "".join(_item_block(it) for it in rows)
        + '</section>'
    )


def _item_block(it: dict) -> str:
    title = html.escape(it.get("title") or "")
    summary = html.escape(it.get("summary") or "")
    url = it.get("url") or ""
    n = it.get("n") or 0
    source = html.escape(it.get("source") or "")
    rel = float(it.get("uae_relevance") or 0)
    sev = float(it.get("severity") or 0)
    doctrine = (it.get("doctrine_relation") or "").strip().lower() or None
    sector = (it.get("policy_sector") or "").strip() or None
    countries = it.get("country_focus") or []

    # Linked title where we have a real URL
    title_html = (
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>'
        if url and url.startswith("http")
        else title
    )

    # Doctrine tag
    doctrine_html = ""
    if doctrine and doctrine in DOCTRINE_TAG_COLOR:
        color = DOCTRINE_TAG_COLOR[doctrine]
        doctrine_html = (
            f'<span class="doctrine-tag" style="background:{color}">'
            f'{html.escape(doctrine.upper())}'
            f'</span>'
        )

    # Country focus → small chips
    country_html = ""
    if countries:
        chips = " · ".join(html.escape(str(c)) for c in countries[:3])
        country_html = f'<span class="ctry">{chips}</span>'

    sector_html = (
        f'<span class="sector">sector: {html.escape(sector)}</span>'
        if sector
        else ""
    )

    # Extra-link list (gmail items carry many)
    extra_links_html = ""
    extra = it.get("extra") or {}
    extras = extra.get("links") if isinstance(extra, dict) else None
    if extras:
        extra_links_html = ' · '.join(
            f'<a href="{html.escape(u)}" target="_blank" rel="noopener">link {i+1}</a>'
            for i, u in enumerate(extras[:5])
        )
        if extra_links_html:
            extra_links_html = ' · ' + extra_links_html

    meta_parts = [
        f'<span class="src">{source}</span>' if source else "",
        f'<span class="rel">UAE rel {rel:.2f}</span>',
        f'<span class="sev">sev {sev:.2f}</span>' if sev > 0 else "",
        country_html,
        sector_html,
        doctrine_html,
    ]
    meta_html = " &middot; ".join(p for p in meta_parts if p) + extra_links_html

    return (
        f'<article class="item" id="item-{n}">'
        f'  <div class="item-head">'
        f'    <span class="item-n">#{n:02d}</span>'
        f'    <h3 class="item-title">{title_html}</h3>'
        f'  </div>'
        f'  <p class="item-summary">{summary}</p>'
        f'  <div class="item-meta">{meta_html}</div>'
        f'</article>'
    )


def _footer(now: datetime) -> str:
    return f"""
<footer class="footer">
  <span>Dalila &mdash; daily intelligence brief</span>
  <span><b>{html.escape(now.strftime("%Y-%m-%d %H:%M %Z"))}</b></span>
</footer>
</div>
"""
