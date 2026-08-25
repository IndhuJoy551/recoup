"""The Watcher: which cases are at risk, and what is knowable about them.

Deliberately stupid. No AI anywhere in this file. Its whole job is to turn a
database row into a small set of plain-English facts that a planner -- LLM or
otherwise -- can reason over, and to apply the handful of judgements that are so
mechanical that involving a model would be worse than useless.

Why bother with a separate stage at all?
----------------------------------------
Two reasons, and both of them are about the interview rather than the code.

First, it makes the ablation honest. If the Watcher hands identical signals to
the rules-only policy and to the LLM, then whatever difference the report card
shows is caused by the planning, not by one of them being fed better data.

Second, it draws a line under what is *not* a judgement call. `error_source ==
"business"` means the payment failed because of the merchant's own configuration.
The customer cannot fix it by trying harder. Asking a language model to weigh
that up would be inviting it to be creative about something with exactly one
correct answer, and creativity there costs a real person a real message.

That hard stop exists because of a real failure in this project's own BUGLOG: a
test payment was declined with `international_transaction_not_allowed`, and the
obvious reading -- "the customer's card failed, chase the customer" -- was wrong.
Razorpay had already told me whose fault it was, in a field I was ignoring.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from app.models import Case

# Above this, a human signs off before anything is sent. Chosen because it is
# roughly six times the median case here: big enough to be rare, small enough
# that the escalation path is exercised by the batch rather than theoretical.
HIGH_VALUE_PAISE = 500_000

# Sources whose failures no message can fix. See the module docstring.
MERCHANT_FAULT_SOURCES = frozenset({"business"})

# Sources where the customer did nothing wrong and there is nothing to say to
# them: the machinery broke. These want a silent retry, not a conversation.
TRANSIENT_SOURCES = frozenset({"bank", "gateway"})

# Failures that will never succeed again with the same instrument, however many
# times you retry it. Retrying a dead card is a free way to look busy.
DEAD_INSTRUMENT_REASONS = frozenset({"card_expired", "mandate_revoked"})


@dataclass
class Signal:
    """Everything Recoup is allowed to know about one case, in one object.

    This is the input to every policy, baselines included. `CaseTruth` is not
    reachable from here and is not imported by this module -- see that class's
    docstring for why the separation is structural rather than a promise.
    """

    case_id: str
    kind: str
    amount_paise: int
    customer_ref: str

    # unrecoverable | retryable | contactable
    recoverability: str
    risk_band: str                      # high | medium | low
    priority: float                     # ordering only, never a probability

    hard_stop: str | None = None        # set => no contact may be attempted
    needs_human: bool = False
    error_source: str | None = None
    failure_reason: str | None = None
    method: str | None = None

    opted_out: bool = False
    prior_purchases: int = 0
    prior_contacts_90d: int = 0
    age_hours: float = 0.0
    detected_hour_ist: int = 10
    # Attempts already made on THIS case in previous runs. The stopping rule is
    # about a case's whole life, not about one day's plan.
    attempts_so_far: int = 0
    days_to_salary_day: int = 1

    facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The exact payload handed to the planner. Nothing hidden, nothing extra."""
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "amount_rupees": round(self.amount_paise / 100, 2),
            "recoverability": self.recoverability,
            "risk_band": self.risk_band,
            "hard_stop": self.hard_stop,
            "needs_human_approval": self.needs_human,
            "error_source": self.error_source,
            "failure_reason": self.failure_reason,
            "payment_method": self.method,
            "customer_opted_out": self.opted_out,
            "prior_purchases": self.prior_purchases,
            "recovery_contacts_last_90d": self.prior_contacts_90d,
            "hours_since_detected": round(self.age_hours, 1),
            **({"attempts_already_made": self.attempts_so_far}
               if self.attempts_so_far else {}),
            "detected_at_hour_ist": self.detected_hour_ist,
            "days_until_next_salary_day": self.days_to_salary_day,
            "facts": self.facts,
        }


def _days_to_next_first(moment: dt.datetime) -> int:
    """Days from `moment` to the next 1st of a month, in IST.

    Salaries in India land on the 1st, and an account that was empty on the 28th
    is usually not empty on the 2nd. This number is the single most useful piece
    of timing information in the whole system and it costs three lines to compute.
    """
    local = moment.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))

    # Payday itself. Without this the answer on the 1st is 30, and the planner is
    # told to sit on a case for a month on the one morning the money is actually
    # in the account. The bug only appears on one day in thirty, which is exactly
    # the kind that survives to production.
    if local.day == 1:
        return 0

    if local.month == 12:
        nxt = local.replace(year=local.year + 1, month=1, day=1)
    else:
        nxt = local.replace(month=local.month + 1, day=1)
    nxt = nxt.replace(hour=10, minute=0, second=0, microsecond=0)
    return max(0, (nxt.date() - local.date()).days)


def assess(case: Case, *, as_of: dt.datetime) -> Signal:
    """Look at one case and say what is true about it. Pure; no database writes."""
    meta = json.loads(case.meta_json or "{}")
    customer = meta.get("customer", {})
    failure = meta.get("failure", {})

    source = failure.get("error_source")
    reason = failure.get("error_reason")
    opted_out = bool(customer.get("opted_out"))
    prior_purchases = int(customer.get("prior_purchases") or 0)
    prior_contacts = int(customer.get("prior_recovery_contacts_90d") or 0)

    detected = case.detected_at
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=dt.timezone.utc)
    age_hours = max(0.0, (as_of - detected).total_seconds() / 3600.0)
    detected_hour_ist = detected.astimezone(
        dt.timezone(dt.timedelta(hours=5, minutes=30))
    ).hour

    facts: list[str] = []
    hard_stop: str | None = None

    # ---------------------------------------------------------- recoverability

    if source in MERCHANT_FAULT_SOURCES:
        recoverability = "unrecoverable"
        hard_stop = "merchant_side_failure"
        facts.append(
            f"Razorpay attributed this failure to error_source='{source}' -- it is a "
            "merchant configuration problem. No message or retry can fix it; only "
            "changing the account settings can."
        )
    elif source in TRANSIENT_SOURCES:
        recoverability = "retryable"
        facts.append(
            f"error_source='{source}': the customer did nothing wrong and the "
            "instrument is fine. This usually clears on a silent retry."
        )
    elif reason in DEAD_INSTRUMENT_REASONS:
        recoverability = "contactable"
        facts.append(
            f"'{reason}' means the saved instrument is dead. Retrying it will fail "
            "again forever; the customer has to supply a new one."
        )
    else:
        recoverability = "contactable"

    if opted_out:
        hard_stop = hard_stop or "customer_opted_out"
        facts.append("This customer has opted out of recovery contact.")

    # -------------------------------------------------------------- timing

    days_to_salary = _days_to_next_first(as_of)
    if reason == "insufficient_funds":
        facts.append(
            f"Declined for insufficient funds. The next salary day is in "
            f"{days_to_salary} day(s); retrying into the same empty account before "
            "then tends to fail the same way."
        )

    # ------------------------------------------------------------ context

    if prior_purchases >= 5:
        facts.append(
            f"Repeat customer: {prior_purchases} previous purchases. Worth a light "
            "touch rather than pressure."
        )
    elif prior_purchases == 0:
        facts.append("First-time customer -- no relationship to spend.")

    if prior_contacts >= 2:
        facts.append(
            f"Already contacted {prior_contacts} times in the last 90 days about "
            "recovery. Further contact carries real opt-out risk."
        )

    if case.kind == "abandoned_checkout":
        checkout = meta.get("checkout", {})
        minutes = checkout.get("minutes_since_abandon")
        if checkout.get("reached_payment_page"):
            facts.append("They reached the payment page before leaving -- high intent.")
        if isinstance(minutes, int) and minutes < 180:
            facts.append("Abandoned within the last few hours; likely just distracted.")
    elif case.kind == "overdue_invoice":
        invoice = meta.get("invoice", {})
        facts.append(
            f"B2B invoice, {invoice.get('days_overdue')} days past {invoice.get('terms_days')}-day terms."
        )
    elif case.kind == "failed_mandate":
        sub = meta.get("subscription", {})
        facts.append(
            f"Subscription auto-debit failed after {sub.get('cycles_paid')} successful cycles."
        )

    # --------------------------------------------------------- risk and value

    needs_human = case.amount_paise >= HIGH_VALUE_PAISE
    if needs_human:
        facts.append(
            f"Rs {case.amount_paise // 100:,} is above the Rs {HIGH_VALUE_PAISE // 100:,} "
            "auto-action ceiling: a human approves before anything is sent."
        )

    # Priority is for ordering a work queue, nothing more. It is deliberately not
    # a probability: calling it one would invite treating a hand-tuned weight as
    # a calibrated forecast, which is how dashboards start lying.
    priority = case.amount_paise / 100_000.0
    if recoverability == "unrecoverable" or opted_out:
        priority = 0.0
    elif recoverability == "retryable":
        priority *= 1.35                      # cheap to attempt, nobody is bothered
    priority *= 1.0 + 0.06 * min(prior_purchases, 10)
    priority *= max(0.55, 1.0 - 0.02 * (age_hours / 24.0))

    if priority >= 6.0:
        band = "high"
    elif priority >= 1.2:
        band = "medium"
    else:
        band = "low"

    return Signal(
        case_id=case.id,
        kind=case.kind,
        amount_paise=case.amount_paise,
        customer_ref=case.customer_ref,
        recoverability=recoverability,
        risk_band=band,
        priority=round(priority, 3),
        hard_stop=hard_stop,
        needs_human=needs_human,
        error_source=source,
        failure_reason=reason,
        method=meta.get("method"),
        opted_out=opted_out,
        prior_purchases=prior_purchases,
        prior_contacts_90d=prior_contacts,
        age_hours=age_hours,
        detected_hour_ist=detected_hour_ist,
        attempts_so_far=int(case.attempts or 0),
        days_to_salary_day=days_to_salary,
        facts=facts,
    )


def scan(cases: list[Case], *, as_of: dt.datetime) -> list[Signal]:
    """Assess a whole batch, highest priority first."""
    signals = [assess(case, as_of=as_of) for case in cases]
    signals.sort(key=lambda s: s.priority, reverse=True)
    return signals


def summarise(signals: list[Signal]) -> dict:
    """What the Watcher found, for the dashboard and the run header."""
    by_band: dict[str, int] = {}
    by_recoverability: dict[str, int] = {}
    for signal in signals:
        by_band[signal.risk_band] = by_band.get(signal.risk_band, 0) + 1
        by_recoverability[signal.recoverability] = (
            by_recoverability.get(signal.recoverability, 0) + 1
        )
    return {
        "cases": len(signals),
        "at_risk_paise": sum(s.amount_paise for s in signals),
        "by_risk_band": by_band,
        "by_recoverability": by_recoverability,
        "hard_stopped": sum(1 for s in signals if s.hard_stop),
        "needs_human": sum(1 for s in signals if s.needs_human),
    }
