"""The Doer: the only component that is allowed to make something happen.

It takes an `Action` that has already passed the Guard and turns it into an
effect. Two modes, one code path:

    simulate   the 300-case batch. Nothing leaves the machine; the referee
               decides what the world does about it.
    live       the demo. Real calls to Razorpay's test-mode API, producing real
               objects with real ids that show up in the Razorpay dashboard.

Keeping both modes in one class is deliberate. If the batch ran through a
different code path from the live demo, the architecture diagram would be a
drawing rather than a description, and the thing shown on camera would not be
the thing measured in the report card.

What is real and what is not
----------------------------
Stated plainly here because it also has to be stated plainly in the video.

  send_payment_link   REAL. Creates a Razorpay payment link in test mode. The
                      link opens, a test card pays it, and the webhook comes
                      back. This is the path a rupee actually travelled down on
                      2026-08-24.
  schedule_retry      PARTLY REAL. Razorpay has no "re-charge this failed
                      payment" endpoint -- a failed payment is terminal, and the
                      real-world equivalent is a fresh order plus a new attempt
                      on the saved instrument. Live mode creates the order. The
                      re-attempt itself needs a saved token this test account
                      does not have, so that half is simulated and labelled.
  send_reminder       SIMULATED. Sending it would need a messaging provider,
                      and a wrong number in a fixture would text a stranger
                      about a debt they do not have.
  offer_installments  SIMULATED. `emi` is `false` on this account -- checked
                      with `scripts/check_methods.py`, output in the BUGLOG. I
                      am not going to pretend otherwise on camera.
  escalate_to_human   REAL, in the only sense it can be: the case is written to
                      an exception queue with its reason, and stops moving.
  do_nothing          Real by construction.

Customer notification is forced off one layer down, in `razorpay_client`, so no
route through this file can text a real phone in test mode.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import ledger
from app.actions import (
    Action,
    DO_NOTHING,
    ESCALATE_TO_HUMAN,
    OFFER_INSTALLMENTS,
    SCHEDULE_RETRY,
    SEND_PAYMENT_LINK,
    SEND_REMINDER,
)
from app.models import Case
from app.razorpay_client import RazorpayClient, RazorpayError
from app.watcher import Signal

# Which actions this project can genuinely perform against Razorpay's test API,
# and which are stubs. Referenced by the README and the video script so the three
# places can never quietly disagree with each other.
REALITY: dict[str, str] = {
    SEND_PAYMENT_LINK: "real",
    SCHEDULE_RETRY: "partial",
    SEND_REMINDER: "simulated",
    OFFER_INSTALLMENTS: "simulated",
    ESCALATE_TO_HUMAN: "real",
    DO_NOTHING: "real",
}


@dataclass
class Execution:
    """What the Doer did, and whether it worked."""

    action: str
    ok: bool
    mode: str                       # simulate | live
    reality: str                    # real | partial | simulated
    reference: str | None = None    # e.g. a Razorpay plink_ id
    detail: str = ""
    attempts: int = 1

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ok": self.ok,
            "mode": self.mode,
            "reality": self.reality,
            "reference": self.reference,
            "detail": self.detail,
            "attempts": self.attempts,
        }


@dataclass
class ExceptionQueue:
    """Cases a human has to look at. Nothing is ever dropped, only parked."""

    items: list[dict] = field(default_factory=list)

    def add(self, case_id: str, reason: str, amount_paise: int) -> None:
        self.items.append({
            "case_id": case_id, "reason": reason, "amount_paise": amount_paise,
        })

    def __len__(self) -> int:
        return len(self.items)


class Doer:
    """Performs approved actions. Refuses to perform unapproved ones."""

    def __init__(
        self,
        *,
        mode: str = "simulate",
        client: RazorpayClient | None = None,
        queue: ExceptionQueue | None = None,
    ) -> None:
        if mode not in ("simulate", "live"):
            raise ValueError(f"mode must be simulate or live, not {mode!r}")
        self.mode = mode
        self._client = client
        self.queue = queue or ExceptionQueue()
        self.executed: int = 0
        self.failures: int = 0

    # ------------------------------------------------------------- plumbing

    @property
    def client(self) -> RazorpayClient:
        if self._client is None:
            self._client = RazorpayClient()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -------------------------------------------------------------- execute

    def execute(
        self,
        case: Case,
        signal: Signal,
        action: Action,
        *,
        session: Session | None = None,
        as_of: dt.datetime | None = None,
    ) -> Execution:
        """Carry out one approved action.

        `session` is optional so the 300-case batch can skip a database write per
        action; when it is supplied every execution lands in the ledger before it
        is considered to have happened.
        """
        reality = REALITY.get(action.kind, "simulated")

        if action.kind == DO_NOTHING:
            result = Execution(action.kind, True, self.mode, "real",
                               detail=action.reason or "no action taken")
        elif action.kind == ESCALATE_TO_HUMAN:
            self.queue.add(case.id, action.reason or "escalated", case.amount_paise)
            result = Execution(action.kind, True, self.mode, "real",
                               reference=f"queue#{len(self.queue)}",
                               detail="parked for a human; the case stops moving")
        elif self.mode == "simulate":
            result = Execution(
                action.kind, True, "simulate", reality,
                reference=f"sim_{case.id}_{action.kind}_{action.wait_days}d",
                detail=f"scheduled for +{action.wait_days}d at {action.hour_ist:02d}:00 IST",
            )
        else:
            result = self._live(case, action, reality)

        if result.ok:
            self.executed += 1
        else:
            self.failures += 1

        if session is not None:
            ledger.record(
                session, actor="doer", event="action_executed", case_id=case.id,
                payload={**action.to_dict(), **result.to_dict()},
            )
        return result

    # ------------------------------------------------------------ live mode

    def _live(self, case: Case, action: Action, reality: str) -> Execution:
        """Real Razorpay calls. Every failure here is caught and reported, never raised.

        A single failed API call must not take down a batch, and it must not
        silently lose a case either. The retry ladder and the circuit breaker
        live in `razorpay_client`; what happens here is the last stop -- turn a
        `RazorpayError` into an outcome the ledger can record and a human can act
        on.
        """
        try:
            if action.kind == SEND_PAYMENT_LINK:
                call = self.client.create_payment_link(
                    amount_paise=case.amount_paise,
                    description=f"Recoup: {case.kind} {case.id}",
                    notes={"recoup_case": case.id, "recoup_action": action.kind},
                )
                return Execution(
                    action.kind, True, "live", reality,
                    reference=call.data.get("id"),
                    detail=call.data.get("short_url", ""),
                    attempts=call.attempts,
                )

            if action.kind == SCHEDULE_RETRY:
                call = self.client.create_order(
                    amount_paise=case.amount_paise,
                    receipt=f"retry_{case.id}"[:40],
                    notes={"recoup_case": case.id, "recoup_action": action.kind},
                )
                return Execution(
                    action.kind, True, "live", reality,
                    reference=call.data.get("id"),
                    detail=("order created; the re-attempt on the saved instrument "
                            "is simulated -- this test account has no saved token"),
                    attempts=call.attempts,
                )

            # Messaging and EMI have no live path on this account. Saying so is
            # cheaper than a demo that quietly fakes a text message.
            return Execution(
                action.kind, True, "live", reality,
                reference=None,
                detail="no live channel on this account; recorded as simulated",
            )

        except RazorpayError as exc:
            self.queue.add(case.id, f"{action.kind} failed: {exc}", case.amount_paise)
            return Execution(
                action.kind, False, "live", reality,
                detail=f"{type(exc).__name__}: {exc}",
                attempts=getattr(exc, "attempts", 1),
            )
