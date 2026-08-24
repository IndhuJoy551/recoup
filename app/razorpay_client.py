"""The one place in this project that talks to Razorpay.

Everything that makes a network call to a payments API needs the same three
things, so they live here rather than being rediscovered at each call site:

* authentication, so no other module ever handles a credential;
* retry with exponential backoff, because networks fail and hammering a
  struggling server instantly turns a blip into an outage;
* a circuit breaker, so a sustained outage costs us one failed call a minute
  instead of four failed calls per case across a 300-case batch.

Safety rule enforced here and not left to callers: customer notifications are
forced off. Razorpay test mode will happily send a real SMS to a real phone.
Recoup decides *when* a merchant should contact someone; it is not going to
message strangers because a test fixture had a plausible phone number in it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings

RETRY_STATUS = {429, 500, 502, 503, 504}


class RazorpayError(RuntimeError):
    """A call failed in a way the caller has to deal with."""

    def __init__(self, message: str, *, status: int | None = None,
                 body: Any = None, attempts: int = 1):
        super().__init__(message)
        self.status = status
        self.body = body
        self.attempts = attempts


class CircuitOpen(RazorpayError):
    """We are not calling Razorpay right now because it kept failing."""


@dataclass
class CircuitBreaker:
    """Trips after `threshold` consecutive failures, resets after `cooldown`."""

    threshold: int = 5
    cooldown_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = field(default=None)

    def before_call(self, now: float) -> None:
        if self.opened_at is None:
            return
        if now - self.opened_at < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - self.opened_at)
            raise CircuitOpen(
                f"circuit open after {self.failures} consecutive failures; "
                f"retrying in {remaining:.0f}s"
            )
        # Cooldown elapsed: allow one probe through.
        self.opened_at = None

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = now

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None


@dataclass
class CallResult:
    """What came back, plus how much effort it took to get it."""

    data: dict[str, Any]
    attempts: int
    elapsed_ms: int


class RazorpayClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        timeout: float = 15.0,
        breaker: CircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings or get_settings()
        self.settings.require_razorpay()
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.breaker = breaker or CircuitBreaker()
        self._client = httpx.Client(
            base_url=self.settings.razorpay_base_url,
            auth=(self.settings.razorpay_key_id, self.settings.razorpay_key_secret),
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RazorpayClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- internals

    def _sleep_for(self, attempt: int) -> float:
        """Exponential backoff with jitter, so parallel workers do not all retry
        in lockstep and re-create the spike that caused the failure."""
        return self.base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)

    def _request(self, method: str, path: str,
                 json_body: dict | None = None,
                 params: dict | None = None) -> CallResult:
        started = time.monotonic()
        self.breaker.before_call(time.monotonic())

        last_error: RazorpayError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.request(
                    method, path, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                last_error = RazorpayError(f"network error: {exc}", attempts=attempt)
            else:
                if response.status_code < 400:
                    self.breaker.record_success()
                    return CallResult(
                        data=response.json() if response.content else {},
                        attempts=attempt,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )

                body = self._safe_json(response)
                message = self._error_message(body) or response.text[:200]

                if response.status_code not in RETRY_STATUS:
                    # 400/401/404 are our fault, not a blip. Retrying a malformed
                    # request just makes the same mistake three more times.
                    self.breaker.record_success()
                    raise RazorpayError(
                        f"{method} {path} -> {response.status_code}: {message}",
                        status=response.status_code, body=body, attempts=attempt,
                    )

                last_error = RazorpayError(
                    f"{method} {path} -> {response.status_code}: {message}",
                    status=response.status_code, body=body, attempts=attempt,
                )

            if attempt < self.max_attempts:
                time.sleep(self._sleep_for(attempt))

        self.breaker.record_failure(time.monotonic())
        assert last_error is not None
        raise last_error

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _error_message(body: Any) -> str | None:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return error.get("description") or error.get("code")
        return None

    # ------------------------------------------------------------------- calls

    def ping(self) -> CallResult:
        """Cheapest call that proves the credentials work."""
        return self._request("GET", "/payments", params={"count": 1})

    def create_customer(self, *, name: str, email: str | None = None,
                        contact: str | None = None,
                        notes: dict | None = None) -> CallResult:
        body: dict[str, Any] = {"name": name, "fail_existing": 0}
        if email:
            body["email"] = email
        if contact:
            body["contact"] = contact
        if notes:
            body["notes"] = notes
        return self._request("POST", "/customers", body)

    def create_order(self, *, amount_paise: int, receipt: str,
                     currency: str = "INR", notes: dict | None = None) -> CallResult:
        return self._request("POST", "/orders", {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": notes or {},
        })

    def create_payment_link(self, *, amount_paise: int, description: str,
                            customer: dict | None = None,
                            currency: str = "INR",
                            notes: dict | None = None,
                            expire_by: int | None = None) -> CallResult:
        body: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "description": description[:2048],
            "accept_partial": False,
            # Never on. See module docstring.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": notes or {},
        }
        if customer:
            body["customer"] = customer
        if expire_by:
            body["expire_by"] = expire_by
        return self._request("POST", "/payment_links", body)

    def fetch_payment_link(self, link_id: str) -> CallResult:
        return self._request("GET", f"/payment_links/{link_id}")

    def fetch_payments(self, count: int = 10) -> CallResult:
        return self._request("GET", "/payments", params={"count": count})
