"""One real case, end to end, against Razorpay test mode. The video demo.

    python -m scripts.demo_live                 (pick the case automatically)
    python -m scripts.demo_live --case case_0042
    python -m scripts.demo_live --dry-run       (everything except the API call)

What this proves that the 300-case batch cannot: the pipeline is not a
simulation with an API-shaped hole in it. The Doer's `execute()` here is the same
method the batch calls, with `mode="live"`, and what comes back is a real
`plink_...` id you can paste into the Razorpay dashboard.

What it does not prove: that any of the *outcomes* in the report card are real.
Those come from the referee. Do not let the two blur together on camera -- say
which is which, out loud.

Requires Razorpay test keys in `.env`. `require_razorpay()` refuses anything that
does not start with `rzp_test_`.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import ledger, watcher
from app.cohort import AS_OF
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.doer import Doer
from app.guard import GuardState, check
from app.models import Case
from app.thinker import Thinker

RULE = "-" * 78


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def pick_case(session) -> Case | None:
    """A case worth watching: recoverable, not opted out, under the human ceiling.

    Chosen by the Watcher's own priority ordering rather than hand-picked, because
    "one cherry-picked match proves nothing" is in the brief and it applies to
    demos too.
    """
    cases = list(session.query(Case).all())
    by_id = {c.id: c for c in cases}

    # A failed card payment the customer has to act on. Preferred because the
    # right answer to it is `send_payment_link`, which is the one action with a
    # genuinely live path -- so the demo ends on a real plink_ id rather than on
    # a stub with an apology attached.
    for signal in watcher.scan(cases, as_of=AS_OF):
        if signal.hard_stop or signal.needs_human:
            continue
        if signal.kind == "failed_payment" and signal.recoverability == "contactable":
            return by_id[signal.case_id]

    for signal in watcher.scan(cases, as_of=AS_OF):
        if not signal.hard_stop and not signal.needs_human:
            return by_id[signal.case_id]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One case, live, against Razorpay test mode.")
    parser.add_argument("--case", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="run the whole pipeline but do not call Razorpay")
    parser.add_argument("--offline", action="store_true",
                        help="planner uses the committed cache only")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not args.dry_run:
        settings.require_razorpay()          # refuses a live key, loudly

    init_db()
    with SessionLocal() as session:
        case = session.get(Case, args.case) if args.case else pick_case(session)
        if case is None:
            print("no suitable case. Run `python -m scripts.generate_cohort` first.")
            return 1

        # ---------------------------------------------------------- WATCHER
        signal = watcher.assess(case, as_of=AS_OF)
        print(f"\n{RULE}\n  WATCHER\n{RULE}")
        print(f"  {case.id}   {rupees(case.amount_paise)}   {case.kind}")
        print(f"  recoverability={signal.recoverability}  band={signal.risk_band}  "
              f"source={signal.error_source or '-'}  reason={signal.failure_reason or '-'}")
        for fact in signal.facts:
            print(f"    - {fact}")

        # ---------------------------------------------------------- THINKER
        brain = Thinker(offline=args.offline)
        plan = brain.plan(signal)
        print(f"\n{RULE}\n  THINKER  ({brain.model})\n{RULE}")
        summary = brain.last_call.get("case_summary")
        if summary:
            print(f'  "{summary}"')
        if brain.last_call.get("fallback"):
            print(f"  [planner unavailable, fell back to rules: "
                  f"{brain.last_call.get('why', '')[:90]}]")
        for action in plan:
            print(f"    {action.kind:<20} +{action.wait_days}d at "
                  f"{action.hour_ist:02d}:00 IST")
            print(f"      why: {action.reason}")

        # ------------------------------------------------------------ GUARD
        print(f"\n{RULE}\n  GUARD\n{RULE}")
        state = GuardState()
        approved = []
        for action in plan:
            decision = check(signal, action, state, as_of=AS_OF)
            mark = "ALLOW " if decision.allowed else "REFUSE"
            print(f"    [{mark}] {action.kind:<20} "
                  f"{decision.rule or 'all rules passed'}")
            if not decision.allowed:
                print(f"             {decision.detail}")
            else:
                approved.append(action)

        if not approved:
            print("\n  Nothing was approved. That is a complete, correct outcome -- "
                  "not a failure.")
            brain.close()
            return 0

        # ------------------------------------------------------------- DOER
        print(f"\n{RULE}\n  DOER  ({'dry run' if args.dry_run else 'LIVE, test mode'})\n{RULE}")
        doer = Doer(mode="simulate" if args.dry_run else "live")
        try:
            for action in approved:
                result = doer.execute(case, signal, action, session=session, as_of=AS_OF)
                state.commit(signal, action, action.scheduled_at(AS_OF))
                status = "ok" if result.ok else "FAILED"
                print(f"    [{status}] {result.action:<20} ({result.reality})")
                if result.reference:
                    print(f"             reference: {result.reference}")
                if result.detail:
                    print(f"             {result.detail}")
                # Only the first real action is needed for the demo; the rest
                # would create Razorpay objects nobody is going to look at.
                if result.ok and result.reality in ("real", "partial"):
                    break
        finally:
            doer.close()

        # ------------------------------------------------------------ DIARY
        print(f"\n{RULE}\n  LEDGER\n{RULE}")
        chain = ledger.verify_chain(session)
        entries = [e for e in ledger.tail(session, limit=12) if e.case_id == case.id]
        for entry in reversed(entries):
            payload = json.loads(entry.payload_json)
            print(f"    #{entry.id}  {entry.actor:<8} {entry.event:<18} "
                  f"{payload.get('action') or payload.get('policy') or ''}")
            print(f"          {entry.prev_hash[:10]} -> {entry.entry_hash[:10]}")
        print(f"\n  chain: {chain.entries} entries, "
              f"{'intact' if chain.ok else 'BROKEN -- ' + chain.detail}")

        if doer.queue:
            print(f"\n  exception queue: {len(doer.queue)} case(s) waiting for a human")

    brain.close()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
