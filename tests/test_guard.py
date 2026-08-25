"""One test per Guard rule, plus a test that checks there is one test per rule.

Submission checklist item 3 says "every Guard rule has a test proving it blocks a
bad action". `test_every_rule_in_the_guard_has_a_test` makes that mechanical: add
a rule to `guard.RULES` without adding a test named after it and the suite fails.
A checklist item you have to remember to honour is a checklist item you will
eventually stop honouring.
"""

import datetime as dt

import pytest

from app import guard
from app.actions import Action
from app.watcher import Signal

AS_OF = dt.datetime(2026, 8, 31, 4, 0, tzinfo=dt.timezone.utc)   # 09:30 IST


def make_signal(**overrides) -> Signal:
    base = dict(
        case_id="case_0001", kind="failed_payment", amount_paise=120_000,
        customer_ref="cust_abc", recoverability="contactable",
        risk_band="medium", priority=1.0,
    )
    base.update(overrides)
    return Signal(**base)


@pytest.fixture
def state():
    return guard.GuardState()


# --------------------------------------------------------------- the rules


def test_customer_opted_out(state):
    signal = make_signal(opted_out=True)
    verdict = guard.check(signal, Action("send_reminder", hour_ist=11), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "customer_opted_out"


def test_customer_opted_out_still_permits_a_silent_retry(state):
    """Opting out is consent to be *contacted*, not consent to be charged.

    A silent retry on a saved instrument the customer already authorised puts no
    message in front of anyone. Blocking it would be over-reading the rule, and
    over-blocking is how a guardrail layer gets switched off in production.
    """
    signal = make_signal(opted_out=True)
    assert guard.check(signal, Action("schedule_retry"), state, as_of=AS_OF).allowed


def test_merchant_side_failure(state):
    signal = make_signal(recoverability="unrecoverable", error_source="business",
                         failure_reason="international_transaction_not_allowed")
    verdict = guard.check(signal, Action("send_payment_link", hour_ist=11), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "merchant_side_failure"
    assert verdict.escalate, "an unfixable case must reach a person, not vanish"


def test_dead_instrument_retry(state):
    signal = make_signal(failure_reason="card_expired")
    verdict = guard.check(signal, Action("schedule_retry"), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "dead_instrument_retry"


@pytest.mark.parametrize("hour", [0, 3, 8, 21, 22, 23])
def test_quiet_hours(state, hour):
    signal = make_signal()
    verdict = guard.check(signal, Action("send_reminder", hour_ist=hour), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "quiet_hours"


@pytest.mark.parametrize("hour", [9, 12, 20])
def test_quiet_hours_allows_daytime(state, hour):
    assert guard.check(
        make_signal(), Action("send_reminder", hour_ist=hour), state, as_of=AS_OF
    ).allowed


def test_weekly_contact_cap(state):
    signal = make_signal()
    for day in range(guard.MAX_CONTACTS_PER_CUSTOMER_PER_WEEK):
        action = Action("send_reminder", wait_days=day, hour_ist=11)
        assert guard.check(signal, action, state, as_of=AS_OF).allowed
        state.commit(signal, action, action.scheduled_at(AS_OF))

    verdict = guard.check(
        signal, Action("send_payment_link", wait_days=4, hour_ist=11), state, as_of=AS_OF
    )
    assert not verdict.allowed
    assert verdict.rule == "weekly_contact_cap"


def test_weekly_cap_counts_contacts_made_before_this_run(state):
    """Someone chased twice last month starts with less room, not a clean slate."""
    signal = make_signal(prior_contacts_90d=2)
    action = Action("send_reminder", wait_days=0, hour_ist=11)
    assert guard.check(signal, action, state, as_of=AS_OF).allowed
    state.commit(signal, action, action.scheduled_at(AS_OF))

    verdict = guard.check(
        signal, Action("send_payment_link", wait_days=2, hour_ist=11), state, as_of=AS_OF
    )
    assert not verdict.allowed
    assert verdict.rule == "weekly_contact_cap"


def test_case_cooldown(state):
    signal = make_signal()
    first = Action("send_reminder", wait_days=1, hour_ist=11)
    state.commit(signal, first, first.scheduled_at(AS_OF))

    verdict = guard.check(
        signal, Action("send_payment_link", wait_days=1, hour_ist=15), state, as_of=AS_OF
    )
    assert not verdict.allowed
    assert verdict.rule == "case_cooldown"


def test_stopping_rule(state):
    signal = make_signal()
    for day in range(guard.MAX_ATTEMPTS_PER_CASE):
        state.commit(signal, Action("schedule_retry", wait_days=day), AS_OF + dt.timedelta(days=day))

    verdict = guard.check(signal, Action("schedule_retry", wait_days=9), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "stopping_rule"
    assert signal.case_id in state.stopped_cases


def test_high_value_needs_human(state):
    signal = make_signal(amount_paise=900_000, needs_human=True)
    verdict = guard.check(signal, Action("send_payment_link", hour_ist=11), state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "high_value_needs_human"
    assert verdict.escalate


def test_high_value_still_permits_the_escalation_itself(state):
    signal = make_signal(amount_paise=900_000, needs_human=True)
    assert guard.check(signal, Action("escalate_to_human"), state, as_of=AS_OF).allowed


def test_duplicate_action(state):
    signal = make_signal()
    action = Action("send_payment_link", wait_days=1, hour_ist=11)
    state.commit(signal, action, action.scheduled_at(AS_OF))

    verdict = guard.check(signal, action, state, as_of=AS_OF)
    assert not verdict.allowed
    assert verdict.rule == "duplicate_action"


def test_duplicate_is_about_replay_not_repetition(state):
    """The same instruction twice is a duplicate. The same *kind* on another day
    is a follow-up, and blocking those is what the cooldown and stopping rule are
    for. Getting this backwards silently crippled the baselines -- see BUGLOG."""
    signal = make_signal()
    first = Action("schedule_retry", wait_days=0)
    state.commit(signal, first, first.scheduled_at(AS_OF))

    later = Action("schedule_retry", wait_days=3)
    assert guard.check(signal, later, state, as_of=AS_OF).allowed


def test_plan_too_long():
    """Enforced at the parser rather than the Guard, so the Guard never sees one."""
    from app.actions import UnknownAction, parse_plan
    with pytest.raises(UnknownAction):
        parse_plan([{"action": "send_reminder", "wait_days": i} for i in range(9)])


# ------------------------------------------------------------- meta-tests


def test_every_rule_in_the_guard_has_a_test():
    import tests.test_guard as me

    tested = {
        name[len("test_"):] for name in dir(me) if name.startswith("test_")
    }
    for rule in guard.RULES:
        assert any(name.startswith(rule) or name == rule for name in tested), (
            f"guard rule {rule!r} has no test. Every rule that can refuse a money "
            "action needs a test proving it refuses one -- submission checklist item 3."
        )


def test_do_nothing_can_never_be_blocked(state):
    """A Guard able to refuse inaction could force the system to act."""
    signal = make_signal(opted_out=True, recoverability="unrecoverable", needs_human=True)
    assert guard.check(signal, Action("do_nothing", reason="x"), state, as_of=AS_OF).allowed


def test_a_blocked_action_does_not_consume_the_customers_quota(state):
    """Refusals must not spend the allowance of the person they protected."""
    signal = make_signal()
    blocked = Action("send_reminder", hour_ist=3)          # quiet hours
    assert not guard.check(signal, blocked, state, as_of=AS_OF).allowed
    assert state.contacts_in_week(signal, AS_OF) == 0
    assert state.attempts_per_case[signal.case_id] == 0


def test_consent_is_checked_before_anything_else(state):
    """When several rules apply, the audit trail must cite the real reason."""
    signal = make_signal(opted_out=True, needs_human=True, amount_paise=900_000)
    verdict = guard.check(signal, Action("send_reminder", hour_ist=3), state, as_of=AS_OF)
    assert verdict.rule == "customer_opted_out"


def test_the_stopping_rule_counts_a_cases_whole_life_not_one_plan(state):
    """A case that arrives having already been chased four times is closed.

    This is the only way the stopping rule can ever fire. A single plan is capped
    at four actions by the parser, so if the rule only counted the current plan
    it could never trigger -- which is exactly what it did until the report card
    showed `stopped: 0` in every row. A rule that cannot fire is not defence in
    depth, it is decoration.
    """
    signal = make_signal(attempts_so_far=guard.MAX_ATTEMPTS_PER_CASE)
    state.attempts_per_case[signal.case_id] = signal.attempts_so_far

    verdict = guard.check(
        signal, Action("send_reminder", wait_days=1, hour_ist=11), state, as_of=AS_OF
    )
    assert not verdict.allowed
    assert verdict.rule == "stopping_rule"


def test_a_case_chased_three_times_before_gets_exactly_one_more(state):
    signal = make_signal(attempts_so_far=guard.MAX_ATTEMPTS_PER_CASE - 1)
    state.attempts_per_case[signal.case_id] = signal.attempts_so_far

    last = Action("send_reminder", wait_days=1, hour_ist=11)
    assert guard.check(signal, last, state, as_of=AS_OF).allowed
    state.commit(signal, last, last.scheduled_at(AS_OF))

    after = guard.check(
        signal, Action("send_payment_link", wait_days=3, hour_ist=11), state, as_of=AS_OF
    )
    assert not after.allowed and after.rule == "stopping_rule"
