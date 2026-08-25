"""Build the 300-case cohort and write it to the database.

Run:  python -m scripts.generate_cohort
      python -m scripts.generate_cohort --seed 7 --size 50   (a small one, to poke at)

Prints the shape of the world every later number is measured against. Read the
two lines at the bottom before trusting any recovery figure: they say how much of
the at-risk money was never winnable, and how much was coming anyway.
"""

from __future__ import annotations

import argparse
import sys

from app import cohort
from app.db import SessionLocal, init_db


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Recoup cohort.")
    parser.add_argument("--seed", type=int, default=cohort.SEED)
    parser.add_argument("--size", type=int, default=cohort.COHORT_SIZE)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the summary without touching the database",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        stats = cohort.summarise(cohort.build_cohort(seed=args.seed, size=args.size))
    else:
        init_db()
        with SessionLocal() as session:
            stats = cohort.load_into(session, seed=args.seed, size=args.size)

    at_risk = stats["at_risk_paise"]
    print(f"cohort   seed={args.seed}  cases={stats['cases']}  as_of={cohort.AS_OF:%Y-%m-%d}")
    print(f"at risk  {rupees(at_risk)}\n")

    for kind, row in sorted(stats["by_kind"].items(), key=lambda kv: -kv[1]["paise"]):
        share = row["paise"] / at_risk * 100
        print(f"  {kind:<20} {row['count']:>4} cases  {rupees(row['paise']):>14}  {share:>5.1f}%")

    print()
    print("What no policy can win, and what no policy should take credit for:")
    print(
        f"  unrecoverable        {stats['unrecoverable_cases']:>4} cases  "
        f"{rupees(stats['unrecoverable_paise']):>14}   "
        "business-source declines: contacting these customers cannot work"
    )
    print(
        f"  would pay unprompted {stats['would_pay_unprompted_cases']:>4} cases  "
        f"{rupees(stats['would_pay_unprompted_paise']):>14}   "
        "already coming; chasing them is the false-positive bill"
    )
    print(
        f"  opted out            {stats['opted_out_cases']:>4} cases"
        "                     contacting these is not allowed at all"
    )

    print(f"\nfingerprint  {stats['fingerprint']}")
    if not args.dry_run:
        print("recorded in the ledger as cohort_generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
