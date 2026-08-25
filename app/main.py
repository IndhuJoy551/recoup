"""The server: webhooks in, dashboard out.

Two jobs, and both of them are about being observable from outside the process.
Razorpay needs somewhere to deliver payment events, and a reviewer needs a page
they can click through without reading any Python.

The routes here are read-only apart from the webhook receiver. Recovery runs are
started from the command line (`scripts/run_report_card.py`), not from an HTTP
endpoint, because a batch that sends messages to three hundred people should not
be one accidental GET away.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app import dashboard, ledger, webhooks
from app.config import get_settings
from app.db import get_session, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with next(get_session()) as session:
        ledger.record(
            session,
            actor="system",
            event="server_started",
            payload={"environment": get_settings().environment},
        )
    yield


app = FastAPI(
    title="Recoup",
    description="Revenue recovery agent for Razorpay merchants.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhooks.router)
app.include_router(dashboard.router)


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    """Is everything wired up? Reports presence of credentials, never their values."""
    settings = get_settings()
    chain = ledger.verify_chain(session)
    return {
        "status": "ok",
        "environment": settings.environment,
        "razorpay_configured": settings.razorpay_configured,
        "llm_provider": settings.llm_provider,
        "llm_configured": settings.llm_configured,
        "ledger": {
            "entries": chain.entries,
            "chain_intact": chain.ok,
            "detail": chain.detail,
        },
    }


@app.get("/ledger")
def read_ledger(limit: int = 50, session: Session = Depends(get_session)) -> dict:
    """The audit trail, newest first."""
    entries = ledger.tail(session, limit=limit)
    return {
        "count": len(entries),
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
