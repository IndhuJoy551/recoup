"""End to end: cohort in, report card out, with the Guard doing its job.

These are the tests that would catch a wiring mistake -- a policy that quietly
bypasses the gate, a ledger that stops recording, an escalation that drops a case
on the floor instead of handing it over.
"""

import json

import pytest

from app import cohort, ledger, report, runner
from app.actions import Action
from app.models import LedgerEntry
from app.policies import Policy, get, plan_rules_only


@pytest.fixture
def loaded(session):
    cohort.load_into(session, size=60)
    return session


@pytest.fixture
def rows(loaded):
    return runner.load_cohort(loaded)


# ------------------------------------------------------------- the harness


def test_every_case_gets_an_outcome_under_every_policy(loaded, rows):
    for name in ("do_nothing", "blast_everyone", "retry_everything", "rules_only"):
        result = runner.run_policy(loaded, get(name), rows, audit=False)
        assert len(result.outcomes) == len(rows), (
            f"{name} lost a case. Silently dropping one is worse than handling it "
            "badly, because nothing in the report card would show it."
        )
        assert {o.case_id for o in result.outcomes} == {c.id for c, _ in rows}


def test_doing_nothing_causes_nothing_but_still_collects(loaded, rows):
    result = runner.run_policy(loaded, get("do_nothing"), rows, audit=False)
    world = report.describe_cohort(rows)
    scored = report.score(result, world)

    assert scored["caused_paise"] == 0
    assert scored["collected_paise"] == world.self_paying_paise, (
        "the control arm must collect exactly the money that was arriving anyway"
    )
    assert scored["contacts"] == 0


def test_the_missing_cohort_error_tells_you_what_to_run(session):
    with pytest.raises(RuntimeError, match="generate_cohort"):
        runner.load_cohort(session)


# ---------------------------------------------------------------- the gate


def test_a_gated_policy_never_contacts_someone_who_opted_out(loaded, rows):
    result = runner.run_policy(loaded, get("rules_only"), rows, audit=False)

    opted_out = {
        c.id for c, _ in rows
        if json.loads(c.meta_json)["customer"]["opted_out"]
    }
    for outcome in result.outcomes:
        if outcome.case_id in opted_out:
            assert outcome.contacts == 0


def test_an_ungated_policy_does_contact_them_and_the_violation_is_counted(loaded, rows):
    result = runner.run_policy(loaded, get("blast_everyone"), rows, audit=False)
    assert result.violations.get("customer_opted_out", 0) > 0
    assert not result.blocked, "an ungated policy is measured, not stopped"


def test_the_same_strategy_gated_and_ungated_differs_only_in_the_gate(loaded, rows):
    """The row that separates 'we target better' from 'we are allowed to send less'."""
    ungated = runner.run_policy(loaded, get("blast_everyone"), rows, audit=False)
    gated = runner.run_policy(loaded, get("blast_everyone_gated"), rows, audit=False)

    assert ungated.proposed == gated.proposed, "same strategy, same proposals"
    assert gated.executed < ungated.executed, "the gate must actually refuse things"
    assert sum(gated.blocked.values()) > 0
    assert sum(ungated.violations.values()) > 0


def test_a_refusal_that_needs_a_person_becomes_an_escalation(loaded, rows):
    """Compliant escalation: blocked is not the same as dropped.

    A policy that only knows how to send messages, run against cases that are
    above the value ceiling or unfixable, must end up handing them to a human
    rather than silently doing nothing.
    """
    messages_only = Policy(
        "messages_only",
        lambda s: [Action("send_payment_link", wait_days=0, hour_ist=11, reason="t")],
        gated=True, blurb="test policy",
    )
    result = runner.run_policy(loaded, messages_only, rows, audit=False)

    escalated = sum(o.escalations for o in result.outcomes)
    assert escalated > 0
    assert result.escalated_to_queue == escalated, (
        "every escalation must be parked in the exception queue, not just counted"
    )


def test_a_planner_that_invents_an_action_is_survived_and_counted(loaded, rows):
    from app.actions import UnknownAction

    def rogue(signal):
        if signal.amount_paise > 100_000:
            raise UnknownAction("issue_refund is not a permitted action")
        return plan_rules_only(signal)

    result = runner.run_policy(
        loaded, Policy("rogue", rogue, gated=True, blurb="t"), rows, audit=False,
    )
    assert result.unknown_actions > 0
    assert len(result.outcomes) == len(rows), "the batch must not stop"
    assert sum(o.escalations for o in result.outcomes) >= result.unknown_actions, (
        "a case the planner could not handle goes to a person"
    )


# --------------------------------------------------------------- the diary


def test_every_case_lands_in_the_audit_trail_with_its_reasoning(loaded, rows):
    before = loaded.query(LedgerEntry).count()
    runner.run_policy(loaded, get("rules_only"), rows, audit=True)

    entries = (
        loaded.query(LedgerEntry)
        .filter_by(event="case_handled")
        .order_by(LedgerEntry.id.asc())
        .all()
    )
    assert len(entries) == len(rows)

    payload = json.loads(entries[0].payload_json)
    assert set(payload) >= {"policy", "signal", "planner", "decisions", "outcome"}
    assert payload["signal"]["facts"], "the audit trail must say what was known"
    for decision in payload["decisions"]:
        assert decision["reason"], "every action has to explain itself"
        assert "allowed" in decision

    assert loaded.query(LedgerEntry).count() > before


def test_the_chain_survives_a_full_run(loaded, rows):
    runner.run_policy(loaded, get("blast_everyone"), rows, audit=True)
    runner.run_policy(loaded, get("rules_only"), rows, audit=True)

    status = ledger.verify_chain(loaded)
    assert status.ok, status.detail
    assert status.entries > len(rows)


def test_the_batched_writer_produces_the_same_chain_as_one_at_a_time(session):
    ledger.record(session, actor="system", event="a")
    ledger.record_many(session, [
        {"actor": "system", "event": "b"},
        {"actor": "system", "event": "c", "case_id": "case_1", "payload": {"x": 1}},
    ])
    ledger.record(session, actor="system", event="d")

    status = ledger.verify_chain(session)
    assert status.ok and status.entries == 4


def test_a_run_records_which_cohort_produced_it(loaded, rows):
    runner.run_policy(loaded, get("rules_only"), rows, audit=False)
    entry = (
        loaded.query(LedgerEntry)
        .filter_by(event="policy_run_completed")
        .order_by(LedgerEntry.id.desc())
        .first()
    )
    payload = json.loads(entry.payload_json)
    assert payload["seed"] == cohort.SEED
    assert payload["policy"] == "rules_only"


# --------------------------------------------------------- reproducibility


def test_running_the_whole_thing_twice_gives_identical_numbers(loaded, rows):
    """Submission checklist item 2, at the level a reviewer would test it."""
    names = ["do_nothing", "blast_everyone", "retry_everything", "rules_only"]

    first = report.build(
        [runner.run_policy(loaded, get(n), rows, audit=False) for n in names], rows
    )
    second = report.build(
        [runner.run_policy(loaded, get(n), rows, audit=False) for n in names], rows
    )
    assert first == second
