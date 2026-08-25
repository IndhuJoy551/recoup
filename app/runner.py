"""One place where Watcher, planner, Guard, Doer and referee are wired together.

Every policy in this project -- the three baselines, the rules-only ablation and
the agent -- goes through this same loop. That is not tidiness, it is the
experiment design: if the baselines took a different code path, any difference in
the report card could be an artefact of the harness rather than of the decision.

The loop, in order:

    Watcher   turns a `Case` row into a `Signal`  (no AI, no truth)
    planner   turns a `Signal` into a plan        (rules, or the model)
    Guard     accepts or refuses each action      (never sees the reasoning)
    Doer      records the action as having happened
    referee   consults `CaseTruth` and says what the world did about it

The only asymmetry is `Policy.gated`. Ungated policies still have every action
checked -- the verdict is recorded as a *violation* instead of being enforced.
That is how "blast everyone sent 61 messages inside quiet hours" becomes a number
in a table rather than an assertion in a paragraph.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import doer as doer_module
from app import guard as guard_module
from app import ledger, simulator, watcher
from app.actions import Action, ESCALATE_TO_HUMAN, UnknownAction, nothing
from app.cohort import AS_OF, SEED
from app.models import Case, CaseTruth
from app.policies import Policy
from app.simulator import Outcome
from app.watcher import Signal


@dataclass
class RunResult:
    """Everything one policy did to the whole cohort, and what it achieved."""

    policy: str
    blurb: str
    gated: bool
    outcomes: list[Outcome] = field(default_factory=list)

    proposed: int = 0
    executed: int = 0

    # Gated policies: refusals that actually stopped something.
    # Ungated policies: rules the behaviour broke, counted but not enforced.
    guard_hits: dict[str, int] = field(default_factory=dict)

    llm_calls: int = 0
    llm_cached: int = 0
    llm_cost_paise: int = 0
    llm_fallbacks: int = 0
    unknown_actions: int = 0
    escalated_to_queue: int = 0
    doer_failures: int = 0

    @property
    def violations(self) -> dict[str, int]:
        return {} if self.gated else dict(self.guard_hits)

    @property
    def blocked(self) -> dict[str, int]:
        return dict(self.guard_hits) if self.gated else {}


def load_cohort(session: Session) -> list[tuple[Case, CaseTruth]]:
    """Read the cases and their hidden halves, paired and in a stable order."""
    cases = list(session.execute(select(Case).order_by(Case.id)).scalars().all())
    truths = {
        t.case_id: t
        for t in session.execute(select(CaseTruth)).scalars().all()
    }
    if not cases:
        # An empty cohort is not "zero at risk", it is a missing setup step, and
        # every policy would score a flawless nothing on it. Failing here costs a
        # second; publishing a table of zeros costs the whole comparison.
        raise RuntimeError(
            "no cases in the database. Run `python -m scripts.generate_cohort` first."
        )

    missing = [c.id for c in cases if c.id not in truths]
    if missing:
        raise RuntimeError(
            f"{len(missing)} cases have no truth row (e.g. {missing[0]}). "
            "Run `python -m scripts.generate_cohort` first."
        )
    return [(case, truths[case.id]) for case in cases]


def run_policy(
    session: Session,
    policy: Policy,
    rows: list[tuple[Case, CaseTruth]],
    *,
    as_of: dt.datetime = AS_OF,
    seed: int = SEED,
    audit: bool = True,
    doer: doer_module.Doer | None = None,
) -> RunResult:
    """Run one policy over the whole cohort and return what happened."""
    result = RunResult(policy=policy.name, blurb=policy.blurb, gated=policy.gated)
    state = guard_module.GuardState()
    # The batch runs the Doer in simulate mode, but it runs it: the live demo
    # calls the same `execute()` with mode="live", so the diagram is a
    # description of the code rather than an illustration next to it.
    doer = doer or doer_module.Doer(mode="simulate")
    audit_rows: list[dict] = []

    truth_by_id = {case.id: truth for case, truth in rows}
    cases_by_id = {case.id: case for case, _ in rows}

    signals = watcher.scan([case for case, _ in rows], as_of=as_of)

    for signal in signals:
        case = cases_by_id[signal.case_id]

        plan, meta = _plan_for(policy, signal, result)
        result.proposed += sum(1 for a in plan if a.kind != "do_nothing")

        executed: list[Action] = []
        decisions: list[dict] = []

        for action in plan:
            decision = guard_module.check(signal, action, state, as_of=as_of)
            decisions.append({**action.to_dict(), **decision.to_dict()})

            if decision.allowed or not policy.gated:
                done = doer.execute(case, signal, action, as_of=as_of)
                decisions[-1]["execution"] = done.to_dict()
                if not done.ok:
                    result.doer_failures += 1
                    continue
                executed.append(action)
                state.commit(signal, action, action.scheduled_at(as_of))
                continue

            # Refused, and the refusal was the kind a person should see. Sending
            # nothing at all would be a silent drop; the case is handed over
            # instead, which is what "compliant escalation" means.
            if decision.escalate and not any(a.kind == ESCALATE_TO_HUMAN for a in executed):
                fallback = Action(
                    ESCALATE_TO_HUMAN, wait_days=0,
                    reason=f"guard refused ({decision.rule}): {decision.detail}",
                )
                verdict = guard_module.check(signal, fallback, state, as_of=as_of)
                if verdict.allowed:
                    done = doer.execute(case, signal, fallback, as_of=as_of)
                    executed.append(fallback)
                    state.commit(signal, fallback, fallback.scheduled_at(as_of))
                    decisions.append({
                        **fallback.to_dict(), **verdict.to_dict(),
                        "execution": done.to_dict(),
                    })

        result.executed += sum(1 for a in executed if a.kind != "do_nothing")

        outcome = simulator.simulate(
            case, truth_by_id[signal.case_id], executed,
            policy=policy.name, seed=seed,
        )
        result.outcomes.append(outcome)

        if audit:
            audit_rows.append({
                "actor": "system",
                "event": "case_handled",
                "case_id": signal.case_id,
                "payload": {
                    "policy": policy.name,
                    "signal": signal.to_dict(),
                    "planner": meta,
                    "decisions": decisions,
                    "outcome": outcome.to_dict(),
                },
            })

    result.guard_hits = dict(state.blocks)
    result.escalated_to_queue = len(doer.queue)

    if audit and audit_rows:
        ledger.record_many(session, audit_rows)

    ledger.record(
        session,
        actor="system",
        event="policy_run_completed",
        payload={
            "policy": policy.name,
            "gated": policy.gated,
            "seed": seed,
            "as_of": as_of.isoformat(),
            "cases": len(result.outcomes),
            "actions_proposed": result.proposed,
            "actions_executed": result.executed,
            "guard_hits": result.guard_hits,
            "llm_calls": result.llm_calls,
            "llm_cost_paise": result.llm_cost_paise,
            "escalated_to_queue": result.escalated_to_queue,
            "doer_failures": result.doer_failures,
        },
    )
    return result


def _plan_for(policy: Policy, signal: Signal, result: RunResult) -> tuple[list[Action], dict]:
    """Ask the policy for a plan, and survive it asking for something impossible.

    An `UnknownAction` here is the single most interesting failure this system
    can have: the planner tried to do something outside the vocabulary. It is not
    swallowed and it is not retried into submission. The case is escalated to a
    person and the incident is counted, because a planner that does this is
    information, not noise.
    """
    try:
        plan = policy.plan(signal)
    except UnknownAction as exc:
        result.unknown_actions += 1
        return (
            [Action(ESCALATE_TO_HUMAN, wait_days=0,
                    reason=f"planner proposed an action outside the vocabulary: {exc}")],
            {"error": "unknown_action", "detail": str(exc)},
        )

    if not plan:
        plan = [nothing("planner returned an empty plan")]

    meta: dict = {"source": "llm" if policy.uses_llm else "rules"}
    if policy.uses_llm:
        # `policy.plan` is a bound method of the Thinker, so the per-call
        # telemetry lives on the instance behind it rather than on the function.
        owner = getattr(policy.plan, "__self__", None)
        stats = getattr(owner, "last_call", None)
        if isinstance(stats, dict):
            meta.update(stats)
            result.llm_calls += 1 if stats.get("source") == "api" else 0
            result.llm_cached += 1 if stats.get("source") == "cache" else 0
            result.llm_cost_paise += int(stats.get("cost_paise", 0))
            if stats.get("fallback"):
                result.llm_fallbacks += 1
    return plan, meta


def run_all(
    session: Session,
    policies: list[Policy],
    *,
    as_of: dt.datetime = AS_OF,
    seed: int = SEED,
    audit: bool = True,
) -> list[RunResult]:
    rows = load_cohort(session)
    return [
        run_policy(session, policy, rows, as_of=as_of, seed=seed, audit=audit)
        for policy in policies
    ]
