"""Three hundred cases that never happened, built so the numbers can be trusted.

Why synthetic
-------------
A recovery agent has to be judged on a counterfactual: *would this customer have
paid anyway?* You cannot get that from real data. Once you send the reminder you
have burned the control group; the month cannot be replayed without the message.
And it is precisely that unknowable column which a false-positive rate is made
of, so without it "we recovered 42,000" is a number with no denominator.

So we build a world where the counterfactual is known, write it to a table Recoup
cannot see (`CaseTruth`), and let the referee compare policies against it.

The honest disclosure
---------------------
I wrote these rules. That matters, and pretending otherwise would make the whole
comparison theatre, so the rules are stated openly here rather than buried:

* declines carry an `error_source`, and it decides what can possibly work --
  `customer` responds to contact, `bank`/`gateway` responds to a silent retry,
  `business` responds to nothing;
* an insufficient-funds decline late in the month is worth more on the 1st,
  because salaries land on the 1st;
* a fraction of every bucket pays unprompted, so contacting everybody buys some
  recoveries that were already coming;
* every unwanted contact carries a chance of a permanent opt-out.

Two defences against having written my own answer key. First, every policy --
the three baselines and the agent alike -- sees exactly the same `Case` row and
is told none of the parameters above. Second, the ablation runs rules-only
against agent and publishes whichever wins.

The failure strings are modelled on Razorpay's documented decline reasons. Two of
them are verbatim from payments this project actually made and lost in test mode
(`payment_cancelled` and `international_transaction_not_allowed`); those are the
shape everything else follows.

Reproducibility
---------------
Nothing here calls `random` at module level, `datetime.now()`, or an unseeded
generator. Same seed in, byte-identical cohort out, verified by a fingerprint the
generator records in the ledger. That is submission checklist item 2: re-run the
report card and get exactly the same numbers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import ledger
from app.models import Case, CaseTruth

SEED = 20260826
COHORT_SIZE = 300
MERCHANT_ID = "merch_kavya_candles"

# The cohort's "now". A fixed anchor, not the clock, or the same seed would give
# different day-of-month effects depending on when you happened to run it.
AS_OF = dt.datetime(2026, 8, 31, 4, 0, tzinfo=dt.timezone.utc)  # 09:30 IST
WINDOW_DAYS = 30


@dataclass(frozen=True)
class Bucket:
    """One kind of at-risk revenue, and how much of it there is.

    The proportions are Kavya's month scaled up: 40 abandoned checkouts, 25 failed
    payments, 12 failed mandates and 8 overdue invoices, times ~3.5. Invoices are
    few and large; checkouts are many and small. Getting that shape right matters
    more than the exact rupee totals, because a policy that only chases big
    numbers looks brilliant on a flat distribution and useless on a real one.
    """

    kind: str
    count: int
    median_paise: int
    spread: float                # lognormal sigma: how uneven the amounts are
    p_unprompted: float          # base chance they pay with no contact at all


BUCKETS = (
    Bucket("abandoned_checkout", 141, 80_000, 0.55, 0.22),
    Bucket("failed_payment", 88, 72_000, 0.60, 0.18),
    Bucket("failed_mandate", 42, 75_000, 0.35, 0.10),
    Bucket("overdue_invoice", 29, 687_500, 0.70, 0.38),
)


@dataclass(frozen=True)
class FailureMode:
    """A decline, and what it implies about the only move that can work.

    `source` is the field doing the work. Razorpay puts it on every failed
    payment and it answers "whose problem is this?" -- a different and far more
    useful question than "what went wrong". A business-source decline is our own
    misconfiguration; the customer cannot fix it, so contacting them is a false
    positive with a cost, every time, forever.
    """

    reason: str
    source: str                      # customer | bank | gateway | business
    step: str
    description: str
    weight: float
    p_contact: tuple[float, float]   # chance contact works, if it can work
    p_retry: tuple[float, float]     # chance a silent retry works on its own


# Verbatim from pay_TTglJUnP6kAk7Z and pay_TTgVd6Qlel1Eah, both captured live via
# webhook on 2026-08-24. See BUGLOG. The rest follow that shape.
CARD_FAILURES = (
    FailureMode(
        "insufficient_funds", "customer", "payment_authorization",
        "Your payment failed as the account had insufficient balance.",
        0.26, (0.42, 0.68), (0.10, 0.20),
    ),
    FailureMode(
        "payment_cancelled", "customer", "payment_authentication",
        "Your payment has been cancelled. Try again or complete the payment later.",
        0.17, (0.30, 0.55), (0.04, 0.10),
    ),
    FailureMode(
        "incorrect_otp", "customer", "payment_authentication",
        "Your payment failed as the OTP entered was incorrect.",
        0.12, (0.45, 0.70), (0.08, 0.16),
    ),
    FailureMode(
        "payment_timeout", "customer", "payment_authentication",
        "Your payment timed out before it could be authenticated.",
        0.09, (0.35, 0.60), (0.15, 0.28),
    ),
    FailureMode(
        "card_expired", "customer", "payment_initiation",
        "Your payment failed as the card has expired.",
        0.08, (0.30, 0.50), (0.00, 0.02),
    ),
    # Nobody's fault and nobody to talk to. A retry later usually just works, and
    # a message about it is noise aimed at the wrong human.
    FailureMode(
        "issuer_down", "bank", "payment_authorization",
        "Your payment failed as the bank was unavailable. Please try again.",
        0.13, (0.20, 0.35), (0.55, 0.80),
    ),
    FailureMode(
        "gateway_technical_error", "gateway", "payment_authorization",
        "Your payment failed due to a temporary technical error.",
        0.07, (0.20, 0.35), (0.50, 0.75),
    ),
    # Our configuration, not their wallet. Unrecoverable until a human fixes a
    # dashboard setting, which is a job for the merchant, not the customer.
    FailureMode(
        "international_transaction_not_allowed", "business", "payment_initiation",
        "Your payment could not be completed as this business accepts domestic "
        "(Indian) card payments only. Try another payment method.",
        0.05, (0.0, 0.0), (0.0, 0.0),
    ),
    FailureMode(
        "payment_method_not_enabled", "business", "payment_initiation",
        "Your payment could not be completed as this method is not enabled.",
        0.03, (0.0, 0.0), (0.0, 0.0),
    ),
)

# A subscription auto-charge fails for a narrower set of reasons, and one of them
# is structurally different: a revoked mandate cannot be retried at all, because
# the standing permission to charge is gone. Only a human re-authorising fixes it.
MANDATE_FAILURES = (
    FailureMode(
        "card_expired", "customer", "payment_initiation",
        "The saved card on this mandate has expired.",
        0.38, (0.35, 0.60), (0.00, 0.02),
    ),
    FailureMode(
        "insufficient_funds", "customer", "payment_authorization",
        "The auto-debit failed as the account had insufficient balance.",
        0.34, (0.45, 0.70), (0.12, 0.24),
    ),
    FailureMode(
        "mandate_revoked", "customer", "payment_initiation",
        "The mandate has been revoked by the customer's bank.",
        0.16, (0.10, 0.22), (0.0, 0.0),
    ),
    FailureMode(
        "issuer_down", "bank", "payment_authorization",
        "The auto-debit failed as the bank was unavailable.",
        0.12, (0.18, 0.30), (0.55, 0.78),
    ),
)

METHODS = (("card", 0.52), ("netbanking", 0.24), ("upi", 0.18), ("wallet", 0.06))

PRIOR_PURCHASE_CHOICES = (0, 1, 2, 3, 5, 8, 14)
PRIOR_PURCHASE_WEIGHTS = (0.28, 0.22, 0.16, 0.12, 0.11, 0.07, 0.04)


def _weighted(rng: random.Random, options, weight):
    """Pick one option in proportion to its weight, using only `rng`.

    `random.choices` would do, but it is called with a fresh weights list each
    time and the draw order matters here: reproducibility depends on every draw
    coming from this one seeded generator in a fixed sequence.
    """
    total = sum(weight(o) for o in options)
    pick = rng.random() * total
    for option in options:
        pick -= weight(option)
        if pick <= 0:
            return option
    return options[-1]


def _amount_paise(rng: random.Random, median: int, sigma: float) -> int:
    """Lognormal, then rounded to whole rupees.

    Real baskets are lognormal -- a long tail of a few large ones -- and the tail
    is the interesting part. A policy that ignores the small cases still looks
    fine on rupees recovered while doing nothing for most customers, and only an
    uneven distribution exposes that.
    """
    value = median * math.exp(rng.gauss(0.0, sigma))
    return max(5_000, int(round(value / 100.0)) * 100)


def _days_until_first(day_of_month: int) -> int:
    """Days from `day_of_month` to the next 1st, capped at a sensible wait.

    Salaries land on the 1st. An insufficient-funds decline on the 28th is a
    different case on the 2nd, and the whole point of a planner is noticing that
    instead of retrying immediately into the same empty account.
    """
    if day_of_month >= 24:
        return min(8, 32 - day_of_month)
    return 2


def build_cohort(seed: int = SEED, size: int = COHORT_SIZE) -> list[tuple[Case, CaseTruth]]:
    """Generate the cohort in memory. Pure: same seed, same list, no I/O."""
    rng = random.Random(seed)
    scale = size / COHORT_SIZE
    rows: list[tuple[Case, CaseTruth]] = []
    index = 0

    for bucket in BUCKETS:
        for _ in range(max(1, round(bucket.count * scale))):
            index += 1
            rows.append(_one_case(rng, bucket, index))

    return rows[:size]


def _one_case(rng: random.Random, bucket: Bucket, index: int) -> tuple[Case, CaseTruth]:
    case_id = f"case_{index:04d}"
    amount = _amount_paise(rng, bucket.median_paise, bucket.spread)

    age_days = rng.randint(0, WINDOW_DAYS - 1)
    detected_at = AS_OF - dt.timedelta(
        days=age_days, hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
    )

    # Customer history. Loyalty is the strongest honest signal we hand a policy:
    # someone who has paid six times is likelier to sort it out themselves, and
    # likelier to respond well if asked. Both effects are real and they pull in
    # opposite directions, which is what makes the decision non-trivial.
    prior_purchases = _weighted(
        rng, PRIOR_PURCHASE_CHOICES,
        lambda n: PRIOR_PURCHASE_WEIGHTS[PRIOR_PURCHASE_CHOICES.index(n)],
    )
    loyalty = min(1.0, prior_purchases / 8.0)
    opted_out = rng.random() < 0.04
    prior_contacts = _weighted(
        rng, (0, 1, 2, 3), lambda n: (0.70, 0.18, 0.08, 0.04)[n]
    )

    meta: dict = {
        "method": _weighted(rng, METHODS, lambda m: m[1])[0],
        "customer": {
            "prior_purchases": prior_purchases,
            "days_since_last_purchase": rng.randint(3, 400) if prior_purchases else None,
            "prior_recovery_contacts_90d": prior_contacts,
            "opted_out": opted_out,
        },
    }

    failure: FailureMode | None = None
    reason: str | None = None

    if bucket.kind in ("failed_payment", "failed_mandate"):
        modes = CARD_FAILURES if bucket.kind == "failed_payment" else MANDATE_FAILURES
        failure = _weighted(rng, modes, lambda m: m.weight)
        reason = failure.reason
        meta["failure"] = {
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": failure.description,
            "error_source": failure.source,
            "error_step": failure.step,
            "error_reason": failure.reason,
        }
        if bucket.kind == "failed_mandate":
            meta["subscription"] = {"cycles_paid": rng.randint(1, 18), "monthly": True}

    elif bucket.kind == "abandoned_checkout":
        meta["checkout"] = {
            "items": rng.randint(1, 5),
            "minutes_since_abandon": rng.randint(20, 60 * 24 * 3),
            "reached_payment_page": rng.random() < 0.62,
        }

    else:  # overdue_invoice
        meta["invoice"] = {
            "days_overdue": rng.randint(3, 95),
            "terms_days": _weighted(rng, (15, 30, 45), lambda d: 1.0),
            "b2b": True,
        }

    truth = _truth_for(rng, case_id, bucket, failure, loyalty, amount)

    case = Case(
        id=case_id,
        merchant_id=MERCHANT_ID,
        kind=bucket.kind,
        customer_ref=f"cust_{rng.randrange(16 ** 8):08x}",
        amount_paise=amount,
        currency="INR",
        status="open",
        razorpay_entity_id=None,
        failure_reason=reason,
        detected_at=detected_at,
        attempts=0,
        last_contact_at=None,
        recovered_paise=0,
        meta_json=json.dumps(meta, sort_keys=True),
    )
    return case, truth


def _truth_for(
    rng: random.Random,
    case_id: str,
    bucket: Bucket,
    failure: FailureMode | None,
    loyalty: float,
    amount: int,
) -> CaseTruth:
    """The hidden half. Everything here is invisible to every policy."""

    # Recoverable at all? A business-source decline is not, by construction, and
    # that is the point: it is the one bucket where every contact is wasted and
    # the only correct action is to leave the customer alone.
    if failure is not None and failure.source == "business":
        return CaseTruth(
            case_id=case_id,
            recoverable=False,
            would_pay_unprompted=False,
            p_pay_if_contacted=0.0,
            p_pay_if_retried=0.0,
            best_wait_days=1,
            annoyance=0.18,
            note=f"business-source decline ({failure.reason}); merchant config, not customer",
        )

    if failure is not None:
        p_contact = rng.uniform(*failure.p_contact)
        p_retry = rng.uniform(*failure.p_retry)
        source = failure.source
        reason = failure.reason
    else:
        # No decline to reason from: a checkout that was walked away from, or an
        # invoice nobody has paid. Contact is the only lever, because no payment
        # was ever attempted and so there is nothing to retry.
        p_contact = (
            rng.uniform(0.22, 0.48)
            if bucket.kind == "abandoned_checkout"
            else rng.uniform(0.30, 0.62)
        )
        p_retry = 0.0
        source = "customer"
        reason = None

    # Loyal customers answer. A big ask is a harder ask.
    p_contact *= 0.85 + 0.35 * loyalty
    p_contact *= 1.0 if amount < 300_000 else 0.82
    p_contact = round(min(0.92, p_contact), 4)
    p_retry = round(p_retry, 4)

    # Would they have paid with no prompting at all? This is the false-positive
    # engine. Loyal customers and B2B invoice payers mostly get there on their own,
    # which is exactly why blasting everyone books recoveries it did not cause.
    p_unprompted = bucket.p_unprompted * (0.7 + 0.8 * loyalty)
    if source in ("bank", "gateway"):
        p_unprompted += 0.12          # they will try again themselves; it was a blip
    if reason == "card_expired":
        p_unprompted *= 0.35          # nothing self-corrects here, the card is dead
    would_pay_unprompted = rng.random() < min(0.75, p_unprompted)

    # Day of month drives the salary-day effect, and it is drawn here rather than
    # read off `detected_at` so the two stay independent draws from one generator.
    day_of_month = (AS_OF - dt.timedelta(days=rng.randint(0, 6))).day
    best_wait = _days_until_first(day_of_month) if reason == "insufficient_funds" else 1

    annoyance = round(rng.uniform(0.02, 0.09) + (0.06 if would_pay_unprompted else 0.0), 4)

    notes = [f"{source}-source {reason}" if reason else bucket.kind]
    if would_pay_unprompted:
        notes.append("would have paid unprompted")
    if p_retry > 0.4:
        notes.append("a silent retry probably fixes it")

    return CaseTruth(
        case_id=case_id,
        recoverable=True,
        would_pay_unprompted=would_pay_unprompted,
        p_pay_if_contacted=p_contact,
        p_pay_if_retried=p_retry,
        best_wait_days=best_wait,
        annoyance=annoyance,
        note="; ".join(notes),
    )


def fingerprint(rows: list[tuple[Case, CaseTruth]]) -> str:
    """A single hash over the whole cohort, so reproducibility is checkable.

    If a future refactor changes the generator by accident, this changes, the test
    fails, and we find out before publishing numbers that quietly moved.
    """
    material = "|".join(
        f"{c.id}:{c.kind}:{c.amount_paise}:{c.failure_reason or '-'}:"
        f"{t.recoverable}:{t.would_pay_unprompted}:{t.p_pay_if_contacted}"
        for c, t in rows
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def summarise(rows: list[tuple[Case, CaseTruth]]) -> dict:
    """The headline shape of the cohort. Used by the CLI and by the report card."""
    by_kind: dict[str, dict] = {}
    for case, _ in rows:
        entry = by_kind.setdefault(case.kind, {"count": 0, "paise": 0})
        entry["count"] += 1
        entry["paise"] += case.amount_paise

    unrecoverable = [(c, t) for c, t in rows if not t.recoverable]
    unprompted = [(c, t) for c, t in rows if t.would_pay_unprompted]

    return {
        "cases": len(rows),
        "at_risk_paise": sum(c.amount_paise for c, _ in rows),
        "by_kind": by_kind,
        "unrecoverable_cases": len(unrecoverable),
        "unrecoverable_paise": sum(c.amount_paise for c, _ in unrecoverable),
        "would_pay_unprompted_cases": len(unprompted),
        "would_pay_unprompted_paise": sum(c.amount_paise for c, _ in unprompted),
        "opted_out_cases": sum(
            1 for c, _ in rows if json.loads(c.meta_json)["customer"]["opted_out"]
        ),
        "fingerprint": fingerprint(rows),
    }


def load_into(session: Session, seed: int = SEED, size: int = COHORT_SIZE) -> dict:
    """Write the cohort to the database and record the fact in the ledger.

    The ledger entry carries the seed and the fingerprint, so the audit trail can
    answer "which cohort produced these numbers?" -- the question anyone checking
    a published result asks first.
    """
    rows = build_cohort(seed=seed, size=size)

    session.query(CaseTruth).delete()
    session.query(Case).delete()
    session.flush()

    for case, truth in rows:
        session.add(case)
        session.add(truth)
    session.commit()

    stats = summarise(rows)
    ledger.record(
        session,
        actor="system",
        event="cohort_generated",
        payload={
            "seed": seed,
            "size": size,
            "as_of": AS_OF.isoformat(),
            "fingerprint": stats["fingerprint"],
            "at_risk_paise": stats["at_risk_paise"],
            "unrecoverable_cases": stats["unrecoverable_cases"],
            "would_pay_unprompted_cases": stats["would_pay_unprompted_cases"],
        },
    )
    return stats
