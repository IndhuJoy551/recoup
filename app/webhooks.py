"""Razorpay phoning us.

Two things have to be true of every webhook handler that touches money, and
neither is optional:

1. **We must know the message really came from Razorpay.** The URL is public.
   Anyone who guesses it can POST "this ₹50,000 invoice was paid" and, without a
   check, we would believe them and stop chasing a real debt. Razorpay signs each
   delivery with HMAC-SHA256 over the exact raw bytes of the body, using a shared
   secret. We recompute that signature and compare. No match, no processing.

2. **We must survive being told the same thing twice.** Razorpay retries a
   delivery whenever our reply is slow, lost, or not a 200. Their docs call this
   at-least-once delivery. It is a design constraint, not a footnote: a duplicate
   `payment.captured` must never become a second ₹1,200 in the recovered total.

The shape of the handler follows from (2). We do the cheap things synchronously —
verify the signature, claim the event id — reply 200 straight away, and do the
real work in the background. Replying first is what stops the retry that causes
the duplicate in the first place.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from sqlalchemy.exc import IntegrityError

from app import ledger
from app.config import get_settings
from app.db import SessionLocal
from app.models import WebhookEvent, utcnow

router = APIRouter(tags=["webhooks"])

# The events we actually care about. Anything else is stored and acknowledged,
# so we have a record of it, but no handler runs.
HANDLED_EVENTS = {
    "payment.captured",
    "payment.failed",
    "payment_link.paid",
    "payment_link.expired",
    "order.paid",
    "subscription.charged",
    "invoice.paid",
}


def signature_is_valid(raw_body: bytes, provided: str | None, secret: str) -> bool:
    """Recompute Razorpay's HMAC over the raw bytes and compare in constant time.

    Two details that look pedantic and are not:

    * We hash the *raw bytes*, not a re-serialised dict. `json.loads` then
      `json.dumps` can reorder keys or change spacing, and the signature is over
      the exact bytes Razorpay sent. Parse first and the check fails for reasons
      that look like a bug in Razorpay.
    * `compare_digest`, not `==`. A normal string comparison returns early on the
      first differing character, so how long it takes leaks how much of the
      signature an attacker got right. Guessing 64 hex characters one at a time
      is feasible; guessing all 64 at once is not.
    """
    if not provided or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


def primary_entity(body: dict) -> dict | None:
    """The entity the event is actually *about*.

    A delivery can carry several. `payment_link.paid` arrives with an order, a
    payment and a payment_link inside it, because all three are involved. Taking
    whichever appears first is a coin flip: on our first real one it returned the
    order id for a payment_link event, and the Watcher would have gone looking for
    a case keyed on the wrong object.

    Razorpay names the subject in the event itself. `payment_link.paid` is about
    the `payment_link`; `order.paid` is about the `order`. Split on the dot and
    ask for that key by name, rather than hoping dict order agrees with intent.
    """
    entities = body.get("payload", {})
    if not isinstance(entities, dict):
        return None

    subject = str(body.get("event", "")).split(".")[0]
    preferred = entities.get(subject)
    if isinstance(preferred, dict) and isinstance(preferred.get("entity"), dict):
        return preferred["entity"]

    # Unknown event shape: fall back to the first entity, and be explicit that
    # this is a guess rather than pretending it is the same thing.
    for wrapper in entities.values():
        if isinstance(wrapper, dict) and isinstance(wrapper.get("entity"), dict):
            return wrapper["entity"]
    return None


def _entity_id(body: dict) -> str | None:
    entity = primary_entity(body)
    return entity.get("id") if entity else None


@router.post("/webhooks/razorpay")
async def receive(
    request: Request,
    background: BackgroundTasks,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    raw = await request.body()

    if not signature_is_valid(raw, x_razorpay_signature, settings.razorpay_webhook_secret):
        # Deliberately terse. Telling a caller *why* their forgery failed is free
        # help for the next attempt. The detail goes in our ledger instead.
        with SessionLocal() as session:
            ledger.record(
                session,
                actor="system",
                event="webhook_rejected",
                payload={
                    "reason": "bad_signature",
                    "event_id": x_razorpay_event_id,
                    "bytes": len(raw),
                    "secret_configured": bool(settings.razorpay_webhook_secret),
                },
            )
        return Response(status_code=401, content='{"error":"invalid signature"}',
                        media_type="application/json")

    try:
        body = json.loads(raw)
    except ValueError:
        return Response(status_code=400, content='{"error":"malformed json"}',
                        media_type="application/json")

    event_name = body.get("event", "unknown")
    # Razorpay always sends the header; the fallback keeps us honest in local
    # replay tests, where a hash of the body is a reasonable stand-in.
    event_id = x_razorpay_event_id or hashlib.sha256(raw).hexdigest()

    with SessionLocal() as session:
        record = WebhookEvent(
            event_id=event_id,
            event=event_name,
            entity_id=_entity_id(body),
            signature_valid=True,
            status="received",
            payload_json=raw.decode("utf-8", errors="replace"),
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            # The unique constraint did its job. This is the duplicate delivery,
            # and the correct response is a cheerful 200 — Razorpay must be told
            # we have it, or it will keep sending it.
            session.rollback()
            ledger.record(
                session,
                actor="system",
                event="webhook_duplicate_ignored",
                payload={"event_id": event_id, "event": event_name},
            )
            return Response(
                status_code=200,
                content='{"status":"duplicate","action":"none"}',
                media_type="application/json",
            )

        ledger.record(
            session,
            actor="system",
            event="webhook_received",
            payload={
                "event_id": event_id,
                "event": event_name,
                "entity_id": record.entity_id,
            },
        )

    # Reply now, think later. See the module docstring: a slow reply is what
    # causes the duplicate we just spent a table preventing.
    background.add_task(process_event, event_id)
    return Response(status_code=200, content='{"status":"accepted"}',
                    media_type="application/json")


def process_event(event_id: str) -> None:
    """Do the real work, after Razorpay has already been told 200.

    Today this only marks the event processed. The Watcher and the Doer plug in
    here on later days: this is where a `payment_link.paid` becomes "case #442
    recovered, ₹1,200, attributed to the action we took on Sep 1".
    """
    with SessionLocal() as session:
        record = (
            session.query(WebhookEvent).filter_by(event_id=event_id).one_or_none()
        )
        if record is None:
            return

        try:
            payload = json.loads(record.payload_json)
            handled = record.event in HANDLED_EVENTS

            record.status = "processed"
            record.processed_at = utcnow()
            session.commit()

            ledger.record(
                session,
                actor="system",
                event="webhook_processed",
                payload={
                    "event_id": event_id,
                    "event": record.event,
                    "handled": handled,
                    "amount_paise": _amount_paise(payload),
                },
            )
        except Exception as exc:  # noqa: BLE001 — nothing is allowed to vanish
            session.rollback()
            record = (
                session.query(WebhookEvent).filter_by(event_id=event_id).one_or_none()
            )
            if record is not None:
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {exc}"[:1000]
                session.commit()
            ledger.record(
                session,
                actor="system",
                event="webhook_processing_failed",
                payload={"event_id": event_id, "error": f"{type(exc).__name__}"},
            )


def _amount_paise(body: dict) -> int | None:
    """Amount from the subject entity, for the same reason as `primary_entity`.

    On a partly-paid link the order total and the payment total differ, so which
    entity you read decides whether the recovered figure is right.
    """
    entity = primary_entity(body)
    amount = entity.get("amount") if entity else None
    return amount if isinstance(amount, int) else None


@router.get("/webhooks/events")
def recent_events(limit: int = 20) -> dict:
    """What Razorpay has told us lately. Useful in the demo video."""
    with SessionLocal() as session:
        rows = (
            session.query(WebhookEvent)
            .order_by(WebhookEvent.id.desc())
            .limit(limit)
            .all()
        )
        return {
            "count": len(rows),
            "events": [
                {
                    "event_id": r.event_id,
                    "event": r.event,
                    "entity_id": r.entity_id,
                    "status": r.status,
                    "received_at": r.received_at.isoformat()
                    if isinstance(r.received_at, dt.datetime)
                    else str(r.received_at),
                    "error": r.error,
                }
                for r in rows
            ],
        }
