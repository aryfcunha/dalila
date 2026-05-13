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
    """on_page: 'home' | 'digest' | 'archive' | 'about'.

    `link_prefix` is the relative path back to the site root. `""` for pages
    at root (index, archive, about); `"../"` for pages one level deep
    (digests/YYYY-MM-DD.html). Keeps links working regardless of whether the
    site is served from the domain root or a project path like `/dalila/`.
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
# About page
# ===========================================================================

def render_about(
    sources: list[dict],
    *,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
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
    body.append(
        '<p style="font-size:17px;color:var(--type);margin-top:18px;">'
        'Dalila is a daily intelligence brief on the global humanitarian, '
        'development, and philanthropy ecosystem &mdash; with a sharp focus on '
        'the UAE&rsquo;s role within it. Built by one person, free public '
        'sources only, designed to replace 30 to 60 minutes of fragmented '
        'morning reading.</p>'
    )

    body.append('<h2>How it works</h2>')
    body.append(
        '<p>Every 30 minutes Dalila ingests new items from the sources below. '
        'A keyword and entity prefilter drops items unrelated to the UAE-aid '
        'remit. Surviving items go through a classifier that assigns a '
        'category, UAE-relevance score, severity, and topical tags. Near-'
        'duplicates are deduplicated. At 06:30 GST an editor model composes '
        'the morning digest from the previous 24 hours of classified items '
        'above threshold.</p>'
    )
    body.append(
        '<p>Two-way commands let subscribers ask for a deeper dive on a topic '
        '(<code>/more &lt;topic&gt;</code>), review tracked UAE doctrine '
        'positions (<code>/doctrine</code>), or pull recent UAE financial '
        'commitments and bilateral meetings (<code>/commitments</code>, '
        '<code>/meetings</code>). Items are numbered so a reply with '
        '<code>link 3</code> returns the underlying source URL.</p>'
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

    body.append('<h2>Subscribe</h2>')
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
