"""Append-only, hash-chained audit log: who did what, when, on which build.

Every row stores the hash of the row before it, so the log is tamper-evident: changing
or deleting any row breaks every hash after it, and ``verify_chain`` finds the first
broken row. The database refuses UPDATE and DELETE on the table (triggers in migration
007), so tampering would need someone to drop the triggers first, and the chain still
shows where it happened.

Events are written by the code that makes the change, in the same transaction, so a
change and its audit row commit or roll back together. Writers pass an explicit actor:
a person's name for sign-offs and reviews, ``ci`` for test-derived results,
``change-control`` for invalidations and re-verification items, ``checker:<model>``
for AI verdicts.

What is audited (the decisions someone will later ask about):
  requirement.created / requirement.changed       backlog loads
  verification.recorded                           manual sign-off
  verification.derived                            a CI-derived result appears or flips
  verification.invalidated                        change control
  reverification.opened / closed / superseded     change control
  verdict.created / verdict.reviewed              AI checker and its human reviewer
  policy.classified                               data classification changes
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from devtrack.db import Database, canonical_json, utcnow

GENESIS = "0" * 64


def _row_hash(prev_hash: str, row: dict[str, Any]) -> str:
    fields = {
        k: row.get(k)
        for k in (
            "recorded_at",
            "effective_at",
            "actor",
            "action",
            "entity",
            "entity_key",
            "build_id",
            "detail",
        )
    }
    if fields["build_id"] is not None:
        fields["build_id"] = int(fields["build_id"])
    return hashlib.sha256((prev_hash + canonical_json(fields)).encode()).hexdigest()


def record(
    db: Database,
    *,
    actor: str,
    action: str,
    entity: str,
    entity_key: str,
    build_id: int | None = None,
    effective_at: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Append one event. Call inside the transaction that makes the change."""
    if db.dialect == "postgres":
        # Serialize writers so two transactions can't both extend the same tail.
        db.execute("SELECT pg_advisory_xact_lock(7007)")
    prev = db.scalar("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1") or GENESIS
    row = {
        "recorded_at": utcnow(),
        "effective_at": effective_at,
        "actor": actor,
        "action": action,
        "entity": entity,
        "entity_key": entity_key,
        "build_id": build_id,
        "detail": canonical_json(detail or {}),
    }
    digest = _row_hash(str(prev), row)
    db.execute(
        "INSERT INTO audit_log (recorded_at, effective_at, actor, action, entity, entity_key, build_id, detail,"
        " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*row.values(), prev, digest),
    )
    return digest


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    rows: int
    first_bad_id: int | None = None
    reason: str = ""


def verify_chain(db: Database) -> ChainCheck:
    """Recompute every hash in order. Reports the first row that doesn't match."""
    prev = GENESIS
    rows = db.query(
        "SELECT id, recorded_at, effective_at, actor, action, entity, entity_key, build_id, detail, prev_hash, hash"
        " FROM audit_log ORDER BY id"
    )
    for r in rows:
        if r["prev_hash"] != prev:
            return ChainCheck(False, len(rows), int(r["id"]), "prev_hash does not match the row before")
        if _row_hash(prev, r) != r["hash"]:
            return ChainCheck(False, len(rows), int(r["id"]), "row content does not match its hash")
        prev = r["hash"]
    return ChainCheck(True, len(rows))


def history(db: Database, entity_key: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    where = " WHERE a.entity_key = ?" if entity_key else ""
    params: tuple[Any, ...] = (entity_key,) if entity_key else ()
    rows = db.query(
        "SELECT a.id, a.recorded_at, a.effective_at, a.actor, a.action, a.entity, a.entity_key, a.detail,"
        " b.repo AS build_repo, b.run_number AS build_run"
        f" FROM audit_log a LEFT JOIN builds b ON b.id = a.build_id{where} ORDER BY a.id DESC LIMIT ?",
        (*params, limit),
    )
    for r in rows:
        r["detail"] = json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"]
    return rows
