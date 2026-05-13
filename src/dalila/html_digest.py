"""HTML site renderer for Dalila.

Generates four kinds of page, all self-contained inline-CSS HTML:

  * `render_digest(items)`         → one daily digest page
  * `render_index(latest_items)`   → home page (the latest brief, full content)
  * `render_archive(briefs)`       → archive list of all past briefs
  * `render_about(sources, ...)`   → static "about Dalila" page

Aesthetic: Bloomberg-terminal density with a bespoke editorial finish.
Black background, warm off-white type, single amber accent (#FFB454).
Mono-dominant for metadata and section headers; serif for item titles so
headlines still read as journalism. ALL-CAPS labels with sharp rules
between sections.

Pages link to each other via the masthead nav (Home · Archive · About).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable

import pytz

from dalila.config import get_config


# ---------------------------------------------------------------------------
# Palette — Bloomberg-terminal-bespoke
# ---------------------------------------------------------------------------
BG          = "#0A0A0A"          # warmer than pure black; easier on eyes
BG_DEEP     = "#000000"
TYPE        = "#E8E1D3"          # warm off-white, paper-like on black
TYPE_DIM    = "#9A958A"          # muted body text
TYPE_MUTED  = "#6E6A60"          # metadata grey
AMBER       = "#FFB454"          # primary accent — the Bloomberg orange, restrained
AMBER_DEEP  = "#D88E30"
CYAN        = "#7AB9C9"          # secondary accent — links, ticker codes
GREEN       = "#7CB36E"          # for positive markers (rarely used)
RED         = "#D67866"          # for warnings (rarely used)
RULE        = "#1F1B16"          # subtle warm-dark rule
RULE_STRONG = "#3A332A"          # for section dividers


SECTIONS = [
    ("humanitarian",                "HUMANITARIAN"),
    ("aid_commitments",             "AID COMMITMENTS"),
    ("reports_evidence",            "REPORTS & EVIDENCE"),
    ("conferences_events",          "CONFERENCES & EVENTS"),
    ("uae_foreign_policy_signals",  "UAE FOREIGN-POLICY SIGNALS"),
    ("uae_leadership_doctrine",     "UAE DOCTRINE"),
    ("uae_ecosystem_moves",         "UAE ECOSYSTEM"),
]


# ===========================================================================
# Shared chrome
# ===========================================================================

def _base_css() -> str:
    return f"""
:root {{
  --bg:{BG};
  --bg-deep:{BG_DEEP};
  --type:{TYPE};
  --type-dim:{TYPE_DIM};
  --muted:{TYPE_MUTED};
  --amber:{AMBER};
  --amber-deep:{AMBER_DEEP};
  --cyan:{CYAN};
  --green:{GREEN};
  --red:{RED};
  --rule:{RULE};
  --rule-strong:{RULE_STRONG};
}}
html,body {{
  background: var(--bg);
  color: var(--type);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
body {{
  margin: 0;
  font-family: "JetBrains Mono", "IBM Plex Mono", "SF Mono", "Source Code Pro", Consolas, monospace;
  font-size: 14px;
  line-height: 1.55;
  letter-spacing: 0.01em;
}}
.serif {{
  font-family: "Iowan Old Style", "Source Serif Pro", "PT Serif", Georgia, "Times New Roman", serif;
  font-feature-settings: "kern", "liga", "calt";
}}
a {{
  color: var(--cyan);
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color 0.12s ease, color 0.12s ease;
}}
a:hover {{
  color: var(--amber);
  border-bottom-color: var(--amber);
}}
.wrap {{ max-width: 880px; margin: 0 auto; padding: 28px 32px 80px; }}

/* ---------- masthead ---------- */
.masthead {{
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 18px;
  align-items: baseline;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--amber);
  margin-bottom: 18px;
}}
.masthead-title {{
  font-family: "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1;
  margin: 0;
  color: var(--type);
}}
.masthead-title a {{ color: var(--type); border: 0; }}
.masthead-title .ar {{
  color: var(--amber);
  font-style: italic;
  font-weight: 400;
  font-size: 0.65em;
  margin-left: 8px;
  vertical-align: 3px;
}}
.masthead-tag {{
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--muted);
  border-left: 1px solid var(--rule-strong);
  padding-left: 14px;
  margin-left: 6px;
}}
.masthead-nav {{
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  text-align: right;
}}
.masthead-nav a {{
  color: var(--muted);
  margin-left: 14px;
  border: 0;
}}
.masthead-nav a[aria-current=page] {{ color: var(--amber); }}
.masthead-nav a:hover {{ color: var(--type); }}

/* ---------- ticker strip ---------- */
.ticker {{
  display: flex;
  flex-wrap: wrap;
  gap: 0;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  padding: 0 0 18px;
  border-bottom: 1px solid var(--rule);
  margin-bottom: 24px;
}}
.ticker span {{
  padding: 0 14px;
  border-right: 1px solid var(--rule-strong);
}}
.ticker span:first-child {{ padding-left: 0; }}
.ticker span:last-child {{ border-right: 0; }}
.ticker b {{ color: var(--amber); font-weight: 700; }}

/* ---------- top stories ---------- */
.top {{ margin: 0 0 28px; }}
.top-head {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 1px solid var(--amber);
  padding-bottom: 6px;
  margin-bottom: 12px;
}}
.top-head .label {{
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--amber);
}}
.top-head .count {{ color: var(--muted); font-size: 10px; letter-spacing: 0.14em; }}
.top ol {{ list-style: none; padding: 0; margin: 0; counter-reset: t; }}
.top li {{
  counter-increment: t;
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 14px;
  padding: 9px 0;
  border-bottom: 1px solid var(--rule);
  align-items: baseline;
}}
.top li:last-child {{ border-bottom: 0; }}
.top li::before {{
  content: counter(t, decimal-leading-zero);
  color: var(--amber);
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.08em;
}}
.top a {{
  color: var(--type);
  font-family: "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-size: 16px;
  line-height: 1.4;
  border-bottom: 1px solid transparent;
}}
.top a:hover {{
  color: var(--amber);
  border-bottom-color: var(--amber);
}}
.top .ref {{ color: var(--muted); font-size: 10px; margin-left: 6px; letter-spacing: 0.08em; }}

/* ---------- sections ---------- */
.section {{ margin: 0 0 30px; }}
.section-head {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 1px solid var(--rule-strong);
  padding-bottom: 6px;
  margin-bottom: 12px;
}}
.section-head .label {{
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--type);
}}
.section-head .count {{
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.14em;
}}

/* ---------- items ---------- */
.item {{
  padding: 14px 0 16px;
  border-bottom: 1px solid var(--rule);
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 14px;
}}
.item:last-child {{ border-bottom: 0; }}
.item-n {{
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.08em;
  padding-top: 2px;
}}
.item-body .title {{
  font-family: "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-size: 16px;
  line-height: 1.4;
  font-weight: 600;
  color: var(--type);
  margin: 0 0 6px;
}}
.item-body .title a {{
  color: var(--type);
  border-bottom: 1px solid transparent;
}}
.item-body .title a:hover {{
  color: var(--amber);
  border-bottom-color: var(--amber);
}}
.item-body .summary {{
  font-family: "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-size: 14px;
  color: var(--type-dim);
  margin: 0 0 8px;
  line-height: 1.55;
}}
.item-body .meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}}
.item-body .meta .src {{ color: var(--type-dim); }}
.item-body .meta .rel {{ color: var(--amber); }}
.item-body .meta .sev {{ color: var(--muted); }}
.item-body .meta a {{ color: var(--cyan); border: 0; }}
.item-body .meta a:hover {{ color: var(--amber); }}
.doctrine-tag {{
  border: 1px solid var(--amber);
  color: var(--amber);
  padding: 1px 6px;
  font-size: 9px;
  letter-spacing: 0.14em;
  font-weight: 700;
}}

/* ---------- footer ---------- */
.foot {{
  margin-top: 56px;
  padding-top: 14px;
  border-top: 1px solid var(--amber);
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 14px;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: var(--muted);
}}
.foot a {{ color: var(--muted); border: 0; }}
.foot a:hover {{ color: var(--amber); }}

/* ---------- archive ---------- */
.archive-row {{
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 18px;
  padding: 14px 0;
  border-bottom: 1px solid var(--rule);
  align-items: baseline;
}}
.archive-date {{
  color: var(--amber);
  font-size: 11px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}}
.archive-title a {{
  color: var(--type);
  font-family: "Iowan Old Style", Georgia, serif;
  font-size: 16px;
  line-height: 1.4;
  border-bottom: 1px solid transparent;
}}
.archive-title a:hover {{ color: var(--amber); border-bottom-color: var(--amber); }}
.archive-preview {{ color: var(--muted); font-size: 12px; margin-top: 6px; line-height: 1.5; }}

/* ---------- about ---------- */
.about p, .about ul, .about li {{
  font-family: "Iowan Old Style", Georgia, serif;
  font-size: 15px;
  line-height: 1.65;
  color: var(--type-dim);
  max-width: 64ch;
}}
.about h2 {{
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--amber);
  border-bottom: 1px solid var(--rule-strong);
  padding-bottom: 6px;
  margin: 34px 0 14px;
}}
.about strong {{ color: var(--type); }}
.about code {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 0.9em;
  color: var(--amber);
  background: var(--bg-deep);
  padding: 1px 5px;
  border: 1px solid var(--rule-strong);
}}
.source-block {{
  margin: 0 0 22px;
  padding: 8px 0 0 16px;
  border-left: 1px solid var(--rule-strong);
}}
.source-block strong {{
  display: block;
  color: var(--amber);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  margin-bottom: 4px;
}}
.source-block ul {{ list-style: none; padding: 0; margin: 0; }}
.source-block li {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
  color: var(--type-dim);
  padding: 3px 0;
}}
.source-block li .note {{ color: var(--muted); margin-left: 4px; }}

/* ---------- countries view ---------- */
.country-controls {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin: 6px 0 22px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--rule);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
}}
.country-controls label {{ color: var(--muted); margin-right: 4px; }}
.region-pill {{
  display: inline-block;
  padding: 3px 8px;
  border: 1px solid var(--rule-strong);
  color: var(--type-dim);
  cursor: pointer;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}}
.region-pill:hover {{ color: var(--amber); border-color: var(--amber-deep); }}
.region-pill.active {{ color: var(--bg); background: var(--amber); border-color: var(--amber); }}

.region-block {{ margin: 0 0 22px; }}
.region-head {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 6px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--rule);
  display: flex;
  justify-content: space-between;
}}
.region-head .count {{ color: var(--muted); }}
.tile-grid {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.tile {{
  width: 44px; height: 44px;
  background: var(--bg-deep);
  border: 1px solid var(--rule-strong);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--type-dim);
  cursor: pointer;
  text-align: center;
  transition: border-color 0.12s ease, color 0.12s ease;
  position: relative;
}}
.tile:hover {{ border-color: var(--amber); color: var(--type); z-index: 2; }}
.tile .num {{
  font-size: 9px;
  font-weight: 400;
  color: var(--muted);
  letter-spacing: 0.02em;
  margin-top: 1px;
}}
.tile.selected {{
  border-color: var(--amber);
  color: var(--bg);
  background: var(--amber);
}}
.tile.selected .num {{ color: rgba(10,10,10,0.6); }}
.tile.dim {{ opacity: 0.28; }}
.tile.empty {{ color: var(--muted); }}
/* Heatmap shades (data-h attribute set by JS) */
.tile[data-h="1"] {{ background: #1a1409; border-color: #2a1f12; color: var(--type-dim); }}
.tile[data-h="2"] {{ background: #2c1f0a; border-color: #3d2912; color: var(--type-dim); }}
.tile[data-h="3"] {{ background: #4a3110; border-color: #5d3e14; color: var(--type); }}
.tile[data-h="4"] {{ background: #75501a; border-color: #8a611f; color: var(--type); }}
.tile[data-h="5"] {{ background: #a47424; border-color: #b88229; color: var(--bg); }}
.tile[data-h="6"] {{ background: var(--amber-deep); border-color: var(--amber); color: var(--bg); }}

#country-detail {{
  margin: 28px 0 0;
  padding-top: 16px;
  border-top: 1px solid var(--rule-strong);
}}
.country-detail-head {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}}
.country-detail-head h2 {{
  margin: 0;
  font-family: "Iowan Old Style", Georgia, serif;
  font-size: 22px;
  color: var(--amber);
  font-weight: 700;
}}
.country-detail-head .meta {{
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
}}
.country-cofreq {{
  font-size: 11px;
  color: var(--type-dim);
  margin: 0 0 14px;
  letter-spacing: 0.04em;
}}
.country-cofreq .pair {{
  display: inline-block;
  margin-right: 12px;
  margin-bottom: 4px;
  padding: 2px 6px;
  border: 1px solid var(--rule-strong);
  color: var(--type-dim);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10px;
  letter-spacing: 0.06em;
}}
.country-cofreq .pair b {{ color: var(--amber); font-weight: 700; margin-left: 4px; }}
.country-news-empty {{
  color: var(--muted);
  font-size: 13px;
  font-style: italic;
  padding: 28px 0;
  text-align: center;
}}

/* ---------- print + mobile ---------- */
@media print {{
  html,body {{ background: white; color: black; }}
  a {{ color: black; text-decoration: underline; }}
  .masthead-nav, .foot {{ display: none; }}
}}
@media (max-width: 640px) {{
  .wrap {{ padding: 22px 18px 60px; }}
  .masthead {{ grid-template-columns: 1fr; gap: 8px; }}
  .masthead-tag {{ border-left: 0; padding-left: 0; margin-left: 0; }}
  .masthead-nav {{ text-align: left; }}
  .ticker {{ font-size: 9px; }}
  .ticker span {{ padding: 0 8px; }}
  .archive-row {{ grid-template-columns: 1fr; gap: 4px; }}
  .item {{ grid-template-columns: 1fr; gap: 6px; }}
  .item-n {{ font-size: 10px; padding: 0; }}
}}
"""


def _doc(title: str, body: str) -> str:
    return (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{html.escape(title)}</title>\n'
        f'<style>{_base_css()}</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="wrap">\n'
        + body
        + '\n</div>\n</body>\n</html>\n'
    )


def _masthead(*, on_page: str, tag: str = "DAILY BRIEF", link_prefix: str = "") -> str:
    """on_page: 'home' | 'digest' | 'archive' | 'about' | 'countries'.

    `link_prefix` is the relative path back to the site root. `""` for pages
    at root (index, archive, about, countries); `"../"` for pages one level
    deep (digests/YYYY-MM-DD.html). Keeps links working regardless of whether
    the site is served from the domain root or a project path like `/dalila/`.
    """
    def _attr(p: str) -> str:
        return ' aria-current="page"' if on_page == p else ""
    home_href = link_prefix or "./"
    return f"""
<header class="masthead">
  <h1 class="masthead-title"><a href="{home_href}">Dalila<span class="ar">دليلة</span></a></h1>
  <div class="masthead-tag">{html.escape(tag)}</div>
  <nav class="masthead-nav">
    <a href="{home_href}"{_attr("home")}>Home</a>
    <a href="{link_prefix}archive.html"{_attr("archive")}>Archive</a>
    <a href="{link_prefix}countries.html"{_attr("countries")}>Countries</a>
    <a href="{link_prefix}about.html"{_attr("about")}>About</a>
  </nav>
</header>
"""


def _footer(*, contact_email: str, telegram_bot: str | None) -> str:
    parts = [f'<span>© {datetime.now().year} Dalila — daily intelligence brief</span>']
    if telegram_bot:
        parts.append(f'<span><a href="https://t.me/{telegram_bot}">Subscribe on Telegram</a></span>')
    parts.append(f'<span><a href="mailto:{contact_email}">Submit a suggestion</a></span>')
    return '<footer class="foot">' + "".join(parts) + '</footer>'


# ===========================================================================
# Digest page
# ===========================================================================

def render_digest(
    items: list[dict],
    *,
    when: datetime | None = None,
    total_ingested: int | None = None,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
    on_page: str = "digest",
    link_prefix: str = "../",
) -> str:
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)
    now = (when or datetime.now(tz)).astimezone(tz)
    date_label = f"{now.strftime('%A').upper()} {now.day} {now.strftime('%B %Y').upper()}"
    time_label = now.strftime("%H:%M %Z")

    numbered = [{**it, "n": n} for n, it in enumerate(items, start=1)]

    def _score(it: dict) -> float:
        return float(it.get("uae_relevance") or 0) * (float(it.get("severity") or 0.5) or 0.5)
    top3 = sorted(numbered, key=_score, reverse=True)[:3]

    by_cat: dict[str, list[dict]] = {}
    for it in numbered:
        by_cat.setdefault(it.get("category") or "other", []).append(it)

    body: list[str] = []
    body.append(_masthead(on_page=on_page, link_prefix=link_prefix))
    body.append(_ticker_strip(date_label, time_label, len(numbered), total_ingested))
    body.append(_top_block(top3))
    for cat_key, label in SECTIONS:
        rows = by_cat.get(cat_key) or []
        if rows:
            body.append(_section_block(label, rows))
    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))

    return _doc(f"Dalila — {date_label.title()}", "\n".join(body))


def _ticker_strip(date_label: str, time_label: str, n_items: int, total: int | None) -> str:
    items_html = f'<span><b>{n_items}</b> ITEMS</span>'
    rev_html = f'<span><b>{total or n_items}</b> REVIEWED 24H</span>' if (total or n_items) else ""
    return f"""
<div class="ticker">
  <span>{html.escape(date_label)}</span>
  {items_html}
  {rev_html}
  <span>COMPOSED {html.escape(time_label)}</span>
</div>
"""


def _top_block(top3: list[dict]) -> str:
    if not top3:
        return ""
    lis: list[str] = []
    for it in top3:
        title = html.escape(it.get("title") or "")
        url = it.get("url") or ""
        n = it.get("n") or 0
        href = url if (url and url.startswith("http")) else f"#item-{n}"
        lis.append(
            f'<li><a href="{html.escape(href)}">{title}</a>'
            f'<span class="ref">#{n}</span></li>'
        )
    return (
        '<section class="top">'
        '<div class="top-head">'
        '<span class="label">▌ Top stories</span>'
        f'<span class="count">{len(top3):02d} of {len(top3):02d}</span>'
        '</div>'
        '<ol>' + "\n".join(lis) + '</ol>'
        '</section>'
    )


def _section_block(label: str, rows: list[dict]) -> str:
    return (
        '<section class="section">'
        '<div class="section-head">'
        f'<span class="label">▌ {html.escape(label)}</span>'
        f'<span class="count">{len(rows):02d} ITEM{"S" if len(rows) != 1 else ""}</span>'
        '</div>'
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

    title_html = (
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>'
        if url and url.startswith("http") else title
    )

    extra = it.get("extra") or {}
    extras = extra.get("links") if isinstance(extra, dict) else None
    extra_links_html = ""
    if extras:
        extra_links_html = " · " + " · ".join(
            f'<a href="{html.escape(u)}" target="_blank" rel="noopener">L{i+1}</a>'
            for i, u in enumerate(extras[:5])
        )

    meta_parts = [
        f'<span class="src">{source}</span>' if source else "",
        f'<span class="rel">REL {rel:.2f}</span>',
        f'<span class="sev">SEV {sev:.2f}</span>' if sev > 0 else "",
        f'<span>{" · ".join(html.escape(str(c)) for c in countries[:3])}</span>' if countries else "",
        f'<span>{html.escape(sector.upper())}</span>' if sector else "",
        f'<span class="doctrine-tag">{html.escape(doctrine.upper())}</span>' if doctrine else "",
    ]
    meta_html = " · ".join(p for p in meta_parts if p) + extra_links_html

    return (
        f'<article class="item" id="item-{n}">'
        f'<span class="item-n">#{n:02d}</span>'
        '<div class="item-body">'
        f'<h3 class="title">{title_html}</h3>'
        f'<p class="summary">{summary}</p>'
        f'<div class="meta">{meta_html}</div>'
        '</div>'
        '</article>'
    )


# ===========================================================================
# Home page (renders the latest digest inline, on_page=home so Home is current)
# ===========================================================================

def render_index(
    latest_items: list[dict],
    *,
    when: datetime | None = None,
    total_ingested: int | None = None,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Home page = latest digest, full content, with nav marking Home as current.

    Always at the site root, so link_prefix='' (links resolve relative to /).
    """
    return render_digest(
        latest_items,
        when=when,
        total_ingested=total_ingested,
        contact_email=contact_email,
        telegram_bot=telegram_bot,
        on_page="home",
        link_prefix="",
    )


# ===========================================================================
# Archive page (chronological list of all past briefs)
# ===========================================================================

def render_archive(
    briefs: list[dict],
    *,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Archive = list of all persisted briefs.

    `briefs` is a list of dicts: { date_label, slug, preview }, newest first.
    """
    body: list[str] = []
    # archive.html sits at the site root → no path prefix needed
    body.append(_masthead(on_page="archive", tag="ARCHIVE", link_prefix=""))

    if not briefs:
        body.append(
            '<p style="color:var(--muted);font-size:13px;margin:32px 0 0;">'
            'No briefs in the archive yet. The next one composes at 06:30 GST.</p>'
        )
    else:
        body.append('<section class="archive">')
        for b in briefs:
            slug = html.escape(b.get("slug") or "")
            date_label = html.escape(b.get("date_label") or "")
            preview = html.escape(b.get("preview") or "")
            body.append(
                '<div class="archive-row">'
                f'<div class="archive-date">{date_label}</div>'
                '<div>'
                f'<div class="archive-title"><a href="digests/{slug}.html">Morning brief — {date_label}</a></div>'
                + (f'<div class="archive-preview">{preview}</div>' if preview else "")
                + '</div>'
                '</div>'
            )
        body.append('</section>')

    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — Archive", "\n".join(body))


# ===========================================================================
# Countries view
# ===========================================================================

def _heat_bucket(n: int, max_n: int) -> int:
    """0..6 heatmap bucket. 0 = no mentions, 6 = brightest."""
    if n <= 0:
        return 0
    if max_n <= 0:
        return 1
    # Log-ish bucketing so a handful of mentions stand out without saturating
    import math
    ratio = math.log1p(n) / math.log1p(max_n) if max_n > 1 else 1.0
    return max(1, min(6, int(ratio * 6) + 1))


def render_countries(
    countries: dict,        # {ISO: {name, region, aliases}, ...}
    regions: dict,          # {slug: {label, countries: [ISO]}, ...}
    counts: dict[str, int], # {ISO: mention count in window}
    items_by_country: dict[str, list[dict]],   # {ISO: [item dicts]} for items list
    cooccurrence: dict[str, dict[str, int]],   # {ISO: {other_ISO: count}}
    *,
    window_days: int = 14,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Render the /countries page.

    Layout: region-grouped tiles, each tile coloured by mention-count heatmap.
    Below: a detail panel that populates when a tile is clicked (country name,
    co-mentions, recent news). All interactivity is inline JS — no external
    libs, opens through any firewall.

    `items_by_country` should only contain countries with at least one item
    (saves page weight); same for `cooccurrence`.
    """
    import json as _json

    max_n = max(counts.values(), default=0)

    body: list[str] = []
    body.append(_masthead(on_page="countries", tag="COUNTRIES", link_prefix=""))

    body.append(
        f'<p style="color:var(--type-dim);font-size:13px;margin:8px 0 18px;'
        f'max-width:64ch;">Country mentions across all sources, last '
        f'<b style="color:var(--type)">{window_days} days</b>. '
        f'Brighter tile = more mentions. Click a tile for the country&rsquo;s '
        f'news + most-co-mentioned countries.</p>'
    )

    # Region filter pills
    body.append('<div class="country-controls" id="region-filter">')
    body.append('<label>Filter region:</label>')
    body.append('<span class="region-pill active" data-region="all">All</span>')
    # Preserve region order from countries.yaml
    region_slugs = list(regions.keys())
    for slug in region_slugs:
        label = regions[slug].get("label", slug)
        body.append(f'<span class="region-pill" data-region="{slug}">{html.escape(label)}</span>')
    body.append('</div>')

    # Region-grouped tile grid
    body.append('<div id="region-grid">')
    for slug in region_slugs:
        spec = regions[slug]
        label = spec.get("label", slug)
        isos = [c for c in (spec.get("countries") or []) if c in countries]
        region_total = sum(counts.get(c, 0) for c in isos)
        body.append(
            f'<section class="region-block" data-region="{slug}">'
            f'<div class="region-head">'
            f'<span>{html.escape(label.upper())}</span>'
            f'<span class="count">{region_total} ITEM{"S" if region_total != 1 else ""}</span>'
            f'</div>'
            f'<div class="tile-grid">'
        )
        for iso in isos:
            n = counts.get(iso, 0)
            h = _heat_bucket(n, max_n)
            name = countries[iso].get("name", iso)
            cls = "tile" + (" empty" if n == 0 else "")
            body.append(
                f'<div class="{cls}" data-iso="{iso}" data-region="{slug}" '
                f'data-h="{h}" data-n="{n}" '
                f'title="{html.escape(name)} — {n} item{"s" if n != 1 else ""}">'
                f'<span>{iso}</span><span class="num">{n}</span>'
                f'</div>'
            )
        body.append('</div></section>')
    body.append('</div>')

    # Detail panel — populated by JS on click
    body.append(
        '<section id="country-detail" hidden>'
        '<div class="country-detail-head">'
        '  <h2 id="cd-name"></h2>'
        '  <span class="meta" id="cd-meta"></span>'
        '</div>'
        '<div class="country-cofreq" id="cd-cofreq"></div>'
        '<div class="section">'
        '  <div class="section-head"><span class="label">▌ Recent news</span><span class="count" id="cd-count"></span></div>'
        '  <div id="cd-items"></div>'
        '</div>'
        '</section>'
    )

    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))

    # Inline data + JS
    payload = {
        "countries": {iso: {"name": spec.get("name", iso)} for iso, spec in countries.items()},
        "counts": counts,
        "items_by_country": items_by_country,
        "cooccurrence": cooccurrence,
    }
    body.append(
        '<script>const DATA = ' + _json.dumps(payload, ensure_ascii=False) + ';</script>'
    )
    body.append("""
<script>
(function(){
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
  const fmtDate = (s) => (s || '').slice(0, 10);

  let activeRegion = 'all';
  let selectedIso = null;

  function applyRegionFilter() {
    $$('.region-block').forEach(b => {
      b.style.display = (activeRegion === 'all' || b.dataset.region === activeRegion) ? '' : 'none';
    });
    if (selectedIso) {
      const tile = document.querySelector('.tile[data-iso="' + selectedIso + '"]');
      if (tile && activeRegion !== 'all' && tile.dataset.region !== activeRegion) {
        clearSelection();
      }
    }
  }

  function clearSelection() {
    $$('.tile.selected').forEach(t => t.classList.remove('selected'));
    $$('.tile.dim').forEach(t => t.classList.remove('dim'));
    $('#country-detail').hidden = true;
    selectedIso = null;
  }

  function selectCountry(iso) {
    selectedIso = iso;
    $$('.tile').forEach(t => {
      t.classList.toggle('selected', t.dataset.iso === iso);
      t.classList.toggle('dim', t.dataset.iso !== iso && !(t.dataset.n > 0));
    });

    const spec = DATA.countries[iso] || {name: iso};
    $('#cd-name').textContent = spec.name + '  (' + iso + ')';
    const n = DATA.counts[iso] || 0;
    $('#cd-meta').textContent = n + ' ITEM' + (n === 1 ? '' : 'S') + ' · LAST """ + str(window_days) + """ DAYS';

    // Co-occurrence chips
    const co = DATA.cooccurrence[iso] || {};
    const pairs = Object.entries(co).sort((a,b) => b[1]-a[1]).slice(0, 10);
    const coEl = $('#cd-cofreq');
    coEl.innerHTML = pairs.length
      ? 'OFTEN MENTIONED WITH: ' + pairs.map(([k,v]) =>
          '<span class="pair">' + (DATA.countries[k]?.name || k) + ' <b>' + v + '</b></span>'
        ).join(' ')
      : '';

    // News items
    const items = DATA.items_by_country[iso] || [];
    $('#cd-count').textContent = items.length + ' SHOWN';
    const list = $('#cd-items');
    if (items.length === 0) {
      list.innerHTML = '<div class="country-news-empty">No items yet — this country was tagged by no recent classification.</div>';
    } else {
      list.innerHTML = items.map((it, i) => {
        const title = it.url
          ? '<a href="' + escapeAttr(it.url) + '" target="_blank" rel="noopener">' + escapeHtml(it.title) + '</a>'
          : escapeHtml(it.title);
        const date = fmtDate(it.published_at || it.ingested_at || '');
        const summary = it.summary ? '<p class="summary">' + escapeHtml(it.summary) + '</p>' : '';
        const rel = (it.uae_relevance || 0).toFixed(2);
        const meta = [
          it.source ? '<span class="src">' + escapeHtml(it.source) + '</span>' : '',
          '<span class="rel">REL ' + rel + '</span>',
          date ? '<span>' + date + '</span>' : '',
          (it.category && it.category !== 'other') ? '<span>' + escapeHtml(it.category.replace(/_/g,' ').toUpperCase()) + '</span>' : '',
        ].filter(Boolean).join(' · ');
        return '<article class="item">'
             + '<span class="item-n">#' + String(i+1).padStart(2,'0') + '</span>'
             + '<div class="item-body">'
             + '<h3 class="title">' + title + '</h3>'
             + summary
             + '<div class="meta">' + meta + '</div>'
             + '</div></article>';
      }).join('');
    }

    $('#country-detail').hidden = false;
    $('#country-detail').scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Wire up region pills
  $$('.region-pill').forEach(p => {
    p.addEventListener('click', () => {
      $$('.region-pill').forEach(o => o.classList.remove('active'));
      p.classList.add('active');
      activeRegion = p.dataset.region;
      applyRegionFilter();
    });
  });

  // Wire up tiles
  $$('.tile').forEach(t => {
    t.addEventListener('click', () => {
      if (selectedIso === t.dataset.iso) {
        clearSelection();
      } else {
        selectCountry(t.dataset.iso);
      }
    });
  });

  // Hash deep-link: /countries.html#AE selects UAE on load
  const hash = location.hash.replace('#','').toUpperCase();
  if (hash && DATA.countries[hash]) {
    selectCountry(hash);
  }
})();
</script>
""")

    return _doc("Dalila — Countries", "\n".join(body))


# ===========================================================================
# About page
# ===========================================================================

def render_countries(
    countries: dict,
    regions: dict,
    country_counts: dict[str, int],
    items_by_country: dict[str, list[dict]],
    cooccurrence: dict[str, dict[str, int]],
    *,
    window_days: int = 14,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
    timeline: dict[str, dict[str, int]] | None = None,
) -> str:
    """Country view: tile cartogram + co-mention curves + news list.

    Pure HTML + inline CSS + minimal JS. No raster images — the previous
    attempt at a real geographic map produced 2000px+ images that broke
    page load. This region-grouped tile cartogram delivers the same
    affordances (heatmap of mentions, hover for count, click for co-mention
    arcs + news list) with a fraction of the bytes, and stays sharp at any
    zoom.

    Args:
      countries:        dict mapping ISO-2 → {name, region, aliases}
                        (from load_countries()["countries"])
      regions:          dict mapping region-slug → {label, countries:[ISO]}
                        (from load_countries()["regions"])
      country_counts:   dict mapping ISO-2 → mention count in window
      items_by_country: dict mapping ISO-2 → list of items (each with
                        title, url, published_at / ingested_at, source,
                        category — see db.items_for_country)
      cooccurrence:     dict mapping ISO-2 → {other_ISO: count}
      window_days:      labelled on the page (the time window the counts
                        and items cover)
    """
    import json as _json

    # The visual order of regions on the page (left-to-right ≈ Americas → Europe
    # → Africa → Asia → Pacific). Slugs must match those in countries.yaml.
    region_order = [
        "north-america", "central-america", "south-america",
        "western-europe", "eastern-europe",
        "middle-east",
        "north-africa", "western-africa", "eastern-africa", "southern-africa",
        "central-asia", "south-asia", "north-east-asia", "south-east-asia",
        "asia-pacific",
    ]

    # Build the slug → label lookup (label preferred from regions.yaml if present)
    region_labels: dict[str, str] = {}
    for slug in region_order:
        spec = regions.get(slug) or {}
        region_labels[slug] = (spec.get("label") or slug.replace("-", " ").title())

    # Bucket countries by region (skip any region not in our 15-slug order)
    region_countries: dict[str, list[tuple[str, str]]] = {slug: [] for slug in region_order}
    for iso, spec in countries.items():
        if not isinstance(spec, dict):
            continue
        slug = spec.get("region") or ""
        if slug not in region_countries:
            continue
        region_countries[slug].append((iso, spec.get("name") or iso))
    for slug in region_countries:
        region_countries[slug].sort(key=lambda kv: kv[1])  # alphabetise

    # The hottest country sets the maximum colour intensity. Floor at 1 to
    # avoid divide-by-zero on a fresh DB.
    max_count = max([1] + list(country_counts.values()))

    # ----- HTML body -----
    body: list[str] = []
    body.append(_masthead(on_page="countries", tag="COUNTRY VIEW", link_prefix=""))

    body.append(
        f'<p class="date-line">LAST {window_days} DAYS · '
        f'{sum(country_counts.values())} ITEM TAGS · '
        f'{len(country_counts)} COUNTRIES WITH ANY MENTIONS</p>'
    )
    body.append(
        '<p class="kicker" style="color:var(--type-dim);font-family:Iowan Old Style,Source Serif Pro,Georgia,serif;'
        'font-size:15px;line-height:1.6;max-width:64ch;margin:8px 0 24px;">'
        'Mention density per country, grouped by region. Hover for the exact count; '
        'click a country to see which other countries appear alongside it '
        '(thicker arc = more co-mentions) and the underlying news. '
        'Use the region buttons below to filter.</p>'
    )

    # Timeline filter chips (client-side: re-bucket counts from the timeline payload)
    # If no timeline supplied, hide the row — falls back to the server-side count window.
    if timeline:
        body.append(
            '<div class="timeline-filter" id="timeline-filter">'
            '<span class="tl-label">WINDOW</span>'
            '<button class="tl-btn" data-days="7">7D</button>'
            '<button class="tl-btn" data-days="30">30D</button>'
            '<button class="tl-btn active" data-days="90">90D</button>'
            '<button class="tl-btn" data-days="0">ALL</button>'
            '<span class="tl-stats" id="tl-stats"></span>'
            '</div>'
        )

    # Region filter buttons
    btns: list[str] = ['<button class="region-btn active" data-region="all">ALL</button>']
    for slug in region_order:
        n_in_region = sum(
            country_counts.get(iso, 0) for iso, _ in region_countries[slug]
        )
        label = region_labels[slug].upper()
        btns.append(
            f'<button class="region-btn" data-region="{slug}" '
            f'title="{html.escape(region_labels[slug])} — {n_in_region} mentions">'
            f'{html.escape(label)}<span class="rb-count">{n_in_region}</span></button>'
        )
    body.append('<div class="region-filter">' + "".join(btns) + '</div>')

    # The cartogram: each region as a row of tiles
    body.append(
        '<div class="carto-wrap">'
        '<svg class="carto-overlay" aria-hidden="true"></svg>'
        '<div class="carto">'
    )
    for slug in region_order:
        rows = region_countries[slug]
        if not rows:
            continue
        body.append(f'<section class="region-row" data-region="{slug}">')
        body.append(
            f'<div class="region-label">{html.escape(region_labels[slug])}</div>'
        )
        body.append('<div class="tiles">')
        for iso, name in rows:
            cnt = country_counts.get(iso, 0)
            intensity = (cnt / max_count) if max_count else 0
            tile_class = "tile" + (" zero" if cnt == 0 else "")
            tip = html.escape(f"{name} — {cnt} mention{'s' if cnt != 1 else ''}")
            body.append(
                f'<button class="{tile_class}" data-iso="{iso}" data-count="{cnt}" '
                f'style="--intensity:{intensity:.3f};" title="{tip}">'
                f'<span class="iso">{iso}</span></button>'
            )
        body.append('</div>')
        body.append('</section>')
    body.append('</div></div>')

    # Detail panel — populated by JS on click
    body.append(
        '<section class="detail" id="detail" hidden>'
        '<div class="detail-head">'
        '<h2 id="detail-title"></h2>'
        '<button class="detail-clear" id="detail-clear" type="button">CLEAR ×</button>'
        '</div>'
        '<div id="detail-co" class="detail-co"></div>'
        '<ul id="detail-items" class="detail-items"></ul>'
        '</section>'
    )

    # Empty-state placeholder shown when no country is selected
    body.append(
        '<p id="empty-state" class="empty-state">'
        'Click a country to see co-mention links and recent news.</p>'
    )

    # Embed the data + JS
    payload = {
        "countries": {
            iso: {
                "name": (countries.get(iso) or {}).get("name") or iso,
                "region": (countries.get(iso) or {}).get("region") or "",
                "count": country_counts.get(iso, 0),
            }
            for iso, _ in (
                (iso, name) for slug in region_order for iso, name in region_countries[slug]
            )
        },
        "regions": region_labels,
        "co": {iso: dict(co_map) for iso, co_map in cooccurrence.items() if co_map},
        # {YYYY-MM-DD: {ISO2: count}} — used by client-side timeline chips to
        # re-bucket the heatmap without a server roundtrip. Empty if caller
        # didn't supply it; the JS will hide the chip row in that case.
        "timeline": timeline or {},
        "items": {
            iso: [
                {
                    "title": it.get("title") or "",
                    "url": it.get("url") or "",
                    "date": (it.get("published_at") or it.get("ingested_at") or "")[:10],
                    "source": it.get("source") or "",
                    "category": it.get("category") or "",
                }
                for it in list(items)[:15]
            ]
            for iso, items in items_by_country.items() if items
        },
    }
    body.append(
        '<script id="country-data" type="application/json">'
        + html.escape(_json.dumps(payload, ensure_ascii=False), quote=False)
        + '</script>'
    )
    body.append(_country_view_script())

    body.append(_country_view_styles())
    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — Country view", "\n".join(body))


def _country_view_styles() -> str:
    """Inline CSS appended to the page (style merged into the global stylesheet
    is doable; this is kept separate for readability of the component)."""
    return """
<style>
  /* timeline filter chips */
  .timeline-filter {
    display:flex; align-items:center; gap:6px; flex-wrap:wrap;
    margin:0 0 14px; padding:10px 0;
    border-top:1px solid var(--rule); border-bottom:1px solid var(--rule);
  }
  .tl-label {
    font:700 10px/1 "JetBrains Mono","IBM Plex Mono",Consolas,monospace;
    letter-spacing:0.16em; color:var(--type-dim); margin-right:8px;
  }
  .tl-btn {
    background:transparent; color:var(--muted); border:1px solid var(--rule-strong);
    font:600 10px/1 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.10em; padding:6px 10px; cursor:pointer;
    transition:color .12s, border-color .12s;
  }
  .tl-btn:hover { color:var(--cyan,#7AB9C9); border-color:var(--cyan,#7AB9C9); }
  .tl-btn.active {
    color:var(--bg); background:var(--cyan,#7AB9C9); border-color:var(--cyan,#7AB9C9);
  }
  .tl-stats {
    margin-left:auto; font:11px/1 "JetBrains Mono",Consolas,monospace;
    color:var(--type-dim); letter-spacing:0.08em;
  }

  /* region filter */
  .region-filter {
    display:flex; flex-wrap:wrap; gap:6px; margin:0 0 18px;
    padding:0 0 14px; border-bottom:1px solid var(--rule-strong);
  }
  .region-btn {
    background:transparent; color:var(--muted); border:1px solid var(--rule-strong);
    font:600 10px/1 "JetBrains Mono","IBM Plex Mono",Consolas,monospace;
    letter-spacing:0.10em; padding:7px 10px; cursor:pointer;
    transition:color .12s, border-color .12s;
  }
  .region-btn:hover { color:var(--amber); border-color:var(--amber); }
  .region-btn.active { color:var(--bg); background:var(--amber); border-color:var(--amber); }
  .region-btn .rb-count { color:inherit; opacity:.7; margin-left:6px; }

  /* cartogram */
  .carto-wrap { position:relative; margin:0 0 24px; }
  .carto-overlay {
    position:absolute; inset:0; width:100%; height:100%;
    pointer-events:none; overflow:visible;
  }
  .carto-overlay path {
    fill:none; stroke:var(--amber); opacity:.55;
    stroke-linecap:round;
  }
  .region-row {
    display:grid; grid-template-columns:120px 1fr; gap:12px;
    padding:8px 0; border-bottom:1px solid var(--rule);
  }
  .region-row.dim { opacity:.18; }
  .region-label {
    font:700 10px/1.2 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.14em; text-transform:uppercase; color:var(--type-dim);
    padding-top:4px;
  }
  .tiles { display:flex; flex-wrap:wrap; gap:3px; }
  .tile {
    width:34px; height:34px; padding:0; cursor:pointer;
    border:1px solid var(--rule-strong);
    background:
      linear-gradient(0deg,
        rgba(255,180,84, calc(var(--intensity) * 0.85)),
        rgba(255,180,84, calc(var(--intensity) * 0.85))
      ),
      var(--bg-deep);
    color:var(--type);
    font:700 9px/1 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.04em; text-transform:uppercase;
    display:flex; align-items:center; justify-content:center;
    transition:transform .08s, border-color .12s;
  }
  .tile.zero { color:var(--muted); border-color:#1a1612; }
  .tile:hover { border-color:var(--amber); transform:scale(1.12); z-index:5; }
  .tile.selected {
    outline:2px solid var(--amber); outline-offset:1px;
    border-color:var(--amber); z-index:6;
  }
  .tile.dim { opacity:.18; pointer-events:none; }

  /* detail panel */
  .detail { margin:24px 0 12px; }
  .detail-head {
    display:flex; align-items:baseline; justify-content:space-between;
    border-bottom:1px solid var(--amber); padding-bottom:6px;
  }
  .detail-head h2 {
    font:700 13px/1.2 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.16em; text-transform:uppercase; color:var(--amber);
    margin:0;
  }
  .detail-clear {
    background:transparent; border:0; color:var(--muted);
    font:700 10px/1 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.12em; cursor:pointer;
  }
  .detail-clear:hover { color:var(--amber); }
  .detail-co {
    margin:10px 0 14px; display:flex; flex-wrap:wrap; gap:6px;
  }
  .co-chip {
    font:600 10px/1 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.06em; color:var(--type-dim);
    border:1px solid var(--rule-strong); padding:5px 8px;
    background:var(--bg-deep);
  }
  .co-chip b { color:var(--amber); margin-right:5px; }

  .detail-items { list-style:none; padding:0; margin:8px 0 0; }
  .detail-items li {
    padding:10px 0; border-bottom:1px solid var(--rule);
    display:grid; grid-template-columns:90px 1fr; gap:14px; align-items:baseline;
  }
  .detail-items .when {
    font:11px/1.4 "JetBrains Mono",Consolas,monospace;
    color:var(--muted); letter-spacing:0.04em;
  }
  .detail-items .title-link {
    font-family:"Iowan Old Style","Source Serif Pro",Georgia,serif;
    font-size:15px; color:var(--type); line-height:1.45;
  }
  .detail-items .title-link a { color:var(--type); border-bottom:1px solid transparent; }
  .detail-items .title-link a:hover { color:var(--amber); border-bottom-color:var(--amber); }
  .detail-items .src {
    font:10px/1.4 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.08em; color:var(--muted); margin-top:4px;
  }

  .empty-state {
    text-align:center; color:var(--muted); font-size:11px;
    letter-spacing:0.16em; text-transform:uppercase; padding:32px 0;
  }
  .empty-state.hidden { display:none; }

  @media (max-width:640px) {
    .region-row { grid-template-columns:1fr; gap:4px; }
    .region-label { padding-top:0; }
    .tile { width:30px; height:30px; }
    .detail-items li { grid-template-columns:1fr; gap:2px; }
  }
</style>
"""


def _country_view_script() -> str:
    """Inline JS — hover handled by browser title; click drives the detail
    panel and the SVG co-mention overlay. Vanilla JS, no dependencies."""
    return """
<script>
(function() {
  const root = document.querySelector('.carto');
  const overlay = document.querySelector('.carto-overlay');
  const detail = document.getElementById('detail');
  const empty = document.getElementById('empty-state');
  const dataEl = document.getElementById('country-data');
  const DATA = JSON.parse(dataEl.textContent);
  let selectedIso = null;
  let activeRegion = 'all';
  // Timeline state: window in days. 0 = ALL (no upper bound on age).
  let windowDays = 90;

  // Compute {ISO: count} restricted to the last `days` (0 = entire timeline).
  // Falls back to server-supplied DATA.countries[iso].count if the timeline
  // payload is empty (caller didn't pass timeline=).
  function countsForWindow(days) {
    const tl = DATA.timeline || {};
    const dates = Object.keys(tl);
    if (!dates.length) {
      const out = {};
      for (const [iso, c] of Object.entries(DATA.countries)) out[iso] = c.count || 0;
      return out;
    }
    let cutoff = '';
    if (days > 0) {
      const d = new Date();
      d.setUTCDate(d.getUTCDate() - days);
      cutoff = d.toISOString().slice(0, 10);
    }
    const totals = {};
    for (const date of dates) {
      if (cutoff && date < cutoff) continue;
      const bucket = tl[date] || {};
      for (const [iso, n] of Object.entries(bucket)) {
        totals[iso] = (totals[iso] || 0) + n;
      }
    }
    return totals;
  }

  function repaintHeat() {
    const counts = countsForWindow(windowDays);
    const max = Math.max(1, ...Object.values(counts));
    let totalTags = 0, countriesSeen = 0;
    document.querySelectorAll('.tile').forEach(t => {
      const iso = t.dataset.iso;
      const cnt = counts[iso] || 0;
      t.dataset.count = cnt;
      t.style.setProperty('--intensity', (cnt / max).toFixed(3));
      t.classList.toggle('zero', cnt === 0);
      const name = (DATA.countries[iso] || {}).name || iso;
      t.title = name + ' — ' + cnt + ' mention' + (cnt === 1 ? '' : 's');
      if (cnt > 0) { totalTags += cnt; countriesSeen += 1; }
    });
    const stats = document.getElementById('tl-stats');
    if (stats) {
      const label = windowDays === 0 ? 'ALL TIME' : ('LAST ' + windowDays + 'D');
      stats.textContent = label + ' · ' + totalTags + ' TAGS · ' + countriesSeen + ' COUNTRIES';
    }
    // Also refresh the per-region totals on the region buttons.
    document.querySelectorAll('.region-btn').forEach(b => {
      const slug = b.dataset.region;
      if (slug === 'all') return;
      let n = 0;
      for (const [iso, c] of Object.entries(DATA.countries)) {
        if ((c.region || '') === slug) n += counts[iso] || 0;
      }
      const span = b.querySelector('.rb-count');
      if (span) span.textContent = n;
    });
    if (selectedIso) drawArcs(selectedIso);  // re-render arcs against new layout
  }

  function tileFor(iso) { return root.querySelector('.tile[data-iso="' + iso + '"]'); }

  function clearOverlay() {
    overlay.innerHTML = '';
  }

  function drawArcs(iso) {
    clearOverlay();
    const co = DATA.co[iso] || {};
    const entries = Object.entries(co).sort((a, b) => b[1] - a[1]).slice(0, 20);
    if (!entries.length) return;
    const wrap = root.parentElement.getBoundingClientRect();
    overlay.setAttribute('viewBox', '0 0 ' + wrap.width + ' ' + wrap.height);
    const srcEl = tileFor(iso);
    if (!srcEl) return;
    const srcR = srcEl.getBoundingClientRect();
    const sx = srcR.left - wrap.left + srcR.width / 2;
    const sy = srcR.top  - wrap.top  + srcR.height / 2;
    const maxCount = Math.max(...entries.map(e => e[1]));
    for (const [other, cnt] of entries) {
      const tgt = tileFor(other);
      if (!tgt) continue;
      const tr = tgt.getBoundingClientRect();
      const tx = tr.left - wrap.left + tr.width / 2;
      const ty = tr.top  - wrap.top  + tr.height / 2;
      // Quadratic bezier control point: midpoint shifted perpendicular for
      // a parabolic feel. Curvature scales with distance so distant arcs
      // bow more dramatically.
      const dx = tx - sx, dy = ty - sy;
      const dist = Math.hypot(dx, dy);
      const cx = (sx + tx) / 2 + (-dy / dist) * (dist * 0.18);
      const cy = (sy + ty) / 2 + ( dx / dist) * (dist * 0.18);
      const w = 1 + 3 * (cnt / maxCount);
      const op = 0.30 + 0.55 * (cnt / maxCount);
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + sx + ' ' + sy + ' Q ' + cx + ' ' + cy + ' ' + tx + ' ' + ty);
      path.setAttribute('stroke-width', w.toFixed(2));
      path.setAttribute('opacity', op.toFixed(2));
      overlay.appendChild(path);
    }
  }

  function showDetail(iso) {
    selectedIso = iso;
    const c = DATA.countries[iso] || {};
    const items = DATA.items[iso] || [];
    const co = DATA.co[iso] || {};

    document.querySelectorAll('.tile').forEach(t => t.classList.remove('selected'));
    const tile = tileFor(iso);
    if (tile) tile.classList.add('selected');

    document.getElementById('detail-title').textContent =
      (c.name || iso) + '  ·  ' + (c.count || 0) + ' MENTION' + (c.count === 1 ? '' : 'S');

    const coDiv = document.getElementById('detail-co');
    coDiv.innerHTML = '';
    const coSorted = Object.entries(co).sort((a, b) => b[1] - a[1]).slice(0, 10);
    if (coSorted.length) {
      coDiv.innerHTML = coSorted.map(
        ([other, n]) =>
          '<span class="co-chip"><b>' + n + '</b>' +
          ((DATA.countries[other] || {}).name || other) + '</span>'
      ).join('');
    }

    const ul = document.getElementById('detail-items');
    ul.innerHTML = '';
    if (!items.length) {
      ul.innerHTML = '<li style="grid-template-columns:1fr;color:var(--muted)">' +
        'No recent items mention this country in the window.</li>';
    } else {
      for (const it of items) {
        const when = (it.date || '').slice(0, 10) || '—';
        const title = it.title || '';
        const url = it.url || '';
        const src = it.source || '';
        const titleHtml = url && url.startsWith('http')
          ? '<a href="' + url + '" target="_blank" rel="noopener">' + escapeHtml(title) + '</a>'
          : escapeHtml(title);
        const li = document.createElement('li');
        li.innerHTML =
          '<span class="when">' + escapeHtml(when) + '</span>' +
          '<div><div class="title-link">' + titleHtml + '</div>' +
          '<div class="src">' + escapeHtml(src) + '</div></div>';
        ul.appendChild(li);
      }
    }

    detail.hidden = false;
    empty.classList.add('hidden');
    drawArcs(iso);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }

  function clearSelection() {
    selectedIso = null;
    document.querySelectorAll('.tile').forEach(t => t.classList.remove('selected'));
    detail.hidden = true;
    empty.classList.remove('hidden');
    clearOverlay();
  }

  function applyRegionFilter(region) {
    activeRegion = region;
    document.querySelectorAll('.region-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.region === region);
    });
    if (region === 'all') {
      document.querySelectorAll('.region-row').forEach(r => r.classList.remove('dim'));
      document.querySelectorAll('.tile').forEach(t => t.classList.remove('dim'));
      return;
    }
    document.querySelectorAll('.region-row').forEach(r => {
      r.classList.toggle('dim', r.dataset.region !== region);
    });
    document.querySelectorAll('.tile').forEach(t => {
      const iso = t.dataset.iso;
      const r = (DATA.countries[iso] || {}).region || '';
      t.classList.toggle('dim', r !== region);
    });
    if (selectedIso) {
      const r = (DATA.countries[selectedIso] || {}).region || '';
      if (r !== region) clearSelection();
      else drawArcs(selectedIso);  // re-render arcs in case layout shifted
    }
  }

  // Wire events
  root.addEventListener('click', e => {
    const tile = e.target.closest('.tile');
    if (tile && !tile.classList.contains('dim')) {
      showDetail(tile.dataset.iso);
    }
  });
  document.getElementById('detail-clear').addEventListener('click', clearSelection);
  document.querySelectorAll('.region-btn').forEach(b => {
    b.addEventListener('click', () => applyRegionFilter(b.dataset.region));
  });
  document.querySelectorAll('.tl-btn').forEach(b => {
    b.addEventListener('click', () => {
      windowDays = parseInt(b.dataset.days, 10);
      document.querySelectorAll('.tl-btn').forEach(x => x.classList.toggle('active', x === b));
      repaintHeat();
    });
  });
  // Initial paint reflects the default window (90D) if timeline data present.
  if (DATA.timeline && Object.keys(DATA.timeline).length) repaintHeat();
  window.addEventListener('resize', () => { if (selectedIso) drawArcs(selectedIso); });
})();
</script>
"""


def render_about(
    sources: list[dict],
    *,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
    use_new_copy: bool = True,
) -> str:
    def _classify(tags: set) -> str:
        if "state" in tags:                            return "uae-state"
        if "entity" in tags:                           return "uae-entity"
        if "uae" in tags and "press" in tags:          return "uae-press"
        if "un" in tags:                               return "un"
        if "multilateral" in tags:                     return "multilateral"
        if "donor" in tags:                            return "donor"
        if "specialist" in tags:                       return "specialist"
        if "wire" in tags or ("press" in tags and "global" in tags):
                                                       return "wire"
        if "humanitarian" in tags:                     return "humanitarian"
        if "events" in tags:                           return "events"
        if "iati" in tags:                             return "iati"
        if "dev-finance" in tags:                      return "dev-finance"
        if "newsletter" in tags or "email" in tags:    return "newsletter"
        return "other"

    bucket_order = [
        ("uae-state",     "UAE state & wire"),
        ("uae-entity",    "UAE operating entities"),
        ("uae-press",     "UAE & Gulf press"),
        ("un",            "UN agencies"),
        ("multilateral",  "Multilateral finance"),
        ("donor",         "Donor agencies (peer set)"),
        ("specialist",    "Specialist trade press"),
        ("wire",          "Wires & global news"),
        ("humanitarian",  "Humanitarian-specialist"),
        ("events",        "Real-time event detection"),
        ("iati",          "Aid-activity data"),
        ("dev-finance",   "Development finance"),
        ("newsletter",    "Email newsletters"),
        ("other",         "Other"),
    ]
    grouped: dict[str, list[dict]] = {}
    for s in sources:
        if not s.get("enabled", True):
            continue
        grouped.setdefault(_classify(set(s.get("tags") or [])), []).append(s)
    bucket_labels = dict(bucket_order)
    sorted_buckets = [k for k, _ in bucket_order if k in grouped]

    body: list[str] = []
    # about.html sits at the site root → no path prefix needed
    body.append(_masthead(on_page="about", tag="ABOUT", link_prefix=""))
    body.append('<div class="about">')
    sub_anchor = (
        f'<a href="#subscribe">steps to subscribe</a>'
    )
    bot_href = (
        f'https://t.me/{telegram_bot}' if telegram_bot else '#subscribe'
    )

    body.append(
        '<p style="font-size:17px;color:var(--type);margin-top:18px;">'
        'Dalila is a daily intelligence brief on the global humanitarian, '
        'development, and philanthropy ecosystem &mdash; with a sharp focus '
        'on the UAE&rsquo;s role within it. It exists both as a website and '
        f'a Telegram bot ({sub_anchor}). Dalila skims through dozens of news '
        'sources to get development practitioners the context they need to '
        'make informed decisions.</p>'
    )

    body.append('<h2>How it works</h2>')
    body.append(
        '<p>Every 30 minutes Dalila ingests new items from the sources below. '
        'A keyword and entity prefilter drops items unrelated to the '
        'humanitarian, development, and philanthropy ecosystem remit. '
        'Surviving items go through a classifier that assigns a category, '
        'UAE-relevance score, severity, and topical tags. Near-duplicates '
        'are deduplicated. At 06:30 GST an editor model composes the morning '
        f'digest from the previous 24 hours of classified items above '
        f'threshold. The digest is shared with subscribers on Telegram '
        f'({sub_anchor}).</p>'
    )
    body.append(
        '<p>Subscribers can ask for a deeper dive on a topic '
        '(<code>/more &lt;topic&gt;</code>), review tracked UAE doctrine '
        'positions (<code>/doctrine</code>), pull recent UAE financial '
        'commitments and bilateral meetings (<code>/commitments</code>, '
        '<code>/meetings</code>), or zoom in on recent news related to a '
        'specific country (<code>/country &lt;name&gt;</code>). Items are '
        'numbered so a reply with <code>link 3</code> returns the underlying '
        'source URL.</p>'
    )

    body.append('<h2>Sources</h2>')
    for bucket in sorted_buckets:
        items = grouped[bucket]
        if not items:
            continue
        label = bucket_labels.get(bucket, bucket.title())
        body.append(f'<div class="source-block"><strong>{html.escape(label)}</strong><ul>')
        for s in sorted(items, key=lambda x: x.get("name") or ""):
            name = html.escape(s.get("name") or s.get("id", ""))
            url = s.get("url") or ""
            kind = html.escape(s.get("kind") or "")
            if url and url.startswith("http"):
                name_html = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{name}</a>'
            else:
                name_html = name
            body.append(f'<li>{name_html} <span class="note">— {kind}</span></li>')
        body.append('</ul></div>')

    body.append('<h2 id="subscribe">Subscribe</h2>')
    if telegram_bot:
        body.append(
            f'<p>1. Open Telegram and search for '
            f'<a href="https://t.me/{telegram_bot}">@{telegram_bot}</a>.<br>'
            f'2. Tap <strong>Start</strong> (or send <code>/start</code>).<br>'
            f'3. The morning digest will arrive around 06:30 GST each day.</p>'
        )
        body.append('<p>To unsubscribe, send <code>/stop</code> in the same chat.</p>')
    else:
        body.append('<p>Telegram bot will be linked here once the bot is published.</p>')

    body.append('<h2>Suggestions</h2>')
    body.append(
        f'<p>Feedback, source suggestions, bug reports, or requests for new '
        f'features: <a href="mailto:{html.escape(contact_email)}">'
        f'{html.escape(contact_email)}</a>.</p>'
    )
    body.append('</div>')

    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — About", "\n".join(body))
