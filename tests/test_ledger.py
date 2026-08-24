"""The ledger's promise is that it cannot be quietly rewritten. These tests are
what make that a claim rather than a hope.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app import ledger
from app.ledger import GENESIS_HASH


def test_first_entry_chains_to_genesis(session):
    entry = ledger.record(session, actor="system", event="case_detected")
    assert entry.prev_hash == GENESIS_HASH
    assert len(entry.entry_hash) == 64


def test_entries_chain_to_their_predecessor(session):
    first = ledger.record(session, actor="watcher", event="case_detected")
    second = ledger.record(session, actor="thinker", event="action_proposed")
    assert second.prev_hash == first.entry_hash


def test_chain_verifies_over_many_entries(session):
    for i in range(25):
        ledger.record(
            session, actor="doer", event="action_executed",
            case_id=f"case_{i}", payload={"amount_paise": 120_000 + i},
        )
    status = ledger.verify_chain(session)
    assert status.ok
    assert status.entries == 25


def test_update_is_rejected_by_the_database(session):
    """Not 'we never write an UPDATE'. The database refuses to accept one."""
    ledger.record(session, actor="thinker", event="action_proposed")
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(text("UPDATE ledger SET event = 'tampered' WHERE id = 1"))
        session.commit()
    session.rollback()


def test_delete_is_rejected_by_the_database(session):
    ledger.record(session, actor="guard", event="guard_blocked")
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(text("DELETE FROM ledger WHERE id = 1"))
        session.commit()
    session.rollback()


def test_tampering_is_detected_even_if_the_triggers_are_bypassed(session):
    """Someone with direct file access can drop the triggers. They cannot
    recompute the rest of the chain without noticing."""
    ledger.record(session, actor="doer", event="action_executed",
                  case_id="case_1", payload={"amount_paise": 120_000})
    ledger.record(session, actor="doer", event="action_executed",
                  case_id="case_2", payload={"amount_paise": 250_000})
    assert ledger.verify_chain(session).ok

    session.execute(text("DROP TRIGGER ledger_no_update"))
    session.execute(
        text("UPDATE ledger SET payload_json = :p WHERE id = 1"),
        {"p": '{"amount_paise":999999}'},
    )
    session.commit()

    status = ledger.verify_chain(session)
    assert not status.ok
    assert status.broken_at == 1
    assert "fingerprint" in status.detail


def test_payload_round_trips(session):
    payload = {"reason": "insufficient_funds", "retry_after": "2026-09-01T09:00:00"}
    entry = ledger.record(session, actor="thinker", event="action_proposed",
                          case_id="case_442", payload=payload)
    import json
    assert json.loads(entry.payload_json) == payload
