"""UAE foreign-aid doctrine tracker.

For each classifier-flagged item (doctrine_relation set), this module makes one
Haiku call that decides whether to (a) create a new doctrine fact, (b) append
to an existing one, or (c) ignore the item as a false positive. The structured
result is then written to the doctrine_facts table via db helpers.

Per CLAUDE.md, this runs as a separate pass after classify rather than inside
the batch — keeps the classifier batch fast and lets doctrine extraction tolerate
its own rate-limit windows independently.
"""

from __future__ import annotations

import json
import logging
import re

from dalila import db, llm
from dalila.config import load_prompt

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,48}$")


def _validate_action(payload: dict) -> tuple[str, dict | None]:
    """Light validation of the LLM's JSON. Returns (action, normalised_payload_or_None).

    Returns ("noop", None) if the payload is unusable so the caller can record
    the item as processed without crashing the pass.
    """
    action = (payload.get("action") or "").lower()
    if action not in ("new", "append", "noop"):
        return "noop", None
    if action == "noop":
        return "noop", None

    topic = (payload.get("topic") or "").strip().lower()
    if not _SLUG_RE.match(topic):
        log.warning("doctrine: rejecting bad topic slug %r", topic)
        return "noop", None
    position = (payload.get("position_summary") or "").strip()
    if not position or len(position) < 12:
        log.warning("doctrine: rejecting empty/short position for topic=%r", topic)
        return "noop", None
    nuance = payload.get("nuance")
    if nuance is not None:
        nuance = str(nuance).strip() or None

    delta = payload.get("confidence_delta")
    try:
        delta = max(-0.2, min(0.2, float(delta or 0)))
    except (TypeError, ValueError):
        delta = 0.0

    evolution_entry = payload.get("evolution_entry")
    if action == "append":
        if not isinstance(evolution_entry, dict):
            log.warning("doctrine: append missing evolution_entry; dropping")
            return "noop", None
        rel = (evolution_entry.get("relation") or "").lower()
        if rel not in ("reinforcing", "refining", "evolving", "contradicting"):
            evolution_entry["relation"] = "reinforcing"
        evolution_entry["summary"] = (evolution_entry.get("summary") or "").strip()[:500]
    else:
        evolution_entry = None

    return action, {
        "topic": topic,
        "position_summary": position[:500],
        "nuance": nuance[:500] if nuance else None,
        "confidence_delta": delta,
        "evolution_entry": evolution_entry,
    }


def _build_user_message(item_row, current_facts: list[dict]) -> str:
    body = (item_row["body"] or "")
    if len(body) > 2000:
        body = body[:2000] + " […truncated]"
    try:
        entities = json.loads(item_row["entities_json"]) if item_row["entities_json"] else []
        entity_names = [e.get("name") if isinstance(e, dict) else str(e) for e in entities][:8]
    except Exception:
        entity_names = []

    item_payload = {
        "id": int(item_row["id"]),
        "title": item_row["title"],
        "summary": item_row["one_line_summary"] or "",
        "body_excerpt": body,
        "classifier_doctrine_relation": item_row["doctrine_relation"],
        "entities": entity_names,
        "ingested_at": item_row["ingested_at"],
    }
    facts_payload = [
        {"topic": f["topic"], "position_summary": f["position_summary"],
         "nuance": f["nuance"], "confidence": f["confidence"]}
        for f in current_facts
    ]
    return (
        "Decide the doctrine update for this item. Follow the system prompt's "
        "rules and return ONE JSON object only.\n\n"
        "```json\n"
        + json.dumps({"item": item_payload, "current_facts": facts_payload},
                     ensure_ascii=False, indent=2)
        + "\n```"
    )


def process_item(conn, item_row) -> dict:
    """Run one doctrine update on `item_row`. Returns the decision dict (for logs/tests)."""
    current = db.list_doctrine_facts(conn)
    user_msg = _build_user_message(item_row, current)
    try:
        payload = llm.call_json(
            model=llm.HAIKU,
            system_prompt=load_prompt("doctrine"),
            user_prompt=user_msg,
            purpose="doctrine",
            timeout=120,
        )
    except Exception as exc:
        log.warning("doctrine LLM call failed for item %s: %s", item_row["id"], exc)
        raise

    action, norm = _validate_action(payload if isinstance(payload, dict) else {})
    decision = {"item_id": int(item_row["id"]), "action": action, "payload": norm}

    if action == "noop" or norm is None:
        db.mark_item_doctrine_processed(conn, int(item_row["id"]))
        return decision

    existing = db.doctrine_fact_for_topic(conn, norm["topic"])
    if action == "new":
        if existing:
            # LLM mis-picked "new" — degrade to append on the existing topic.
            db.append_doctrine_entry(
                conn,
                topic=norm["topic"],
                position_summary=norm["position_summary"],
                nuance=norm["nuance"],
                evolution_entry={"relation": "reinforcing",
                                 "summary": "LLM picked 'new' on existing topic; merged."},
                source_item_id=int(item_row["id"]),
                confidence_delta=norm["confidence_delta"],
            )
        else:
            db.upsert_doctrine_fact_new(
                conn,
                topic=norm["topic"],
                position_summary=norm["position_summary"],
                nuance=norm["nuance"],
                source_item_id=int(item_row["id"]),
            )
    else:  # append
        if not existing:
            # LLM picked "append" but topic doesn't exist — create instead.
            db.upsert_doctrine_fact_new(
                conn,
                topic=norm["topic"],
                position_summary=norm["position_summary"],
                nuance=norm["nuance"],
                source_item_id=int(item_row["id"]),
            )
        else:
            db.append_doctrine_entry(
                conn,
                topic=norm["topic"],
                position_summary=norm["position_summary"],
                nuance=norm["nuance"],
                evolution_entry=norm["evolution_entry"] or {"relation": "reinforcing", "summary": ""},
                source_item_id=int(item_row["id"]),
                confidence_delta=norm["confidence_delta"],
            )

    db.mark_item_doctrine_processed(conn, int(item_row["id"]))
    return decision


def run_pass(limit: int = 20) -> dict:
    """Process pending doctrine items. Returns summary counts.

    Aborts the pass cleanly on the first transient (rate-limit) error so items
    stay pending and the next tick retries. Per-item exceptions other than
    transients still mark the item processed so a single bad row can't block
    the queue forever.
    """
    from dalila.pipeline import _is_transient_error  # avoid circular import at module load

    counts = {"considered": 0, "new": 0, "append": 0, "noop": 0,
              "errors": 0, "rate_limited": False}

    with db.connect() as conn:
        rows = db.items_pending_doctrine(conn, limit=limit)
        log.info("doctrine pass: %d pending items", len(rows))
        for r in rows:
            counts["considered"] += 1
            try:
                decision = process_item(conn, r)
                counts[decision["action"]] = counts.get(decision["action"], 0) + 1
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                if _is_transient_error(err):
                    log.warning("doctrine pass: transient — aborting: %s", err[:200])
                    counts["rate_limited"] = True
                    break
                # Mark item processed so it doesn't poison subsequent passes.
                counts["errors"] += 1
                log.warning("doctrine: marking item %s processed despite error: %s",
                            r["id"], err[:200])
                db.mark_item_doctrine_processed(conn, int(r["id"]))
    return counts
