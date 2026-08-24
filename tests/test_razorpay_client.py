"""The resilience behaviour is the part that has to work when Razorpay does not.

These tests never touch the network. httpx.MockTransport lets us make the API
fail exactly how we want it to, which is the only practical way to test "what
happens during an outage" before the outage.
"""

import json

import httpx
import pytest

from app.config import Settings
from app.razorpay_client import (
    CircuitBreaker,
    CircuitOpen,
    RazorpayClient,
    RazorpayError,
)


def fake_settings() -> Settings:
    return Settings(
        razorpay_key_id="rzp_test_fake123",
        razorpay_key_secret="secret",
        razorpay_webhook_secret="whsec",
    )


def client_with(handler, **kwargs) -> RazorpayClient:
    return RazorpayClient(
        fake_settings(),
        transport=httpx.MockTransport(handler),
        base_delay=0,  # no real waiting in tests
        **kwargs,
    )


def test_transient_failures_are_retried_then_succeed():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"description": "unavailable"}})
        return httpx.Response(200, json={"count": 0, "items": []})

    with client_with(handler) as client:
        result = client.ping()

    assert result.attempts == 3
    assert calls["n"] == 3


def test_bad_request_is_not_retried():
    """A 400 is our mistake. Sending it three more times just repeats the mistake."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            400, json={"error": {"description": "Recurring digits in contact"}}
        )

    with client_with(handler) as client:
        with pytest.raises(RazorpayError) as exc:
            client.ping()

    assert calls["n"] == 1
    assert exc.value.status == 400
    assert "Recurring digits" in str(exc.value)


def test_retries_give_up_after_max_attempts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"description": "boom"}})

    with client_with(handler, max_attempts=4) as client:
        with pytest.raises(RazorpayError):
            client.ping()

    assert calls["n"] == 4


def test_circuit_opens_after_repeated_failures_and_stops_calling():
    """The point of the breaker: the third attempt costs zero network calls."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"description": "boom"}})

    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
    with client_with(handler, max_attempts=2, breaker=breaker) as client:
        for _ in range(2):
            with pytest.raises(RazorpayError):
                client.ping()

        assert breaker.is_open
        calls_before = calls["n"]

        with pytest.raises(CircuitOpen):
            client.ping()

    assert calls["n"] == calls_before, "an open circuit must not reach the network"


def test_circuit_closes_again_after_the_cooldown():
    state = {"fail": True}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500, json={"error": {"description": "boom"}})
        return httpx.Response(200, json={"count": 0, "items": []})

    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    with client_with(handler, max_attempts=1, breaker=breaker) as client:
        with pytest.raises(RazorpayError):
            client.ping()
        assert breaker.is_open

        # Pretend the cooldown has elapsed.
        breaker.opened_at -= 61
        state["fail"] = False

        result = client.ping()

    assert result.attempts == 1
    assert not breaker.is_open


def test_payment_links_never_notify_the_customer():
    """Test mode sends real SMS. This is enforced in the client, not left to
    callers to remember."""
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "plink_test", "short_url": "https://x"})

    with client_with(handler) as client:
        client.create_payment_link(
            amount_paise=120_000,
            description="test",
            customer={"name": "A", "contact": "+919876543210"},
        )

    assert captured["notify"] == {"sms": False, "email": False}
    assert captured["reminder_enable"] is False
    assert captured["amount"] == 120_000


def test_live_keys_are_refused():
    """Recoup takes real money actions. It only ever runs against test mode."""
    live = Settings(razorpay_key_id="rzp_live_real", razorpay_key_secret="secret")
    with pytest.raises(RuntimeError, match="not a test key"):
        live.require_razorpay()
