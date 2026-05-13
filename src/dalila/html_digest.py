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

/* Methodology page */
.methodology {{
  max-width: 880px;
  margin: 0 auto;
  padding: 12px 0 32px;
  font-family: "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-size: 15px;
  line-height: 1.6;
  color: var(--type);
}}
.methodology h1 {{ display: none; }}  /* MASTHEAD already shows title */
.methodology h2 {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--amber);
  border-top: 1px solid var(--rule-strong);
  padding-top: 18px;
  margin: 28px 0 12px;
}}
.methodology h3 {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--type);
  margin: 18px 0 8px;
}}
.methodology p {{ margin: 0 0 12px; }}
.methodology ul {{ margin: 0 0 14px; padding-left: 22px; }}
.methodology li {{ margin: 4px 0; }}
.methodology hr {{ border: none; border-top: 1px dashed var(--rule); margin: 24px 0; }}
.methodology code {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 0.88em; color: var(--amber); background: var(--bg-deep);
  padding: 1px 5px; border: 1px solid var(--rule-strong);
}}
.method-table-wrap {{ overflow-x: auto; margin: 8px 0 18px; }}
.method-table {{
  width: 100%; border-collapse: collapse;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
}}
.method-table th, .method-table td {{
  border: 1px solid var(--rule-strong);
  padding: 8px 10px; text-align: left; vertical-align: top;
}}
.method-table thead th {{
  background: var(--bg-deep);
  color: var(--amber);
  font-weight: 700;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}}
.method-table td:first-child {{ color: var(--type); font-weight: 600; }}
.method-table td strong {{ color: var(--amber); }}

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
    <a href="{link_prefix}methodology.html"{_attr("methodology")}>Methodology</a>
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

    # World map (D3 + world-atlas topojson, rendered client-side).
    # The container is empty server-side; JS injects a Natural-Earth-projected
    # SVG and colours each country path by mention count. Arcs for co-mentions
    # are drawn into the `.carto-overlay` layer the same way the old cartogram
    # did, just between SVG path centroids instead of tile centres.
    body.append(
        '<div class="carto-wrap">'
        '<div id="world-map" class="world-map" aria-label="World mention heatmap">'
        '<div class="map-loading">Loading world map…</div>'
        '</div>'
        '<svg class="carto-overlay" aria-hidden="true"></svg>'
        '<div id="map-tooltip" class="map-tooltip" hidden></div>'
        '<div class="map-legend" id="map-legend" aria-hidden="true">'
        '<span class="legend-label">MENTIONS</span>'
        '<span class="legend-swatch" data-h="0" title="0"></span>'
        '<span class="legend-swatch" data-h="1"></span>'
        '<span class="legend-swatch" data-h="2"></span>'
        '<span class="legend-swatch" data-h="3"></span>'
        '<span class="legend-swatch" data-h="4"></span>'
        '<span class="legend-swatch" data-h="5"></span>'
        '<span class="legend-swatch" data-h="6"></span>'
        '<span class="legend-swatch" data-h="7"></span>'
        '<span class="legend-swatch" data-h="8"></span>'
        '<span class="legend-low">low</span>'
        '<span class="legend-high" id="legend-high">high</span>'
        '</div>'
        '</div>'
    )

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

  /* world map (D3 + world-atlas) */
  .carto-wrap { position:relative; margin:0 0 24px; background:var(--bg-deep);
    border:1px solid var(--rule-strong); padding:8px;
  }
  .carto-overlay {
    position:absolute; inset:0; width:100%; height:100%;
    pointer-events:none; overflow:visible;
  }
  .carto-overlay path {
    fill:none; stroke:var(--amber); opacity:.65;
    stroke-linecap:round;
  }
  .world-map { width:100%; min-height:420px; }
  .world-map svg { display:block; width:100%; height:auto; }
  .world-map path.country {
    /* Default fill is overridden inline by JS via fillForCount(); this is
       the pre-paint state and the "no data" fallback. Stroke is a touch
       darker than the lightest stop so the lowest-tier countries still
       show their borders. */
    fill:#1a1612; stroke:#0a0805; stroke-width:0.4;
    cursor:pointer; transition:fill .12s, stroke .12s;
  }
  .world-map path.country.has-data { cursor:pointer; }
  .world-map path.country:hover {
    stroke:var(--amber); stroke-width:1.0;
  }
  .world-map path.country.selected {
    stroke:var(--amber); stroke-width:1.5;
  }
  .world-map path.country.dim { opacity:.22; }
  .world-map .graticule {
    fill:none; stroke:#1f1a12; stroke-width:0.3; opacity:.55;
  }
  .world-map .sphere {
    fill:#0e0c08; stroke:#2a2218; stroke-width:0.5;
  }
  .map-tooltip {
    position:absolute; pointer-events:none; z-index:20;
    background:rgba(10,10,10,0.95); color:var(--type);
    border:1px solid var(--amber); padding:6px 9px;
    font:600 11px/1.3 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.04em; max-width:220px;
    transform:translate(-50%, calc(-100% - 10px));
  }
  .map-tooltip b { color:var(--amber); }

  /* loading hint shown until the topojson + D3 load */
  .map-loading {
    padding:120px 20px; text-align:center; color:var(--muted);
    font:600 12px/1.3 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.10em; text-transform:uppercase;
  }

  /* legend below the map */
  .map-legend {
    display:flex; align-items:center; gap:4px;
    margin:10px 4px 0; padding-top:8px;
    border-top:1px dashed var(--rule);
    font:600 10px/1 "JetBrains Mono",Consolas,monospace;
    letter-spacing:0.10em; color:var(--type-dim);
  }
  .legend-label { margin-right:8px; }
  .legend-swatch {
    width:22px; height:14px; display:inline-block;
    border:1px solid #2a2218;
  }
  /* Heat swatches — kept in sync with HEAT_STOPS in _country_view_script */
  .legend-swatch[data-h="0"] { background:#1a1612; }
  .legend-swatch[data-h="1"] { background:#3d2a14; }
  .legend-swatch[data-h="2"] { background:#5e3d18; }
  .legend-swatch[data-h="3"] { background:#80541c; }
  .legend-swatch[data-h="4"] { background:#a06d22; }
  .legend-swatch[data-h="5"] { background:#c08628; }
  .legend-swatch[data-h="6"] { background:#dba038; }
  .legend-swatch[data-h="7"] { background:#f0b84a; }
  .legend-swatch[data-h="8"] { background:#ffcf66; }
  .legend-low { margin-left:6px; color:var(--muted); }
  .legend-high { margin-left:auto; color:var(--amber); }

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
    """Inline JS for the world-map country view.

    Loads d3-geo + topojson + the world-atlas 110m TopoJSON from jsdelivr,
    projects via d3.geoNaturalEarth1, then colours each country path by
    mention count. ISO 3166-1 numeric → alpha-2 lookup is embedded inline
    (~250 entries) so we never round-trip to a remote API for IDs.

    All the existing affordances are preserved: hover (now via tooltip
    instead of native title), click for detail panel + co-mention arcs,
    region filter dims out-of-region paths, timeline chips re-bucket the
    heatmap from the embedded timeline payload.
    """
    return """
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/topojson-client@3/dist/topojson-client.min.js"></script>
<script>
(function() {
  const wrap = document.querySelector('.carto-wrap');
  const mapEl = document.getElementById('world-map');
  const overlay = document.querySelector('.carto-overlay');
  const tooltip = document.getElementById('map-tooltip');
  const detail = document.getElementById('detail');
  const empty = document.getElementById('empty-state');
  const dataEl = document.getElementById('country-data');
  const DATA = JSON.parse(dataEl.textContent);
  let selectedIso = null;
  let activeRegion = 'all';
  // Timeline state: window in days. 0 = ALL (no upper bound on age).
  let windowDays = 90;

  // ISO 3166-1 numeric → alpha-2. world-atlas v2 keys features by numeric.
  const N3_TO_A2 = {
    "004":"AF","008":"AL","010":"AQ","012":"DZ","016":"AS","020":"AD","024":"AO",
    "028":"AG","031":"AZ","032":"AR","036":"AU","040":"AT","044":"BS","048":"BH",
    "050":"BD","051":"AM","052":"BB","056":"BE","060":"BM","064":"BT","068":"BO",
    "070":"BA","072":"BW","076":"BR","084":"BZ","090":"SB","092":"VG","096":"BN",
    "100":"BG","104":"MM","108":"BI","112":"BY","116":"KH","120":"CM","124":"CA",
    "132":"CV","140":"CF","144":"LK","148":"TD","152":"CL","156":"CN","158":"TW",
    "170":"CO","174":"KM","178":"CG","180":"CD","188":"CR","191":"HR","192":"CU",
    "196":"CY","203":"CZ","204":"BJ","208":"DK","212":"DM","214":"DO","218":"EC",
    "222":"SV","226":"GQ","231":"ET","232":"ER","233":"EE","242":"FJ","246":"FI",
    "250":"FR","254":"GF","258":"PF","260":"TF","262":"DJ","266":"GA","268":"GE",
    "270":"GM","275":"PS","276":"DE","288":"GH","292":"GI","296":"KI","300":"GR",
    "304":"GL","308":"GD","312":"GP","316":"GU","320":"GT","324":"GN","328":"GY",
    "332":"HT","340":"HN","344":"HK","348":"HU","352":"IS","356":"IN","360":"ID",
    "364":"IR","368":"IQ","372":"IE","376":"IL","380":"IT","384":"CI","388":"JM",
    "392":"JP","398":"KZ","400":"JO","404":"KE","408":"KP","410":"KR","414":"KW",
    "417":"KG","418":"LA","422":"LB","426":"LS","428":"LV","430":"LR","434":"LY",
    "438":"LI","440":"LT","442":"LU","446":"MO","450":"MG","454":"MW","458":"MY",
    "462":"MV","466":"ML","470":"MT","474":"MQ","478":"MR","480":"MU","484":"MX",
    "492":"MC","496":"MN","498":"MD","499":"ME","500":"MS","504":"MA","508":"MZ",
    "512":"OM","516":"NA","520":"NR","524":"NP","528":"NL","531":"CW","533":"AW",
    "534":"SX","540":"NC","548":"VU","554":"NZ","558":"NI","562":"NE","566":"NG",
    "570":"NU","578":"NO","580":"MP","583":"FM","584":"MH","585":"PW","586":"PK",
    "591":"PA","598":"PG","600":"PY","604":"PE","608":"PH","612":"PN","616":"PL",
    "620":"PT","624":"GW","626":"TL","630":"PR","634":"QA","638":"RE","642":"RO",
    "643":"RU","646":"RW","652":"BL","654":"SH","659":"KN","660":"AI","662":"LC",
    "663":"MF","666":"PM","670":"VC","674":"SM","678":"ST","682":"SA","686":"SN",
    "688":"RS","690":"SC","694":"SL","702":"SG","703":"SK","704":"VN","705":"SI",
    "706":"SO","710":"ZA","716":"ZW","724":"ES","728":"SS","729":"SD","732":"EH",
    "740":"SR","744":"SJ","748":"SZ","752":"SE","756":"CH","760":"SY","762":"TJ",
    "764":"TH","768":"TG","772":"TK","776":"TO","780":"TT","784":"AE","788":"TN",
    "792":"TR","795":"TM","796":"TC","798":"TV","800":"UG","804":"UA","807":"MK",
    "818":"EG","826":"GB","834":"TZ","840":"US","850":"VI","854":"BF","858":"UY",
    "860":"UZ","862":"VE","876":"WF","882":"WS","887":"YE","894":"ZM"
  };

  // Build the SVG world map once, then drive the rest with state updates.
  const WIDTH = 980, HEIGHT = 500;
  let pathGen = null;  // d3.geoPath() — for arc centroids later
  let projection = null;  // exposed so capital lookups can project directly
  let mapReady = false;
  let countriesGeo = null;

  // Capital cities — ISO-2 → [longitude, latitude]. Used as arc anchors
  // instead of polygon centroids, which land in geographically unhelpful
  // places (USA centroid → Kansas, Russia → Siberia, Indonesia → ocean).
  // Capitals are the recognizable "this is where this country is" pin.
  // Fallback to polygon centroid for any ISO not in this table.
  const CAPITALS = {
    "AE":[54.37,24.45],"AF":[69.18,34.53],"AL":[19.82,41.33],"AM":[44.50,40.18],
    "AO":[13.23,-8.84],"AR":[-58.38,-34.61],"AT":[16.37,48.21],"AU":[149.13,-35.31],
    "AZ":[49.87,40.41],"BA":[18.41,43.86],"BB":[-59.62,13.10],"BD":[90.41,23.81],
    "BE":[4.35,50.85],"BF":[-1.52,12.37],"BG":[23.32,42.70],"BH":[50.59,26.23],
    "BI":[29.36,-3.38],"BJ":[2.63,6.50],"BN":[114.94,4.90],"BO":[-68.15,-16.50],
    "BR":[-47.93,-15.79],"BS":[-77.34,25.05],"BT":[89.64,27.47],"BW":[25.92,-24.65],
    "BY":[27.57,53.90],"BZ":[-88.77,17.25],"CA":[-75.70,45.42],"CD":[15.27,-4.32],
    "CF":[18.56,4.36],"CG":[15.27,-4.27],"CH":[7.45,46.95],"CI":[-5.27,6.83],
    "CL":[-70.66,-33.45],"CM":[11.50,3.85],"CN":[116.41,39.90],"CO":[-74.08,4.71],
    "CR":[-84.09,9.93],"CU":[-82.36,23.13],"CV":[-23.51,14.93],"CY":[33.38,35.18],
    "CZ":[14.44,50.08],"DE":[13.41,52.52],"DJ":[43.15,11.59],"DK":[12.57,55.68],
    "DM":[-61.39,15.31],"DO":[-69.93,18.49],"DZ":[3.06,36.75],"EC":[-78.46,-0.18],
    "EE":[24.75,59.43],"EG":[31.24,30.05],"ER":[38.93,15.34],"ES":[-3.70,40.42],
    "ET":[38.74,9.03],"FI":[24.94,60.17],"FJ":[178.45,-18.12],"FM":[158.16,6.92],
    "FR":[2.35,48.86],"GA":[9.45,0.42],"GB":[-0.13,51.51],"GD":[-61.74,12.05],
    "GE":[44.79,41.72],"GH":[-0.19,5.55],"GM":[-16.58,13.45],"GN":[-13.71,9.51],
    "GQ":[8.78,3.75],"GR":[23.73,37.98],"GT":[-90.51,14.63],"GW":[-15.59,11.86],
    "GY":[-58.16,6.80],"HN":[-87.21,14.07],"HR":[15.99,45.81],"HT":[-72.34,18.59],
    "HU":[19.04,47.50],"ID":[106.83,-6.21],"IE":[-6.27,53.35],"IL":[35.21,31.78],
    "IN":[77.21,28.61],"IQ":[44.36,33.32],"IR":[51.39,35.69],"IS":[-21.95,64.13],
    "IT":[12.50,41.90],"JM":[-76.79,17.97],"JO":[35.93,31.95],"JP":[139.69,35.69],
    "KE":[36.82,-1.29],"KG":[74.59,42.87],"KH":[104.92,11.55],"KI":[172.97,1.45],
    "KM":[43.27,-11.70],"KN":[-62.73,17.30],"KP":[125.75,39.04],"KR":[126.98,37.57],
    "KW":[47.98,29.38],"KZ":[71.43,51.13],"LA":[102.62,17.97],"LB":[35.50,33.89],
    "LC":[-61.00,14.01],"LI":[9.52,47.14],"LK":[79.86,6.93],"LR":[-10.80,6.30],
    "LS":[27.48,-29.31],"LT":[25.28,54.69],"LU":[6.13,49.61],"LV":[24.11,56.95],
    "LY":[13.19,32.89],"MA":[-6.84,34.02],"MC":[7.42,43.74],"MD":[28.86,47.01],
    "ME":[19.26,42.44],"MG":[47.52,-18.88],"MH":[171.18,7.12],"MK":[21.43,41.99],
    "ML":[-8.00,12.65],"MM":[96.13,19.76],"MN":[106.92,47.92],"MR":[-15.98,18.08],
    "MT":[14.51,35.90],"MU":[57.50,-20.16],"MV":[73.51,4.18],"MW":[33.78,-13.96],
    "MX":[-99.13,19.43],"MY":[101.69,3.14],"MZ":[32.57,-25.97],"NA":[17.08,-22.56],
    "NE":[2.11,13.51],"NG":[7.49,9.06],"NI":[-86.25,12.11],"NL":[4.90,52.37],
    "NO":[10.75,59.91],"NP":[85.32,27.71],"NR":[166.92,-0.55],"NZ":[174.78,-41.29],
    "OM":[58.38,23.59],"PA":[-79.52,8.98],"PE":[-77.04,-12.04],"PG":[147.18,-9.44],
    "PH":[120.98,14.60],"PK":[73.05,33.69],"PL":[21.01,52.23],"PS":[35.20,31.90],
    "PT":[-9.14,38.71],"PY":[-57.58,-25.26],"QA":[51.53,25.29],"RO":[26.10,44.43],
    "RS":[20.46,44.81],"RU":[37.62,55.75],"RW":[30.06,-1.94],"SA":[46.68,24.71],
    "SB":[159.96,-9.43],"SC":[55.46,-4.62],"SD":[32.56,15.50],"SE":[18.07,59.33],
    "SG":[103.85,1.35],"SI":[14.51,46.05],"SK":[17.11,48.15],"SL":[-13.23,8.48],
    "SM":[12.45,43.94],"SN":[-17.45,14.69],"SO":[45.34,2.05],"SR":[-55.20,5.85],
    "SS":[31.59,4.86],"ST":[6.73,0.34],"SV":[-89.21,13.69],"SY":[36.30,33.51],
    "SZ":[31.13,-26.32],"TD":[15.05,12.13],"TG":[1.21,6.13],"TH":[100.50,13.76],
    "TJ":[68.79,38.54],"TL":[125.58,-8.56],"TM":[58.38,37.96],"TN":[10.18,36.81],
    "TO":[-175.20,-21.13],"TR":[32.87,39.93],"TT":[-61.51,10.65],"TV":[179.21,-8.52],
    "TW":[121.57,25.03],"TZ":[35.74,-6.16],"UA":[30.52,50.45],"UG":[32.58,0.35],
    "US":[-77.04,38.91],"UY":[-56.16,-34.90],"UZ":[69.24,41.30],"VA":[12.45,41.90],
    "VC":[-61.22,13.16],"VE":[-66.92,10.49],"VN":[105.85,21.03],"VU":[168.32,-17.73],
    "WS":[-171.77,-13.83],"YE":[44.21,15.35],"ZA":[28.19,-25.75],"ZM":[28.28,-15.40],
    "ZW":[31.05,-17.83]
  };

  function buildMap(world) {
    countriesGeo = topojson.feature(world, world.objects.countries);
    // fitSize with land-only feature collection rather than the full sphere —
    // Natural Earth's actual landmass extents fill the viewport better than
    // fitExtent against the polar regions, which were leaving empty bands.
    projection = d3.geoNaturalEarth1()
      .fitSize([WIDTH, HEIGHT], countriesGeo);
    pathGen = d3.geoPath(projection);

    // Remove the "Loading world map…" placeholder.
    const loader = mapEl.querySelector('.map-loading');
    if (loader) loader.remove();

    const svg = d3.select(mapEl).append("svg")
      .attr("viewBox", "0 0 " + WIDTH + " " + HEIGHT)
      .attr("preserveAspectRatio", "xMidYMid meet");
    svg.append("path").datum({type: "Sphere"})
      .attr("class", "sphere").attr("d", pathGen);
    svg.append("path").datum(d3.geoGraticule10())
      .attr("class", "graticule").attr("d", pathGen);
    svg.append("g").attr("class", "countries")
      .selectAll("path")
      .data(countriesGeo.features)
      .join("path")
      .attr("class", "country")
      .attr("d", pathGen)
      .attr("data-iso", d => N3_TO_A2[String(d.id).padStart(3, "0")] || "")
      .attr("data-name", d => (d.properties && d.properties.name) || "");
    mapReady = true;
    repaintHeat();

    // Hover handlers
    svg.selectAll("path.country")
      .on("mousemove", function(ev) {
        const iso = this.dataset.iso;
        if (!iso) { tooltip.hidden = true; return; }
        const c = DATA.countries[iso] || {};
        const cnt = parseInt(this.dataset.count || "0", 10);
        const name = c.name || this.dataset.name || iso;
        tooltip.innerHTML = name + ' &middot; <b>' + cnt + '</b> mention' + (cnt === 1 ? '' : 's');
        const r = wrap.getBoundingClientRect();
        tooltip.style.left = (ev.clientX - r.left) + "px";
        tooltip.style.top  = (ev.clientY - r.top)  + "px";
        tooltip.hidden = false;
      })
      .on("mouseleave", () => { tooltip.hidden = true; })
      .on("click", function() {
        const iso = this.dataset.iso;
        if (!iso) return;
        if (this.classList.contains('dim')) return;
        showDetail(iso);
      });
  }

  // Kick off the topojson load. world-atlas v2 50m is ~520KB but gives
  // dramatically more accurate borders — capital coordinates reliably
  // land inside their country polygons (the 110m simplification was off
  // by enough on small/coastal countries that arc anchors looked wrong).
  // ~5× larger payload, cached by the CDN, one-time download per user.
  fetch("https://cdn.jsdelivr.net/npm/world-atlas@2/countries-50m.json")
    .then(r => r.json())
    .then(buildMap)
    .catch(err => {
      mapEl.innerHTML = '<div style="padding:24px;color:var(--muted);font:13px Consolas,monospace">' +
        'World map failed to load (' + err + '). Country counts shown above.</div>';
    });

  function pathFor(iso) {
    return mapEl.querySelector('path.country[data-iso="' + iso + '"]');
  }

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

  // Amber heat ramp, 9 stops. Stop 0 is "no mentions in window" — sits just
  // above the ocean tone (#0e0c08) so untracked land is visibly distinct
  // from sea but recedes. Stops 1+ stretch from a perceptible warm tint up
  // to bright amber so even single-digit mention counts have a discernible
  // color. Square-root scaling (vs the previous log) gives mid-volume
  // countries clearly distinct intensities — a country with 30 mentions
  // looks visibly hotter than one with 3, and one with 300 looks visibly
  // hotter than one with 30.
  const HEAT_STOPS = [
    "#1a1612",   // 0  — no mentions: barely above ocean, present but quiet
    "#3d2a14",   // 1  — minimum signal: faint warm tint
    "#5e3d18",
    "#80541c",
    "#a06d22",
    "#c08628",
    "#dba038",
    "#f0b84a",
    "#ffcf66"    // 8  — top-tier: bright amber, max contrast
  ];
  function fillForCount(cnt, max) {
    if (cnt <= 0) return HEAT_STOPS[0];
    // sqrt(t) brightens mid-range without letting the top country dominate.
    // Floor at index 1 so any nonzero count is *visibly* different from zero.
    const t = Math.sqrt(cnt / Math.max(max, 1));
    const idx = Math.min(HEAT_STOPS.length - 1,
                         Math.max(1, Math.round(t * (HEAT_STOPS.length - 1))));
    return HEAT_STOPS[idx];
  }

  function repaintHeat() {
    const counts = countsForWindow(windowDays);
    const max = Math.max(1, ...Object.values(counts));
    let totalTags = 0, countriesSeen = 0;
    if (mapReady) {
      mapEl.querySelectorAll('path.country').forEach(p => {
        const iso = p.dataset.iso;
        const cnt = counts[iso] || 0;
        p.dataset.count = cnt;
        // Use inline `style.fill` (not setAttribute) — the CSS rule
        // `path.country { fill: #1a1612 }` has higher specificity than the
        // `fill` presentation attribute and would silently override us.
        // Inline style wins the cascade.
        p.style.fill = fillForCount(cnt, max);
        p.classList.toggle('has-data', cnt > 0);
      });
    }
    for (const [iso, c] of Object.entries(DATA.countries)) {
      const cnt = counts[iso] || 0;
      if (cnt > 0) { totalTags += cnt; countriesSeen += 1; }
    }
    const stats = document.getElementById('tl-stats');
    if (stats) {
      const label = windowDays === 0 ? 'ALL TIME' : ('LAST ' + windowDays + 'D');
      stats.textContent = label + ' · ' + totalTags + ' TAGS · ' + countriesSeen + ' COUNTRIES';
    }
    // Update legend's "high" label so users can decode the colour ramp.
    const legendHigh = document.getElementById('legend-high');
    if (legendHigh) legendHigh.textContent = 'high (' + max + ')';
    // Per-region totals on the region buttons.
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
    if (selectedIso) drawArcs(selectedIso);  // re-render arcs in new SVG space
  }

  function clearOverlay() { overlay.innerHTML = ''; }

  // Anchor point for arcs — capital city if known, polygon centroid otherwise.
  // Capitals are the geographically-meaningful pin for arc endpoints
  // (USA centroid is in Kansas; capital is Washington DC, much more useful).
  function centroidFor(iso) {
    if (!mapReady) return null;
    const cap = CAPITALS[iso];
    if (cap && projection) {
      const xy = projection(cap);
      if (xy && isFinite(xy[0]) && isFinite(xy[1])) return xy;
    }
    // Fallback: polygon centroid (for any country not in CAPITALS table,
    // or if projection somehow can't resolve the lon/lat).
    const feature = countriesGeo.features.find(
      f => (N3_TO_A2[String(f.id).padStart(3, "0")] || "") === iso
    );
    if (!feature) return null;
    return pathGen.centroid(feature);
  }

  function drawArcs(iso) {
    clearOverlay();
    if (!mapReady) return;
    const co = DATA.co[iso] || {};
    const entries = Object.entries(co).sort((a, b) => b[1] - a[1]).slice(0, 20);
    if (!entries.length) return;
    // Arcs draw into the overlay SVG, sized to the same viewBox as the map.
    overlay.setAttribute('viewBox', '0 0 ' + WIDTH + ' ' + HEIGHT);
    overlay.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    const src = centroidFor(iso);
    if (!src) return;
    const [sx, sy] = src;
    const maxCount = Math.max(...entries.map(e => e[1]));
    for (const [other, cnt] of entries) {
      const tgt = centroidFor(other);
      if (!tgt) continue;
      const [tx, ty] = tgt;
      const dx = tx - sx, dy = ty - sy;
      const dist = Math.hypot(dx, dy) || 1;
      const cx = (sx + tx) / 2 + (-dy / dist) * (dist * 0.22);
      const cy = (sy + ty) / 2 + ( dx / dist) * (dist * 0.22);
      // Discrete-bucket visibility: each arc falls into one of 5 strength
      // tiers based on sqrt(cnt/max). Stepped width + opacity together so
      // arcs read as clearly "weak / medium / strong / very strong" rather
      // than blending into a continuous gradient.
      const r = Math.sqrt(cnt / Math.max(maxCount, 1));
      const TIERS = [
        [0.4, 0.35],   // tier 0: faintest
        [0.9, 0.50],
        [1.5, 0.65],
        [2.2, 0.80],
        [3.0, 0.95],   // tier 4: top
      ];
      const tier = Math.min(TIERS.length - 1, Math.floor(r * TIERS.length));
      const [w, op] = TIERS[tier];
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

    mapEl.querySelectorAll('path.country').forEach(p => p.classList.remove('selected'));
    const pth = pathFor(iso);
    if (pth) pth.classList.add('selected');

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
    mapEl.querySelectorAll('path.country').forEach(p => p.classList.remove('selected'));
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
      mapEl.querySelectorAll('path.country').forEach(p => p.classList.remove('dim'));
      return;
    }
    mapEl.querySelectorAll('path.country').forEach(p => {
      const iso = p.dataset.iso;
      const r = (DATA.countries[iso] || {}).region || '';
      // Countries we don't track (no entry in DATA.countries) also get dimmed
      // when a specific region is active, so the filter visually isolates.
      p.classList.toggle('dim', !DATA.countries[iso] || r !== region);
    });
    if (selectedIso) {
      const r = (DATA.countries[selectedIso] || {}).region || '';
      if (r !== region) clearSelection();
      else drawArcs(selectedIso);
    }
  }

  // Wire events (click handler lives inside buildMap for SVG paths)
  document.getElementById('detail-clear').addEventListener('click', clearSelection);
  document.querySelectorAll('.region-btn').forEach(b => {
    b.addEventListener('click', () => applyRegionFilter(b.dataset.region));
  });
  document.querySelectorAll('.tl-btn').forEach(b => {
    b.addEventListener('click', () => {
      windowDays = parseInt(b.dataset.days, 10);
      document.querySelectorAll('.tl-btn').forEach(x => x.classList.toggle('active', x === b));
      if (mapReady) repaintHeat();
    });
  });
  window.addEventListener('resize', () => { if (selectedIso) drawArcs(selectedIso); });

  // Click on empty ocean / outside any country path clears the selection.
  // (Country clicks are stopped by the SVG path handler via mouseleave logic.)
  mapEl.addEventListener('click', e => {
    if (e.target.tagName !== 'path' || !e.target.classList.contains('country')) {
      if (selectedIso) clearSelection();
    }
  });
})();
</script>
"""


def render_methodology(
    *,
    md_path: "Path | str | None" = None,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Render METHODOLOGY.md into a styled HTML page.

    Source of truth is the repo-root METHODOLOGY.md so the page never drifts
    from the file commits track. Inline converter handles the markdown subset
    we actually use (h1/h2/h3, paragraphs, fenced tables, lists, inline code,
    bold, italic, links). No external dependency on python-markdown / mistune.
    """
    from pathlib import Path as _Path
    if md_path is None:
        # repo root is two levels up from src/dalila/
        md_path = _Path(__file__).resolve().parents[2] / "METHODOLOGY.md"
    md_path = _Path(md_path)
    if not md_path.exists():
        md_text = "# Methodology\n\n_METHODOLOGY.md not found at expected path._"
    else:
        md_text = md_path.read_text(encoding="utf-8")

    body_md = _md_to_html(md_text)

    body: list[str] = []
    body.append(_masthead(on_page="methodology", tag="METHODOLOGY", link_prefix=""))
    body.append('<div class="methodology">')
    body.append(
        '<p class="kicker" style="font-style:italic;color:var(--type-dim);'
        'max-width:64ch;margin:14px 0 24px;font-size:15px;line-height:1.55;">'
        'Every numeric threshold, sampling rate, batch size, and model choice '
        'in this brief encodes a decision. This page is the source of truth '
        'for those decisions and the empirical evidence behind them. '
        'It is generated directly from <code>METHODOLOGY.md</code> in the '
        'repository — when a knob is tuned, the rationale here is updated '
        'in the same commit.</p>'
    )
    body.append(body_md)
    body.append('</div>')
    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — Methodology", "\n".join(body))


def _md_to_html(md: str) -> str:
    """Minimal Markdown → HTML for the subset METHODOLOGY.md uses.

    Handles: headings (#, ##, ###), paragraphs, GFM tables (with | separators
    and the `---` divider row), unordered lists, bold (**x** or __x__),
    italic (*x* or _x_), inline code (`x`), and links ([text](url)).
    Code fences and HTML blocks are passed through verbatim.

    Deliberately not using `markdown` / `mistune` to avoid a runtime dep
    for what is ~80 lines of recognisable regex.
    """
    import re

    lines = md.split("\n")
    out: list[str] = []
    i = 0
    in_ul = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def inline(s: str) -> str:
        # Code first so other patterns don't touch its content
        s = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", s)
        # Links
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        # Bold
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        # Italic
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        return s

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Horizontal rule
        if stripped == "---":
            close_ul()
            out.append("<hr/>")
            i += 1; continue

        # Headings
        for level in (3, 2, 1):
            prefix = "#" * level + " "
            if stripped.startswith(prefix):
                close_ul()
                out.append(f"<h{level}>{inline(stripped[len(prefix):])}</h{level}>")
                i += 1; break
        else:
            # GFM table — line starts with | and next line is the divider
            if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"\|[-:\s|]+\|", lines[i+1].strip()):
                close_ul()
                # Header row
                header_cells = [c.strip() for c in stripped.strip("|").split("|")]
                out.append('<div class="method-table-wrap"><table class="method-table"><thead><tr>')
                for c in header_cells:
                    out.append(f"<th>{inline(c)}</th>")
                out.append("</tr></thead><tbody>")
                i += 2  # skip header and divider
                while i < len(lines) and lines[i].strip().startswith("|"):
                    row_cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    out.append("<tr>")
                    for c in row_cells:
                        out.append(f"<td>{inline(c)}</td>")
                    out.append("</tr>")
                    i += 1
                out.append("</tbody></table></div>")
                continue

            # Unordered list
            if stripped.startswith("- ") or stripped.startswith("* "):
                if not in_ul:
                    out.append("<ul>")
                    in_ul = True
                out.append(f"<li>{inline(stripped[2:])}</li>")
                i += 1; continue

            # Blank line: close any open list, emit paragraph break
            if not stripped:
                close_ul()
                i += 1; continue

            # Default: paragraph (greedily eat consecutive non-blank lines)
            close_ul()
            buf = [inline(stripped)]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(("#", "|", "- ", "* ", "---")):
                buf.append(inline(lines[i].strip()))
                i += 1
            out.append("<p>" + " ".join(buf) + "</p>")

    close_ul()
    return "\n".join(out)


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
        'sources, multilateral feeds, and forecast indices to get '
        'development practitioners the context they need to make informed '
        'decisions.</p>'
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
        '<p>Alongside the news feeds, Dalila ingests <strong>forecast and '
        'early-warning indices</strong> (ACLED CAST for conflict escalation; '
        'ACAPS INFORM for crisis severity; WFP HungerMap for food insecurity; '
        'GDACS for real-time disaster alerts). These appear in the digest&rsquo;s '
        '<em>Foresight</em> section, but with a strict rule: only '
        '<strong>changes</strong> are surfaced, never states. Sudan being '
        'hungry every day produces zero items; Sudan getting hungrier produces '
        'one, tagged 🔴 (worsening) or 🟢 (improving) and showing the delta '
        'against the prior observation.</p>'
    )
    body.append(
        '<p>Every numeric threshold, sampling rate, and model choice in this '
        f'pipeline is documented in the <a href="methodology.html">methodology '
        f'page</a> with its rationale and the empirical evidence behind it. '
        'When a knob is tuned, the rationale is updated in the same commit.</p>'
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
