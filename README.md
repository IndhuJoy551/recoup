# Recoup

An agent that recovers a merchant's at-risk revenue on Razorpay — failed payments, failed
subscription mandates, abandoned checkouts and overdue invoices — and is structurally prevented
from doing anything reckless with it.

Built for the **Razorpay AI Buildathon 2026**, track: *AI Revenue Recovery*.

---

## Run it in three minutes

No API key needed. The planner's answers are committed to the repository, so a clone reproduces
the published numbers exactly.

```bash
git clone https://github.com/IndhuJoy551/recoup && cd recoup
python -m venv .venv && .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m scripts.generate_cohort      # build the 300-case world
python -m scripts.run_report_card      # run five policies over it and score them
python -m scripts.break_it             # break it on purpose, five ways
pytest -q                              # the test suite

uvicorn app.main:app --reload          # then open http://127.0.0.1:8000
```

---

## The problem, with a number

Kavya sells handmade candles online. In one month, ₹1.14 lakh of her revenue is in a *maybe*
state: 40 abandoned checkouts, 25 failed payments, 12 failed subscription mandates, 8 overdue
invoices. Most of it is recoverable — a card declined for insufficient funds on the 28th will
often clear on the 1st — but she has a business to run and 85 cases to read.

The obvious fix is to message everyone. That is also the expensive one, and the expense does not
show up in the revenue line. It shows up in customers who stop answering.

---

## The one thing this project is actually about

**A recovery tool cannot be measured on the money it collects.**

Some of your customers were always going to pay. If you message them and they pay, a normal
dashboard records a win — and you caused none of it, while spending a message and some goodwill.
On this cohort, that is **₹1,38,491 of ₹5,48,910 at risk: a quarter of the money, arriving with
or without us.**

You cannot measure this in production. Once you have sent the reminder, the month cannot be
replayed without it; the control group is gone. So Recoup is scored against a **synthetic cohort
that knows the counterfactual** and is structurally forbidden from telling anyone who is being
graded. Two tables:

| `cases` | `case_truth` |
|---|---|
| what Recoup sees | whether they would have paid anyway, and how likely each action is to work |
| read by Watcher, planner, Guard, Doer | read *only* by the referee and the report card |

`tests/test_isolation.py` enforces the split by walking the import graph, not by trusting a
docstring. Every policy — including the three baselines — receives an identical `Signal` object
and is told none of the parameters that generated the world.

The generative rules are stated openly in [`app/cohort.py`](app/cohort.py). I wrote the world, so
pretending otherwise would make the whole comparison theatre.

---

## Results

Full table: [`results/RESULTS.md`](results/RESULTS.md). Regenerate with
`python -m scripts.run_report_card`.

Five policies over the same 300 cases, same seed, same luck per case:

| policy | what it does |
|---|---|
| `do_nothing` | no recovery process. The honest zero point — and it still *collects* ₹1.38 lakh |
| `blast_everyone` | three messages to everyone, immediately, no compliance layer |
| `blast_everyone_gated` | the same blast with Recoup's Guard in front, isolating targeting from compliance |
| `retry_everything` | silently retry every failure three times |
| `rules_only` | Recoup with the model switched **off** — the ablation opponent |
| `recoup` | Watcher → LLM planner → Guard → Doer |

Columns that matter, and why:

- **caused** — money that arrived *because of* the policy. Self-payers removed from every row.
  The only column worth comparing.
- **collected** — every rupee that arrived. What a normal dashboard shows. `do_nothing` scores
  well here, which is the point.
- **false positives** — cases where we contacted someone who was already coming.
- **opt-outs** — customers permanently lost to being chased.
- **violations** — guard rules the policy broke. Baselines run ungated, because a merchant
  blasting their customer list does not have a compliance layer; that is what makes it a bad
  idea. Their broken rules are counted and published rather than silently prevented.
- **cost %** — messages plus staff time as a share of money caused.

### The ablation

`rules_only` and `recoup` see identical inputs and differ only in who plans. Whichever wins is
published. The first run went **against** the model by 27%, and the cause was an instruction of
mine — my system prompt said "propose the smallest plan", so it proposed one action for 259 of
300 cases while the rules got a two- or three-step ladder.

The report card **refuses to name a winner** when more than 5% of cases fell back to the rules,
and says so instead. A fallback runs `plan_rules_only`, so a case that fell back is literally the
rules-only policy wearing the agent's label; past a few percent the two columns being compared
are partly the same code. To get a clean verdict, run `python -m scripts.warm_cache` until it
reports the cache complete, then re-run the report card.

---

## Architecture

```
   WATCHER  ───▶  THINKER  ───▶  GUARD  ───▶  DOER  ───▶  Razorpay
   detect         propose        allow?       execute
   no AI          the only AI    no AI        no AI
     │              │              │            │
     └──────────────┴──────────────┴────────────┘
                          ▼
                       LEDGER            append-only, hash-chained
                          │
                          ▼
                      REFEREE            the only reader of case_truth
                          ▼
                    REPORT CARD
```

| Component | File | Uses an LLM? |
|---|---|---|
| **Watcher** | [`app/watcher.py`](app/watcher.py) — case row → plain-English facts | No |
| **Thinker** | [`app/thinker.py`](app/thinker.py) — proposes a plan, with reasons | **Yes** |
| **Guard** | [`app/guard.py`](app/guard.py) — nine named rules, one test each | No |
| **Doer** | [`app/doer.py`](app/doer.py) — one `execute()`, simulate and live modes | No |
| **Ledger** | [`app/ledger.py`](app/ledger.py) — hash-chained, trigger-protected | No |
| **Referee** | [`app/simulator.py`](app/simulator.py) — reads `case_truth` | No |

Full write-up: [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Bounded

There are exactly six actions and nothing else exists:

```
send_payment_link   schedule_retry   send_reminder
offer_installments  escalate_to_human   do_nothing
```

The planner's output must survive `actions.parse_plan()`. A request for `issue_refund`, a
400-day delay, or a nine-step plan raises `UnknownAction`, and the case is escalated to a person.
No proposal is ever partially honoured.

### Gated

| Rule | Why |
|---|---|
| `customer_opted_out` | consent withdrawn. Not a trade-off |
| `merchant_side_failure` | `error_source: business` — the customer cannot fix our configuration |
| `dead_instrument_retry` | an expired card fails every retry identically |
| `quiet_hours` | money messages only between 09:00 and 21:00 IST |
| `weekly_contact_cap` | ≤3 contacts per customer per 7 days |
| `case_cooldown` | ≥24h between attempts on one case |
| `stopping_rule` | 4 attempts on a case, then never again |
| `high_value_needs_human` | above the ceiling, a person approves first |
| `duplicate_action` | replay protection: same instruction, same time, once |

Every rule has a test in `tests/test_guard.py`, and
`test_every_rule_in_the_guard_has_a_test` fails the build if a rule is added without one.

The Guard runs *after* the planner and cannot be reached from the prompt. Refusals name a rule,
so "we blocked 61 actions" can be grouped, counted and defended.

### Explainable

Every case produces a ledger entry containing the facts the Watcher extracted, the plan proposed
with its written reasons, the Guard's verdict on each action with the rule it cited, and what
happened. The dashboard's **Case detail** tab renders exactly that, per case.

---

## What is real and what is simulated

Stated here, in the video, and in [`app/doer.py`](app/doer.py) — three places that cannot
disagree.

| Action | Status |
|---|---|
| `send_payment_link` | **Real.** Creates a Razorpay test-mode payment link. A ₹1,200 test payment was recovered end to end through this path on 2026-08-24, webhook and all |
| `schedule_retry` | **Partly real.** Live mode creates the order; the re-attempt on a saved instrument needs a token this test account does not have |
| `send_reminder` | Simulated. Sending it needs a messaging provider, and a wrong number in a fixture would text a stranger about a debt |
| `offer_installments` | Simulated. `emi` is `false` on this account — checked with `scripts/check_methods.py` |
| `escalate_to_human` | Real: the case is written to an exception queue with its reason and stops moving |

Customer notification is forced off inside `razorpay_client`, so no route through this codebase
can text a real phone in test mode.

The 300-case batch is a **simulation** throughout. The Doer runs in `simulate` mode through the
same `execute()` the live demo calls — one code path, so the diagram describes the code rather
than sitting next to it.

---

## Reproducibility

`python -m scripts.run_report_card`, twice, gives byte-identical numbers.

- The cohort is generated from one seeded RNG. No module-level `random`, no `datetime.now()`,
  a fixed `AS_OF` anchor. A SHA-256 fingerprint over the whole cohort is pinned in
  `tests/test_cohort.py` and recorded in the ledger with the seed.
- The referee's rolls are seeded from `(seed, policy, case_id)`, so every policy gets the *same
  luck on the same case*. A paired comparison, not a race between two dice.
- The planner's answers are cached to `data/thinker_cache.json`, keyed by model **and system
  prompt** **and** case, and committed. Temperature is zero, but zero-temperature is not a
  guarantee and providers retire model names without asking.

---

## Failure handling

`python -m scripts.break_it` breaks it five ways and shows the end state of each:

1. **Razorpay returns 503 to everything** → exponential backoff, then the circuit breaker trips.
   An outage costs one call a minute instead of four per case across 300 cases.
2. **The same webhook, three times** → exactly one recovery row. The unique constraint on
   `event_id` *is* the idempotency check, and the failed insert is supposed to happen.
3. **The planner proposes a refund** → refused at the parser, case escalated, nothing partially
   honoured.
4. **The planner is unreachable for every case** → all 300 still planned via the rules fallback,
   and every fallback is counted and printed.
5. **Someone edits the audit trail** → the SQLite trigger refuses it; and on a copy with the
   triggers removed, the hash chain still names the row where tampering starts.

---

## Known weaknesses

Stated before anyone has to ask.

- **The world is synthetic and I wrote its rules.** The defences are that the rules are published,
  every policy sees identical inputs, and the ablation is allowed to go against me — but a
  synthetic cohort is not a live A/B test and this is the project's biggest limitation.
- **The referee's model is hand-specified**, not fitted to real recovery data, which I do not
  have. The numbers are a comparison between policies under one stated model, not a forecast.
- **Two of the six actions have no live channel** on this test account (see above).
- **The cost column uses published list prices** at an assumed ₹87/USD. Token counts are exact;
  the rupee conversion is an assumption, stated in `app/thinker.py`.
- **One merchant, one month.** No seasonality, no repeat cohorts, no long-run effect of opt-outs
  on future revenue.

---

## What broke

[`BUGLOG.md`](BUGLOG.md) — written as things happened, not reconstructed afterwards. The ones
that changed the design:

- **An idempotency key that crippled the baselines and flattered me.** Keyed on action *kind*, so
  "retry Monday" and "retry Thursday" were the same instruction. Recoup beat the baselines by a
  mile because I had accidentally handcuffed them.
- **The AI never ran, and my error handling made sure I could not tell.** A retired model name
  returned 404 on every call; the graceful fallback caught it and quietly served my own rules back
  to me under an "AI on" label. A fallback nobody counts is a cover-up.
- **The script that proves nothing gets lost, deleted the cohort.** `break_it.py` ran against the
  real database.

---

## The planner

`gemini-3.1-flash-lite-preview` by default, reached through Google's API; `provider_for()`
also dispatches OpenAI-compatible model names to Groq. Two providers, and on the final day
both of them mattered: Groq planned 262 of the 300 cases and then hit a 200,000
token-per-**day** ceiling, and the obvious replacement, `gemini-3.6-flash`, allows 20 requests
a day on the free tier. The published cohort is planned by the one model that could serve all
300 in a single run, because a column stitched together from two planners is not an ablation.

Calls are paced client-side to whichever budget the provider actually meters -- tokens per
minute on Groq, requests per minute on Gemini -- rather than discovering the limit by hitting
it. A reply that stops at `finishReason: MAX_TOKENS` is treated as a failure rather than an
answer, so a truncated plan can never reach the cache. Every answer is cached to `data/thinker_cache.json`, keyed by
`(model, system prompt, case)` and committed — which is why a clone with no API key reproduces
the numbers.

Override with `--model`, or `RECOUP_MODEL`. `RECOUP_OFFLINE=1` forces cache-only.

## Layout

```
app/         actions guard watcher thinker doer simulator report runner
             ledger razorpay_client webhooks cohort models db config main
             static/dashboard.html
scripts/     generate_cohort  run_baselines  run_report_card  warm_cache
             break_it  seed_razorpay  ping_razorpay  check_methods
tests/       actions guard watcher simulator runner thinker dashboard
             cohort ledger webhooks razorpay_client isolation
results/     report_card.json  RESULTS.md
data/        thinker_cache.json (committed)  recoup.db (not)
```

Built by Sodadasi Indhu Joy · RGUKT Nuzvid, 2027.
