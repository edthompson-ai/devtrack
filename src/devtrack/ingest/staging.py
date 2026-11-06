"""Staging: land every fetched record as-is before touching the core tables.

Why stage first
  * The raw payload is kept exactly as received, so a normalizer bug can be fixed and
    replayed without re-fetching from the API (which costs rate limit and time).
  * Loading and transforming are separate steps: a bad record fails its own row
    (``error`` column) instead of aborting the whole sync.
  * Identical re-fetches are free: the unique key includes the payload hash, so the
    same record fetched again is a no-op, and only changed records get processed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

from devtrack.db import Database, canonical_json, utcnow

log = logging.getLogger(__name__)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def start_batch(db: Database, source_system: str, scope: str) -> int:
    batch_id = db.scalar(
        "INSERT INTO ingest_batches (source_system, scope, status) VALUES (?, ?, 'running') RETURNING id",
        (source_system, scope),
    )
    db.commit()
    return int(batch_id)


def finish_batch(db: Database, batch_id: int, status: str, stats: dict[str, Any]) -> None:
    db.execute(
        "UPDATE ingest_batches SET status = ?, finished_at = ?, stats = ? WHERE id = ?",
        (status, utcnow(), db.json(stats), batch_id),
    )
    db.commit()


def stage(
    db: Database,
    batch_id: int,
    source_system: str,
    records: Iterable[tuple[str, str, dict[str, Any]]],
) -> tuple[int, int]:
    """Insert (entity, source_id, payload) records. Returns (new, unchanged) counts."""
    new = unchanged = 0
    with db.transaction():
        for entity, source_id, payload in records:
            inserted = db.execute(
                "INSERT INTO staging_records"
                " (batch_id, source_system, entity, source_id, payload, payload_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_system, entity, source_id, payload_hash) DO NOTHING",
                (batch_id, source_system, entity, source_id, db.json(payload), payload_hash(payload)),
            )
            if inserted:
                new += 1
            else:
                unchanged += 1
    log.info("staged %d new and %d unchanged records (batch %d)", new, unchanged, batch_id)
    return new, unchanged


def get_watermark(db: Database, source_system: str, scope: str, entity: str) -> str | None:
    value = db.scalar(
        "SELECT watermark FROM sync_state WHERE source_system = ? AND scope = ? AND entity = ?",
        (source_system, scope, entity),
    )
    return str(value) if value is not None else None


def set_watermark(db: Database, source_system: str, scope: str, entity: str, watermark: str) -> None:
    """Advance the watermark. Never moves it backwards."""
    current = get_watermark(db, source_system, scope, entity)
    if current is not None and current >= watermark:
        return
    db.execute(
        "INSERT INTO sync_state (source_system, scope, entity, watermark, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (source_system, scope, entity)"
        " DO UPDATE SET watermark = excluded.watermark, updated_at = excluded.updated_at",
        (source_system, scope, entity, watermark, utcnow()),
    )
    db.commit()
