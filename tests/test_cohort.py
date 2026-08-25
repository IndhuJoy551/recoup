"""The cohort is the measuring instrument, so it gets tested like one.

If the cohort quietly changes, every published number changes with it and nothing
complains. These tests are the thing that complains.
"""

import json

import pytest

from app import cohort
from app.models import Case, CaseTruth

# Pinned on 2026-08-26 from seed 20260826. If this changes, the generator changed,
# and every number in the report card moved with it. That is allowed -- but it has
# to be a decision, not a surprise, so update this line deliberately.
EXPECTED_FINGERPRINT = "0d07e645a56905b580667ed083ad6833d6e079ca0dec3806a39e7cc8706f17a0"


@pytest.fixture(scope="module")
def rows():
    return cohort.build_cohort()


# ------------------------------------------------------------ reproducibility


def test_the_same_seed_gives_the_same_cohort(rows):
    """Submission checklist item 2: re-run and get exactly the same numbers."""
    assert cohort.fingerprint(cohort.build_cohort()) == cohort.fingerprint(rows)


def test_a_different_seed_gives_a_different_cohort(rows):
    """Otherwise the seed is decoration and 'reproducible' means nothing."""
    assert cohort.fingerprint(cohort.build_cohort(seed=99)) != cohort.fingerprint(rows)


def test_the_cohort_has_not_drifted(rows):
    assert cohort.fingerprint(rows) == EXPECTED_FINGERPRINT, (
        "the generator changed. Every number in the report card just moved. "
        "If that was deliberate, update EXPECTED_FINGERPRINT in this test."
    )


def test_generation_never_reads_the_wall_clock():
    """`datetime.now()` in a generator makes the day-of-month effects depend on
    when you happened to run it, which quietly breaks reproducibility months
    later, in a way no test would catch."""
    source = (cohort.__file__.replace(".pyc", ".py"))
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    body = text.split('"""', 2)[-1]          # skip the module docstring
    assert "datetime.now" not in body
    assert "dt.datetime.now" not in body
    assert "utcnow()" not in body


# ------------------------------------------------------------------- shape


def test_the_cohort_is_three_hundred_cases(rows):
    assert len(rows) == 300
    assert len({case.id for case, _ in rows}) == 300, "case ids must be unique"


def test_every_case_has_exactly_one_truth_row(rows):
    assert {c.id for c, _ in rows} == {t.case_id for _, t in rows}


def test_money_is_always_whole_rupees_in_paise(rows):
    """Integer paise, never floats, and never fractions of a rupee -- an amount
    like 12345 paise would print as Rs 123.45 and is not a thing a cart produces."""
    for case, _ in rows:
        assert isinstance(case.amount_paise, int)
        assert case.amount_paise % 100 == 0
        assert case.amount_paise > 0


def test_the_four_buckets_are_all_present_and_realistically_sized(rows):
    counts = {}
    for case, _ in rows:
        counts[case.kind] = counts.get(case.kind, 0) + 1
    assert set(counts) == {
        "abandoned_checkout", "failed_payment", "failed_mandate", "overdue_invoice",
    }
    # Invoices are few and large, checkouts many and small. A flat distribution
    # would make every policy look equally good at picking what to chase.
    assert counts["abandoned_checkout"] > counts["overdue_invoice"] * 3

    def mean(kind):
        amounts = [c.amount_paise for c, _ in rows if c.kind == kind]
        return sum(amounts) / len(amounts)

    assert mean("overdue_invoice") > mean("abandoned_checkout") * 4


def test_every_declined_case_carries_a_razorpay_shaped_failure(rows):
    """Same five fields Razorpay actually sends. Verified against the live
    payloads captured on 2026-08-24 -- see BUGLOG."""
    expected = {
        "error_code", "error_description", "error_source", "error_step", "error_reason",
    }
    for case, _ in rows:
        meta = json.loads(case.meta_json)
        if case.kind in ("failed_payment", "failed_mandate"):
            assert set(meta["failure"]) == expected
            assert meta["failure"]["error_source"] in (
                "customer", "bank", "gateway", "business",
            )
            assert case.failure_reason == meta["failure"]["error_reason"]
        else:
            assert "failure" not in meta
            assert case.failure_reason is None


# --------------------------------------------------- the hidden half is honest


def test_business_source_declines_are_unrecoverable_by_anything(rows):
    """The whole reason `error_source` is in this project. A customer cannot fix
    our dashboard setting, so contact and retry both have probability zero, and
    any policy that chases one has spent a message to buy nothing."""
    seen = 0
    for case, truth in rows:
        meta = json.loads(case.meta_json)
        if meta.get("failure", {}).get("error_source") == "business":
            seen += 1
            assert truth.recoverable is False
            assert truth.p_pay_if_contacted == 0.0
            assert truth.p_pay_if_retried == 0.0
            assert truth.would_pay_unprompted is False
    assert seen > 0, "a cohort with no unwinnable cases cannot measure a false positive"


def test_bank_and_gateway_failures_respond_to_retrying_not_to_messaging(rows):
    """The distinction that makes 'blast everyone' and 'retry everything' fail in
    different ways. If both policies failed identically the comparison would say
    nothing."""
    blips = [
        (c, t) for c, t in rows
        if json.loads(c.meta_json).get("failure", {}).get("error_source")
        in ("bank", "gateway")
    ]
    assert blips, "expected some bank/gateway declines"
    for _, truth in blips:
        assert truth.p_pay_if_retried > truth.p_pay_if_contacted


def test_some_cases_would_have_paid_without_us(rows):
    """The false-positive engine. Without these, 'blast everyone' has no downside
    and every honest metric we publish is one-sided."""
    self_payers = [t for _, t in rows if t.would_pay_unprompted]
    assert 30 <= len(self_payers) <= 120, len(self_payers)


def test_insufficient_funds_late_in_the_month_is_worth_waiting_for(rows):
    """Salaries land on the 1st. This is the signal a planner is supposed to find,
    and rules-only policies can find it too -- that is fair, and it is the point."""
    waits = [t.best_wait_days for c, t in rows if c.failure_reason == "insufficient_funds"]
    assert waits, "expected insufficient-funds cases"
    assert max(waits) > 1, "no case ever benefits from waiting; the timing signal is missing"


def test_probabilities_stay_inside_zero_and_one(rows):
    for _, truth in rows:
        assert 0.0 <= truth.p_pay_if_contacted <= 1.0
        assert 0.0 <= truth.p_pay_if_retried <= 1.0
        assert 0.0 <= truth.annoyance <= 1.0


def test_some_customers_have_opted_out(rows):
    """A guardrail with nothing to block is a guardrail that is never tested."""
    opted_out = [
        c for c, _ in rows if json.loads(c.meta_json)["customer"]["opted_out"]
    ]
    assert opted_out, "no opted-out customers means the opt-out rule is untested"


# ----------------------------------------------------------------- persistence


def test_loading_writes_both_tables_and_records_the_seed(session):
    stats = cohort.load_into(session, size=40)

    assert session.query(Case).count() == 40
    assert session.query(CaseTruth).count() == 40

    from app.models import LedgerEntry

    entry = (
        session.query(LedgerEntry)
        .filter_by(event="cohort_generated")
        .order_by(LedgerEntry.id.desc())
        .first()
    )
    assert entry is not None, "a cohort that is not in the audit trail is not reproducible"
    payload = json.loads(entry.payload_json)
    assert payload["seed"] == cohort.SEED
    assert payload["fingerprint"] == stats["fingerprint"]


def test_reloading_replaces_rather_than_duplicates(session):
    cohort.load_into(session, size=20)
    cohort.load_into(session, size=20)
    assert session.query(Case).count() == 20
    assert session.query(CaseTruth).count() == 20
