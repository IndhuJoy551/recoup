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
import re
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

# Chosen for the unit economics, not for the leaderboard. The median case in this
# cohort is worth about Rs 800, so a planner costing several paise and ten seconds
# per case is the wrong tool however well it reasons. This one answers in ~2s and
# still gets the error_source and salary-day judgements right, which the report
# card checks rather than assumes.
DEFAULT_MODEL = "openai/gpt-oss-120b"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def provider_for(model: str) -> str:
    """Which backend serves this model name.

    Two providers, not for redundancy theatre -- because one of them ran out of
    free-tier quota partway through building this, throttled to roughly a call a
    minute, and a 300-case batch stopped being possible. The planner was already
    behind one function, so a second implementation cost twenty lines. That is
    the argument for keeping model access behind an interface even in a
    thirteen-day project: the thing you cannot control is the vendor.
    """
    return "gemini" if "gemini" in model else "groq"

# Token counts in the report card are exact -- they come back from the API. The
# rupee figure is those exact counts times the rates below: published list prices
# in USD per million tokens, converted at Rs 87/USD. The assumption is stated here
# rather than buried in a constant so that anyone who disagrees with the rate can
# recompute the cost column instead of having to distrust it.
RUPEES_PER_USD = 87.0

USD_PER_M: dict[str, tuple[float, float]] = {          # model -> (input, output)
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.6-flash": (0.30, 2.50),
}
FALLBACK_USD_PER_M = (0.30, 2.50)                       # assume the expensive tier


def rates_for(model: str) -> tuple[float, float]:
    """Paise per 1000 input tokens, paise per 1000 output tokens."""
    usd_in, usd_out = USD_PER_M.get(model, FALLBACK_USD_PER_M)
    return (usd_in / 1000 * RUPEES_PER_USD * 100,
            usd_out / 1000 * RUPEES_PER_USD * 100)

TIMEOUT_SECONDS = 120.0
MAX_WORKERS = 3

# Client-side pacing, so the batch stops discovering the rate limit by hitting it.
# The free tier here allows 8,000 tokens a minute and a planning call measures
# ~1,230 tokens (1,105 in, 128 out, from the cache), which is about six and a half
# calls a minute. Three workers racing produced a steady stream of 429s, each one
# costing a full reset window; spacing the calls out instead means the retry path
# is for genuine failures rather than for arithmetic anyone could have done up
# front. Raise the budget when the account does.
TOKENS_PER_MINUTE = 8_000                       # Groq free tier

# Gemini's free tier is metered per request per minute rather than per token, so
# the budget that matters is a different one. Pacing is therefore per provider:
# reusing Groq's arithmetic here would idle at 13s a call for no reason.
GEMINI_REQUESTS_PER_MINUTE = 16

# `max_tokens` is a RESERVATION against the rate limit, not a cap on what you are
# charged. A 429 spelled it out: "Requested 2348" for a call whose prompt is
# ~1,150 tokens -- the other 1,200 were the output ceiling I had set, and the
# plans actually come back at a median of 128 tokens. So two thirds of every
# request's rate-limit cost bought tokens that were never generated. A three-step
# plan with reasons has never exceeded ~350.
MAX_OUTPUT_TOKENS = 600

# Gemini 3.x thinks before it answers, and `maxOutputTokens` is one budget shared
# between the thinking and the answer -- not a cap on the answer alone. At 600 the
# model spent 573 tokens reasoning, had 27 left, and returned JSON cut off
# mid-string. Every plan. `thinkingConfig.thinkingBudget = 0` is rejected outright
# by this model, so the only lever is headroom: observed thinking runs to ~1,040
# tokens and a plan is ~100, so 3,000 leaves roughly a 2x margin. Gemini bills
# tokens produced rather than tokens reserved, so unused headroom is free here --
# which is exactly the opposite of the Groq case above, and the reason this is a
# per-provider number instead of one constant.
GEMINI_MAX_OUTPUT_TOKENS = 3_000

# Prompt + reservation, which is what the limiter actually counts.
EST_TOKENS_PER_CALL = 1_750


class Pacer:
    """Lets one call start every `interval` seconds, across all worker threads."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_at)
            self._next_at = start_at + self.interval
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


def default_interval(model: str = DEFAULT_MODEL) -> float:
    """Seconds between calls, from whichever budget the provider actually meters."""
    if provider_for(model) == "gemini":
        return 60.0 / GEMINI_REQUESTS_PER_MINUTE
    return 60.0 / max(1.0, TOKENS_PER_MINUTE / EST_TOKENS_PER_CALL)


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
    model: str = ""

    @property
    def cost_paise(self) -> int:
        if self.source == "cache":
            return 0
        per_1k_in, per_1k_out = rates_for(self.model)
        return round(self.input_tokens / 1000 * per_1k_in
                     + self.output_tokens / 1000 * per_1k_out)


class LLMUnavailable(RuntimeError):
    """The model could not be reached or did not answer usefully."""


RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
MAX_RATE_LIMIT_WAIT = 70.0


def _parse_duration(text: str) -> float | None:
    """Turn "58.47s", "577ms", "1m12s" or "30" into seconds.

    `ms` has to be matched before `m`, which is the whole reason this is a regex
    and not a character loop: the loop version read "577ms" as 577 *minutes* and
    would have slept for nine and a half hours inside a retry.
    """
    text = (text or "").strip().lower()
    if not text:
        return None
    try:
        return float(text)                      # a bare number of seconds
    except ValueError:
        pass

    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    total = 0.0
    found = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", text):
        total += float(number) * units[unit]
        found = True
    return total if found else None


def _rate_limit_wait(response: httpx.Response, attempt: int) -> float:
    """How long to actually wait, preferring what the server told us.

    Exponential backoff is the right answer for an overloaded server and the
    wrong one for a rate limiter. A token bucket that refills once a minute does
    not care that you waited 0.75s, then 1.5s, then 3s -- all three fail, the
    call is abandoned, and the batch quietly falls back to rules for a case the
    model would have answered fine ten seconds later. That is what happened here:
    a 300-case warm-up crawled to a handful per round while the provider's own
    headers were saying exactly how long to wait.
    """
    for header in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        seconds = _parse_duration(response.headers.get(header, ""))
        if seconds is not None and 0 < seconds <= MAX_RATE_LIMIT_WAIT:
            return seconds + 1.0                # a beat past the reset, not exactly on it
    return min(MAX_RATE_LIMIT_WAIT, 0.75 * 2 ** (attempt - 1)) + random.random() * 0.25


def _call_groq(prompt: str, *, model: str, api_key: str, client: httpx.Client,
               attempt: int = 1) -> Reply:
    """OpenAI-compatible chat completions. Same contract as the Gemini path."""
    response = client.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "max_tokens": MAX_OUTPUT_TOKENS,
        },
        timeout=TIMEOUT_SECONDS,
    )

    if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
        time.sleep(_rate_limit_wait(response, attempt))
        return _call_groq(prompt, model=model, api_key=api_key,
                          client=client, attempt=attempt + 1)

    if response.status_code != 200:
        raise LLMUnavailable(
            f"{model} returned HTTP {response.status_code} after {attempt} attempt(s)"
        )

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise LLMUnavailable(f"{model} returned no choices")

    if choices[0].get("finish_reason") == "length":
        raise LLMUnavailable(
            f"{model} hit max_tokens; the answer is truncated, not an answer"
        )

    text = (choices[0].get("message", {}).get("content") or "").strip()
    if not text:
        raise LLMUnavailable(f"{model} returned an empty answer")

    usage = body.get("usage", {})
    return Reply(
        text=text, model=model,
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
    )


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
                "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
            },
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
        time.sleep(_rate_limit_wait(response, attempt))
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

    # A truncated answer is the dangerous failure, because it looks like a
    # success: valid HTTP, non-empty text, and JSON that dies on the last line.
    # Cached once, it is a permanent silent fallback for that case in every run
    # afterwards. So truncation is an error here, before anything can store it.
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        raise LLMUnavailable(
            f"{model} hit maxOutputTokens; the answer is truncated, not an answer"
        )

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
        text=text, model=model,
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

    pacer: Pacer | None = None
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
                text=cached["text"], model=self.model,
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                source="cache",
            )

        if self.offline:
            raise LLMUnavailable("offline: this case is not in the committed cache")

        settings = get_settings()
        provider = provider_for(self.model)
        api_key = (settings.gemini_api_key if provider == "gemini"
                   else settings.groq_api_key)
        if not api_key:
            raise LLMUnavailable(f"no API key configured for provider {provider!r}")

        if self.pacer is not None:
            self.pacer.wait()

        call = _call_gemini if provider == "gemini" else _call_groq
        reply = call(
            prompt, model=self.model,
            api_key=api_key, client=self._client_or_new(),
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
                progress: bool = True, pace_seconds: float | None = None) -> dict:
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

        previous_pacer = self.pacer
        interval = default_interval(self.model) if pace_seconds is None else pace_seconds
        self.pacer = self.pacer or Pacer(interval)

        errors: list[str] = []
        done = 0
        succeeded = 0
        lock = threading.Lock()

        def fetch(signal: Signal) -> None:
            nonlocal done, succeeded
            ok = False
            try:
                self.ask(signal)
                ok = True
            except Exception as exc:            # noqa: BLE001 - reported, not raised
                with lock:
                    errors.append(f"{signal.case_id}: {type(exc).__name__}: {str(exc)[:90]}")
            with lock:
                done += 1
                succeeded += int(ok)
                if done % 10 == 0:
                    # Checkpoint. A batch that dies at case 280 should not throw
                    # away 279 paid-for answers.
                    self.cache.flush()
                    if progress:
                        print(f"  thinker: {succeeded}/{done} of {len(missing)} planned "
                              f"({done - succeeded} failed)", flush=True)

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(fetch, missing))
        finally:
            self.pacer = previous_pacer

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
