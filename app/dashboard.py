"""Read-only HTTP views over what Recoup did. No endpoint here changes anything.

The dashboard exists to answer three questions a reviewer will actually ask:

  * what did it decide about *this* case, and why?
  * how does it compare to doing nothing, blasting everyone, and retrying?
  * can I see the audit trail, and is it intact?

Everything is served from the same database and the same `results/report_card.json`
the command-line report writes, so the web page cannot disagree with the terminal.
That is a small thing that matters: two code paths computing "recovered" separately
is how a dashboard ends up quoting a number nobody can reproduce.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ledger, watcher
from app.cohort import AS_OF
from app.config import PROJECT_ROOT
from app.db import get_session
from app.guard import RULES
from app.models import Case, LedgerEntry

router = APIRouter(tags=["dashboard"])

REPORT_PATH = PROJECT_ROOT / "results" / "report_card.json"
PAGE_PATH = Path(__file__).parent / "static" / "dashboard.html"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> str:
    if not PAGE_PATH.exists():
        return "<h1>Recoup</h1><p>Dashboard file missing.</p>"
    return PAGE_PATH.read_text(encoding="utf-8")


@router.get("/api/report")
def report_card() -> dict:
    """The published comparison. Written by `scripts/run_report_card.py`."""
    if not REPORT_PATH.exists():
        raise HTTPException(
            404,
            "no report card yet -- run `python -m scripts.run_report_card` first",
        )
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


@router.get("/api/cases")
def cases(
    limit: int = 100,
    kind: str | None = None,
    recoverability: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """The Watcher's view of the queue: what is at risk, worst first."""
    query = select(Case)
    if kind:
        query = query.where(Case.kind == kind)
    rows = list(session.execute(query).scalars().all())

    signals = watcher.scan(rows, as_of=AS_OF)
    if recoverability:
        signals = [s for s in signals if s.recoverability == recoverability]

    return {
        "summary": watcher.summarise(signals),
        "cases": [s.to_dict() for s in signals[:limit]],
    }


@router.get("/api/cases/{case_id}")
def case_detail(case_id: str, session: Session = Depends(get_session)) -> dict:
    """One case, everything known about it, and every decision made about it.

    This is the endpoint the video lingers on. A reviewer picks a case, sees the
    facts the Watcher extracted, the plan the model proposed with its reasoning,
    the Guard's verdict on each action with the rule it cited, and what happened.
    """
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(404, f"no case {case_id}")

    signal = watcher.assess(case, as_of=AS_OF)

    entries = list(
        session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.case_id == case_id)
            .order_by(LedgerEntry.id.asc())
        ).scalars().all()
    )

    return {
        "case": {
            "id": case.id,
            "kind": case.kind,
            "amount_paise": case.amount_paise,
            "status": case.status,
            "failure_reason": case.failure_reason,
            "detected_at": case.detected_at.isoformat() if case.detected_at else None,
            "meta": json.loads(case.meta_json or "{}"),
        },
        "signal": signal.to_dict(),
        "history": [
            {
                "id": e.id,
                "ts": e.ts,
                "actor": e.actor,
                "event": e.event,
                "payload": json.loads(e.payload_json),
            }
            for e in entries
        ],
    }


@router.get("/api/guard/rules")
def guard_rules() -> dict:
    """Every rule that can refuse a money action, and why it exists."""
    return {"rules": [{"rule": k, "why": v} for k, v in RULES.items()]}


@router.get("/api/audit")
def audit(limit: int = 40, session: Session = Depends(get_session)) -> dict:
    chain = ledger.verify_chain(session)
    entries = ledger.tail(session, limit=limit)
    return {
        "chain": {
            "intact": chain.ok,
            "entries": chain.entries,
            "broken_at": chain.broken_at,
            "detail": chain.detail,
        },
        "entries": [
            {
                "id": e.id,
                "ts": e.ts,
                "case_id": e.case_id,
                "actor": e.actor,
                "event": e.event,
                "payload": json.loads(e.payload_json),
                "entry_hash": e.entry_hash[:12],
                "prev_hash": e.prev_hash[:12],
            }
            for e in entries
        ],
    }


@router.get("/api/meta")
def meta(session: Session = Depends(get_session)) -> dict:
    """What this instance is running, for the header line on the page."""
    from app.config import get_settings
    from app.thinker import DEFAULT_MODEL

    settings = get_settings()
    return {
        "as_of": AS_OF.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cases": session.query(Case).count(),
        "model": DEFAULT_MODEL,
        "razorpay_configured": settings.razorpay_configured,
        "environment": settings.environment,
        "has_report": REPORT_PATH.exists(),
    }
