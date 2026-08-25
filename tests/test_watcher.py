"""The Watcher's job is to be boringly correct about things that are not opinions."""

import datetime as dt
import json

import pytest

from app import cohort, watcher
from app.models import Case

AS_OF = cohort.AS_OF


def make_case(**overrides) -> Case:
    meta = {
        "method": "card",
        "customer": {
            "prior_purchases": 3, "days_since_last_purchase": 40,
            "prior_recovery_contacts_90d": 0, "opted_out": False,
        },
    }
    meta.update(overrides.pop("meta", {}))
    base = dict(
        id="case_test", merchant_id="m", kind="failed_payment",
        customer_ref="cust_1", amount_paise=120_000, currency="INR",
        status="open", failure_reason=None,
        detected_at=AS_OF - dt.timedelta(hours=5), attempts=0,
        recovered_paise=0, meta_json=json.dumps(meta),
    )
    base.update(overrides)
    return Case(**base)


def with_failure(reason: str, source: str, **overrides) -> Case:
    case = make_case(failure_reason=reason, **overrides)
    meta = json.loads(case.meta_json)
    meta["failure"] = {
        "error_code": "BAD_REQUEST_ERROR", "error_description": "d",
        "error_source": source, "error_step": "payment_authorization",
        "error_reason": reason,
    }
    case.meta_json = json.dumps(meta)
    return case


# ------------------------------------------------------- the three-way split


def test_a_merchant_side_decline_is_unrecoverable_and_hard_stopped():
    """The BUGLOG entry that earned this rule: Razorpay told me whose fault it
    was, in a field I was ignoring, and the obvious reading was wrong."""
    signal = watcher.assess(
        with_failure("international_transaction_not_allowed", "business"), as_of=AS_OF
    )
    assert signal.recoverability == "unrecoverable"
    assert signal.hard_stop == "merchant_side_failure"
    assert signal.priority == 0.0, "an unwinnable case must not outrank a winnable one"


@pytest.mark.parametrize("source", ["bank", "gateway"])
def test_a_transient_failure_is_marked_retryable_not_contactable(source):
    signal = watcher.assess(with_failure("issuer_down", source), as_of=AS_OF)
    assert signal.recoverability == "retryable"
    assert any("retry" in fact for fact in signal.facts)


def test_a_customer_side_decline_is_contactable():
    signal = watcher.assess(with_failure("incorrect_otp", "customer"), as_of=AS_OF)
    assert signal.recoverability == "contactable"


def test_a_dead_instrument_is_contactable_and_says_so():
    """Retryable and contactable are not the same thing, and an expired card is
    the case that separates them: the bank is fine, the card is not."""
    signal = watcher.assess(with_failure("card_expired", "customer"), as_of=AS_OF)
    assert signal.recoverability == "contactable"
    assert any("new one" in fact or "fail again" in fact for fact in signal.facts)


# ------------------------------------------------------------------ consent


def test_an_opted_out_customer_is_hard_stopped():
    case = make_case(meta={"customer": {
        "prior_purchases": 1, "prior_recovery_contacts_90d": 0, "opted_out": True,
    }})
    signal = watcher.assess(case, as_of=AS_OF)
    assert signal.opted_out
    assert signal.hard_stop == "customer_opted_out"


def test_a_merchant_side_failure_keeps_its_own_reason_when_also_opted_out():
    """Two hard stops, one field. The more fundamental one wins, so the audit
    trail says "we could never have recovered this" rather than "they said no"."""
    case = with_failure("payment_method_not_enabled", "business")
    meta = json.loads(case.meta_json)
    meta["customer"]["opted_out"] = True
    case.meta_json = json.dumps(meta)
    assert watcher.assess(case, as_of=AS_OF).hard_stop == "merchant_side_failure"


# ------------------------------------------------------------------- timing


def test_the_salary_day_countdown_is_computed_in_ist():
    """Late on the 31st in UTC is already the 1st in India. Getting this wrong
    would tell the planner to wait a month."""
    late = dt.datetime(2026, 8, 31, 20, 0, tzinfo=dt.timezone.utc)   # 01:30 IST, Sep 1
    assert watcher._days_to_next_first(late) == 0

    mid = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)
    assert watcher._days_to_next_first(mid) == 12


def test_an_insufficient_funds_case_is_told_when_payday_is():
    signal = watcher.assess(with_failure("insufficient_funds", "customer"), as_of=AS_OF)
    assert any("salary day" in fact for fact in signal.facts)


# -------------------------------------------------------------------- value


def test_a_high_value_case_demands_a_human():
    signal = watcher.assess(make_case(amount_paise=900_000), as_of=AS_OF)
    assert signal.needs_human


def test_an_ordinary_case_does_not():
    assert not watcher.assess(make_case(amount_paise=90_000), as_of=AS_OF).needs_human


# ------------------------------------------------------------------- batch


def test_scanning_the_real_cohort_sorts_by_priority_and_finds_every_shape():
    rows = cohort.build_cohort()
    signals = watcher.scan([case for case, _ in rows], as_of=AS_OF)

    assert len(signals) == 300
    assert signals == sorted(signals, key=lambda s: s.priority, reverse=True)

    kinds = {s.recoverability for s in signals}
    assert kinds == {"unrecoverable", "retryable", "contactable"}, (
        "the cohort must exercise all three, or the Guard rules that depend on "
        "them are never tested by a real run"
    )

    summary = watcher.summarise(signals)
    assert summary["hard_stopped"] > 0
    assert summary["needs_human"] > 0
