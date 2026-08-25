"""Break Recoup on purpose, five ways, and show that nothing is lost or doubled.

    python -m scripts.break_it

Razorpay's brief asks for "one failure handled gracefully". This script does five,
because the interesting claim is not that one path has a try/except -- it is that
every way this system can fail has a defined, visible end state, and that none of
them ends with money counted twice or a case disappearing.

Nothing here talks to the real Razorpay API. Each scenario replaces one component
with a broken version and watches what the rest of the system does about it.

  1. Razorpay is down            -> backoff, then the circuit breaker trips
  2. The same webhook, three times -> exactly one recovery is recorded
  3. The planner proposes a refund -> refused at the parser, case escalated
  4. The planner goes down entirely -> every case still gets a plan
  5. Someone edits the audit trail -> the hash chain names the row

The last one is the only scenario where the correct behaviour is to fail loudly
rather than continue.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Point at a throwaway database BEFORE any app module is imported, because
# `app.db` builds its engine at import time from this setting. Scenario 4
# deliberately deletes and reloads the case table, and the first version of this
# script did that to the real one -- destroying the cohort the report card is
# computed from, in a script whose entire purpose is proving nothing gets lost.
_SCRATCH = Path(tempfile.mkdtemp(prefix="recoup-breakit-")) / "scratch.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_SCRATCH}"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ledger, thinker  # noqa: E402
from app.actions import UnknownAction, parse_plan  # noqa: E402
from app.cohort import AS_OF, build_cohort  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Case, LedgerEntry  # noqa: E402
from app.policies import Policy  # noqa: E402
from app.razorpay_client import CircuitOpen, RazorpayClient, RazorpayError  # noqa: E402
from app.watcher import scan  # noqa: E402

RULE = "=" * 78


def head(n: int, title: str) -> None:
    print(f"\n{RULE}\n  {n}. {title}\n{RULE}")


def ok(msg: str) -> None:
    print(f"     [handled] {msg}")


def bad(msg: str) -> None:
    print(f"     [FAILED]  {msg}")


# --------------------------------------------------------------------- 1


def razorpay_is_down() -> bool:
    """Every call returns 503. We should back off, then stop calling entirely."""
    head(1, "Razorpay returns 503 to everything")

    calls: list[float] = []

    def always_503(request: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        return httpx.Response(503, json={"error": {"description": "service unavailable"}})

    from app.config import Settings
    from app.razorpay_client import CircuitBreaker

    client = RazorpayClient(
        Settings(razorpay_key_id="rzp_test_break", razorpay_key_secret="x"),
        transport=httpx.MockTransport(always_503),
        max_attempts=4, base_delay=0.05,
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=60.0),
    )

    try:
        client.ping()
        bad("a 503 storm returned success, which cannot be right")
        return False
    except RazorpayError as exc:
        gaps = [round(b - a, 3) for a, b in zip(calls, calls[1:])]
        ok(f"gave up after {exc.attempts} attempts instead of hammering: "
           f"waits of {gaps}s between retries")

    # Now keep failing until the breaker opens.
    opened = False
    for _ in range(12):
        try:
            client.ping()
        except CircuitOpen as exc:
            ok(f"circuit breaker tripped: {exc}")
            opened = True
            break
        except RazorpayError:
            continue

    if not opened:
        bad("the circuit breaker never opened; an outage would cost us 300x this")
        return False

    before = len(calls)
    for _ in range(5):
        try:
            client.ping()
        except CircuitOpen:
            pass
    ok(f"while open, 5 further calls made {len(calls) - before} network requests "
       "-- an outage now costs one call a minute, not four per case")
    client.close()
    return True


# --------------------------------------------------------------------- 2


def the_same_webhook_three_times() -> bool:
    """At-least-once delivery is Razorpay's contract, not a bug. One recovery only."""
    head(2, "Razorpay delivers the same payment.captured three times")

    init_db()
    secret = "break_it_secret"

    from app.config import get_settings
    settings = get_settings()
    original = settings.razorpay_webhook_secret
    settings.razorpay_webhook_secret = secret

    body = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_break_it_001", "amount": 120_000, "currency": "INR",
            "status": "captured", "notes": {"recoup_case": "case_0001"},
        }}},
    }
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    try:
        with TestClient(app) as client:
            statuses = []
            for _ in range(3):
                response = client.post(
                    "/webhooks/razorpay",
                    content=raw,
                    headers={
                        "X-Razorpay-Signature": signature,
                        "X-Razorpay-Event-Id": "evt_break_it_001",
                        "content-type": "application/json",
                    },
                )
                statuses.append(response.status_code)

        with SessionLocal() as session:
            stored = session.query(
                __import__("app.models", fromlist=["WebhookEvent"]).WebhookEvent
            ).filter_by(event_id="evt_break_it_001").count()

        if statuses != [200, 200, 200]:
            bad(f"replies were {statuses}; a non-200 makes Razorpay retry forever")
            return False
        ok(f"all three deliveries answered {statuses} -- a 500 would have caused "
           "a fourth, a fifth, and a sixth")
        if stored != 1:
            bad(f"{stored} rows stored for one event id; the money would be counted twice")
            return False
        ok("exactly one row stored. The unique constraint on event_id *is* the "
           "idempotency check, and the failed insert is supposed to happen")
        return True
    finally:
        settings.razorpay_webhook_secret = original


# --------------------------------------------------------------------- 3


def the_planner_asks_for_a_refund() -> bool:
    """The failure this project exists to make boring."""
    head(3, "The planner proposes issuing a refund, and a 400-day delay")

    for payload, label in [
        ({"plan": [{"action": "issue_refund", "wait_days": 0,
                    "reason": "the customer seems upset"}]}, "an action that does not exist"),
        ({"plan": [{"action": "send_payment_link", "wait_days": 400,
                    "reason": "eventually"}]}, "a real action with an impossible time"),
        ({"plan": [{"action": "send_reminder"}] * 9}, "nine attempts on one case"),
    ]:
        try:
            parse_plan(payload["plan"])
            bad(f"{label} was accepted")
            return False
        except UnknownAction as exc:
            ok(f"{label}: refused at the parser -- {str(exc)[:90]}")

    ok("no refused proposal is partially honoured, and none reaches the Doer")
    return True


# --------------------------------------------------------------------- 4


def the_planner_disappears() -> bool:
    """The model is unreachable for the whole batch. Every case still gets a plan."""
    head(4, "The planning model is unreachable for every single case")

    from app import runner

    rows = build_cohort(size=40)
    signals = scan([case for case, _ in rows], as_of=AS_OF)

    brain = thinker.Thinker(offline=True, cache=thinker.Cache(
        __import__("pathlib").Path("data") / "nonexistent_cache.json"
    ))
    policy = Policy("recoup_offline", brain.plan, gated=True, blurb="t", uses_llm=True)

    init_db()
    with SessionLocal() as session:
        session.query(__import__("app.models", fromlist=["CaseTruth"]).CaseTruth).delete()
        session.query(Case).delete()
        for case, truth in rows:
            session.add(case)
            session.add(truth)
        session.commit()

        loaded = runner.load_cohort(session)
        result = runner.run_policy(session, policy, loaded, audit=False)

    if len(result.outcomes) != len(rows):
        bad(f"{len(rows) - len(result.outcomes)} cases were dropped")
        return False

    ok(f"all {len(result.outcomes)} cases still planned, via the rules fallback")
    ok(f"{brain.fallbacks} fallbacks counted and reported -- a fallback nobody "
       "counts is a cover-up, not a fallback")

    if brain.fallbacks != len(rows):
        bad("the fallback count does not match the number of cases")
        return False
    return True


# --------------------------------------------------------------------- 5


def someone_edits_the_audit_trail() -> bool:
    """The one failure where continuing would be the wrong answer.

    Two independent layers, so this is shown twice. The database refuses the
    edit outright. And if someone got past the database -- edited the file, or
    restored a doctored backup -- the hash chain still names the row.
    """
    head(5, "Someone edits a historical row in the audit trail")

    init_db()
    with SessionLocal() as session:
        for i in range(4):
            ledger.record(session, actor="system", event="break_it_probe",
                          payload={"i": i})

        status = ledger.verify_chain(session)
        if not status.ok:
            bad(f"the chain was already broken: {status.detail}")
            return False
        ok(f"chain intact across {status.entries} entries")

        target = (
            session.query(LedgerEntry)
            .filter_by(event="break_it_probe")
            .order_by(LedgerEntry.id.asc())
            .first()
        )
        target_id = target.id

    # ---- layer one: the database will not do it -----------------------------
    from sqlalchemy import text
    from app.db import engine

    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ledger SET payload_json = :p WHERE id = :i"),
                {"p": '{"i":999}', "i": target_id},
            )
        bad("the append-only trigger allowed an UPDATE")
        return False
    except Exception as exc:                           # noqa: BLE001
        detail = str(exc).splitlines()[0]
        ok(f"layer 1, the database refused it: {detail[-70:]}")

    # ---- layer two: suppose they got past it --------------------------------
    # A copy of the schema with the triggers deliberately left off, standing in
    # for an attacker who edited the file directly or restored a doctored
    # backup. The triggers cannot help there. The hash chain still can.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    scratch = Path(tempfile.mkdtemp(prefix="recoup-tamper-")) / "unprotected.db"
    unprotected = create_engine(f"sqlite:///{scratch}", future=True)
    Base.metadata.create_all(unprotected)              # tables only; no triggers
    Scratch = sessionmaker(bind=unprotected, expire_on_commit=False)

    with Scratch() as session:
        for i in range(4):
            ledger.record(session, actor="system", event="probe", payload={"i": i})

        victim = session.query(LedgerEntry).order_by(LedgerEntry.id.asc()).offset(1).first()
        with unprotected.begin() as conn:
            conn.execute(
                text("UPDATE ledger SET payload_json = :p WHERE id = :i"),
                {"p": '{"i":"tampered"}', "i": victim.id},
            )
        ok("layer 2, on a copy with the triggers removed, the UPDATE succeeded")

        session.expire_all()
        after = ledger.verify_chain(session)

    unprotected.dispose()

    if after.ok:
        bad("the hash chain did not notice a rewritten payload")
        return False

    ok(f"...and the hash chain caught it anyway: entry {after.broken_at}, "
       f"{after.detail}")
    ok("tampering is detectable rather than merely discouraged, and the report "
       "says WHERE it started")
    return True


# ---------------------------------------------------------------------


def main() -> int:
    print("\n  Recoup: breaking it on purpose")
    print("  Nothing below talks to the real Razorpay API.")

    scenarios = [
        razorpay_is_down,
        the_same_webhook_three_times,
        the_planner_asks_for_a_refund,
        the_planner_disappears,
        someone_edits_the_audit_trail,
    ]
    results = [scenario() for scenario in scenarios]

    print(f"\n{RULE}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} failures handled as designed")
    if passed == len(results):
        print("  No money double-counted. No case lost. Three end states: recovered,")
        print("  parked for a human, or explicitly given up on with a reason.")
    print(RULE)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
