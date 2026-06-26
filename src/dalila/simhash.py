"""SimHash for near-duplicate news titles.

A 64-bit SimHash over word tokens lets us detect cross-outlet reposts of the
same story ("Reuters: Iran agrees ceasefire", "BBC: Iran agrees ceasefire")
without paying for embeddings or running pgvector. Empirical distances on
real headlines (measured in this repo):
  - cross-outlet repost of same story:  ~6-10 bits
  - tense / wording rephrase:           ~15-20 bits
  - unrelated stories:                  ~28-40 bits
`is_near_duplicate`'s standalone default is 12, but the LIVE dedup path runs
looser: `pipeline._dedupe_by_simhash` defaults to 16 (the value used at every
pipeline call site) and the archive-render dedup uses 24. SimHash only catches
near-identical wording — same-event stories with different vocabulary land at
~22-26 bits (above all these thresholds) and are handled by the separate
semantic dedup pass, not here.

Cost: O(tokens) per insert, ~50µs on commodity hardware. Storage: 16 bytes
of hex per item.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

# Outlet prefixes / generic boilerplate to strip before hashing so
# "Reuters: X" and "BBC News - X" don't drag the hash apart.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "from", "by", "as", "is", "are", "was", "were", "be",
    "been", "being", "has", "have", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "this", "that",
    "these", "those", "it", "its",
    # Outlet boilerplate we see in feeds
    "reuters", "bbc", "ap", "afp", "cnn", "nyt", "wapo",
    "news", "live", "update", "updates", "exclusive", "breaking",
    "report", "analysis", "opinion", "video", "watch",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, alnum-only tokens, stopwords dropped."""
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def simhash64(text: str) -> int:
    """64-bit SimHash of `text`. Returns 0 if no usable tokens."""
    tokens = _tokenize(text)
    if not tokens:
        return 0
    bits = [0] * 64
    for tok in tokens:
        # md5 is plenty for a hash function; we only need uniform bit distribution.
        h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            if h & (1 << i):
                bits[i] += 1
            else:
                bits[i] -= 1
    out = 0
    for i in range(64):
        if bits[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    """Bit-count of XOR — how many bits differ."""
    return (a ^ b).bit_count()


def to_hex(h: int) -> str:
    """Zero-padded 16-char hex (so column sorts predictably and is human-grep-able)."""
    return f"{h & 0xFFFFFFFFFFFFFFFF:016x}"


def from_hex(s: str | None) -> int:
    if not s:
        return 0
    try:
        return int(s, 16)
    except ValueError:
        return 0


def is_near_duplicate(a_hex: str | None, b_hex: str | None, threshold: int = 12) -> bool:
    """True iff both hashes are present and within `threshold` bits Hamming distance."""
    if not a_hex or not b_hex:
        return False
    return hamming(from_hex(a_hex), from_hex(b_hex)) <= threshold
