"""The webhook receiver is the one door into this system that faces the internet.

Everything here is a claim we would have to defend in an interview:

* an unsigned or wrongly-signed delivery changes nothing;
* the same delivery arriving three times produces exactly one action;
* the id we file an event under is the object the event is actually about.

The signatures are computed the same way Razorpay computes them, so these are not
tests of a mock — they are tests of the real check against real bytes.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import webhooks
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import WebhookEvent

SECRET = "test_webhook_secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    """Pin the secret for the duration of each test, without touching .env."""
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SECRET)
    return SECRET


@pytest.fixture
def client(session):
    # `session` comes from conftest: a fresh database per test.
    with TestClient(app) as c:
        yield c


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def delivery(event: str = "payment.captured", amount: int = 120_000) -> bytes:
    """A payment_link.paid-shaped body: several entities, one subject."""
    payload = {
        "entity": "event",
        "event": event,
        "payload": {
            # Deliberately not in the order you would guess. Razorpay's real
            # payment_link.paid puts the order first.
            "order": {"entity": {"id": "order_abc", "amount": 500_000}},
            "payment": {"entity": {"id": "pay_abc", "amount": amount}},
            "payment_link": {"entity": {"id": "plink_abc", "amount": amount}},
        },
    }
    return json.dumps(payload).encode()


def post(client, body: bytes, *, signature: str | None = None, event_id: str = "evt_1"):
    headers = {"Content-Type": "application/json", "X-Razorpay-Event-Id": event_id}
    if signature is not None:
        headers["X-Razorpay-Signature"] = signature
    return client.post("/webhooks/razorpay", content=body, headers=headers)


def stored() -> list[WebhookEvent]:
    with SessionLocal() as s:
        return s.query(WebhookEvent).order_by(WebhookEvent.id).all()


# --------------------------------------------------------------- authenticity


def test_a_correctly_signed_delivery_is_accepted(client):
    body = delivery()
    response = post(client, body, signature=sign(body))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    rows = stored()
    assert len(rows) == 1
    assert rows[0].signature_valid is True
    assert rows[0].status == "processed"


def test_an_unsigned_delivery_is_rejected_and_stored_nowhere(client):
    body = delivery()
    response = post(client, body, signature=None)

    assert response.status_code == 401
    assert stored() == [], "an unauthenticated caller must not write to our tables"


def test_a_forged_signature_is_rejected(client):
    """Anyone can find the URL. Only Razorpay knows the secret."""
    body = delivery()
    response = post(client, body, signature=sign(body, secret="not_the_secret"))

    assert response.status_code == 401
    assert stored() == []


def test_a_tampered_body_is_rejected(client):
    """Sign a real ₹1,200 event, then try to spend the signature on ₹5,00,000."""
    honest = delivery(amount=120_000)
    signature = sign(honest)
    tampered = honest.replace(b'"amount": 120000', b'"amount": 500000')

    response = post(client, tampered, signature=signature)

    assert response.status_code == 401
    assert stored() == []


# ------------------------------------------------------------- idempotency


def test_the_same_delivery_three_times_produces_exactly_one_action(client):
    """Razorpay's delivery guarantee is at-least-once. Ours is exactly-once."""
    body = delivery()
    signature = sign(body)

    responses = [
        post(client, body, signature=signature, event_id="evt_repeat")
        for _ in range(3)
    ]

    assert [r.status_code for r in responses] == [200, 200, 200], (
        "every delivery must be acknowledged, or Razorpay keeps retrying forever"
    )
    assert [r.json()["status"] for r in responses] == [
        "accepted", "duplicate", "duplicate",
    ]

    rows = stored()
    assert len(rows) == 1, "three deliveries, one row, one ₹1,200"


def test_two_genuinely_different_events_both_land(client):
    """The guard is on the event id, not on the body. A real second payment for
    the same amount must not be swallowed as a duplicate."""
    body = delivery()
    signature = sign(body)

    post(client, body, signature=signature, event_id="evt_one")
    post(client, body, signature=signature, event_id="evt_two")

    assert len(stored()) == 2


# ------------------------------------------------------- reading the payload


def test_the_filed_entity_is_the_subject_of_the_event():
    """`payment_link.paid` is about the link, even though an order arrives first."""
    body = json.loads(delivery("payment_link.paid"))
    assert webhooks._entity_id(body) == "plink_abc"

    body = json.loads(delivery("order.paid"))
    assert webhooks._entity_id(body) == "order_abc"

    body = json.loads(delivery("payment.captured"))
    assert webhooks._entity_id(body) == "pay_abc"


def test_the_amount_comes_from_the_subject_too():
    """The order says ₹5,000, the payment says ₹1,200. Reading the wrong one
    overstates what we recovered."""
    body = json.loads(delivery("payment.captured", amount=120_000))
    assert webhooks._amount_paise(body) == 120_000

    body = json.loads(delivery("order.paid"))
    assert webhooks._amount_paise(body) == 500_000


def test_an_unknown_event_shape_still_files_something():
    body = {"event": "something.new", "payload": {"widget": {"entity": {"id": "w_1"}}}}
    assert webhooks._entity_id(body) == "w_1"


def test_an_empty_payload_does_not_explode():
    assert webhooks._entity_id({"event": "x.y", "payload": {}}) is None
    assert webhooks._amount_paise({"event": "x.y"}) is None


# --------------------------------------------------------------- misc safety


def test_malformed_json_with_a_valid_signature_is_a_400_not_a_500(client):
    body = b"{not json at all"
    response = post(client, body, signature=sign(body))

    assert response.status_code == 400
    assert stored() == []


def test_no_secret_configured_means_nothing_is_accepted(client, monkeypatch):
    """Failing closed. An unset secret must not become 'skip the check'."""
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", "")
    body = delivery()

    response = post(client, body, signature=sign(body))

    assert response.status_code == 401
