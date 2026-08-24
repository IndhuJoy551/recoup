"""Database tables.

Two rules that hold everywhere in this project:

1. Money is stored as an integer number of paise. Never a float. 0.1 + 0.2 != 0.3
   in binary floating point, and a rounding error inside a recovery total is the
   kind of bug that is invisible until it is expensive.
2. Timestamps are stored in UTC. Business rules like "no messages after 9pm" are
   evaluated in IST at the moment of the check, not baked into stored data.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Case(Base):
    """One piece of at-risk revenue that Recoup might try to recover."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)

    # failed_payment | failed_mandate | abandoned_checkout | overdue_invoice
    kind: Mapped[str] = mapped_column(String(32), index=True)

    customer_ref: Mapped[str] = mapped_column(String(128), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    # open | in_progress | recovered | escalated | given_up
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)

    razorpay_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    detected_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    recovered_paise: Mapped[int] = mapped_column(Integer, default=0)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


class LedgerEntry(Base):
    """One immutable line in the audit trail.

    Immutability is enforced twice over, deliberately:

    * `entry_hash` fingerprints this row *and* the fingerprint of the row before
      it. Editing any historical row breaks every hash after it, so tampering is
      detectable rather than merely discouraged.
    * SQLite triggers (see db.py) make UPDATE and DELETE on this table fail.

    Belt and braces, because "we keep an audit log" and "we can prove the audit
    log wasn't edited" are very different claims to make to a payments company.
    """

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32), index=True)

    case_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    # watcher | thinker | guard | doer | system | human
    actor: Mapped[str] = mapped_column(String(32), index=True)

    # e.g. case_detected, action_proposed, guard_blocked, action_executed
    event: Mapped[str] = mapped_column(String(64), index=True)

    payload_json: Mapped[str] = mapped_column(Text, default="{}")

    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)


class WebhookEvent(Base):
    """One delivery from Razorpay, written down before it is acted on.

    Razorpay promises *at-least-once* delivery: if our reply is slow, lost, or
    non-200, it sends the same event again. That is not a bug in their system,
    it is the contract. So `event_id` carries a unique constraint, and the second
    delivery fails to insert instead of creating a second recovery row. The
    failed insert *is* the idempotency check. It is supposed to happen.
    """

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Razorpay's own id for this delivery, from the X-Razorpay-Event-Id header.
    event_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)

    event: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False)

    # received | processed | failed
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)

    received_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
