"""HTML site renderer for Dalila.

Generates three kinds of page, all as self-contained HTML with inline CSS:

  * `render_digest(items)`         → one daily digest page
  * `render_about(sources, ...)`   → a static "about Dalila" page
  * `render_index(recent_digests)` → archive landing page

Aesthetic direction (deliberately not the leadership-pitch palette): a small
editorial publication. Off-white background, charcoal ink, serif throughout,
one muted forest-green accent used very sparingly. Type-driven, lots of
whitespace, restrained metadata. Reads like a research newsletter, not a
corporate brand artifact.

Pages link to each other:
  index.html   →  digests/<date>.html (most recent first), and to about.html
  about.html   →  /  (back to index)
  digests/*    →  /  (back to index), and to about.html in the footer
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable

import pytz

from dalila.config import get_config


# ---------------------------------------------------------------------------
# Palette — editorial, not pitch-deck
# ---------------------------------------------------------------------------
BG          = "#FAF9F5"          # warm off-white, paper-like
INK         = "#1F2430"          # near-black with a touch of blue for warmth
INK_MUTED   = "#5A6068"          # grey for metadata and dates
RULE        = "#E6E2D6"          # hairline rule, slightly warmer than the bg
ACCENT      = "#3F6E54"          # muted forest green — used VERY sparingly
ACCENT_DEEP = "#2A4D3A"


# Section labels (order = display order). Glyphs are restrained.
SECTIONS = [
    ("humanitarian",                "Humanitarian"),
    ("aid_commitments",             "Aid commitments"),
    ("reports_evidence",            "Reports & evidence"),
    ("conferences_events",          "Conferences & events"),
    ("uae_foreign_policy_signals",  "UAE foreign-policy signals"),
    ("uae_leadership_doctrine",     "UAE doctrine"),
    ("uae_ecosystem_moves",         "UAE ecosystem"),
]


# ===========================================================================
# Shared chrome
# ===========================================================================

def _base_css() -> str:
    """Inline CSS used on every page."""
    return f"""
:root {{
  --bg:{BG};
  --ink:{INK};
  --muted:{INK_MUTED};
  --rule:{RULE};
  --accent:{ACCENT};
  --accent-deep:{ACCENT_DEEP};
}}
html,body {{
  background: var(--bg);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
body {{
  margin: 0;
  font-family: "Source Serif Pro", "Lora", "PT Serif", Georgia, "Times New Roman", serif;
  font-size: 17px;
  line-height: 1.55;
}}
.mono {{
  font-family: "JetBrains Mono", "IBM Plex Mono", "SF Mono", Consolas, monospace;
  font-size: 0.78em;
  letter-spacing: 0.04em;
}}
a {{ color: var(--ink); text-decoration: underline; text-decoration-color: rgba(31,36,48,0.25); text-underline-offset: 3px; }}
a:hover {{ text-decoration-color: var(--accent); color: var(--accent-deep); }}
.wrap {{ max-width: 700px; margin: 0 auto; padding: 56px 32px 96px; }}
hr.rule {{ border: 0; border-top: 1px solid var(--rule); margin: 28px 0; }}

/* ---------- masthead (shared across pages) ---------- */
.masthead {{
  display: flex;
  align-items: baseline;
  gap: 18px;
  padding-bottom: 14px;
  border-bottom: 2px solid var(--ink);
  margin-bottom: 28px;
}}
.masthead a {{ text-decoration: none; color: var(--ink); }}
.masthead-title {{
  margin: 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.005em;
  line-height: 1;
}}
.masthead-title .ar {{
  font-style: italic;
  font-weight: 400;
  color: var(--accent);
  margin-left: 8px;
  font-size: 0.7em;
  vertical-align: 2px;
}}
.masthead-nav {{
  margin-left: auto;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}}
.masthead-nav a {{ color: var(--muted); text-decoration: none; margin-left: 14px; }}
.masthead-nav a:hover {{ color: var(--accent-deep); }}

/* ---------- footer ---------- */
.footer {{
  margin-top: 64px;
  padding-top: 18px;
  border-top: 1px solid var(--rule);
  color: var(--muted);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.06em;
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}}
.footer a {{ color: var(--muted); }}

/* ---------- digest-specific ---------- */
.date-line {{
  color: var(--muted);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  margin: 0 0 6px;
}}
.kicker {{
  font-size: 18px;
  line-height: 1.5;
  color: var(--ink);
  margin: 0 0 36px;
  max-width: 56ch;
}}
.kicker .top-stat {{ color: var(--accent-deep); font-weight: 600; }}

.section {{ margin: 0 0 36px; }}
.section-title {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink);
  border-bottom: 1px solid var(--rule);
  padding-bottom: 6px;
  margin: 0 0 18px;
  display: flex;
  align-items: baseline;
}}
.section-title .count {{
  margin-left: auto;
  color: var(--muted);
  font-weight: 400;
}}

.item {{ padding: 0 0 22px; margin-bottom: 22px; border-bottom: 1px dashed var(--rule); }}
.item:last-child {{ border-bottom: none; }}
.item-title {{ margin: 0 0 8px; font-size: 18px; line-height: 1.35; font-weight: 600; }}
.item-title a {{ color: var(--ink); }}
.item-summary {{ margin: 0 0 8px; color: var(--ink); line-height: 1.55; }}
.item-meta {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: baseline;
}}
.item-meta .src {{ color: var(--ink); }}
.item-meta .rel {{ color: var(--accent-deep); }}
.item-meta a {{ color: var(--muted); text-decoration-color: var(--rule); }}
.item-meta a:hover {{ color: var(--accent-deep); }}
.doctrine-tag {{
  display: inline-block;
  padding: 0 6px;
  border: 1px solid var(--accent);
  color: var(--accent-deep);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}}

/* ---------- top stories (no heavy panel, just a small block) ---------- */
.top-stories {{ margin: 0 0 42px; }}
.top-stories ol {{ margin: 0; padding: 0; list-style: none; counter-reset: top; }}
.top-stories li {{
  counter-increment: top;
  padding: 10px 0;
  display: grid;
  grid-template-columns: 28px 1fr;
  gap: 14px;
  align-items: baseline;
  border-top: 1px solid var(--rule);
}}
.top-stories li:first-child {{ border-top: none; }}
.top-stories li::before {{
  content: counter(top);
  color: var(--accent);
  font-weight: 700;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 14px;
}}
.top-stories a {{ color: var(--ink); font-size: 17px; line-height: 1.4; }}

/* ---------- index / archive ---------- */
.archive-item {{
  padding: 14px 0;
  border-bottom: 1px solid var(--rule);
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 16px;
  align-items: baseline;
}}
.archive-date {{
  color: var(--muted);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}}
.archive-title {{ font-size: 17px; line-height: 1.4; }}
.archive-preview {{ color: var(--muted); font-size: 14px; line-height: 1.5; margin-top: 6px; }}

/* ---------- about-page ---------- */
.about h2 {{
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  border-bottom: 1px solid var(--rule);
  padding-bottom: 6px;
  margin: 36px 0 16px;
}}
.about p {{ margin: 0 0 16px; max-width: 60ch; }}
.about ul {{ margin: 0 0 16px 24px; padding: 0; }}
.about li {{ margin-bottom: 6px; }}
.about .source-block {{
  border-left: 2px solid var(--rule);
  padding: 4px 0 4px 14px;
  margin: 0 0 18px;
}}
.about .source-block strong {{ color: var(--ink); }}
.about .source-block .note {{ color: var(--muted); font-size: 14px; }}
.about .contact a {{
  color: var(--accent-deep);
  text-decoration-color: var(--accent);
}}

/* ---------- print ---------- */
@media print {{
  body {{ background: white; }}
  .wrap {{ max-width: none; padding: 24px 32px; }}
  .item, .section {{ page-break-inside: avoid; }}
  .masthead-nav, .footer {{ display: none; }}
}}
/* ---------- small screens ---------- */
@media (max-width: 600px) {{
  .wrap {{ padding: 36px 22px 64px; }}
  .masthead {{ flex-wrap: wrap; }}
  .masthead-nav {{ margin-left: 0; }}
  .archive-item {{ grid-template-columns: 1fr; }}
  .archive-date {{ font-size: 11px; }}
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


def _masthead(*, on_page: str) -> str:
    """on_page: 'home' | 'digest' | 'about'."""
    home_attr = ' aria-current="page"' if on_page == "home" else ""
    about_attr = ' aria-current="page"' if on_page == "about" else ""
    return f"""
<header class="masthead">
  <h1 class="masthead-title"><a href="/">Dalila<span class="ar">دليلة</span></a></h1>
  <nav class="masthead-nav">
    <a href="/"{home_attr}>Archive</a>
    <a href="/about.html"{about_attr}>About</a>
  </nav>
</header>
"""


def _footer(*, contact_email: str, telegram_bot: str | None) -> str:
    parts = [f'<span>© {datetime.now().year} Dalila</span>']
    if telegram_bot:
        parts.append(f'<span><a href="https://t.me/{telegram_bot}">Subscribe on Telegram</a></span>')
    parts.append(f'<span><a href="mailto:{contact_email}">Suggestions</a></span>')
    return '<footer class="footer">' + "".join(parts) + '</footer>'


# ===========================================================================
# Per-digest page
# ===========================================================================

def render_digest(
    items: list[dict],
    *,
    when: datetime | None = None,
    total_ingested: int | None = None,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Render one daily digest as a publishable HTML page."""
    cfg = get_config()
    tz = pytz.timezone(cfg.timezone)
    now = (when or datetime.now(tz)).astimezone(tz)
    date_label = f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}"

    numbered = [{**it, "n": n} for n, it in enumerate(items, start=1)]

    def _score(it: dict) -> float:
        return float(it.get("uae_relevance") or 0) * (float(it.get("severity") or 0.5) or 0.5)
    top3 = sorted(numbered, key=_score, reverse=True)[:3]

    by_cat: dict[str, list[dict]] = {}
    for it in numbered:
        by_cat.setdefault(it.get("category") or "other", []).append(it)

    body_parts = [_masthead(on_page="digest")]

    body_parts.append(f'<p class="date-line">{html.escape(date_label)}</p>')
    body_parts.append(
        f'<p class="kicker">'
        f'<span class="top-stat">{len(numbered)} items</span> above relevance threshold '
        f'from <span class="top-stat">{total_ingested or len(numbered)}</span> reviewed in the last 24 hours.'
        f'</p>'
    )

    body_parts.append(_top_stories_block(top3))

    for cat_key, label in SECTIONS:
        rows = by_cat.get(cat_key) or []
        if rows:
            body_parts.append(_section_block(label, rows))

    body_parts.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))

    return _doc(f"Dalila — {date_label}", "\n".join(body_parts))


def _top_stories_block(top3: list[dict]) -> str:
    if not top3:
        return ""
    lis = []
    for it in top3:
        title = html.escape(it.get("title") or "")
        url = it.get("url") or ""
        href = url if (url and url.startswith("http")) else f"#item-{it.get('n', 0)}"
        lis.append(f'<li><a href="{html.escape(href)}">{title}</a></li>')
    return (
        '<section class="top-stories">'
        '<h2 class="section-title">Top stories <span class="count">3</span></h2>'
        '<ol>' + "\n".join(lis) + '</ol>'
        '</section>'
    )


def _section_block(label: str, rows: list[dict]) -> str:
    return (
        '<section class="section">'
        f'<h2 class="section-title">{html.escape(label)} <span class="count">{len(rows)} item{"s" if len(rows) != 1 else ""}</span></h2>'
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
        extra_links_html = " &middot; " + " &middot; ".join(
            f'<a href="{html.escape(u)}" target="_blank" rel="noopener">link {i+1}</a>'
            for i, u in enumerate(extras[:5])
        )

    meta_parts = [
        f'<span class="src">{source}</span>' if source else "",
        f'<span class="rel">rel {rel:.2f}</span>',
        f'<span>sev {sev:.2f}</span>' if sev > 0 else "",
        f'<span>{" · ".join(html.escape(str(c)) for c in countries[:3])}</span>' if countries else "",
        f'<span>sector: {html.escape(sector)}</span>' if sector else "",
        f'<span class="doctrine-tag">{html.escape(doctrine.upper())}</span>' if doctrine else "",
    ]
    meta_html = " &middot; ".join(p for p in meta_parts if p) + extra_links_html

    return (
        f'<article class="item" id="item-{n}">'
        f'<h3 class="item-title">{title_html}</h3>'
        f'<p class="item-summary">{summary}</p>'
        f'<div class="item-meta">{meta_html}</div>'
        '</article>'
    )


# ===========================================================================
# About page (project, sources, subscribe, contact)
# ===========================================================================

def render_about(
    sources: list[dict],
    *,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Render the static About page."""

    # Bucket each enabled source by combinations of tags.
    # The list order is the priority — first matching predicate wins.
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

    bucket_order: list[tuple[str, str]] = [
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

    body = [_masthead(on_page="about")]
    body.append('<div class="about">')
    body.append('<p class="date-line">About</p>')
    body.append('<p class="kicker">Dalila is a small daily intelligence brief on the global humanitarian, development, and philanthropy ecosystem — with a sharp focus on the UAE&rsquo;s role within it. It is built by one person, runs on free public sources, and exists to replace 30 to 60 minutes of fragmented morning reading.</p>')

    body.append('<h2>How it works</h2>')
    body.append(
        '<p>Every 30 minutes, Dalila pulls new items from the sources listed below. '
        'A cheap keyword and entity prefilter drops items unrelated to the UAE-aid '
        'remit. Surviving items go through a classifier model that assigns a '
        'category, UAE-relevance score, severity, and topical tags. Items that '
        'cluster as the same story are deduplicated. At 06:30 GST, an editor '
        'model composes the morning digest from the previous 24 hours of '
        'classified items above threshold.</p>'
    )
    body.append(
        '<p>Two-way commands let subscribers ask for a deeper dive on a topic '
        '(<code>/more &lt;topic&gt;</code>), review tracked UAE doctrine positions '
        '(<code>/doctrine</code>), or pull recent UAE financial commitments and '
        'bilateral meetings (<code>/commitments</code>, <code>/meetings</code>). '
        'Items are numbered so a reply with <code>link 3</code> returns the '
        'underlying source URL.</p>'
    )

    body.append('<h2>Sources</h2>')
    for bucket in sorted_buckets:
        items = grouped[bucket]
        if not items:
            continue
        label = bucket_labels.get(bucket, bucket.title())
        body.append(f'<div class="source-block"><strong>{html.escape(label)}</strong>')
        body.append('<ul>')
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
        f'<p class="contact">Feedback, source suggestions, bug reports, or '
        f'requests for new features: <a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a>.</p>'
    )

    body.append('</div>')  # close .about
    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — About", "\n".join(body))


# ===========================================================================
# Index / archive page
# ===========================================================================

def render_index(
    recent_digests: list[dict],
    *,
    contact_email: str = "dalila.dev.digest@gmail.com",
    telegram_bot: str | None = "dalila_development_digest_bot",
) -> str:
    """Render the archive landing page.

    `recent_digests` is a list of dicts with: date_label, slug (date for url),
    and optionally `preview` (a short prose summary or "X items, top: ...").
    Most recent first.
    """
    body = [_masthead(on_page="home")]
    body.append('<p class="date-line">Daily archive</p>')
    body.append(
        '<p class="kicker">Daily intelligence brief on humanitarian, development, '
        'and philanthropy — with a focus on the UAE. Composed at 06:30 GST.</p>'
    )

    if not recent_digests:
        body.append(
            '<p class="kicker">No digests published yet — the first morning '
            'digest will appear here.</p>'
        )
    else:
        for d in recent_digests:
            date_label = html.escape(d.get("date_label") or "")
            slug = html.escape(d.get("slug") or "")
            preview = html.escape(d.get("preview") or "")
            body.append(
                '<div class="archive-item">'
                f'<div class="archive-date">{date_label}</div>'
                '<div>'
                f'<div class="archive-title"><a href="digests/{slug}.html">Morning digest — {date_label}</a></div>'
                + (f'<div class="archive-preview">{preview}</div>' if preview else "")
                + '</div>'
                '</div>'
            )

    body.append(_footer(contact_email=contact_email, telegram_bot=telegram_bot))
    return _doc("Dalila — Daily Development Brief", "\n".join(body))
