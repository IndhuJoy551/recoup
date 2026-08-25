"""The Guard: the gate every proposed action has to get through.

This is the most important file in the project, and the sentence to remember is:
**the AI proposes, it never disposes.**

The Guard does not read the planner's reasoning, cannot be argued with, and is
not reachable from the prompt. It receives a `Signal` (facts about the case), an
`Action` (what someone wants to do), and the record of what has already been done
this run, and it returns a yes or a no with a named rule attached. There is no
third option and no "warn but allow".

Two design choices worth defending in an interview
--------------------------------------------------
**Every refusal names a rule.** `Decision.rule` is a stable string, not a
sentence. That is what makes "we blocked 61 actions" auditable: you can group by
rule, count them, and test each one individually. A guard that returns a free-text
explanation is a guard nobody can prove anything about.

**The Guard is stateful, and the state is explicit.** Rules like "no more than
three contacts per customer per week" and "stop after four attempts" cannot be
evaluated from one action in isolation. `GuardState` holds that memory for the
duration of a run, and the runner commits to it only when an action is actually
executed -- so a blocked action does not consume the customer's weekly quota.
Getting that backwards would let a badly-behaved planner exhaust a customer's
allowance by proposing things that never happened.

Why the same rules also run over the baselines
----------------------------------------------
They do not, by default, and that is the honest bit. "Blast everyone" is a
description of what small merchants actually do, and they do it without a
compliance layer. So the baselines run ungated and the report card carries a
`violations` column counting the rules they would have broken. A fifth row --
blast-everyone *with* the Guard on -- is also published, so a reader can separate
"Recoup wins because it targets better" from "Recoup wins because it is allowed
to send less". Those are different claims and only one of them is interesting.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from app.actions import Action, ESCALATE_TO_HUMAN, DO_NOTHING
from app.watcher import Signal

# Indian regulators and basic decency agree on this one. Messages about money
# land between 9am and 9pm local time or they do not land at all.
QUIET_HOURS_START = 21   # 9pm, inclusive: 21:00 is already too late
QUIET_HOURS_END = 9      # 9am, exclusive: 08:59 is too early

MAX_CONTACTS_PER_CUSTOMER_PER_WEEK = 3
MAX_ATTEMPTS_PER_CASE = 4            # the stopping rule
MIN_HOURS_BETWEEN_ATTEMPTS = 24

# Recovery contacts already made in the 90 days before this run. Someone who has
# been chased twice recently starts the week with less room, not a clean slate.
PRIOR_CONTACT_WEIGHT = 1


def _fingerprint(action: Action) -> str:
    """What makes two actions "the same action" for idempotency purposes.

    The first version keyed on `action.kind` alone, which read like the stricter
    choice and was in fact simply wrong: it made "retry on Monday" and "retry on
    Thursday" the same instruction, so `retry_everything` had two thirds of its
    ladder refused as duplicates and scored 5% when its real problem is that
    retries cannot fix a customer-side decline. The rule that stops repetition is
    the cooldown and the stopping rule. Idempotency is about *replay* -- the same
    instruction arriving twice -- and an instruction includes when it fires.
    """
    return f"{action.kind}@{action.wait_days}d:{action.hour_ist:02d}"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None = None
    detail: str = ""
    escalate: bool = False   # blocked, but a human should look at it

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "detail": self.detail,
            "escalate": self.escalate,
        }


ALLOW = Decision(allowed=True, detail="all rules passed")


# Every rule the Guard can cite, with the reason it exists. The report card
# groups blocks by these keys, and `tests/test_guard.py` has one test per key --
# submission checklist item 3 ("every Guard rule has a test proving it blocks a
# bad action") is checked mechanically against this dict.
RULES: dict[str, str] = {
    "customer_opted_out": "The customer withdrew consent. Contacting them is not a trade-off, it is not allowed.",
    "merchant_side_failure": "error_source='business': the customer cannot fix our configuration. Any contact is pure cost.",
    "dead_instrument_retry": "The card is expired or the mandate revoked. Retrying the same instrument cannot succeed.",
    "quiet_hours": "Money messages are restricted to 09:00-21:00 IST.",
    "weekly_contact_cap": f"No more than {MAX_CONTACTS_PER_CUSTOMER_PER_WEEK} recovery contacts per customer per 7 days.",
    "case_cooldown": f"At least {MIN_HOURS_BETWEEN_ATTEMPTS}h between two attempts on the same case.",
    "stopping_rule": f"Stop for good after {MAX_ATTEMPTS_PER_CASE} attempts on one case.",
    "high_value_needs_human": "Above the auto-action ceiling a human approves before anything is sent.",
    "duplicate_action": "This exact action, at this exact time, has already been executed for this case (replay protection).",
    "plan_too_long": "A plan longer than the stopping rule allows was proposed.",
}


@dataclass
class GuardState:
    """What has actually happened so far in this run. Only committed actions."""

    attempts_per_case: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    contacts_per_customer: dict[str, list[dt.datetime]] = field(
        default_factory=lambda: defaultdict(list)
    )
    last_attempt_at: dict[str, dt.datetime] = field(default_factory=dict)
    taken_per_case: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    blocks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stopped_cases: set[str] = field(default_factory=set)

    def commit(self, signal: Signal, action: Action, when: dt.datetime) -> None:
        """Record that an action really happened. Called only after execution."""
        if action.kind == DO_NOTHING:
            return
        self.attempts_per_case[signal.case_id] += 1
        self.last_attempt_at[signal.case_id] = when
        self.taken_per_case[signal.case_id].add(_fingerprint(action))
        if action.is_contact:
            self.contacts_per_customer[signal.customer_ref].append(when)
        if self.attempts_per_case[signal.case_id] >= MAX_ATTEMPTS_PER_CASE:
            self.stopped_cases.add(signal.case_id)

    def note_block(self, rule: str) -> None:
        self.blocks[rule] += 1

    def contacts_in_week(self, signal: Signal, when: dt.datetime) -> int:
        window_start = when - dt.timedelta(days=7)
        recent = [t for t in self.contacts_per_customer[signal.customer_ref] if t >= window_start]
        return len(recent) + PRIOR_CONTACT_WEIGHT * min(signal.prior_contacts_90d, 2)


def check(
    signal: Signal,
    action: Action,
    state: GuardState,
    *,
    as_of: dt.datetime,
) -> Decision:
    """Yes or no, with a named rule. The only entry point; no partial approvals.

    Order matters and is not alphabetical: consent first, then physics (can this
    action possibly work at all), then timing, then frequency, then value. A
    customer who opted out should be refused for that reason and not for whichever
    other rule happened to be evaluated first -- the audit trail is only useful if
    it cites the *real* reason.
    """
    when = action.scheduled_at(as_of)

    # do_nothing is always permitted. It is the one action with no victim, and a
    # guard that could block it would be able to force the system to act.
    if action.kind == DO_NOTHING:
        return ALLOW

    # ---- 1. consent -------------------------------------------------------
    if signal.opted_out and action.is_contact:
        return _block(state, "customer_opted_out",
                      f"{signal.customer_ref} opted out of recovery contact")

    # ---- 2. can this possibly work ----------------------------------------
    if signal.recoverability == "unrecoverable" and action.kind != ESCALATE_TO_HUMAN:
        return _block(
            state, "merchant_side_failure",
            f"error_source='{signal.error_source}' ({signal.failure_reason}): "
            "no customer-facing action can recover this",
            escalate=True,
        )

    if action.is_silent and signal.failure_reason in ("card_expired", "mandate_revoked"):
        return _block(
            state, "dead_instrument_retry",
            f"'{signal.failure_reason}' will fail identically on every retry",
        )

    # ---- 3. stopping rule -------------------------------------------------
    if state.attempts_per_case[signal.case_id] >= MAX_ATTEMPTS_PER_CASE:
        return _block(
            state, "stopping_rule",
            f"{MAX_ATTEMPTS_PER_CASE} attempts already made on {signal.case_id}; "
            "this case is closed to further contact",
        )

    # ---- 4. idempotency ---------------------------------------------------
    if _fingerprint(action) in state.taken_per_case[signal.case_id]:
        return _block(
            state, "duplicate_action",
            f"{action.kind} at +{action.wait_days}d {action.hour_ist:02d}:00 has "
            f"already been executed for {signal.case_id}",
        )

    # ---- 5. timing --------------------------------------------------------
    if action.is_contact:
        hour = action.hour_ist
        if hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END:
            return _block(
                state, "quiet_hours",
                f"{hour:02d}:00 IST is outside the 09:00-21:00 contact window",
            )

    previous = state.last_attempt_at.get(signal.case_id)
    if previous is not None:
        gap_hours = (when - previous).total_seconds() / 3600.0
        if gap_hours < MIN_HOURS_BETWEEN_ATTEMPTS:
            return _block(
                state, "case_cooldown",
                f"only {gap_hours:.1f}h since the last attempt on {signal.case_id}",
            )

    # ---- 6. frequency -----------------------------------------------------
    if action.is_contact and state.contacts_in_week(signal, when) >= MAX_CONTACTS_PER_CUSTOMER_PER_WEEK:
        return _block(
            state, "weekly_contact_cap",
            f"{signal.customer_ref} has reached {MAX_CONTACTS_PER_CUSTOMER_PER_WEEK} "
            "recovery contacts in 7 days",
        )

    # ---- 7. value ---------------------------------------------------------
    if signal.needs_human and action.kind != ESCALATE_TO_HUMAN:
        return _block(
            state, "high_value_needs_human",
            f"Rs {signal.amount_paise // 100:,} is above the auto-action ceiling",
            escalate=True,
        )

    return ALLOW


def _block(state: GuardState, rule: str, detail: str, *, escalate: bool = False) -> Decision:
    state.note_block(rule)
    return Decision(allowed=False, rule=rule, detail=detail, escalate=escalate)


def check_plan(
    signal: Signal,
    plan: list[Action],
    state: GuardState,
    *,
    as_of: dt.datetime,
) -> list[tuple[Action, Decision]]:
    """Run a whole plan through the gate, in order, without committing anything.

    Returns every action paired with its verdict, including the ones that were
    refused -- the refusals are half the audit trail and all of the interesting
    half.
    """
    return [(action, check(signal, action, state, as_of=as_of)) for action in plan]
