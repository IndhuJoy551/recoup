"""The Thinker: the one component in Recoup that is a language model.

It reads the facts the Watcher assembled about a single case and proposes a plan
of one to three actions. It proposes. It cannot act, cannot see `CaseTruth`,
cannot reach the database, and cannot call Razorpay. Its entire output is a small
JSON object that has to survive `actions.parse_plan()` before anything downstream
will look at it.

Why an LLM here and nowhere else
--------------------------------
The honest test for whether a model earns its place is: *could an `if` statement
do this?* For most of this project the answer is yes, so there is no model there.
Here the answer is genuinely no, for one reason -- the useful information is in
prose. `error_description` is a sentence written for a customer. An invoice has
notes. The relationship history is "eight purchases, none in the last five
months, contacted twice recently about money". Weighing that against a rupee
amount and a calendar is the shape of problem language models are actually good
at, and the shape hand-written rules get brittle at.

The rules-only policy exists to check that claim rather than assert it, and the
ablation publishes whichever wins. If the rules win, the honest conclusion is
that the model was decoration here, and that goes in the report.

Determinism, and why the cache is committed
-------------------------------------------
Temperature is zero, but zero-temperature is not a guarantee -- providers reserve
the right to change a model behind a name, and "re-run and get the same numbers"
is submission checklist item 2. So every response is cached to disk, keyed by a
hash of the exact prompt and model name, and `data/thinker_cache.json` is
committed to the repository. A reviewer with no API key clones the repo, runs the
report card, and gets the published numbers back. A reviewer *with* a key can
delete the cache and watch it rebuild.

Failure is a first-class path
-----------------------------
Network error, timeout, malformed JSON, an action outside the vocabulary, a plan
longer than the stopping rule -- all of them land in one place: fall back to the
rules-only planner for that case, mark the case `fallback=True`, and carry on.
The batch does not stop, no case is silently dropped, and the report card prints
the fallback count. A system that only works when the model behaves is not a
system, it is a demo.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.actions import Action, MAX_PLAN_LENGTH, UnknownAction, parse_plan
from app.config import PROJECT_ROOT, get_settings
from app.guard import (
    MAX_ATTEMPTS_PER_CASE,
    MAX_CONTACTS_PER_CUSTOMER_PER_WEEK,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
)
from app.policies import Policy, plan_rules_only
from app.watcher import Signal

CACHE_PATH = PROJECT_ROOT / "data" / "thinker_cache.json"

# Chosen for the unit economics, not for the leaderboard. The median case in
# this cohort is worth about Rs 800, so a planner that costs several paise per
# case and takes ten seconds is the wrong tool regardless of how well it reasons.
# This tier answers in ~1.5s, emits no billed reasoning tokens, and still gets
# the error_source and salary-day judgements right -- which the report card
# checks rather than assumes.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Token counts in the report card are exact -- they come back from the API. The
# rupee figure is those exact counts multiplied by the rate below, which is the
# published list price for the Gemini Flash tier converted at Rs 87/USD. Stating
# the assumption here rather than burying a number means the cost column can be
# recomputed by anyone who disagrees with the rate.
USD_PER_M_INPUT = 0.30
USD_PER_M_OUTPUT = 2.50
RUPEES_PER_USD = 87.0

INPUT_PAISE_PER_1K = USD_PER_M_INPUT / 1000 * RUPEES_PER_USD * 100
OUTPUT_PAISE_PER_1K = USD_PER_M_OUTPUT / 1000 * RUPEES_PER_USD * 100

TIMEOUT_SECONDS = 45.0
MAX_WORKERS = 6


SYSTEM_PROMPT = f"""\
You plan revenue-recovery actions for an Indian business that uses Razorpay.
You are given the facts about ONE unpaid or failed case. Propose the plan with
the best chance of getting the money, without harming the customer relationship.

A plan is a LADDER, not a list of things that all happen. Steps run in order and
each one only fires if the money has not arrived yet, so a second step costs
nothing at all when the first one works. Most cases deserve two steps: an opening
move, and one follow-up on a different day if it goes quiet. Use one step when
further contact would do more harm than good, and three only when the case is
genuinely worth it.

You may only use these six actions. Nothing else exists.

  send_payment_link    A message with a one-tap link to pay. Highest conversion,
                       and the right answer when the customer must supply a new
                       instrument (expired card) or simply never finished.
  send_reminder        A lighter nudge with no link. Lower conversion, less
                       pressure. Right for B2B invoices stuck in an approvals
                       queue, and for a good customer you do not want to push.
  schedule_retry       Silently re-attempt the charge. Contacts nobody, annoys
                       nobody, costs nothing. Only works when the instrument is
                       still valid and the failure was not the customer's choice
                       -- bank outages, gateway errors, and a balance that will
                       be topped up on salary day.
  offer_installments   Split a large balance into parts. Only helps when the
                       amount itself is the obstacle.
  escalate_to_human    Hand the case to a person. Use for high-value cases, and
                       for failures the customer cannot fix.
  do_nothing           The correct answer more often than it looks. Use it when
                       contact would cost more than it could earn.

Rules that will be enforced after you answer, so plan inside them:
  - No contact before {QUIET_HOURS_END:02d}:00 or at/after {QUIET_HOURS_START:02d}:00 IST.
  - At most {MAX_CONTACTS_PER_CUSTOMER_PER_WEEK} contacts per customer per 7 days.
  - At most {MAX_ATTEMPTS_PER_CASE} attempts on one case, ever.
  - At least 24 hours between two attempts on the same case.
  - If the customer has opted out, no contact of any kind is permitted.
  - If recoverability is "unrecoverable", no customer-facing action can work.

Judgement you are expected to apply:
  - Timing is most of the decision. A payment that failed for insufficient funds
    is a different case after salary day; retrying into the same empty account
    before then usually fails the same way.
  - error_source tells you whose problem it is. "customer" responds to being
    asked. "bank" and "gateway" respond to a quiet retry and NOT to a message
    about someone else's outage. "business" is our own configuration and no
    customer action can fix it.
  - An expired card or a revoked mandate will fail every retry identically.
  - Every message carries a real chance the customer opts out permanently. A
    plan of three messages is not three times better than one -- but a plan of
    one message, sent at the wrong moment, is often worth nothing.
  - A silent retry is free and cannot annoy anybody. When the instrument is
    still valid, put it before any message rather than instead of one: retry
    first, and keep a message in reserve for when the retry does not clear it.
  - Do not put every step on day zero. Attempts on the same case must be at
    least 24 hours apart, and a follow-up sent too soon reads as pressure.

Answer with JSON only:
{{"plan": [{{"action": "...", "wait_days": 0, "hour_ist": 11, "reason": "..."}}],
 "case_summary": "one sentence on what is going on with this case"}}

At most {MAX_PLAN_LENGTH} actions. `reason` must say WHY this action at this
time for this case -- it goes into an audit trail a human will read.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "case_summary": {"type": "string"},
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "wait_days": {"type": "integer"},
                    "hour_ist": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "wait_days", "hour_ist", "reason"],
            },
        },
    },
    "required": ["plan"],
}


# --------------------------------------------------------------------- cache


class Cache:
    """Prompt hash -> model response. Committed to the repo; see module docstring."""

    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # A half-written cache is not worth a crash. Rebuild it.
                self._data = {}

    def key(self, model: str, prompt: str, system: str = "") -> str:
        """Identity of a planning call: the model, the instructions, and the case.

        The first version keyed on model plus case only. Editing SYSTEM_PROMPT
        therefore changed what the planner was told and changed nothing about
        which cached answer came back -- so a prompt fix appeared to do nothing,
        and worse, a committed cache would claim to reproduce numbers that a
        fresh run could never produce. A cache key has to cover every input, and
        the instructions are an input.
        """
        fingerprint = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
        material = f"{model}\n{fingerprint}\n{prompt}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8"
            )
            self._dirty = False

    def __len__(self) -> int:
        return len(self._data)


# -------------------------------------------------------------------- client


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    source: str = "api"          # api | cache

    @property
    def cost_paise(self) -> int:
        if self.source == "cache":
            return 0
        return round(
            self.input_tokens / 1000 * INPUT_PAISE_PER_1K
            + self.output_tokens / 1000 * OUTPUT_PAISE_PER_1K
        )


class LLMUnavailable(RuntimeError):
    """The model could not be reached or did not answer usefully."""


RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def _call_gemini(prompt: str, *, model: str, api_key: str, client: httpx.Client,
                 attempt: int = 1) -> Reply:
    """One planning call, with backoff on the statuses that mean "try later".

    Eight concurrent workers against a rate-limited endpoint will get 429s, and a
    429 is not a model failure -- falling back to the rules for it would quietly
    turn a throughput problem into a headline about the model being useless. The
    retry ladder here mirrors the one in `razorpay_client`; the difference is that
    exhausting it is allowed to be fatal, because a cache miss is recoverable and
    a wrong ablation is not.
    """
    response = client.post(
        GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "maxOutputTokens": 2048,
            },
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
        time.sleep(min(8.0, 0.75 * 2 ** (attempt - 1)) + random.random() * 0.25)
        return _call_gemini(prompt, model=model, api_key=api_key,
                            client=client, attempt=attempt + 1)

    if response.status_code != 200:
        raise LLMUnavailable(
            f"{model} returned HTTP {response.status_code} after {attempt} attempt(s)"
        )

    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise LLMUnavailable(f"{model} returned no candidates")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise LLMUnavailable(f"{model} returned an empty answer")

    usage = body.get("usageMetadata", {})
    # Reasoning models bill their internal thinking as output. On a trivial probe
    # this model produced 5 answer tokens and 231 thought tokens, so counting only
    # `candidatesTokenCount` would have under-reported the cost of the batch by
    # something like forty times. The cost column is supposed to be the one nobody
    # else publishes; publishing a flattering version of it would be worse than
    # leaving it out.
    return Reply(
        text=text,
        input_tokens=int(usage.get("promptTokenCount", 0)),
        output_tokens=int(usage.get("candidatesTokenCount", 0))
        + int(usage.get("thoughtsTokenCount", 0)),
    )


# ------------------------------------------------------------------- planner


@dataclass
class Thinker:
    """A planner backed by a language model, with the rules as its parachute."""

    model: str = DEFAULT_MODEL
    cache: Cache = field(default_factory=Cache)
    offline: bool = False            # cache-only; never opens a socket
    last_call: dict = field(default_factory=dict)

    calls: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    cost_paise: int = 0
    _client: httpx.Client | None = field(default=None, repr=False)

    # ------------------------------------------------------------ prompting

    def prompt_for(self, signal: Signal) -> str:
        return json.dumps(signal.to_dict(), indent=1, sort_keys=True)

    # -------------------------------------------------------------- calling

    def _client_or_new(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self.cache.flush()

    def ask(self, signal: Signal) -> Reply:
        """Cache first, then the API. Raises LLMUnavailable rather than guessing."""
        prompt = self.prompt_for(signal)
        key = self.cache.key(self.model, prompt, SYSTEM_PROMPT)

        cached = self.cache.get(key)
        if cached is not None:
            return Reply(
                text=cached["text"],
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                source="cache",
            )

        if self.offline:
            raise LLMUnavailable("offline: this case is not in the committed cache")

        settings = get_settings()
        if not settings.gemini_api_key:
            raise LLMUnavailable("no GEMINI_API_KEY configured")

        reply = _call_gemini(
            prompt, model=self.model,
            api_key=settings.gemini_api_key, client=self._client_or_new(),
        )
        self.cache.put(key, {
            "text": reply.text,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        })
        return reply

    # --------------------------------------------------------------- public

    def plan(self, signal: Signal) -> list[Action]:
        """Propose a plan for one case. Never raises; degrades to rules."""
        try:
            reply = self.ask(signal)
        except (LLMUnavailable, httpx.HTTPError, json.JSONDecodeError) as exc:
            return self._fall_back(signal, f"{type(exc).__name__}: {exc}")

        try:
            payload = json.loads(reply.text)
            plan = parse_plan(payload.get("plan", payload))
        except (json.JSONDecodeError, UnknownAction, TypeError) as exc:
            # The most interesting failure in the system: the model asked for
            # something outside the vocabulary, or produced something unparseable.
            # It is counted, named, and never partially honoured.
            return self._fall_back(signal, f"rejected: {exc}", rejected=True)

        if reply.source == "cache":
            self.cache_hits += 1
        else:
            self.calls += 1
            self.cost_paise += reply.cost_paise

        self.last_call = {
            "source": reply.source,
            "model": self.model,
            "cost_paise": reply.cost_paise,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "case_summary": Action._clean(
                str(json.loads(reply.text).get("case_summary") or "")
            )[:300],
            "fallback": False,
        }
        return plan

    def _fall_back(self, signal: Signal, why: str, *, rejected: bool = False) -> list[Action]:
        self.fallbacks += 1
        self.last_call = {
            "source": "fallback",
            "model": self.model,
            "cost_paise": 0,
            "fallback": True,
            "rejected_by_parser": rejected,
            "why": why[:300],
        }
        return plan_rules_only(signal)

    # -------------------------------------------------------------- warm-up

    def prewarm(self, signals: list[Signal], *, workers: int = MAX_WORKERS,
                progress: bool = True) -> dict:
        """Fill the cache concurrently before the sequential run.

        The runner walks cases one at a time on purpose -- the Guard is stateful,
        and "how many times have we contacted this customer this week" only means
        something in a fixed order. But 300 sequential HTTPS round trips is eight
        minutes of staring at a terminal. So the network part is done first and in
        parallel, and the ordered part then runs against a warm cache.
        """
        missing = [
            s for s in signals
            if self.cache.get(
                self.cache.key(self.model, self.prompt_for(s), SYSTEM_PROMPT)
            ) is None
        ]
        if not missing or self.offline:
            return {"requested": 0, "cached_already": len(signals) - len(missing)}

        errors: list[str] = []
        done = 0
        lock = threading.Lock()

        def fetch(signal: Signal) -> None:
            nonlocal done
            try:
                self.ask(signal)
            except Exception as exc:            # noqa: BLE001 - reported, not raised
                with lock:
                    errors.append(f"{signal.case_id}: {type(exc).__name__}")
            with lock:
                done += 1
                if done % 25 == 0:
                    # Checkpoint. A batch that dies at case 280 should not throw
                    # away 279 paid-for answers.
                    self.cache.flush()
                    if progress:
                        print(f"  thinker: {done}/{len(missing)} planned", flush=True)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, missing))

        self.cache.flush()

        # If every single call failed, the run that follows will quietly fall
        # back to the rules for all 300 cases and produce an "AI on" column that
        # is really the "AI off" column with a different label. That is a worse
        # outcome than crashing, so it is raised rather than reported.
        if errors and len(errors) == len(missing):
            raise LLMUnavailable(
                f"all {len(missing)} planning calls to {self.model} failed "
                f"(first: {errors[0]}). Refusing to publish an ablation where "
                "the model never actually ran."
            )

        return {
            "requested": len(missing),
            "cached_already": len(signals) - len(missing),
            "errors": len(errors),
            "first_errors": errors[:5],
        }


def build_policy(
    *, model: str | None = None, offline: bool | None = None, name: str = "recoup"
) -> tuple[Policy, Thinker]:
    """The agent, as a `Policy` the runner can treat like any other.

    Returned alongside its Thinker so the caller can pre-warm the cache and read
    the token counts afterwards. `RECOUP_OFFLINE=1` forces cache-only, which is
    how the test suite runs and how a reviewer without a key reproduces the
    published numbers.
    """
    thinker = Thinker(
        model=model or os.environ.get("RECOUP_MODEL", DEFAULT_MODEL),
        offline=offline if offline is not None
        else os.environ.get("RECOUP_OFFLINE", "") == "1",
    )
    policy = Policy(
        name=name,
        plan=thinker.plan,
        gated=True,
        blurb="Watcher -> LLM planner -> Guard -> Doer. The model proposes; the "
              "Guard disposes.",
        uses_llm=True,
    )
    return policy, thinker
