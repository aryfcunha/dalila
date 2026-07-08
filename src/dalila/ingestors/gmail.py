"""Gmail IMAP ingestor.

Captures newsletters that don't expose usable RSS (Devex Newswire, The
Economist's morning briefings, CGD newsletter, others). We subscribe a
dedicated Gmail account (dalila.dev.digest@gmail.com) to those newsletters
and pull from its inbox via IMAP.

Each email becomes ONE item. The body is the readable text of the email,
the title is the email subject (often the publication's headline), and
extracted hyperlinks land in `extra["links"]` for the editor to surface.

Setup
-----
1. Subscribe the Gmail account to each newsletter you want.
2. In Gmail settings → Security → enable 2-step verification.
3. Create an **App password** for "Mail" and copy the 16-char string.
4. Set env vars:
       GMAIL_USER=dalila.dev.digest@gmail.com
       GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx   (no spaces)
5. Optionally: GMAIL_LABEL=Newsletters  (defaults to INBOX). Use a Gmail
   filter to apply this label to incoming newsletters; keeps the inbox clean.

The ingestor only reads emails received in the last `window_days` (default 1).
Older emails are ignored — we expect ingest to run daily; backfill is not
its job.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from bs4 import BeautifulSoup

from dalila.models import RawItem

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


def fetch(src: dict) -> list[RawItem]:
    user = os.getenv("GMAIL_USER") or ""
    password = os.getenv("GMAIL_APP_PASSWORD") or ""
    if not user or not password:
        from dalila.ingestors.base import SourceSkipped
        raise SourceSkipped("GMAIL_USER / GMAIL_APP_PASSWORD not set")

    mailbox = os.getenv("GMAIL_LABEL") or "INBOX"
    window_days = int(src.get("window_days", 1))
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%d-%b-%Y")

    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(user, password)
    except Exception as exc:
        log.warning("Gmail IMAP login failed: %s", type(exc).__name__)
        return []

    items: list[RawItem] = []
    try:
        status, _ = conn.select(f'"{mailbox}"', readonly=True)
        if status != "OK":
            log.warning("Gmail IMAP select(%s) failed; falling back to INBOX", mailbox)
            conn.select("INBOX", readonly=True)

        status, data = conn.search(None, f"(SINCE {since})")
        if status != "OK" or not data or not data[0]:
            log.info("Gmail ingestor: no messages since %s", since)
            return []
        ids = data[0].split()
        log.info("Gmail ingestor: %d messages since %s", len(ids), since)

        for msg_id in ids[-200:]:  # cap per-run
            try:
                item = _fetch_one(conn, msg_id, source_id=src["id"])
                if item is not None:
                    items.append(item)
            except Exception as exc:
                log.warning("Gmail msg %s parse failed: %s", msg_id, exc)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return items


def _fetch_one(conn: imaplib.IMAP4_SSL, msg_id: bytes, *, source_id: str) -> RawItem | None:
    status, data = conn.fetch(msg_id, "(RFC822)")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None

    raw = data[0][1]
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    subject = _decode_header(msg.get("Subject") or "")
    if not subject.strip():
        return None
    from_hdr = _decode_header(msg.get("From") or "")
    date_hdr = msg.get("Date") or ""
    msg_id_hdr = msg.get("Message-ID") or msg.get("Message-Id") or ""

    body_text, links = _extract_body_and_links(msg)
    if not body_text.strip():
        return None

    # Tag the subject with the sender brand so the classifier can attribute
    # consistently. e.g. "[Devex Newswire] Aid sector responds to..."
    brand = _brand_from_sender(from_hdr)
    title = f"[{brand}] {subject}" if brand else subject

    # Sender-specific cleanup of common boilerplate footers.
    body_text = _strip_newsletter_chrome(body_text)

    try:
        published_at = email.utils.parsedate_to_datetime(date_hdr)
        if published_at and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
    except Exception:
        published_at = None

    # Use Message-ID as canonical url-hash anchor — stable, unique per email.
    canonical_url = f"gmail:{msg_id_hdr.strip('<>')}" if msg_id_hdr else f"gmail:{msg_id.decode()}"

    return RawItem(
        source_id=source_id,
        title=title[:300],
        url=canonical_url,
        body=body_text[:8000],
        author=from_hdr[:200] if from_hdr else None,
        published_at=published_at,
        extra={"links": links[:30], "gmail_subject": subject, "gmail_from": from_hdr},
    )


def _decode_header(value: str) -> str:
    """Decode RFC2047-encoded header (=?utf-8?B?...) to plain str."""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        return "".join(
            (p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p)
            for p, enc in parts
        )
    except Exception:
        return value


def _extract_body_and_links(msg: EmailMessage) -> tuple[str, list[str]]:
    """Pull the readable text + external links from a (likely multipart) email."""
    plain = None
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = _payload_to_text(part)
            elif ctype == "text/html" and html is None:
                html = _payload_to_text(part)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain = _payload_to_text(msg)
        elif ctype == "text/html":
            html = _payload_to_text(msg)

    links: list[str] = []
    text = ""
    if html:
        soup = BeautifulSoup(html, "lxml")
        # Collect external links before stripping HTML
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http") and "unsubscribe" not in href.lower() and "mailto:" not in href.lower():
                links.append(href)
        # Text content
        for s in soup(["script", "style"]):
            s.decompose()
        text = soup.get_text(separator="\n", strip=True)
    if not text and plain:
        text = plain
        # Plain-text fallback: regex out raw URLs
        for m in re.finditer(r"https?://[^\s)>\]]+", plain):
            links.append(m.group(0))

    # De-duplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return text, deduped


def _payload_to_text(part) -> str:
    """Safely decode a part's payload to a unicode string."""
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except Exception:
                return payload.decode("utf-8", errors="replace")
        return str(payload or "")


def _brand_from_sender(from_hdr: str) -> str | None:
    """Map common From headers to short brand labels for titling."""
    s = from_hdr.lower()
    for needle, brand in (
        ("devex", "Devex Newswire"),
        ("economist.com", "The Economist"),
        ("cgdev.org", "CGD"),
        ("alliancemagazine", "Alliance Magazine"),
        ("relief", "ReliefWeb"),
        ("ocha", "OCHA"),
        ("worldbank.org", "World Bank"),
    ):
        if needle in s:
            return brand
    # Generic: take the display name in 'Name <addr>' if present
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', from_hdr)
    if m:
        name = m.group(1).strip()
        if 2 <= len(name) <= 60:
            return name
    return None


_FOOTER_PATTERNS = [
    re.compile(r"^\s*Unsubscribe.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*You are receiving this.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Update (your )?(email )?preferences.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*View this email in your browser.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Copyright © \d{4}.*$", re.IGNORECASE | re.MULTILINE),
]


def _strip_newsletter_chrome(text: str) -> str:
    """Remove boilerplate unsubscribe/copyright lines that pollute the body."""
    for pat in _FOOTER_PATTERNS:
        text = pat.sub("", text)
    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
