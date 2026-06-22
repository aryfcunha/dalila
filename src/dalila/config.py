"""Configuration: env loading, paths, source/entity registry."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Project root = parent of the `src/` directory.
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    timezone: str
    digest_time: str
    ingest_interval_minutes: int
    telegram_bot_token: str | None
    claude_bin: str
    # ACLED moved to OAuth in 2026 — username (email) + password get exchanged
    # for a 24h Bearer token. Old ACLED_API_KEY env var kept for back-compat
    # in `dalila check` but no longer used by the ingestor.
    acled_username: str | None
    acled_password: str | None
    acled_api_key: str | None  # legacy, ignored — kept so existing .env files don't error
    # ACAPS INFORM Severity Index (free public registration at api.acaps.org).
    # Unset → INFORM ingestor silently returns []. HungerMap and GDACS need
    # no creds; ACLED CAST reuses the ACLED OAuth pair.
    acaps_api_key: str | None
    iati_api_key: str | None
    daily_classifier_call_cap: int
    # One-shot startup backfill: if > 0, the bot composes any of the last N
    # days that lack a brief, writes their pages, and publishes — once, ~2 min
    # after boot, without broadcasting the historical briefs to Telegram. 0 =
    # off (default). Set DALILA_BACKFILL_DAYS_ON_BOOT=14 and restart to recover
    # a backlog automatically; safe to leave on (only_missing → no redundant
    # LLM calls for days that already have a brief).
    backfill_days_on_boot: int
    sources_path: Path
    entities_path: Path
    prompts_dir: Path
    migrations_dir: Path


@lru_cache(maxsize=1)
def get_config() -> Config:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env", override=False)

    db_path = Path(os.getenv("DALILA_DB_PATH", str(ROOT / "dalila.db"))).resolve()
    return Config(
        root=ROOT,
        db_path=db_path,
        timezone=os.getenv("DALILA_TIMEZONE", "Asia/Dubai"),
        digest_time=os.getenv("DALILA_DIGEST_TIME", "06:30"),
        ingest_interval_minutes=int(os.getenv("DALILA_INGEST_INTERVAL_MINUTES", "30")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        claude_bin=os.getenv("DALILA_CLAUDE_BIN") or "claude",
        # ACLED v2026 OAuth: username = the email you registered with;
        # password is your myACLED account password.
        acled_username=(os.getenv("ACLED_USERNAME") or os.getenv("ACLED_EMAIL")) or None,
        acled_password=os.getenv("ACLED_PASSWORD") or None,
        acled_api_key=os.getenv("ACLED_API_KEY") or None,  # legacy, ignored
        acaps_api_key=os.getenv("ACAPS_API_KEY") or None,
        iati_api_key=os.getenv("IATI_API_KEY") or None,
        daily_classifier_call_cap=int(os.getenv("DALILA_DAILY_CLASSIFIER_CALL_CAP", "2000")),
        backfill_days_on_boot=int(os.getenv("DALILA_BACKFILL_DAYS_ON_BOOT", "0")),
        sources_path=ROOT / "sources.yaml",
        entities_path=ROOT / "entities.yaml",
        prompts_dir=ROOT / "prompts",
        migrations_dir=ROOT / "migrations",
    )


@lru_cache(maxsize=1)
def load_sources() -> list[dict]:
    cfg = get_config()
    data = yaml.safe_load(cfg.sources_path.read_text(encoding="utf-8"))
    return data.get("sources", [])


@lru_cache(maxsize=1)
def load_prefilter_keywords() -> list[str]:
    cfg = get_config()
    data = yaml.safe_load(cfg.sources_path.read_text(encoding="utf-8"))
    return [kw.lower() for kw in data.get("prefilter_keywords", [])]


@lru_cache(maxsize=1)
def load_entities_yaml_text() -> str:
    """Raw YAML text of the entity watchlist — fed verbatim to the classifier."""
    cfg = get_config()
    return cfg.entities_path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_entity_aliases() -> list[str]:
    """Flat list of (lowercased) entity names + aliases for prefilter matching."""
    cfg = get_config()
    data = yaml.safe_load(cfg.entities_path.read_text(encoding="utf-8"))
    out: list[str] = []
    for group in data.values():
        if not isinstance(group, list):
            continue
        for entity in group:
            if not isinstance(entity, dict):
                continue
            name = entity.get("name")
            if name:
                out.append(name.lower())
            for alias in entity.get("aliases", []) or []:
                out.append(alias.lower())
    return out


def load_prompt(name: str) -> str:
    """Load a prompt file from prompts/{name}.md."""
    cfg = get_config()
    return (cfg.prompts_dir / f"{name}.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_convening_events() -> list[dict]:
    """Convening calendar — see events.yaml for the schema."""
    cfg = get_config()
    path = cfg.root / "events.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("events", []) or []


@lru_cache(maxsize=1)
def load_regions() -> dict[str, dict]:
    """Region-slug → {label, countries:[ISO-codes]} mapping for /region.

    Returns {} if the file is missing. Slugs are normalised to lowercase
    for case-insensitive lookups.
    """
    cfg = get_config()
    path = cfg.root / "regions.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    regions = data.get("regions", {}) or {}
    out: dict[str, dict] = {}
    for slug, spec in regions.items():
        if not isinstance(spec, dict):
            continue
        countries = [str(c).strip().upper() for c in (spec.get("countries") or []) if c]
        out[str(slug).strip().lower()] = {
            "label": str(spec.get("label", slug)),
            "countries": countries,
        }
    return out


def resolve_region(query: str) -> tuple[str, dict] | None:
    """Resolve a free-text user query to a region. Case/punctuation-insensitive.

    Matches exact slug, label, or any unambiguous substring of either.
    Returns (slug, spec) or None if no/ambiguous match.
    """
    if not query:
        return None
    q = query.strip().lower().replace(" ", "-").replace("_", "-").replace("&", "and")
    regions = load_regions()
    if q in regions:
        return q, regions[q]
    # Substring match against slug or label
    candidates = []
    for slug, spec in regions.items():
        label_norm = spec["label"].lower().replace(" ", "-").replace("&", "and")
        if q in slug or q in label_norm:
            candidates.append((slug, spec))
    if len(candidates) == 1:
        return candidates[0]
    return None


@lru_cache(maxsize=1)
def load_countries() -> dict:
    """Load countries.yaml.

    Returns a dict:
      {
        "countries": {ISO2: {name, aliases, region}, ...},
        "regions":   {slug: {label, countries: [ISO2, ...]}, ...},
        "alias_index": {lowercase-alias: ISO2, ...},  # built from canonical names + aliases
      }

    `alias_index` is the lookup used by /country (Telegram) and any classifier
    post-processing — pass a free-text country name through it to recover the
    ISO-2 code. Missing file → returns empty stubs (system still works, just
    no country features).
    """
    cfg = get_config()
    path = cfg.root / "countries.yaml"
    if not path.exists():
        return {"countries": {}, "regions": {}, "alias_index": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_countries: dict = data.get("countries", {}) or {}
    regions: dict = data.get("regions", {}) or {}

    # Coerce all keys to uppercase string ISO codes. Guards against YAML 1.1
    # parsing bare 2-letter codes like NO/ON/YES/NULL as booleans/None.
    countries: dict = {}
    for k, v in raw_countries.items():
        if not isinstance(v, dict):
            continue
        if isinstance(k, bool) or k is None:
            # Skip if the key got lost to YAML 1.1 boolean coercion.
            # The user should quote the key in countries.yaml (e.g. "NO":).
            continue
        countries[str(k).upper()] = v

    # Annotate each country with its region slug (back-reference for fast lookup)
    for slug, spec in regions.items():
        for iso in (spec.get("countries") or []):
            iso_u = str(iso).upper() if iso else ""
            if iso_u in countries and isinstance(countries[iso_u], dict):
                countries[iso_u]["region"] = slug

    # Build alias index — lowercased name + every alias, mapping to ISO-2
    alias_index: dict[str, str] = {}
    for iso, spec in countries.items():
        if not isinstance(spec, dict):
            continue
        name = (spec.get("name") or "").strip()
        if name:
            alias_index[name.lower()] = iso
        for alias in (spec.get("aliases") or []):
            a = str(alias).strip().lower()
            if a:
                alias_index[a] = iso
        # The ISO code itself also resolves
        alias_index[iso.lower()] = iso

    return {"countries": countries, "regions": regions, "alias_index": alias_index}


def resolve_country(query: str) -> str | None:
    """Resolve a free-text country query to its canonical ISO-2 code.

    Case-insensitive, punctuation-tolerant. Returns None on no match
    or ambiguous match. Examples:
      resolve_country("USA")              → "US"
      resolve_country("united states")    → "US"
      resolve_country("UAE")              → "AE"
      resolve_country("DRC")              → "CD"
      resolve_country("Cote d Ivoire")    → "CI"
      resolve_country("xyz")              → None
    """
    if not query:
        return None
    cat = load_countries()
    idx = cat["alias_index"]
    q = query.strip().lower().strip(".,;:!?")
    if q in idx:
        return idx[q]
    # Try normalising punctuation/whitespace
    q_norm = " ".join(q.replace(".", " ").replace("-", " ").replace("'", " ").split())
    if q_norm in idx:
        return idx[q_norm]
    # Substring fallback — only if exactly one alias contains the query
    matches = {iso for alias, iso in idx.items() if q in alias}
    return next(iter(matches)) if len(matches) == 1 else None


@lru_cache(maxsize=1)
def load_doctrine_topic_seeds() -> list[dict]:
    """Preferred doctrine topic vocabulary — see doctrine_topics.yaml.

    Returns a list of {slug, description} dicts. Used by the doctrine module
    to bias the LLM toward canonical topic slugs without hard-blocking
    invention of new ones.
    """
    cfg = get_config()
    path = cfg.root / "doctrine_topics.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("topics", []) or []
