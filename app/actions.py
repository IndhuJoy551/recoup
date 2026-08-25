"""The complete list of things Recoup is able to do. There are six.

This module is what the word "bounded" means in "every money action explainable,
bounded and gated". Not a guideline in a prompt -- a closed vocabulary that the
planner's output has to survive being parsed into. An LLM that asks for
`issue_refund`, or `send_payment_link` with a 400-day delay, or invents action
number seven, does not get a warning and a best-effort interpretation. It gets an
`UnknownAction` and the case is escalated to a human.

That is deliberate and it is the wrong default for most software. Normally you
are forgiving in what you accept. Here the thing on the other side of the parser
is an API that moves money, so the parser is the last place a hallucination can
be stopped cheaply.

A note on `wait_days` and `hour_ist`
-----------------------------------
Every action carries *when*, not just *what*, because in recovery the timing is
most of the decision. An insufficient-funds decline on the 28th is a different
case on the 1st. And a technically perfect reminder delivered at 3am is a
compliance problem, so the hour is part of the proposal and therefore part of
what the Guard can refuse.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- the six

SEND_PAYMENT_LINK = "send_payment_link"
SCHEDULE_RETRY = "schedule_retry"
SEND_REMINDER = "send_reminder"
OFFER_INSTALLMENTS = "offer_installments"
ESCALATE_TO_HUMAN = "escalate_to_human"
DO_NOTHING = "do_nothing"

ACTION_KINDS: tuple[str, ...] = (
    SEND_PAYMENT_LINK,
    SCHEDULE_RETRY,
    SEND_REMINDER,
    OFFER_INSTALLMENTS,
    ESCALATE_TO_HUMAN,
    DO_NOTHING,
)

# Actions that put a message in front of a human being. Everything the Guard
# cares about most -- quiet hours, opt-out, frequency caps, harassment -- keys off
# this set, so membership here is a compliance decision, not a taxonomy.
CONTACT_ACTIONS: frozenset[str] = frozenset(
    {SEND_PAYMENT_LINK, SEND_REMINDER, OFFER_INSTALLMENTS}
)

# A silent retry touches the gateway, not the customer. It cannot annoy anyone,
# which is exactly why it is the right answer for a bank-side failure and the
# wrong answer for a customer who chose to cancel.
SILENT_ACTIONS: frozenset[str] = frozenset({SCHEDULE_RETRY})

MAX_WAIT_DAYS = 14
MAX_PLAN_LENGTH = 4  # the stopping rule, enforced again in the Guard


class UnknownAction(ValueError):
    """The planner asked for something outside the vocabulary.

    Raised rather than defaulted. A silent fallback to `do_nothing` would hide
    the most interesting failure this system can have -- and that failure is one
    of the things the video is supposed to show.
    """


@dataclass(frozen=True)
class Action:
    """One bounded, timed, explained thing to do about one case."""

    kind: str
    wait_days: int = 0
    hour_ist: int = 10
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise UnknownAction(
                f"{self.kind!r} is not one of the six permitted actions: "
                f"{', '.join(ACTION_KINDS)}"
            )
        if not isinstance(self.wait_days, int) or not 0 <= self.wait_days <= MAX_WAIT_DAYS:
            raise UnknownAction(
                f"wait_days={self.wait_days!r} is outside 0..{MAX_WAIT_DAYS}. A "
                "recovery attempt scheduled beyond the window is a lost case "
                "dressed up as a plan."
            )
        if not isinstance(self.hour_ist, int) or not 0 <= self.hour_ist <= 23:
            raise UnknownAction(f"hour_ist={self.hour_ist!r} is not an hour of the day")

    # -------------------------------------------------------------- helpers

    @property
    def is_contact(self) -> bool:
        return self.kind in CONTACT_ACTIONS

    @property
    def is_silent(self) -> bool:
        return self.kind in SILENT_ACTIONS

    def scheduled_at(self, as_of: dt.datetime) -> dt.datetime:
        """The exact UTC instant this action would fire.

        The hour is chosen in IST and then converted, rather than stored in UTC
        and hoped about. "No messages after 9pm" is a rule about the customer's
        evening, and the customer is in India.
        """
        local = as_of.astimezone(IST) + dt.timedelta(days=self.wait_days)
        local = local.replace(hour=self.hour_ist, minute=0, second=0, microsecond=0)
        return local.astimezone(dt.timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.kind,
            "wait_days": self.wait_days,
            "hour_ist": self.hour_ist,
            "reason": self.reason,
        }

    @classmethod
    def _clean(cls, text: str) -> str:
        """Make model-written text safe to print, log and store.

        `reason` is the one field on an Action that comes from a language model
        rather than from this codebase, so it is the one field that can contain
        anything at all. The rupee sign is the concrete example that bit: the
        model writes "Rs 1,734" as "₹ 1,734", the Windows console this is
        developed on is cp1252, and printing the audit trail crashed. Normalising
        here rather than at each print site means there is exactly one place
        where untrusted text becomes trusted text.
        """
        replacements = {
            "₹": "Rs ", "’": "'", "‘": "'",
            "“": '"', "”": '"', "—": " -- ", "–": "-",
            " ": " ",
        }
        for bad, good in replacements.items():
            text = text.replace(bad, good)
        return text.encode("ascii", "replace").decode("ascii").strip()

    @classmethod
    def parse(cls, payload: Any) -> Action:
        """Turn whatever the planner produced into an Action, or refuse.

        Written defensively on purpose: `payload` here is model output, so it may
        be a string, a dict with the wrong keys, a float where an int belongs, or
        a plausible-looking action that does not exist.
        """
        if isinstance(payload, str):
            payload = {"action": payload}
        if not isinstance(payload, dict):
            raise UnknownAction(f"expected an object describing an action, got {type(payload).__name__}")

        kind = payload.get("action") or payload.get("kind")
        if not isinstance(kind, str):
            raise UnknownAction("the proposal has no `action` field")
        kind = kind.strip().lower()

        def _int(key: str, default: int) -> int:
            value = payload.get(key, default)
            if isinstance(value, bool):
                raise UnknownAction(f"{key} must be a number, not a boolean")
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            if not isinstance(value, int):
                raise UnknownAction(f"{key}={value!r} is not a whole number")
            return value

        reason = payload.get("reason") or payload.get("why") or ""
        return cls(
            kind=kind,
            wait_days=_int("wait_days", 0),
            hour_ist=_int("hour_ist", 10),
            reason=cls._clean(str(reason))[:400],
        )


def parse_plan(payload: Any, *, limit: int = MAX_PLAN_LENGTH) -> list[Action]:
    """Parse a whole proposed plan. Raises on the first thing it does not know."""
    if isinstance(payload, dict):
        payload = payload.get("plan", payload.get("actions", [payload]))
    if not isinstance(payload, list):
        raise UnknownAction("a plan must be a list of actions")
    if len(payload) > limit:
        raise UnknownAction(
            f"a plan of {len(payload)} actions exceeds the stopping rule of {limit}"
        )
    return [Action.parse(item) for item in payload]


def nothing(reason: str) -> Action:
    """The explicit do-nothing. Always carries a reason -- silence is not a plan."""
    return Action(kind=DO_NOTHING, reason=reason)
