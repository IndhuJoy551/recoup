# Recoup — architecture

This document explains *why* the pieces are shaped the way they are. For how to run it, see
[README.md](README.md). For what broke along the way, see [BUGLOG.md](BUGLOG.md).

---

## The constraint everything follows from

An LLM that makes a mistake in prose writes a bad sentence. An LLM that makes a mistake with
money moves rupees that belong to someone else. So the design question is not "how do I get the
model to behave" — a prompt is a request, not a control — but **"what is still true when the
model misbehaves?"**

Three answers, and each one is a component:

| If the model… | …then |
|---|---|
| invents an action | the parser refuses it. The vocabulary is closed and typed |
| proposes a legal action that is not allowed | the Guard refuses it, citing a named rule |
| is unreachable, or wrong for 300 cases running | the batch completes on rules, and every fallback is counted |

None of those three depends on the prompt being good. That is deliberate: the prompt is the one
part of the system I cannot write a test for.

---

## The pipeline

```
  ┌─────────┐   Case row      ┌──────────┐   Signal        ┌──────────┐
  │ WATCHER │ ──────────────▶ │ THINKER  │ ──────────────▶ │  GUARD   │
  │ rules   │   facts, no AI  │ the LLM  │   a plan        │  rules   │
  └─────────┘                 └──────────┘                 └────┬─────┘
       ▲                            │                           │ allowed?
       │                            │ falls back to rules       ▼
       │                            │ on ANY failure       ┌──────────┐
  ┌────┴────┐                       └─────────────────────▶│   DOER   │
  │ cases   │                                              │ 6 actions│
  │ table   │                                              └────┬─────┘
  └─────────┘                                                   │
                                                                ▼
  ┌────────────┐                                          ┌──────────┐
  │ case_truth │ ────────────────────────────────────────▶│ REFEREE  │
  │  hidden    │      only this arrow exists              │ outcome  │
  └────────────┘                                          └────┬─────┘
                                                                ▼
                    LEDGER  ◀────── every stage writes ────  REPORT CARD
                 append-only, hash-chained
```

Read the diagram twice. The second read is for the arrow that **is not there**: nothing from
`case_truth` reaches the Watcher, the Thinker, the Guard or the Doer.

---

## Component by component

### Watcher — `app/watcher.py`

Turns a database row into a `Signal`: a small set of plain-English facts plus three derived
judgements — `recoverability`, `risk_band`, and a `hard_stop` if one applies.

**No AI here on purpose.** Two reasons, and both are about the experiment rather than the code.

*It makes the ablation honest.* The rules-only policy and the LLM receive the identical `Signal`.
Any difference the report card shows is caused by the planning, not by one of them having been
fed better data.

*It draws a line under what is not a judgement call.* `error_source == "business"` means the
payment failed because of the merchant's own configuration. The customer cannot fix it by trying
harder. Asking a model to weigh that up invites creativity about something with exactly one
correct answer, and creativity there costs a real person a real message.

That hard stop exists because of a real failure in this project's own BUGLOG: a test payment was
declined with `international_transaction_not_allowed`, and the obvious reading — "the customer's
card failed, chase the customer" — was wrong. Razorpay had already said whose fault it was, in a
field I was ignoring.

The three-way split that follows from `error_source`:

| source | what actually works | what the wrong move costs |
|---|---|---|
| `customer` | ask them | — |
| `bank` / `gateway` | retry quietly | a message about *their bank's* outage |
| `business` | nothing customer-facing | harassing someone about *your* dashboard setting, forever |

### Thinker — `app/thinker.py`

The only LLM in the project.

**Why here and nowhere else.** The honest test is: *could an `if` statement do this?* Almost
everywhere in this codebase the answer is yes, so there is no model there. Here it is genuinely
no, because the useful information is in prose — `error_description` is a sentence written for a
customer, the relationship history is "eight purchases, none in five months, chased twice
recently". Weighing that against a rupee amount and a calendar is what language models are for.

That is a claim, and `rules_only` exists to check it rather than assert it.

**Two providers.** `provider_for()` dispatches on the model name — Gemini or an OpenAI-compatible
endpoint. Not redundancy theatre: one provider's free-tier quota ran out midway through building
this and throttled a 300-case batch to roughly a call a minute. The planner was already behind
one function, so the second implementation cost twenty lines. The thing you cannot control is the
vendor.

**Determinism.** Temperature is zero, but zero-temperature is not a guarantee and providers retire
model names without asking (they did, mid-build — see BUGLOG). So every answer is cached to disk,
keyed by `(model, system prompt, case)`, and the cache is **committed**. A reviewer with no API
key reproduces the published numbers exactly.

**Failure.** Network error, timeout, malformed JSON, an action outside the vocabulary, a plan
longer than the stopping rule — all land in one place: fall back to `plan_rules_only` for that
case, mark it, count it, carry on. `prewarm()` *raises* if 100% of calls fail, because a total
outage that degrades silently produces an "AI on" column that is really the "AI off" column with
a different label.

### Guard — `app/guard.py`

The most important file here. **The AI proposes; it never disposes.**

The Guard receives a `Signal`, an `Action`, and the record of what has already happened this run.
It returns yes or no with a named rule. There is no third option and no "warn but allow".

**Every refusal names a rule.** `Decision.rule` is a stable string, not a sentence. That is what
makes "we blocked 61 actions" auditable — group by rule, count, test each one. A guard that
returns free text is a guard nobody can prove anything about.

**Order is not alphabetical.** Consent, then physics (can this action possibly work at all), then
the stopping rule, then idempotency, then timing, then frequency, then value. Someone who opted
out is refused *for that reason*, not for whichever rule happened to be checked first — an audit
trail is only useful if it cites the real cause.

**State is explicit, and only committed actions touch it.** "Three contacts per customer per
week" cannot be evaluated from one action in isolation. `GuardState` holds that memory, and the
runner commits to it only after an action actually executes — so a *blocked* action does not
consume the customer's weekly quota. Backwards, and a badly-behaved planner could exhaust
someone's allowance by proposing things that never happened.

### Doer — `app/doer.py`

Six actions, one `execute()`, two modes. The 300-case batch runs `simulate`; the live demo runs
`live` and makes real Razorpay test-mode calls. **Same method.** If the batch took a different
code path from the demo, the architecture diagram would be a drawing rather than a description.

`REALITY` maps each action to `real` / `partial` / `simulated`, and the README and the video
script both read from it, so those three places cannot quietly disagree.

### Ledger — `app/ledger.py`

Append-only twice over.

*Hash chain.* Each row fingerprints its own contents plus the fingerprint of the row before it.
Alter any historical row and every fingerprint after it stops matching, so `verify_chain()`
reports not just that tampering happened but **where it started**.

*Database triggers.* SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers make the operations
fail outright.

Belt and braces, because "we keep an audit log" and "we can prove the audit log was not edited"
are very different claims to make to a payments company. `scripts/break_it.py` demonstrates both
layers independently — including on a copy of the schema with the triggers deliberately removed,
standing in for someone who edited the file directly.

One deliberate inversion from the audit logger I wrote in an earlier project: there, audit writes
were fire-and-forget, because a failed log line should never break a real user action. Here the
priority is reversed. **If we cannot record what we are about to do to someone's money, we do not
do it.** `record()` raises, and callers let that abort the action.

`record_many()` chains a batch in memory and commits once — a 300-case run across six policies
produces thousands of lines, and committing each separately turned a nine-second report card into
a four-minute one. A slow report card is one nobody re-runs, which quietly costs the
reproducibility check.

### Referee — `app/simulator.py`

The only component besides the report card that reads `case_truth`.

Every roll comes from `random.Random(f"{seed}|{policy}|{case_id}")`. Two consequences: re-running
gives byte-identical numbers, and **each case gets the same luck under every policy**. When
"blast everyone" beats us on some case it is because of the decision, not because it drew a
better die. That is a paired comparison, and it is the difference between an experiment and an
anecdote.

The model is stated openly in code rather than hidden behind a fitted curve — a reader has to
accept these rules for the report card to mean anything, so they are short enough to read:
timing (punishes impatience much harder than delay), fatigue (each further message is worth
less), action-fit per case kind, annoyance rolled whether or not the message worked.

### Report card — `app/report.py`

Two decisions shape it.

**Collected is not caused.** `collected` is every rupee that arrived on cases we touched — the
number a normal recovery dashboard shows. `caused` subtracts customers who were going to pay
anyway. `do_nothing` is in the table specifically to make that visible: it collects a quarter of
the at-risk money while causing none of it.

**The denominator is the winnable money.** Some of the cohort cannot be recovered by anybody, and
some was never lost. Quoting recovery as a share of *total* at-risk revenue makes every policy
look worse than it is and hides which are leaving real money behind.

---

## Data model

| table | who reads it |
|---|---|
| `cases` | everything |
| `case_truth` | referee and report card **only** |
| `ledger` | append-only; anyone may read |
| `webhook_events` | the receiver; `event_id` is unique, which *is* the idempotency check |

Two rules that hold everywhere: **money is an integer number of paise, never a float** — a
rounding error inside a recovery total is invisible until it is expensive — and **timestamps are
UTC**, with business rules like "no messages after 9pm" evaluated in IST at the moment of the
check rather than baked into stored data.

SQLite on purpose. A reviewer should be able to clone and run without asking anyone for a
credential.

---

## Security

- HMAC-SHA256 over the **raw request bytes** for webhook signatures, compared with
  `hmac.compare_digest`. Parsing before verifying would mean verifying something other than what
  was signed.
- Reply 200 first, process after. Razorpay's contract is at-least-once delivery; a slow handler
  causes redeliveries, which is how a customer gets charged twice.
- `require_razorpay()` refuses any key not starting with `rzp_test_`. Recoup sends real money
  actions and is only ever run against test mode.
- `create_payment_link` forces `notify.sms` and `notify.email` to `false`. Razorpay test mode
  will happily text a real phone, and a plausible-looking number in a fixture is not a good enough
  reason to message a stranger about a debt.
- Nothing prints a secret. `/health` reports whether credentials are *present*, which is what you
  actually need at 11pm when calls start failing.

---

## What I would build next

- **Live A/B, small.** Hold out 5% of real cases as an untouched control. That converts the
  counterfactual from something I modelled into something measured, and it is the single change
  that would most improve every number here.
- **Per-merchant calibration.** `p_pay_if_contacted` is currently a hand-specified distribution;
  with real outcome data it becomes a fitted model per merchant, per failure reason.
- **A learned stopping rule.** "Four attempts" is a defensible constant, not an optimum. The
  right number differs by case value and relationship depth.
- **Cost-aware planning.** The planner currently does not see that a message costs 85 paise and
  an escalation ₹120. On ₹400 cases that ratio should change the answer.
- **The message text itself.** Recoup decides *what* and *when*, never *what to say*. Generating
  the copy is the obvious next use of the model — and the obvious next place to need a Guard.
