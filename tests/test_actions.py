"""The vocabulary is closed, and these tests are what makes that a fact.

Everything here is one question asked several ways: *can anything outside the six
actions get through the parser?* The planner is a language model, so the answer
has to be no for inputs nobody thought to write down, not just for the obvious
ones.
"""

import datetime as dt

import pytest

from app.actions import (
    ACTION_KINDS,
    Action,
    MAX_PLAN_LENGTH,
    MAX_WAIT_DAYS,
    UnknownAction,
    parse_plan,
)


def test_there_are_exactly_six_actions():
    """If this number changes, the video, the README and the Guard change too."""
    assert len(ACTION_KINDS) == 6
    assert set(ACTION_KINDS) == {
        "send_payment_link", "schedule_retry", "send_reminder",
        "offer_installments", "escalate_to_human", "do_nothing",
    }


@pytest.mark.parametrize("invented", [
    "issue_refund",           # plausible, catastrophic
    "call_customer",          # plausible, does not exist
    "send_payment_links",     # a typo away from real
    "",
    "send payment link",      # spaces instead of underscores
])
def test_an_action_outside_the_vocabulary_is_refused(invented):
    with pytest.raises(UnknownAction):
        Action.parse({"action": invented})


def test_case_and_whitespace_are_normalised_rather_than_refused():
    """Deliberately forgiving in exactly one place, and no further.

    Models emit "SEND_PAYMENT_LINK" and " send_reminder" constantly. Refusing
    those would escalate cases to humans over a capital letter, which trains
    everyone to distrust the parser. Normalising the *shape* of a name is safe;
    guessing at an unknown name is not, and is refused above.
    """
    assert Action.parse({"action": "SEND_PAYMENT_LINK "}).kind == "send_payment_link"


def test_a_real_action_with_an_absurd_delay_is_refused():
    """`send_payment_link in 400 days` is inside the vocabulary and still wrong."""
    with pytest.raises(UnknownAction):
        Action.parse({"action": "send_reminder", "wait_days": MAX_WAIT_DAYS + 1})


def test_nonsense_shapes_are_refused_rather_than_coerced():
    for payload in [
        {"action": "send_reminder", "wait_days": "tomorrow"},
        {"action": "send_reminder", "hour_ist": 25},
        {"action": "send_reminder", "wait_days": True},   # bool is not an int here
        {"reason": "no action field at all"},
        ["send_reminder"],
        42,
    ]:
        with pytest.raises(UnknownAction):
            Action.parse(payload)


def test_a_plan_longer_than_the_stopping_rule_is_refused_whole():
    plan = [{"action": "send_reminder", "wait_days": i} for i in range(MAX_PLAN_LENGTH + 1)]
    with pytest.raises(UnknownAction):
        parse_plan(plan)


def test_a_float_that_is_really_an_integer_is_accepted():
    """Models emit 2.0 for 2 constantly. Refusing that is pedantry, not safety."""
    assert Action.parse({"action": "schedule_retry", "wait_days": 2.0}).wait_days == 2


def test_model_written_text_is_made_safe_before_it_reaches_a_log():
    action = Action.parse({
        "action": "send_reminder",
        "reason": "Chase ₹1,734 — the customer’s card",
    })
    action.reason.encode("ascii")     # would raise if anything slipped through
    assert "Rs " in action.reason


def test_the_hour_is_chosen_in_ist_and_stored_in_utc():
    """Quiet hours are a rule about the customer's evening, not about UTC."""
    as_of = dt.datetime(2026, 8, 31, 4, 0, tzinfo=dt.timezone.utc)   # 09:30 IST
    when = Action("send_reminder", wait_days=1, hour_ist=10).scheduled_at(as_of)
    assert when == dt.datetime(2026, 9, 1, 4, 30, tzinfo=dt.timezone.utc)


def test_do_nothing_still_has_to_say_why():
    from app.actions import nothing
    assert nothing("customer opted out").reason
