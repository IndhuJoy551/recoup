"""The referee: what actually happens when a policy acts on a case.

This is the only module besides the report card that is allowed to read
`CaseTruth`. Everything upstream of it -- Watcher, planner, Guard, Doer -- sees
`Case` and nothing else. The import list at the top of each of those files is the
proof, and there is a test that checks it.

What this file is really for
----------------------------
A recovery tool that only measures "money that arrived" cannot tell the
difference between these two months:

* we sent 300 messages, and 56 customers who were always going to pay, paid;
* we sent 40 messages, and 38 customers who would otherwise have vanished, paid.

The first looks better on a dashboard and is worth nothing. Separating them needs
a counterfactual -- what would have happened if we had done nothing -- and in the
real world that is unknowable, because contacting someone destroys the control
group. So the cohort carries the answer, this file consults it, and the report
card publishes both numbers side by side: `collected` (what a naive tool would
claim) and `caused` (what we can actually take credit for).

Determinism
-----------
Every roll comes from `random.Random(f"{seed}|{policy}|{case_id}")`. Two
consequences that both matter. Re-running the report card gives byte-identical
numbers, which is submission checklist item 2. And the same case gets the *same*
luck under every policy, so when "blast everyone" beats us on some case it is
because of the decision, not because it drew a better die. That is a paired
comparison, and it is the difference between an experiment and an anecdote.

The model is stated openly, in code, rather than hidden behind a fitted curve --
these are the rules a reader has to accept for the report card to mean anything,
so they are short enough to read.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.actions import (
    Action,
    DO_NOTHING,
    ESCALATE_TO_HUMAN,
    OFFER_INSTALLMENTS,
    SCHEDULE_RETRY,
    SEND_PAYMENT_LINK,
    SEND_REMINDER,
)
from app.models import Case, CaseTruth

# ------------------------------------------------------------------- costs
# Real, checkable numbers rather than round ones, because "cost per rupee
# recovered" is a metric almost nobody publishes and it is worthless if the
# inputs are invented.

CONTACT_COST_PAISE = 85          # one WhatsApp/SMS recovery message, Indian rates
RETRY_COST_PAISE = 0             # a gateway attempt costs nothing until it succeeds
HUMAN_COST_PAISE = 12_000        # ~15 min of support time at Rs 480/hour

# How much a repeat contact is worth compared to the one before it. People stop
# reading. A policy that sends four messages is not four times as effective.
FATIGUE = 0.62

# A silent retry that already failed once is mostly a formality the second time.
RETRY_DECAY = 0.5

# How well each action suits each kind of case. A reminder is a nudge; a payment
# link removes the friction; an instalment offer only helps if the problem is the
# size of the number. These are judgement calls and they are visible on purpose.
FIT: dict[str, dict[str, float]] = {
    SEND_PAYMENT_LINK: {
        "failed_payment": 1.00, "failed_mandate": 0.95,
        "abandoned_checkout": 1.00, "overdue_invoice": 0.90,
    },
    SEND_REMINDER: {
        "failed_payment": 0.70, "failed_mandate": 0.72,
        "abandoned_checkout": 0.66, "overdue_invoice": 0.88,
    },
    OFFER_INSTALLMENTS: {
        "failed_payment": 0.85, "failed_mandate": 0.80,
        "abandoned_checkout": 0.78, "overdue_invoice": 0.95,
    },
}

# Below this, being offered instalments is faintly insulting and works less well.
INSTALMENT_SWEET_SPOT_PAISE = 300_000


@dataclass
class Event:
    """One thing the referee saw happen. Goes straight into the ledger."""

    action: str
    wait_days: int
    outcome: str          # paid | no_response | opted_out | not_applicable
    probability: float
    cost_paise: int
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "wait_days": self.wait_days,
            "outcome": self.outcome,
            "p_success": round(self.probability, 4),
            "cost_paise": self.cost_paise,
            "note": self.note,
        }


@dataclass
class Outcome:
    """What one policy achieved on one case, and what it cost to achieve it."""

    case_id: str
    policy: str
    amount_paise: int

    paid: bool = False                 # money arrived, for any reason at all
    caused: bool = False               # money arrived *because of* something we did
    would_have_paid_anyway: bool = False

    contacts: int = 0
    retries: int = 0
    escalations: int = 0
    cost_paise: int = 0

    opted_out: bool = False            # we annoyed them into leaving, permanently
    false_intervention: bool = False   # we contacted someone who was already coming
    wasted_contact: bool = False       # we contacted an unrecoverable case
    correctly_left_alone: bool = False # unrecoverable, and we did not chase it

    events: list[Event] = field(default_factory=list)

    @property
    def collected_paise(self) -> int:
        """What a naive tool would put on the dashboard."""
        return self.amount_paise if self.paid else 0

    @property
    def caused_paise(self) -> int:
        """What we can honestly claim. The only number worth comparing."""
        return self.amount_paise if self.caused else 0

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "policy": self.policy,
            "amount_paise": self.amount_paise,
            "paid": self.paid,
            "caused": self.caused,
            "collected_paise": self.collected_paise,
            "caused_paise": self.caused_paise,
            "contacts": self.contacts,
            "retries": self.retries,
            "escalations": self.escalations,
            "cost_paise": self.cost_paise,
            "opted_out": self.opted_out,
            "false_intervention": self.false_intervention,
            "wasted_contact": self.wasted_contact,
            "events": [e.to_dict() for e in self.events],
        }


def _timing_multiplier(action: Action, truth: CaseTruth) -> float:
    """How much the chosen moment helps or hurts.

    The asymmetry is the point. Acting too early on an insufficient-funds decline
    is close to worthless -- the money is genuinely not in the account yet and you
    have spent your best message on an empty balance. Acting late is merely
    slightly worse than acting on time. Recovery punishes impatience much harder
    than it punishes delay, and a planner that has not noticed that will look
    busy and underperform.
    """
    best = max(1, truth.best_wait_days)
    if best <= 1:
        # Nothing to wait for; dithering costs a little momentum, that is all.
        return 1.0 if action.wait_days <= 2 else max(0.80, 1.0 - 0.03 * action.wait_days)

    if action.wait_days >= best:
        return max(0.62, 1.0 - 0.07 * (action.wait_days - best))

    # Too early: scale up towards 1.0 as you approach the right day.
    return 0.35 + 0.65 * (action.wait_days / best)


def _contact_probability(
    case: Case, truth: CaseTruth, action: Action, fatigue: float
) -> float:
    base = truth.p_pay_if_contacted
    fit = FIT.get(action.kind, {}).get(case.kind, 0.7)

    if action.kind == OFFER_INSTALLMENTS:
        # Splitting a small payment into parts solves a problem nobody had.
        fit *= 1.15 if case.amount_paise >= INSTALMENT_SWEET_SPOT_PAISE else 0.72

    return max(0.0, min(0.95, base * fit * _timing_multiplier(action, truth) * fatigue))


def simulate(
    case: Case,
    truth: CaseTruth,
    actions: list[Action],
    *,
    policy: str,
    seed: int,
) -> Outcome:
    """Play out one case under one policy and report what happened.

    `actions` are the ones that survived the Guard. Everything the Guard refused
    never reaches the world, which is the entire point of the Guard, so refusals
    cost nothing here and are counted separately by the report card.
    """
    rng = random.Random(f"{seed}|{policy}|{case.id}")

    outcome = Outcome(
        case_id=case.id,
        policy=policy,
        amount_paise=case.amount_paise,
        would_have_paid_anyway=truth.would_pay_unprompted,
    )

    real_actions = [a for a in actions if a.kind != DO_NOTHING]
    if not real_actions and not truth.recoverable:
        outcome.correctly_left_alone = True

    fatigue = 1.0
    retry_strength = 1.0

    for action in real_actions:
        if outcome.paid or outcome.opted_out:
            break

        # ---------------------------------------------------------- escalate
        if action.kind == ESCALATE_TO_HUMAN:
            outcome.escalations += 1
            outcome.cost_paise += HUMAN_COST_PAISE
            if not truth.recoverable:
                # A person reads the decline, sees it is our own configuration,
                # and files it for the merchant. No money today -- but this is
                # the correct end state for the case, not a failure.
                outcome.correctly_left_alone = True
                outcome.events.append(Event(
                    action.kind, action.wait_days, "not_applicable", 0.0,
                    HUMAN_COST_PAISE,
                    "unrecoverable: routed to the merchant instead of the customer",
                ))
                continue
            # A human is better than a template, and much more expensive.
            p = min(0.85, truth.p_pay_if_contacted * 1.25 * _timing_multiplier(action, truth))
            hit = rng.random() < p
            outcome.events.append(Event(
                action.kind, action.wait_days, "paid" if hit else "no_response",
                p, HUMAN_COST_PAISE, "handled by a person",
            ))
            if hit:
                outcome.paid = True
            continue

        # ------------------------------------------------------------- retry
        if action.kind == SCHEDULE_RETRY:
            outcome.retries += 1
            outcome.cost_paise += RETRY_COST_PAISE
            p = max(0.0, truth.p_pay_if_retried * _timing_multiplier(action, truth) * retry_strength)
            retry_strength *= RETRY_DECAY
            hit = rng.random() < p
            outcome.events.append(Event(
                action.kind, action.wait_days, "paid" if hit else "no_response",
                p, RETRY_COST_PAISE, "silent retry; no customer was contacted",
            ))
            if hit:
                outcome.paid = True
            continue

        # ----------------------------------------------------------- contact
        outcome.contacts += 1
        outcome.cost_paise += CONTACT_COST_PAISE

        if truth.would_pay_unprompted:
            outcome.false_intervention = True
        if not truth.recoverable:
            outcome.wasted_contact = True

        p = _contact_probability(case, truth, action, fatigue)
        fatigue *= FATIGUE
        hit = rng.random() < p

        # Rolled whether or not the message worked, so the random stream does not
        # depend on the outcome -- but a customer who paid is not recorded as
        # lost, because for this case it no longer matters.
        #
        # One roll, not two. An earlier version rolled a second time for
        # self-payers on the grounds that being chased for money you already
        # meant to pay is the most irritating version. That is true, and the
        # cohort already prices it in: `_truth_for` adds 0.06 to `annoyance` for
        # exactly this. Charging it twice in two files is how a model ends up
        # with a penalty nobody can find the source of.
        annoyed = rng.random() < truth.annoyance

        if hit:
            outcome.paid = True
            outcome.events.append(Event(
                action.kind, action.wait_days, "paid", p, CONTACT_COST_PAISE,
            ))
        elif annoyed:
            outcome.opted_out = True
            outcome.events.append(Event(
                action.kind, action.wait_days, "opted_out", p, CONTACT_COST_PAISE,
                "customer opted out of all future recovery contact",
            ))
        else:
            outcome.events.append(Event(
                action.kind, action.wait_days, "no_response", p, CONTACT_COST_PAISE,
            ))

    # ------------------------------------------------- the counterfactual
    # Anyone who was going to pay on their own still pays, whether we acted or
    # not. This single line is what stops every policy in this project -- doing
    # nothing included -- from being scored on money it did not earn.
    if truth.would_pay_unprompted and not outcome.opted_out:
        outcome.paid = True

    outcome.caused = outcome.paid and not truth.would_pay_unprompted

    return outcome
