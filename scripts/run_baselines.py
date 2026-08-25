"""Run the baselines over the cohort and print the report card.

    python -m scripts.run_baselines
    python -m scripts.run_baselines --policies do_nothing,rules_only
    python -m scripts.run_baselines --no-audit      (skip the ledger writes)

This is the Day 3-4 gate: numbers on screen, from four policies that do not
involve an LLM at all. If the agent cannot beat `rules_only` on this table, the
agent is decoration and the report card will say so.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import report, runner
from app.cohort import SEED
from app.db import SessionLocal, init_db
from app.policies import BASELINES, get

DEFAULT = ["do_nothing", "blast_everyone", "blast_everyone_gated",
           "retry_everything", "rules_only"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run policies over the cohort.")
    parser.add_argument("--policies", default=",".join(DEFAULT),
                        help=f"comma-separated. available: {', '.join(BASELINES)}")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--no-audit", action="store_true",
                        help="do not write per-case entries to the ledger")
    parser.add_argument("--json", metavar="PATH",
                        help="also write the report card as JSON")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.policies.split(",") if n.strip()]
    policies = [get(name) for name in names]

    init_db()
    with SessionLocal() as session:
        rows = runner.load_cohort(session)
        results = runner.run_all(
            session, policies, seed=args.seed, audit=not args.no_audit,
        )

    card = report.build(results, rows)
    print(report.render(card))
    print(report.render_rule_breakdown(card))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(card, handle, indent=2)
        print(f"written  {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
