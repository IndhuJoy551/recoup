"""The planner is the untrusted component, so these tests are mostly about refusal.

Nothing here calls the API. The model is replaced with fakes that return the
things a real model actually returns on a bad day -- an invented action, a
truncated JSON object, an HTTP 500, a plan of nine steps -- and the question in
every case is the same: does the system stay honest and keep going?
"""

import json

import httpx
import pytest

from app import thinker
from app.actions import Action
from app.cohort import AS_OF, build_cohort
from app.thinker import Cache, LLMUnavailable, Thinker
from app.watcher import scan


@pytest.fixture
def signal():
    rows = build_cohort(size=8)
    return scan([case for case, _ in rows], as_of=AS_OF)[0]


@pytest.fixture
def brain(tmp_path):
    return Thinker(cache=Cache(tmp_path / "cache.json"), offline=True)


def reply(payload: dict) -> str:
    return json.dumps(payload)


def use_fake_model(monkeypatch, brain, fn) -> None:
    """Replace whichever provider backs the current model, and both keys.

    Patched provider-agnostically on purpose. An earlier version patched only
    the Gemini call; when the default model moved to the other provider these
    tests silently started making real network calls -- which is how a unit
    suite goes from 11 seconds to 108 and nobody notices what it is doing.
    """
    monkeypatch.setattr(thinker, "_call_gemini", fn)
    monkeypatch.setattr(thinker, "_call_groq", fn)
    settings = thinker.get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "groq_api_key", "test-key", raising=False)
    brain.offline = False


# ------------------------------------------------------------------- cache


def test_the_cache_key_covers_the_instructions_as_well_as_the_case(brain):
    """A prompt change must invalidate the cache, or a fix appears to do nothing.

    Worse than nothing: the committed cache would promise numbers that a fresh
    run with the current prompt could never reproduce. See BUGLOG.
    """
    a = brain.cache.key("m", "case", "instructions v1")
    b = brain.cache.key("m", "case", "instructions v2")
    assert a != b


def test_the_cache_key_changes_with_the_model(brain):
    assert brain.cache.key("m1", "case", "s") != brain.cache.key("m2", "case", "s")


def test_a_corrupt_cache_file_is_rebuilt_rather_than_fatal(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert len(Cache(path)) == 0


def test_offline_mode_never_opens_a_socket(brain, signal, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("offline mode made a network call")

    monkeypatch.setattr(thinker, "_call_gemini", explode)
    brain.plan(signal)              # falls back to rules; must not raise
    assert brain.fallbacks == 1


# ----------------------------------------------------------- bad model output


@pytest.mark.parametrize("bad, why", [
    ({"plan": [{"action": "issue_refund", "wait_days": 0}]}, "invented an action"),
    ({"plan": [{"action": "send_reminder", "wait_days": 400}]}, "absurd delay"),
    ({"plan": [{"action": "send_reminder", "hour_ist": 99}]}, "not an hour"),
    ({"plan": [{"action": "send_reminder"}] * 9}, "longer than the stopping rule"),
    ({"plan": "send a reminder I guess"}, "not a list"),
    ({"nope": []}, "no plan at all"),
])
def test_bad_proposals_are_refused_and_the_case_falls_back_to_rules(
    brain, signal, monkeypatch, bad, why
):
    use_fake_model(monkeypatch, brain, lambda *a, **k: thinker.Reply(text=reply(bad)))

    plan = brain.plan(signal)

    assert plan, "a refused proposal must still leave the case with a plan"
    assert all(isinstance(a, Action) for a in plan)
    assert brain.fallbacks == 1
    assert brain.last_call["fallback"] is True
    assert brain.last_call["rejected_by_parser"] is True, why


def test_truncated_json_is_survived(brain, signal, monkeypatch):
    use_fake_model(monkeypatch, brain,
                   lambda *a, **k: thinker.Reply(text='{"plan": [{"action": "send_rem'))

    assert brain.plan(signal)
    assert brain.fallbacks == 1


def test_a_network_failure_is_survived(brain, signal, monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    use_fake_model(monkeypatch, brain, boom)

    assert brain.plan(signal)
    assert brain.fallbacks == 1
    assert "ConnectError" in brain.last_call["why"]


def test_a_good_proposal_is_accepted_and_cached(brain, signal, monkeypatch):
    good = {
        "case_summary": "a test case",
        "plan": [{"action": "schedule_retry", "wait_days": 1, "hour_ist": 10,
                  "reason": "the bank was down"}],
    }
    calls = {"n": 0}

    def once(*args, **kwargs):
        calls["n"] += 1
        return thinker.Reply(text=reply(good), input_tokens=900, output_tokens=120)

    use_fake_model(monkeypatch, brain, once)

    first = brain.plan(signal)
    assert [a.kind for a in first] == ["schedule_retry"]
    assert brain.calls == 1 and brain.cost_paise > 0

    second = brain.plan(signal)
    assert second == first
    assert calls["n"] == 1, "the second ask must be served from the cache"
    assert brain.cache_hits == 1


# ------------------------------------------------------------------- cost


def test_reasoning_tokens_are_billed_as_output(monkeypatch):
    """Under-reporting the cost column would be worse than omitting it."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "candidates": [{"content": {"parts": [{"text": '{"plan": []}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 1000,
                    "candidatesTokenCount": 100,
                    "thoughtsTokenCount": 900,
                },
            }

    class FakeClient:
        def post(self, *args, **kwargs):
            captured["called"] = True
            return FakeResponse()

    got = thinker._call_gemini("p", model="gemini-3.6-flash", api_key="k",
                               client=FakeClient())
    assert got.output_tokens == 1000, "thinking tokens are output tokens"
    assert captured["called"]


def test_a_total_outage_raises_instead_of_quietly_becoming_the_rules(brain, monkeypatch):
    """The bug that nearly shipped a fake ablation. See BUGLOG.

    If every planning call fails, the run that follows is the rules-only policy
    wearing an "AI on" label, and the ablation would print a confident dead heat.
    """
    rows = build_cohort(size=12)
    signals = scan([case for case, _ in rows], as_of=AS_OF)

    def always_fail(*args, **kwargs):
        raise LLMUnavailable("HTTP 429")

    use_fake_model(monkeypatch, brain, always_fail)

    with pytest.raises(LLMUnavailable, match="Refusing to publish"):
        brain.prewarm(signals, workers=2, progress=False)


def test_a_partial_outage_does_not_raise(brain, monkeypatch):
    """One failed call is a degraded run; every failed call is a broken one."""
    rows = build_cohort(size=12)
    signals = scan([case for case, _ in rows], as_of=AS_OF)
    seen = {"n": 0}

    def flaky(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] % 3 == 0:
            raise LLMUnavailable("HTTP 500")
        return thinker.Reply(text=reply({"plan": [{"action": "do_nothing",
                                                   "wait_days": 0, "hour_ist": 10,
                                                   "reason": "x"}]}))

    use_fake_model(monkeypatch, brain, flaky)

    stats = brain.prewarm(signals, workers=1, progress=False)
    assert 0 < stats["errors"] < stats["requested"]


# ------------------------------------------------------------------ wiring


def test_the_agent_is_a_gated_policy_like_any_other():
    policy, brain = thinker.build_policy(offline=True)
    assert policy.gated, "the model's proposals go through the same gate as everything else"
    assert policy.uses_llm
    assert policy.plan.__self__ is brain
