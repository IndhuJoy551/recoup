"""What to do about a case: four ways of deciding, none of them an LLM.

Three of these are baselines -- the dumb comparisons that make Recoup's number
mean something. Without them "we recovered Rs 42,000" has no denominator and no
opinion attached to it.

    do_nothing        Ignore all of it. This is what most small merchants
                      genuinely do, and it is the only honest zero point: some
                      of the money arrives anyway, and any policy that cannot
                      beat *that* is worse than useless.

    blast_everyone    Three messages to every at-risk customer, sent the moment
                      the case is detected, whatever the hour and whoever they
                      are. Simple, common, and the thing a compliance team has
                      nightmares about.

    retry_everything  Silently retry every failed payment three times. Costs
                      nothing, annoys nobody, and cannot help a customer who
                      cancelled on purpose or whose card is dead.

The fourth, `rules_only`, is the ablation opponent, and it is deliberately good.
Hobbling it would let the LLM "win" fraudulently, and the whole point of running
the ablation is to find out whether the model earns its place. If plain rules
beat it, the report card says so.

Everything here takes a `Signal` and only a `Signal` -- the same object the LLM
planner gets. Same information, different reasoning. That is what makes the
comparison a comparison.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.actions import (
    Action,
    ESCALATE_TO_HUMAN,
    OFFER_INSTALLMENTS,
    SCHEDULE_RETRY,
    SEND_PAYMENT_LINK,
    SEND_REMINDER,
    nothing,
)
from app.watcher import Signal

Planner = Callable[[Signal], list[Action]]


@dataclass(frozen=True)
class Policy:
    """A way of deciding, plus whether the Guard is switched on in front of it.

    `gated` is the interesting field. The baselines run with the Guard off,
    because a merchant blasting their customer list does not have a compliance
    layer -- that is the whole reason blasting is a bad idea. Their broken rules
    are counted and published instead of being silently prevented, so the report
    card can show *what the behaviour costs*, not just that we stopped it.
    """

    name: str
    plan: Planner
    gated: bool
    blurb: str
    uses_llm: bool = False


# --------------------------------------------------------------- baseline 1


def plan_do_nothing(signal: Signal) -> list[Action]:
    return [nothing("baseline: this merchant has no recovery process at all")]


# --------------------------------------------------------------- baseline 2


def plan_blast_everyone(signal: Signal) -> list[Action]:
    """Message everyone, now, then twice more. No targeting, no timing.

    The `hour_ist` is the hour the case was detected, because "immediately" is
    exactly what this policy means by it -- and a payment that fails at 1am gets
    chased at 1am. That is where its quiet-hours violations come from: not from a
    contrived example, but from the definition of the strategy.
    """
    hour = signal.detected_hour_ist
    return [
        Action(SEND_REMINDER, wait_days=0, hour_ist=hour,
               reason="baseline: contact every at-risk customer immediately"),
        Action(SEND_PAYMENT_LINK, wait_days=1, hour_ist=hour,
               reason="baseline: follow up with a link"),
        Action(SEND_REMINDER, wait_days=3, hour_ist=hour,
               reason="baseline: chase again"),
    ]


# --------------------------------------------------------------- baseline 3


def plan_retry_everything(signal: Signal) -> list[Action]:
    """Retry every failure on a fixed ladder. Never talks to anyone."""
    return [
        Action(SCHEDULE_RETRY, wait_days=0, reason="baseline: retry immediately"),
        Action(SCHEDULE_RETRY, wait_days=1, reason="baseline: retry tomorrow"),
        Action(SCHEDULE_RETRY, wait_days=3, reason="baseline: retry once more"),
    ]


# ------------------------------------------------------------- the ablation


def plan_rules_only(signal: Signal) -> list[Action]:
    """Hand-written heuristics over the same signals the model sees.

    Reading order matters: consent, then whether anything can work at all, then
    value, then the failure-specific play. Each branch below is a claim about
    payments that I can defend out loud, which is the bar for putting it in.
    """
    # --- nothing to decide ------------------------------------------------
    if signal.opted_out:
        return [nothing("customer opted out of recovery contact")]

    if signal.recoverability == "unrecoverable":
        return [Action(
            ESCALATE_TO_HUMAN, wait_days=0,
            reason=(f"error_source='{signal.error_source}' ({signal.failure_reason}): "
                    "a merchant configuration problem. Route to the merchant, "
                    "never to the customer."),
        )]

    if signal.needs_human:
        return [Action(
            ESCALATE_TO_HUMAN, wait_days=0,
            reason=f"Rs {signal.amount_paise // 100:,} is above the auto-action ceiling",
        )]

    # Someone chased twice already this quarter has very little patience left,
    # and the expected value of a third template message is negative on a small
    # balance. Restraint is a strategy, not an absence of one.
    if signal.prior_contacts_90d >= 2 and signal.amount_paise < 100_000:
        return [nothing(
            f"already contacted {signal.prior_contacts_90d} times in 90 days for "
            f"Rs {signal.amount_paise // 100:,}; not worth the opt-out risk"
        )]

    # --- the machinery broke, not the customer ---------------------------
    if signal.recoverability == "retryable":
        return [
            Action(SCHEDULE_RETRY, wait_days=0,
                   reason=f"error_source='{signal.error_source}': a transient failure, "
                          "retry silently before bothering anyone"),
            Action(SCHEDULE_RETRY, wait_days=2,
                   reason="second silent retry once the outage has had time to clear"),
            Action(SEND_PAYMENT_LINK, wait_days=4, hour_ist=11,
                   reason="retries did not clear it; now it is worth asking"),
        ]

    # --- the money is not there yet --------------------------------------
    if signal.failure_reason == "insufficient_funds":
        wait = max(1, signal.days_to_salary_day)
        return [
            Action(SCHEDULE_RETRY, wait_days=wait,
                   reason=f"insufficient funds; salary day is in {wait} day(s), so "
                          "retry then rather than into the same empty account"),
            Action(SEND_PAYMENT_LINK, wait_days=wait + 1, hour_ist=11,
                   reason="if the auto-retry missed, ask directly the day after payday"),
        ]

    # --- the instrument is dead ------------------------------------------
    if signal.failure_reason in ("card_expired", "mandate_revoked"):
        return [
            Action(SEND_PAYMENT_LINK, wait_days=0, hour_ist=11,
                   reason=f"'{signal.failure_reason}': retrying is pointless, they "
                          "have to supply a new instrument"),
            Action(SEND_REMINDER, wait_days=3, hour_ist=11,
                   reason="one follow-up, then stop"),
        ]

    # --- ordinary customer-side declines ---------------------------------
    if signal.kind == "abandoned_checkout":
        plan = [Action(
            SEND_REMINDER, wait_days=0, hour_ist=max(9, min(20, signal.detected_hour_ist)),
            reason="abandoned checkout: a light nudge while intent is still fresh",
        )]
        if signal.amount_paise >= 150_000 or signal.prior_purchases >= 3:
            plan.append(Action(
                SEND_PAYMENT_LINK, wait_days=2, hour_ist=11,
                reason="worth a second, more direct attempt at this value/relationship",
            ))
        return plan

    if signal.kind == "overdue_invoice":
        plan = [Action(
            SEND_REMINDER, wait_days=0, hour_ist=10,
            reason="B2B invoices are usually stuck in someone's approvals queue, "
                   "not refused; a reminder is what moves them",
        )]
        if signal.amount_paise >= 300_000:
            plan.append(Action(
                OFFER_INSTALLMENTS, wait_days=4, hour_ist=11,
                reason="large balance still unpaid; splitting it removes the excuse",
            ))
        return plan

    # failed_payment / failed_mandate, customer-side, instrument still alive
    return [
        Action(SEND_PAYMENT_LINK, wait_days=1, hour_ist=11,
               reason=f"'{signal.failure_reason}': the customer has to act, so give "
                      "them the shortest possible path to doing it"),
        Action(SEND_REMINDER, wait_days=4, hour_ist=11,
               reason="one follow-up, then the stopping rule takes over"),
    ]


# ------------------------------------------------------------------ registry


BASELINES: dict[str, Policy] = {
    "do_nothing": Policy(
        "do_nothing", plan_do_nothing, gated=False,
        blurb="No recovery process. The honest zero point.",
    ),
    "blast_everyone": Policy(
        "blast_everyone", plan_blast_everyone, gated=False,
        blurb="Three messages to everyone, immediately, no compliance layer.",
    ),
    "blast_everyone_gated": Policy(
        "blast_everyone_gated", plan_blast_everyone, gated=True,
        blurb="The same blast, with Recoup's Guard in front of it. Isolates "
              "targeting from compliance.",
    ),
    "retry_everything": Policy(
        "retry_everything", plan_retry_everything, gated=False,
        blurb="Silently retry every failure three times. Annoys nobody, "
              "understands nothing.",
    ),
    "rules_only": Policy(
        "rules_only", plan_rules_only, gated=True,
        blurb="Recoup with the model switched off: hand-written heuristics over "
              "the same signals. The ablation opponent.",
    ),
}


def get(name: str) -> Policy:
    if name not in BASELINES:
        raise KeyError(f"unknown policy {name!r}; have {', '.join(BASELINES)}")
    return BASELINES[name]
