"""The Diary: an append-only, hash-chained audit trail.

Every decision Recoup makes and every action it takes lands here before it is
considered to have happened.

One deliberate difference from the audit logger I wrote in an earlier project:
there, audit writes were fire-and-forget, because a failed log line should never
break a real user action. Here the priority is inverted. If we cannot record what
we are about to do to someone's money, we do not do it. `record()` raises, and
callers are expected to let that abort the action.

The chain
---------
Each row stores a SHA-256 fingerprint of its own contents *plus* the fingerprint
of the row before it. The first row chains to a fixed genesis value. Alter any
historical row and every fingerprint after it stops matching, so `verify_chain()`
reports not just that tampering happened but where it started.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerEntry, utcnow

GENESIS_HASH = "0" * 64

# prev_hash is read-then-written, so two concurrent writers could otherwise chain
# off the same parent and produce a fork.
_write_lock = threading.Lock()


def _canonical(payload: dict[str, Any]) -> str:
    """Serialise deterministically, so the same content always hashes the same."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(
    *, ts: str, case_id: str | None, actor: str, event: str,
    payload_json: str, prev_hash: str,
) -> str:
    material = "|".join([ts, case_id or "", actor, event, payload_json, prev_hash])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def record(
    session: Session,
    *,
    actor: str,
    event: str,
    case_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Append one entry. Raises if it cannot be written — by design.

    actor: watcher | thinker | guard | doer | system | human
    event: case_detected, action_proposed, guard_blocked, action_executed, ...
    """
    payload_json = _canonical(payload or {})
    ts = utcnow().isoformat()

    with _write_lock:
        prev = session.execute(
            select(LedgerEntry).order_by(LedgerEntry.id.desc()).limit(1)
        ).scalars().first()
        prev_hash = prev.entry_hash if prev else GENESIS_HASH

        entry = LedgerEntry(
            ts=ts,
            case_id=case_id,
            actor=actor,
            event=event,
            payload_json=payload_json,
            prev_hash=prev_hash,
            entry_hash=_digest(
                ts=ts, case_id=case_id, actor=actor, event=event,
                payload_json=payload_json, prev_hash=prev_hash,
            ),
        )
        session.add(entry)
        session.commit()

    return entry


def record_many(
    session: Session,
    rows: list[dict[str, Any]],
) -> int:
    """Append many entries under one commit, chaining them in memory first.

    A 300-case batch run across five policies produces thousands of audit lines.
    Committing each one separately turned a nine-second report card into a
    four-minute one, and a slow report card is one nobody re-runs -- which
    quietly costs you the reproducibility check.

    The invariant is unchanged: every row still fingerprints its own contents
    plus its predecessor's fingerprint, and `verify_chain()` still walks the lot.
    The only thing that moved is where the chain is computed. Each dict needs
    `actor` and `event`; `case_id` and `payload` are optional.
    """
    if not rows:
        return 0

    with _write_lock:
        prev = session.execute(
            select(LedgerEntry).order_by(LedgerEntry.id.desc()).limit(1)
        ).scalars().first()
        prev_hash = prev.entry_hash if prev else GENESIS_HASH

        for row in rows:
            payload_json = _canonical(row.get("payload") or {})
            ts = utcnow().isoformat()
            case_id = row.get("case_id")
            actor = row["actor"]
            event = row["event"]
            entry_hash = _digest(
                ts=ts, case_id=case_id, actor=actor, event=event,
                payload_json=payload_json, prev_hash=prev_hash,
            )
            session.add(LedgerEntry(
                ts=ts, case_id=case_id, actor=actor, event=event,
                payload_json=payload_json, prev_hash=prev_hash,
                entry_hash=entry_hash,
            ))
            prev_hash = entry_hash

        session.commit()

    return len(rows)


@dataclass
class ChainStatus:
    ok: bool
    entries: int
    broken_at: int | None = None
    detail: str = ""


def verify_chain(session: Session) -> ChainStatus:
    """Walk the whole ledger and confirm nothing has been altered."""
    entries = session.execute(
        select(LedgerEntry).order_by(LedgerEntry.id.asc())
    ).scalars().all()

    expected_prev = GENESIS_HASH
    for entry in entries:
        if entry.prev_hash != expected_prev:
            return ChainStatus(
                ok=False, entries=len(entries), broken_at=entry.id,
                detail="chain is broken: an entry is missing or was reordered",
            )
        recomputed = _digest(
            ts=entry.ts, case_id=entry.case_id, actor=entry.actor,
            event=entry.event, payload_json=entry.payload_json,
            prev_hash=entry.prev_hash,
        )
        if recomputed != entry.entry_hash:
            return ChainStatus(
                ok=False, entries=len(entries), broken_at=entry.id,
                detail="entry contents do not match their recorded fingerprint",
            )
        expected_prev = entry.entry_hash

    return ChainStatus(ok=True, entries=len(entries), detail="chain intact")


def tail(session: Session, limit: int = 50) -> list[LedgerEntry]:
    """Most recent entries first — what the dashboard shows."""
    return list(
        session.execute(
            select(LedgerEntry).order_by(LedgerEntry.id.desc()).limit(limit)
        ).scalars().all()
    )
