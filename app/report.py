"""The report card: the numbers, and the numbers that stop them flattering us.

Razorpay's brief asked for "honest metrics including false-positive cost" and
warned that "one cherry-picked match proves nothing". This module is the answer
to both sentences, so it is worth being explicit about the two decisions that
shape it.

**Collected is not caused.** Every policy here reports two recovery totals.
`collected` is the money that arrived on cases we touched -- the number a normal
recovery dashboard shows, and the one every vendor quotes. `caused` subtracts
the customers who were going to pay anyway. On this cohort that gap is about a
quarter of the at-risk money, which means a tool measured on `collected` can do
nothing at all and still look like it earned its licence fee. Doing nothing is in
the table for exactly that reason: it is the control, and it collects a lot.

**The denominator is the winnable money, not all of it.** Some of this cohort
cannot be recovered by anybody -- merchant-side declines that no message can fix
-- and some of it was never lost. Quoting recovery as a share of total at-risk
revenue makes every policy look worse than it is and hides which ones are
leaving real money behind. `winnable_paise` is the honest denominator.

Everything below is computed from `Outcome` objects, which come from the referee,
which is the only component that reads `CaseTruth`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Case, CaseTruth
from app.runner import RunResult


@dataclass
class Cohortwide:
    """Facts about the world that are true no matter which policy is running."""

    cases: int
    at_risk_paise: int
    winnable_paise: int          # recoverable, and would not have paid unprompted
    winnable_cases: int
    unwinnable_paise: int        # merchant-side: no policy can take this
    unwinnable_cases: int
    self_paying_paise: int       # arriving with or without us
    self_paying_cases: int
    opted_out_cases: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def describe_cohort(rows: list[tuple[Case, CaseTruth]]) -> Cohortwide:
    winnable = [(c, t) for c, t in rows if t.recoverable and not t.would_pay_unprompted]
    unwinnable = [(c, t) for c, t in rows if not t.recoverable]
    self_paying = [(c, t) for c, t in rows if t.would_pay_unprompted]

    import json

    return Cohortwide(
        cases=len(rows),
        at_risk_paise=sum(c.amount_paise for c, _ in rows),
        winnable_paise=sum(c.amount_paise for c, _ in winnable),
        winnable_cases=len(winnable),
        unwinnable_paise=sum(c.amount_paise for c, _ in unwinnable),
        unwinnable_cases=len(unwinnable),
        self_paying_paise=sum(c.amount_paise for c, _ in self_paying),
        self_paying_cases=len(self_paying),
        opted_out_cases=sum(
            1 for c, _ in rows if json.loads(c.meta_json)["customer"]["opted_out"]
        ),
    )


def score(result: RunResult, world: Cohortwide) -> dict:
    """Turn one policy's outcomes into the row that appears in the table."""
    outcomes = result.outcomes

    collected = sum(o.collected_paise for o in outcomes)
    caused = sum(o.caused_paise for o in outcomes)
    caused_cases = sum(1 for o in outcomes if o.caused)

    contacts = sum(o.contacts for o in outcomes)
    retries = sum(o.retries for o in outcomes)
    escalations = sum(o.escalations for o in outcomes)
    cost = sum(o.cost_paise for o in outcomes)

    false_interventions = [o for o in outcomes if o.false_intervention]
    wasted = [o for o in outcomes if o.wasted_contact]
    opt_outs = [o for o in outcomes if o.opted_out]
    left_alone = [o for o in outcomes if o.correctly_left_alone]

    return {
        "policy": result.policy,
        "blurb": result.blurb,
        "gated": result.gated,

        # --- what arrived -------------------------------------------------
        "collected_paise": collected,
        "caused_paise": caused,
        "caused_cases": caused_cases,
        "share_of_winnable": _ratio(caused, world.winnable_paise),

        # --- what it took -------------------------------------------------
        "actions_proposed": result.proposed,
        "actions_executed": result.executed,
        "contacts": contacts,
        "retries": retries,
        "escalations": escalations,
        "cost_paise": cost,

        # --- what it cost other people ------------------------------------
        # A recovery tool's damage is done to customers, and none of it shows up
        # in the revenue line. These four are the whole reason this table exists.
        "false_interventions": len(false_interventions),
        "false_intervention_paise": sum(o.amount_paise for o in false_interventions),
        "false_intervention_rate": _ratio(len(false_interventions), max(1, sum(1 for o in outcomes if o.contacts))),
        "wasted_contacts": len(wasted),
        "opt_outs": len(opt_outs),
        "correctly_left_alone": len(left_alone),

        # --- efficiency ----------------------------------------------------
        "contacts_per_1000_caused": _per(contacts, caused, 100_000),
        "cost_pct_of_caused": _pct(cost, caused),

        # --- compliance ----------------------------------------------------
        "blocked": result.blocked,
        "blocked_total": sum(result.blocked.values()),
        "violations": result.violations,
        "violations_total": sum(result.violations.values()),

        # --- the model ------------------------------------------------------
        "llm_calls": result.llm_calls,
        "llm_cached": result.llm_cached,
        "llm_cost_paise": result.llm_cost_paise,
        "llm_fallbacks": result.llm_fallbacks,
        "unknown_actions": result.unknown_actions,
    }


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _pct(cost_paise: int, caused_paise: int) -> float | None:
    """What fraction of the money it recovered did the policy spend recovering it?

    The most useful single efficiency number here, because it is unit-free and
    survives being compared across policies with wildly different volumes. An
    earlier version reported "paise of cost per Rs 100 caused" and then printed
    the result divided by another hundred, so a policy spending 28 paise per
    Rs 100 appeared as "0.28p". Wrong by two orders of magnitude and completely
    plausible-looking, which is the dangerous kind.
    """
    if caused_paise <= 0:
        return None
    return round(100.0 * cost_paise / caused_paise, 3)


def _per(amount: int, caused_paise: int, unit_paise: int) -> float | None:
    """`amount` per `unit_paise` of caused recovery, or None if it caused nothing.

    Returning None rather than 0 or infinity is deliberate: a policy that
    recovered nothing does not have a good cost-per-rupee, it has an undefined
    one, and printing 0.0 there would make do-nothing look like the cheapest way
    to recover money.
    """
    if caused_paise <= 0:
        return None
    return round(amount / (caused_paise / unit_paise), 2)


def build(results: list[RunResult], rows: list[tuple[Case, CaseTruth]]) -> dict:
    world = describe_cohort(rows)
    scored = [score(r, world) for r in results]
    return {"cohort": world.to_dict(), "policies": scored}


# --------------------------------------------------------------- rendering
# ASCII only. The Windows console this was developed on is cp1252 and a rupee
# sign in a print() crashed the first version of the cohort script. See BUGLOG.


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def render(card: dict) -> str:
    world = card["cohort"]
    lines: list[str] = []
    add = lines.append

    add("=" * 96)
    add("RECOUP REPORT CARD")
    add("=" * 96)
    add("")
    add(f"  cohort            {world['cases']} cases, {rupees(world['at_risk_paise'])} at risk")
    add(f"  winnable          {world['winnable_cases']} cases, {rupees(world['winnable_paise'])}"
        "   <- the only money any policy can earn")
    add(f"  arriving anyway   {world['self_paying_cases']} cases, {rupees(world['self_paying_paise'])}"
        "   <- chasing these is the false-positive bill")
    add(f"  unwinnable        {world['unwinnable_cases']} cases, {rupees(world['unwinnable_paise'])}"
        "   <- merchant-side declines; no message helps")
    add("")

    header = (
        f"{'policy':<22}{'caused':>12}{'% winnable':>12}{'collected':>12}"
        f"{'msgs':>7}{'retries':>9}{'esc':>6}{'optout':>8}{'false+':>8}"
    )
    add(header)
    add("-" * len(header))
    for row in card["policies"]:
        add(
            f"{row['policy']:<22}"
            f"{rupees(row['caused_paise']):>12}"
            f"{row['share_of_winnable'] * 100:>11.1f}%"
            f"{rupees(row['collected_paise']):>12}"
            f"{row['contacts']:>7}"
            f"{row['retries']:>9}"
            f"{row['escalations']:>6}"
            f"{row['opt_outs']:>8}"
            f"{row['false_interventions']:>8}"
        )
    add("")

    header2 = (
        f"{'policy':<22}{'msgs/Rs1000':>14}{'cost % of':>12}{'blocked':>10}"
        f"{'violations':>13}{'left alone':>13}"
    )
    add(header2)
    add("-" * len(header2))
    for row in card["policies"]:
        per_k = row["contacts_per_1000_caused"]
        per_c = row["cost_pct_of_caused"]
        add(
            f"{row['policy']:<22}"
            f"{('n/a' if per_k is None else f'{per_k:.2f}'):>14}"
            f"{('n/a' if per_c is None else f'{per_c:.2f}%'):>12}"
            f"{row['blocked_total']:>10}"
            f"{row['violations_total']:>13}"
            f"{row['correctly_left_alone']:>13}"
        )
    add("")

    add("  caused      = money that arrived BECAUSE of the policy. Customers who")
    add("                would have paid unprompted are excluded, in every row.")
    add("  collected   = every rupee that arrived, including those customers. This")
    add("                is the number a normal recovery dashboard would show you.")
    add("  false+      = cases where we contacted someone who was already coming.")
    add("  violations  = guard rules the policy broke. Ungated policies are not")
    add("                stopped, only counted -- that is what the behaviour costs.")
    add("  blocked     = actions the Guard actually refused, for gated policies.")
    add("  cost % of   = what the policy spent (messages + staff time) as a")
    add("                share of the money it actually caused to arrive.")
    add("  left alone  = unwinnable cases correctly not chased.")
    return "\n".join(lines)


def render_rule_breakdown(card: dict) -> str:
    lines = ["", "Guard activity, by rule:", ""]
    for row in card["policies"]:
        hits = row["blocked"] or row["violations"]
        if not hits:
            continue
        label = "blocked" if row["gated"] else "VIOLATED"
        lines.append(f"  {row['policy']}  ({label})")
        for rule, count in sorted(hits.items(), key=lambda kv: -kv[1]):
            lines.append(f"      {rule:<28} {count:>5}")
        lines.append("")
    return "\n".join(lines)


def render_ablation(card: dict, *, agent: str = "recoup", rules: str = "rules_only") -> str:
    """AI on versus AI off, and a verdict that is allowed to go against us.

    Razorpay's brief says an LLM that a plain `if` could replace is decoration.
    The only way to answer that is to run the same pipeline with the model
    switched off and publish both columns, including the case where the rules
    win. This function is written so that outcome prints just as cleanly as the
    flattering one -- if the verdict is only readable when it is good news, it is
    not a verdict.
    """
    rows = {row["policy"]: row for row in card["policies"]}
    if agent not in rows or rules not in rows:
        return ""

    a, r = rows[agent], rows[rules]
    lines = ["", "=" * 96, "ABLATION: does the model earn its place?", "=" * 96, ""]

    def line(label: str, av, rv, fmt=str) -> None:
        lines.append(f"  {label:<34}{fmt(av):>16}{fmt(rv):>16}")

    lines.append(f"  {'':<34}{'AI on (' + agent + ')':>16}{'AI off (rules)':>16}")
    lines.append("  " + "-" * 66)
    line("money caused", a["caused_paise"], r["caused_paise"], rupees)
    line("share of winnable money", a["share_of_winnable"], r["share_of_winnable"],
         lambda v: f"{v * 100:.1f}%")
    line("messages sent", a["contacts"], r["contacts"])
    line("messages per Rs 1000 caused", a["contacts_per_1000_caused"],
         r["contacts_per_1000_caused"], lambda v: "n/a" if v is None else f"{v:.2f}")
    line("customers lost to opt-out", a["opt_outs"], r["opt_outs"])
    line("chased someone already paying", a["false_interventions"], r["false_interventions"])
    line("handed to a human", a["escalations"], r["escalations"])
    line("cost as % of money caused", a["cost_pct_of_caused"], r["cost_pct_of_caused"],
         lambda v: "n/a" if v is None else f"{v:.2f}%")
    lines.append("")

    delta = a["caused_paise"] - r["caused_paise"]
    pct = (delta / r["caused_paise"] * 100) if r["caused_paise"] else 0.0
    msg_delta = a["contacts"] - r["contacts"]

    lines.append(f"  model cost for the batch      {rupees(a['llm_cost_paise'])}"
                 f"   ({a['llm_calls']} live calls, {a['llm_cached']} from cache, "
                 f"{a['llm_fallbacks']} fell back to rules)")
    if a["caused_paise"]:
        share = a["llm_cost_paise"] / a["caused_paise"] * 100
        lines.append(f"  model cost as % of recovery   {share:.3f}%")
    lines.append("")

    if delta > 0:
        lines.append(f"  VERDICT: the model is worth it here. It caused {rupees(delta)} more "
                     f"({pct:+.1f}%)")
        lines.append(f"           with {msg_delta:+d} messages and "
                     f"{a['opt_outs'] - r['opt_outs']:+d} opt-outs.")
    elif delta < 0:
        lines.append(f"  VERDICT: the rules win. The model caused {rupees(-delta)} LESS "
                     f"({pct:+.1f}%).")
        lines.append("           On this cohort the LLM is decoration, and the honest "
                     "recommendation")
        lines.append("           is to ship the rules and keep the model for the cases "
                     "they cannot express.")
    else:
        lines.append("  VERDICT: a dead heat on money caused. Compare the message count and "
                     "the opt-outs;")
        lines.append("           if those are equal too, the rules are cheaper and should ship.")
    return "\n".join(lines)


def to_markdown(card: dict) -> str:
    """The table that goes in RESULTS.md, for people reading the repo on GitHub."""
    world = card["cohort"]
    out = [
        "| policy | caused | % of winnable | collected | messages | retries | escalated | opt-outs | false positives | violations |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in card["policies"]:
        out.append(
            f"| `{row['policy']}` | **{rupees(row['caused_paise'])}** "
            f"| {row['share_of_winnable'] * 100:.1f}% "
            f"| {rupees(row['collected_paise'])} "
            f"| {row['contacts']} | {row['retries']} | {row['escalations']} "
            f"| {row['opt_outs']} | {row['false_interventions']} "
            f"| {row['violations_total']} |"
        )
    out.append("")
    out.append(
        f"Cohort: {world['cases']} cases, {rupees(world['at_risk_paise'])} at risk. "
        f"Of that, {rupees(world['winnable_paise'])} is winnable, "
        f"{rupees(world['self_paying_paise'])} was arriving anyway, and "
        f"{rupees(world['unwinnable_paise'])} cannot be recovered by anyone."
    )
    return "\n".join(out)
