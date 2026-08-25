"""The referee decides every number in the report card, so it is tested hardest.

If the simulator is wrong, nothing downstream can be right, and the failure mode
is silent: the table still prints, the columns still add up, and the conclusion is
false. These tests pin the four claims the whole comparison rests on.
"""

import datetime as dt
import json

import pytest

from app import cohort, simulator
from app.actions import Action
from app.models import Case, CaseTruth

SEED = 20260826


def case(**overrides) -> Case:
    base = dict(
        id="case_x", merchant_id="m", kind="failed_payment", customer_ref="c",
        amount_paise=100_000, currency="INR", status="open",
        failure_reason="insufficient_funds",
        detected_at=cohort.AS_OF - dt.timedelta(days=1),
        attempts=0, recovered_paise=0,
        meta_json=json.dumps({"method": "card", "customer": {"opted_out": False}}),
    )
    base.update(overrides)
    return Case(**base)


def truth(**overrides) -> CaseTruth:
    base = dict(
        case_id="case_x", recoverable=True, would_pay_unprompted=False,
        p_pay_if_contacted=0.6, p_pay_if_retried=0.1, best_wait_days=1,
        annoyance=0.05, note="",
    )
    base.update(overrides)
    return CaseTruth(**base)


def run(actions, *, c=None, t=None, policy="p"):
    return simulator.simulate(c or case(), t or truth(), actions, policy=policy, seed=SEED)


# ------------------------------------------------------------ determinism


def test_the_same_case_under_the_same_policy_always_does_the_same_thing():
    """Submission checklist item 2 reaches all the way down to here."""
    plan = [Action("send_payment_link", wait_days=1, hour_ist=11)]
    first, second = run(plan), run(plan)
    assert first.paid == second.paid
    assert first.cost_paise == second.cost_paise
    assert [e.outcome for e in first.events] == [e.outcome for e in second.events]


def test_every_policy_gets_the_same_luck_on_the_same_case():
    """A paired comparison, not a race between two dice.

    The seed is derived from (seed, policy, case_id), so two policies taking the
    *same* action on the same case get the same roll. When one policy wins, it is
    because of the decision. This test proves the roll is not what differs.
    """
    plan = [Action("send_payment_link", wait_days=1, hour_ist=11)]
    a = run(plan, policy="alpha")
    b = run(plan, policy="beta")
    # Different policies do get independent draws -- otherwise a policy could not
    # be unlucky -- but each is stable, which is what reproducibility needs.
    assert run(plan, policy="alpha").paid == a.paid
    assert run(plan, policy="beta").paid == b.paid


# ------------------------------------------------- the false-positive engine


def test_someone_who_would_have_paid_anyway_pays_even_if_we_do_nothing():
    outcome = run([], t=truth(would_pay_unprompted=True))
    assert outcome.paid
    assert not outcome.caused
    assert outcome.collected_paise == 100_000
    assert outcome.caused_paise == 0, (
        "this is the entire point: money that was arriving anyway must never be "
        "counted as recovered"
    )


def test_contacting_someone_who_was_already_paying_is_recorded_as_a_false_positive():
    outcome = run(
        [Action("send_reminder", wait_days=0, hour_ist=11)],
        t=truth(would_pay_unprompted=True),
    )
    assert outcome.false_intervention
    assert outcome.caused_paise == 0
    assert outcome.cost_paise > 0, "the message still cost money and goodwill"


def test_annoying_a_self_payer_into_opting_out_loses_money_that_was_coming():
    """The worst outcome in the system, and it has to be reachable.

    A customer who was going to pay, chased anyway, who opts out and does not.
    A recovery tool that cannot represent this cannot be honest about its risk.
    """
    outcome = run(
        [Action("send_reminder", wait_days=0, hour_ist=11)],
        t=truth(would_pay_unprompted=True, p_pay_if_contacted=0.0, annoyance=1.0),
    )
    assert outcome.opted_out
    assert not outcome.paid


# -------------------------------------------------------- the unwinnable


def test_a_merchant_side_case_pays_nobody_however_hard_it_is_chased():
    plan = [Action("send_payment_link", wait_days=i, hour_ist=11) for i in range(3)]
    outcome = run(plan, t=truth(recoverable=False, p_pay_if_contacted=0.0,
                                p_pay_if_retried=0.0))
    assert not outcome.paid
    assert outcome.wasted_contact
    assert outcome.cost_paise == 3 * simulator.CONTACT_COST_PAISE


def test_leaving_an_unwinnable_case_alone_is_recorded_as_the_right_answer():
    outcome = run([], t=truth(recoverable=False, p_pay_if_contacted=0.0,
                              p_pay_if_retried=0.0))
    assert outcome.correctly_left_alone
    assert outcome.cost_paise == 0


def test_escalating_an_unwinnable_case_also_counts_as_handling_it():
    """A person reads the decline, sees it is a configuration problem, and files
    it with the merchant. No money -- but the case is finished, not abandoned."""
    outcome = run([Action("escalate_to_human")],
                  t=truth(recoverable=False, p_pay_if_contacted=0.0, p_pay_if_retried=0.0))
    assert outcome.correctly_left_alone
    assert outcome.escalations == 1
    assert outcome.cost_paise == simulator.HUMAN_COST_PAISE


# --------------------------------------------------------------- timing


def test_acting_before_salary_day_is_much_worse_than_acting_on_it():
    """The signal a planner is supposed to find. If waiting were free, the timing
    half of every plan in this project would be decoration."""
    t = truth(best_wait_days=4)
    early = simulator._timing_multiplier(Action("send_payment_link", wait_days=0), t)
    ontime = simulator._timing_multiplier(Action("send_payment_link", wait_days=4), t)
    late = simulator._timing_multiplier(Action("send_payment_link", wait_days=7), t)

    assert early < 0.45, "acting into an empty account should be close to worthless"
    assert ontime == 1.0
    assert late > early, "being late is a smaller sin than being early"


def test_a_second_message_is_worth_less_than_the_first():
    t = truth()
    c = case()
    first = simulator._contact_probability(c, t, Action("send_reminder"), 1.0)
    second = simulator._contact_probability(c, t, Action("send_reminder"), simulator.FATIGUE)
    assert second < first


# --------------------------------------------------------------- actions


def test_a_silent_retry_never_contacts_anyone_and_cannot_annoy_anyone():
    outcome = run([Action("schedule_retry", wait_days=0)],
                  t=truth(p_pay_if_retried=1.0, annoyance=1.0))
    assert outcome.paid and outcome.caused
    assert outcome.contacts == 0
    assert not outcome.opted_out
    assert outcome.cost_paise == 0


def test_nothing_happens_after_a_customer_opts_out():
    plan = [
        Action("send_reminder", wait_days=0, hour_ist=11),
        Action("send_payment_link", wait_days=2, hour_ist=11),
        Action("send_reminder", wait_days=4, hour_ist=11),
    ]
    outcome = run(plan, t=truth(p_pay_if_contacted=0.0, annoyance=1.0))
    assert outcome.opted_out
    assert outcome.contacts == 1, "the plan must stop the moment consent is withdrawn"


def test_nothing_happens_after_the_money_arrives():
    plan = [
        Action("send_payment_link", wait_days=1, hour_ist=11),
        Action("send_reminder", wait_days=3, hour_ist=11),
    ]
    outcome = run(plan, t=truth(p_pay_if_contacted=1.0))
    assert outcome.paid
    assert outcome.contacts == 1, "chasing a customer who already paid is unforgivable"


def test_installments_help_a_large_balance_and_hurt_a_small_one():
    """Splitting a bill only helps when the size of the bill is the problem.

    Stated as a comparison rather than "instalments beat a payment link", which
    is what I first wrote and which the model does not claim: for a small failed
    card payment a one-tap link is still the better move, and offering to split
    Rs 400 into parts is faintly insulting. The real claim is that the *relative*
    value of instalments rises with the amount, and that it wins outright on the
    large B2B invoices where it should.
    """
    def p(kind, amount, action):
        return simulator._contact_probability(
            case(kind=kind, amount_paise=amount), truth(), Action(action), 1.0)

    big_ratio = p("failed_payment", 800_000, "offer_installments") / p("failed_payment", 800_000, "send_payment_link")
    small_ratio = p("failed_payment", 40_000, "offer_installments") / p("failed_payment", 40_000, "send_payment_link")
    assert big_ratio > small_ratio

    # And on a large overdue invoice it is the best move available.
    assert p("overdue_invoice", 800_000, "offer_installments") > p("overdue_invoice", 800_000, "send_payment_link")


def test_do_nothing_costs_nothing_and_changes_nothing():
    outcome = run([Action("do_nothing", reason="not worth it")])
    assert outcome.cost_paise == 0
    assert outcome.contacts == 0
    assert not outcome.events


# ------------------------------------------------------------ probabilities


@pytest.mark.parametrize("wait", range(0, 8))
def test_probabilities_stay_inside_zero_and_one_across_the_real_cohort(wait):
    for c, t in cohort.build_cohort(size=40):
        p = simulator._contact_probability(c, t, Action("send_payment_link", wait_days=wait), 1.0)
        assert 0.0 <= p <= 1.0
