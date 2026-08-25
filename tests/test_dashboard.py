"""The dashboard is read-only, and it must not be able to disagree with the terminal.

Two failure modes worth testing. One, an endpoint that quietly starts a batch --
a recovery run sends messages to real people and must never be one accidental GET
away. Two, a page that computes its own version of "recovered", which is how a
dashboard ends up quoting a number nobody can reproduce.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import cohort, report, runner
from app.db import SessionLocal
from app.main import app
from app.policies import get


@pytest.fixture
def client(session):
    cohort.load_into(session, size=30)
    with TestClient(app) as c:
        yield c


def test_the_page_loads_and_is_self_contained(client):
    """No CDN, no external script. It has to work on a laptop with no internet."""
    body = client.get("/").text
    assert "Recoup" in body
    assert "http://" not in body.replace("http://127.0.0.1", "")
    assert "https://" not in body
    assert "<script" in body


@pytest.mark.parametrize("path", [
    "/api/meta", "/api/cases?limit=5", "/api/guard/rules", "/api/audit?limit=5",
])
def test_the_read_endpoints_answer(client, path):
    assert client.get(path).status_code == 200


def test_no_endpoint_can_start_a_recovery_run(client):
    """A batch that contacts hundreds of people is a command-line operation."""
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        for verb in methods:
            if verb.lower() in ("post", "put", "patch", "delete"):
                assert path.startswith("/webhooks"), (
                    f"{verb.upper()} {path} is a write endpoint outside the webhook "
                    "receiver. Recovery runs must not be reachable over HTTP."
                )


def test_every_guard_rule_is_published_with_a_reason(client):
    from app.guard import RULES

    rules = client.get("/api/guard/rules").json()["rules"]
    assert {r["rule"] for r in rules} == set(RULES)
    assert all(r["why"] for r in rules)


def test_the_queue_is_the_watchers_view_and_carries_no_answers(client):
    payload = client.get("/api/cases?limit=50").json()
    assert payload["summary"]["cases"] > 0

    blob = json.dumps(payload)
    for forbidden in ("would_pay_unprompted", "p_pay_if_contacted", "recoverable\":"):
        assert forbidden not in blob, (
            "the dashboard must not leak the answer key; a reviewer reading the "
            "network tab would rightly stop believing the report card"
        )


def test_case_detail_shows_the_reasoning_and_the_guard_verdicts(client, session):
    with SessionLocal() as s:
        rows = runner.load_cohort(s)
        runner.run_policy(s, get("rules_only"), rows, audit=True)

    case_id = rows[0][0].id
    payload = client.get(f"/api/cases/{case_id}").json()

    assert payload["case"]["id"] == case_id
    assert "facts" in payload["signal"]

    handled = [h for h in payload["history"] if h["event"] == "case_handled"]
    assert handled, "a case that has been through a run must show that run"
    for decision in handled[0]["payload"]["decisions"]:
        assert "allowed" in decision
        assert decision["reason"] or decision["detail"]


def test_an_unknown_case_is_a_404_not_a_crash(client):
    assert client.get("/api/cases/case_does_not_exist").status_code == 404


def test_a_missing_report_card_says_how_to_make_one(client, monkeypatch, tmp_path):
    from app import dashboard

    monkeypatch.setattr(dashboard, "REPORT_PATH", tmp_path / "nope.json")
    response = client.get("/api/report")
    assert response.status_code == 404
    assert "run_report_card" in response.json()["detail"]


def test_the_report_endpoint_serves_exactly_what_the_terminal_computed(client, session):
    """One source of truth. The page reads the file the CLI wrote; it does not
    recompute anything, so the two cannot drift apart."""
    with SessionLocal() as s:
        rows = runner.load_cohort(s)
        results = [runner.run_policy(s, get(n), rows, audit=False)
                   for n in ("do_nothing", "rules_only")]
    card = report.build(results, rows)

    from app import dashboard
    dashboard.REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    original = dashboard.REPORT_PATH.read_text(encoding="utf-8") \
        if dashboard.REPORT_PATH.exists() else None
    try:
        dashboard.REPORT_PATH.write_text(json.dumps(card), encoding="utf-8")
        assert client.get("/api/report").json() == card
    finally:
        if original is not None:
            dashboard.REPORT_PATH.write_text(original, encoding="utf-8")


def test_the_audit_endpoint_reports_whether_the_chain_is_intact(client):
    chain = client.get("/api/audit?limit=5").json()["chain"]
    assert chain["intact"] is True
    assert chain["entries"] > 0
