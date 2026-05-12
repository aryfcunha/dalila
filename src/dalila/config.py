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
    acled_api_key: str | None
    acled_email: str | None
    daily_classifier_call_cap: int
    sources_path: Path
    entities_path: Path
    prompts_dir: Path
    migrations_dir: Path


@lru_cache(maxsize=1)
def get_config() -> Config:
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
        acled_api_key=os.getenv("ACLED_API_KEY") or None,
        acled_email=os.getenv("ACLED_EMAIL") or None,
        daily_classifier_call_cap=int(os.getenv("DALILA_DAILY_CLASSIFIER_CALL_CAP", "2000")),
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
