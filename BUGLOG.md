# BUGLOG

Every failure hit while building Recoup, written down at the moment it happened — not
reconstructed afterwards.

Format for each entry:

- **Symptom** — what I actually saw
- **Why** — the real cause, once I found it
- **Fix** — what I changed
- **What it taught me** — the design lesson, if the fix changed how the system is built

---

## The answer for the form

*Razorpay's application asks "what broke, and how you got out", and says it is the field they
read first. This is the ~200 words. The full entries are below.*

> The worst one didn't look like a failure at all. I was testing the LLM planner on four cases
> and every plan came back sensible — a silent retry for the bank outage, wait-for-salary-day for
> the insufficient-funds decline, escalation for the merchant-side one. The only thing wrong on
> the screen was `cost_paise: 0`.
>
> Two failures had stacked. The model name I'd hardcoded had been retired for new API keys, so
> every call was returning 404. And my "graceful degradation" caught it, fell back to my
> hand-written rules, and carried on. The good plans I was admiring were my own rules, wearing
> the model's label.
>
> Scaled to 300 cases that would have produced an ablation table where "AI on" and "AI off" were
> the same code, printing a confident dead heat, fully reproducible and completely false. I'd
> have published it.
>
> The fix was three things: make the total-outage case *raise* instead of degrade, because a
> 100% failure is not a degraded run; count every fallback and print it in the report; and treat
> "what does this look like when it's completely broken?" as a required question for every
> fallback path, not just "what if one call fails?"
>
> A fallback nobody counts isn't a fallback. It's a cover-up.

---

## 2026-08-24 — No credit card, so no Anthropic API key. The whole AI layer was blocked on day zero.

**Symptom:** I couldn't get an API key for the planning model. The Anthropic console won't issue
a key until a payment method is on file, and I don't have a card available. My Claude Code
subscription doesn't help here — that's a seat licence for an editor, not API access for a
program I'm writing. So the Thinker (the LLM that decides what to do about each at-risk payment)
had nothing to call, and Days 5–7 of the plan were dead before they started.

**Why:** I had written the model vendor into the plan as if it were a fixed part of the
architecture. It isn't. It's a dependency, and I'd given it no fallback — the same mistake as
hardcoding a payment gateway or a database host and finding out later that it's unavailable in
your environment.

**Fix:** Two changes, one immediate and one structural.

1. Immediate: Google AI Studio issues a Gemini API key with no card and no billing account, on a
   free tier that comfortably covers a 300-case evaluation run. Groq (`console.groq.com`) is the
   second key, obtained the same way, as a spare.
2. Structural: the model does not get called directly from the agent. Everything goes through one
   small `LLMClient` interface — `propose(case, tools) -> ProposedAction` — with a per-provider
   adapter behind it and the provider chosen by an env var. Swapping vendors is a config change,
   not a refactor.

**What it taught me:** A free tier isn't just cheaper, it's *rate limited* — and that turned out
to be useful. Because I have to survive 429s to run my own evaluation at all, retry-with-backoff
and a circuit breaker stopped being a feature I'd add later to look thorough, and became something
the batch run genuinely cannot work without. The constraint pushed the resilience work forward
instead of leaving it as decoration.

Related: this is also why the run is reproducible. The evaluation replays from a fixed seed and
caches model responses, so a rate limit halfway through a 300-case batch doesn't invalidate the
numbers — it just resumes.

---

## 2026-08-24 — Committed the database to a public repo, ten minutes after writing the .gitignore

**Symptom:** My first real commit included `data/recoup.db-shm` and `data/recoup.db-wal`. I had
written `*.db` into `.gitignore` specifically to prevent this, and watched it happen anyway. The
contents were harmless this time — one `server_started` ledger row, no keys — but the same commit
on a later day would have published every case, customer reference and recovery amount in the
database to a public GitHub repo.

**Why:** I turned on SQLite's write-ahead logging (`PRAGMA journal_mode=WAL`) for better
concurrent writes. WAL keeps pending data in two sidecar files next to the database —
`recoup.db-wal` and `recoup.db-shm`. Those are database contents, but their filenames do not end
in `.db`, so my pattern did not match them. I had ignored the thing I was thinking about rather
than the category it belongs to.

**Fix:** `*.db-wal`, `*.db-shm` and `*.db-journal` added to `.gitignore`, and the two files
removed from tracking with `git rm --cached`.

**What it taught me:** An ignore rule that names one filename is a guess; the rule I actually
wanted was "no local data store, in any of the forms it takes". The broader point is that turning
on a database feature quietly changed the project's on-disk shape, and I only found out because I
read my own `git status` output instead of skimming it. I now check what a commit actually
contains before pushing, rather than trusting that a rule written earlier still covers the case.

Worth noting for the video: this is exactly the class of mistake the ledger's append-only design
is meant to survive. A leaked file can be untracked. A silently edited audit row could not be
recovered at all, which is why that table is protected at the database level rather than by my
good intentions.

---

## 2026-08-24 — Two entities created, third rejected, no way to undo the first two

**Symptom:** The seed script creates a customer, then an order, then a payment link. The first
two succeeded. The third failed with `BAD_REQUEST_ERROR: Recurring digits in customer contact are
disallowed`. I was using `+919999999999`, which is the placeholder in Razorpay's own docs. It left
me with `cust_TTaXtaDYSMprTD` and `order_TTaXtzf1uahFWU` in the account and nothing to attach them
to.

**Why:** Two separate things.

1. `/customers` accepts a contact number with repeated digits. `/payment_links` rejects it as
   likely fake. Same account, same key, same field, different validation. I had assumed a value
   accepted by one endpoint would be accepted by the next.
2. More importantly: I had written a three-step create as if it were one operation. There is no
   transaction across three HTTP calls and no rollback. A failure at step three leaves steps one
   and two committed on Razorpay's side, permanently.

**Fix:** Immediate fix was `+919876543210`. The real fix is that the Doer, when it lands, will not
be written this way. Each externally-visible create carries an idempotency key derived from the
case and the action, and the result is written to our side before the next call is made. Re-running
after a partial failure then resumes rather than duplicating — the customer and order from the
failed run get reused instead of a second pair being created.

**What it taught me:** I had been thinking about idempotency as protection against Razorpay calling
*me* twice, which is the webhook case. This is the mirror image: protection against me calling
*Razorpay* twice after failing halfway through. Both are the same underlying fact — a money action
can be attempted more than once and must only take effect once — and I had only designed for one
direction of it.

The stray customer and order are still in the test account. I am leaving them there rather than
quietly cleaning up, because "what does your system do with the debris from a half-finished
recovery attempt?" is a question worth having an answer to, and pretending it never happened is
not one.

---

## 2026-08-24 — The test card the whole internet recommends is an international card

**Symptom:** First real payment attempt on our own seed link, `plink_TTaYEsKRzybzIO`, ₹1,200.
Card `4111 1111 1111 1111`, the canonical test Visa. Razorpay's checkout refused it:
*"payment could not be completed, international cards are not supported."*

**Why:** I read the failure properly instead of guessing, because by then the webhook receiver
existed and had already captured the event. Razorpay's own words, off the wire:

```
error_reason        international_transaction_not_allowed
error_source        business
error_description   ...this business accepts domestic (Indian) card payments only.
international       true
```

`4111 1111 1111 1111` is not an Indian BIN. A fresh Razorpay test account has international
payments disabled, so the card is rejected before it ever reaches a bank. Nothing was
misconfigured. The account behaved exactly as an Indian merchant account should, and I had
handed it a foreign card.

Note `error_source: business`. Razorpay is saying the failure is on the *merchant's* side of
the transaction, not the customer's.

**Fix:** Stopped using cards for the happy path. Test mode UPI (`success@razorpay`) and the
simulated netbanking page are domestic by construction and can't hit this. Cards stay in the
cohort as a *failure* generator, which turns out to be what we actually needed them for.

**What it taught me:** Two things, and the second one changed the design.

First, the boring one: documentation examples are not account-valid. This is the second time in
one day — `+919999999999` came from Razorpay's docs too. Their docs show the shape of a value.
Whether *your* account accepts it is a different question, and only your account can answer it.

Second, the one that matters. I had been thinking of `error_source` as diagnostic noise. It
isn't — it decides whether a case is recoverable at all. A `customer`-sourced failure
(insufficient funds, wrong OTP) is worth chasing: the customer can fix it, and a well-timed
retry is exactly the product. A `business`-sourced failure like this one cannot be fixed by
contacting the customer. Retrying it will fail identically, forever, at ₹0.02 of AI spend per
attempt, while sending a real person messages about a problem that is not theirs.

So `error_source: business` becomes a hard stop in the Watcher, before the Thinker ever sees the
case, and a Guard rule behind that in case the Thinker proposes contact anyway. And it gives the
report card a row I would not otherwise have thought to measure: **cases correctly identified as
unrecoverable**. Not chasing money you cannot get is a result, not an absence of one.

The nice irony: this is the first at-risk case Recoup ever saw, it is real, and the correct
action on it is to do nothing and say why.

---

## 2026-08-24 — A webhook filed under the wrong object's id, and it looked fine

**Symptom:** Nothing failed. Six real webhooks arrived from Razorpay, all signature-verified,
all processed, no errors anywhere. Only when I printed the table to admire it did one row read:

```
payment_link.paid      order_TTf4sNCvkTzFPS
```

A `payment_link` event filed under an `order` id.

**Why:** A delivery can carry more than one entity. `payment_link.paid` arrives with all three
objects involved, because all three changed:

```
payload.order         -> order_TTf4sNCvkTzFPS
payload.payment       -> pay_TTgoSQWwQ21qY2
payload.payment_link  -> plink_TTaYEsKRzybzIO
```

My extractor walked the dict and returned the first entity with an id. That is a coin flip
dressed up as a rule. It happened to be right for `payment.captured` (one entity, no ambiguity),
which is the event I built it against, and wrong for the two multi-entity events. Two of six.

**Fix:** Razorpay already names the subject — it is the event name. `payment_link.paid` is about
the `payment_link`. Split on the dot and ask for that key by name. Twelve tests now cover the
receiver, including one that asserts the same body under three different event names files three
different ids. Backfilled the two wrong rows from the stored payloads and recorded the correction
in the ledger, since an audit trail that quietly improves itself is not an audit trail.

The same bug was in the amount extractor, which mattered more: the order said ₹5,000 and the
payment said ₹1,200. On a partly-paid link, reading the wrong entity overstates the recovered
total — the single number this whole project is judged on.

**What it taught me:** This is the first bug today that produced no error message. The other
three announced themselves — a 400, a rejected card, a file in a commit. This one returned a
valid id, of a real object, that genuinely was part of the event. Every automated check passed.

I only caught it because I read output I had no reason to read. That is not a strategy.

So the lesson is about the shape of the mistake rather than the mistake: I wrote code that
*searched* for a plausible answer when the input already *stated* the answer. `for wrapper in
entities.values(): return the first one with an id` is a guess. `entities[event.split(".")[0]]`
is a lookup. Guessing is how you get an answer that is well-formed, confident, and about the
wrong object — and money systems are full of well-formed wrong answers, because every id looks
like every other id.

Wherever the payload tells you what it is, use that. Never infer from position what is written
down in the data.

## 2026-08-25 — "Idempotent" was doing the opposite of what I meant, and it looked like a win

**Symptom:** First run of the report card. `retry_everything` recovered 5% of the winnable
money and the Guard reported 530 `duplicate_action` blocks against it — more blocks than that
policy had cases. `blast_everyone` had 285. Both baselines were being neutered by a rule that
was supposed to be a safety net, not a strategy.

**Why:** My idempotency key was `action.kind`. So the Guard read "retry on day 0", "retry on
day 1" and "retry on day 3" as *the same instruction repeated*, and refused two thirds of every
retry ladder. Idempotency is not "never do this kind of thing twice" — that is what the cooldown
and the stopping rule are for. Idempotency is replay protection: the *same instruction* arriving
twice must have the effect of arriving once. And an instruction to move money includes **when**.

**Fix:** Key on `kind@wait_days:hour` (`guard._fingerprint`). The repetition rules — 24h cooldown,
3 contacts per customer per week, 4 attempts then stop forever — do the job they were always
supposed to do, and now actually get exercised instead of being shadowed.

**What it taught me:** The bug was invisible because it made my numbers *better*. Recoup beat the
baselines by a mile, and the reason was that I had accidentally handcuffed them. If I had not gone
looking at the per-rule breakdown — which I only printed because the report card needed a
compliance column — I would have shipped a comparison that was rigged in my favour and never known
it. Baselines need to be debugged as carefully as the thing you are trying to prove, because
nobody is motivated to find the bugs that flatter them.

## 2026-08-25 — The AI never ran, and my error handling made sure I could not tell

**Symptom:** First test of the LLM planner on four cases. Every plan came back sensible and
correctly reasoned — a silent retry for the bank outage, wait-for-salary-day for the insufficient
funds case, escalation for the merchant-side decline. `cost_paise: 0` on all four. I nearly moved
on. The zero is the only thing that was wrong on the screen, and it is the kind of zero that reads
like good news.

**Why:** Two failures stacked, and the second hid the first. `gemini-2.5-flash` had been retired
for new API keys — every call was returning HTTP 404 with a message telling me which model to use
instead. And my "graceful degradation" path caught it, fell back to `plan_rules_only`, and carried
on. So the four good plans I was admiring were my own hand-written rules, being shown back to me
with an LLM's label on them.

Scaled up, this was about to produce a report card where the "AI on" column and the "AI off"
column were the same code, with the ablation printing a dead heat and a confident paragraph about
how the model adds nothing. I would have published that. It would have been completely wrong and
completely reproducible.

**Fix:** Three things. The model name moved to a constant that is exercised on every run.
`prewarm()` now *raises* if 100% of planning calls fail, rather than reporting an error count
nobody reads — a total outage is not a degraded run and must not be allowed to look like one.
And the report card prints `llm_calls`, `llm_cached` and `llm_fallbacks` in the ablation block,
so "the model did not actually run" is a number on the page rather than something you have to
already suspect.

Separately: this model bills its internal reasoning as output tokens (231 thought tokens for a
5-token answer on a trivial probe). I was only counting `candidatesTokenCount`, which would have
under-reported the cost of the batch by roughly forty times — in the one column of the report card
that nobody else publishes.

**What it taught me:** A fallback that is not *counted* is not a fallback, it is a cover-up. The
entire value of "handle failure gracefully" depends on the failure still being visible after it
has been handled, and mine was designed so that the more completely the system broke, the more
normal it looked. I now treat "what does this look like when it is 100% broken?" as a required
question for every degradation path, not just "what does it look like when one call fails?"

## 2026-08-25 — The script that proves nothing gets lost, deleted the cohort

**Symptom:** Ran `scripts/break_it.py` — the "break it on purpose" demo — and it printed
`5/5 failures handled as designed`. Then the report card started reporting 40 cases instead of
300, with a completely different fingerprint.

**Why:** Scenario 4 ("the planning model is unreachable for every case") needs a cohort to run
against, so it did `session.query(Case).delete()` and loaded a 40-case one. Against the real
database. The script whose entire thesis is *nothing is ever silently lost* silently destroyed
the thing every published number is computed from — and reported complete success while doing it,
because I had only written checks for the failures I was demonstrating, not for the damage the
demonstration itself caused.

**Fix:** `break_it.py` sets `DATABASE_URL` to a fresh temp file **before importing any app
module** — `app.db` builds its engine at import time, so doing it afterwards would have been too
late and would have looked like it worked. The scratch path is printed at the top of the run so
it is obvious where it went.

**What it taught me:** I had been treating "test code" and "demo code" as lower-stakes than
application code, and they are not — they run with the same credentials against the same database.
The fix was three lines and the risk was the entire dataset. Test isolation is not hygiene, it is
a blast radius decision, and `conftest.py` had got this right on day one purely because pytest
made the right thing easy. Nothing made it easy in a script, so I did the wrong thing.

## 2026-08-25 — A guardrail that could never fire, and I only noticed because I printed it

**Symptom:** Added a `stopped` column to the report card — cases closed for good by the
four-attempt stopping rule. It read `0` for all six policies. Not "low". Zero, everywhere.

**Why:** Two mechanisms were enforcing the same limit and the outer one made the inner one
unreachable. `actions.parse_plan()` refuses a plan longer than four actions, and the Guard's
stopping rule blocks the *fifth* attempt on a case. Since no plan could ever contain a fifth
action, the rule had nothing left to block. It had looked like defence in depth for two days.
It was dead code with a test that passed because the test drove `GuardState` directly rather
than going through a run.

**Fix:** The stopping rule is about a case's **lifetime**, not one day's plan — "we have chased
this person four times, ever, stop" — so `GuardState.attempts_per_case` is now seeded from
`Case.attempts`, the count persisted from previous runs. Two new tests drive it the way a real
run would: a case arriving with four prior attempts is refused, and a case with three gets
exactly one more and then closes.

It still reports 0 on this cohort, because every case in it is new. So the report card now
*says* that, and says why, instead of printing a zero that looks like a working rule.

**What it taught me:** I found this only because I put a column on a report nobody asked for.
Every unit test for the stopping rule passed the whole time — they set up the state by hand,
which meant they were testing the rule's logic and not its reachability. **A test that
constructs the state directly can prove a rule works and still tell you nothing about whether
anything can reach it.** For guardrails specifically, "does it fire on a real run?" is a
different question from "does it fire when I make it fire", and only the first one matters.

## 2026-08-25 — Exponential backoff against a rate limiter, which is the wrong shape entirely

**Symptom:** Pre-planning the 300-case cohort crawled. Each round of the warm-up landed a
handful of cases and then reported the rest as errors; an hour in, the cache had 658 of the 802
entries it needed. Meanwhile the provider's own headers said
`x-ratelimit-remaining-tokens: 7923` out of 8000 — the bucket was nearly **full**. I was being
throttled and idle at the same time.

**Why:** My retry ladder was the same exponential backoff I use for Razorpay: wait 0.75s, then
1.5s, then 3s, then give up. That is exactly right for a server that is overloaded — you are
giving it room to recover. It is exactly wrong for a **token bucket that refills on a fixed 60
second cycle**, which does not care that you waited three seconds. All four attempts landed
inside the same closed window, failed identically, and the case fell back to the rules for a
question the model would have answered fine forty seconds later.

So the fallback rate was high, the ablation was being computed against a planner that had been
switched off for a third of its cases, and nothing anywhere said "rate limited" — it said
"errors".

**Fix:** `_rate_limit_wait()` reads `retry-after`, then `x-ratelimit-reset-tokens`, then
`x-ratelimit-reset-requests`, and sleeps for what the server actually said, capped at 70 seconds
so a `3h46m` reset does not park a worker until tomorrow. Exponential backoff stays as the
fallback for when there is no header. Concurrency dropped from 6 to 3, because six workers
against an 8,000-token-per-minute budget are five workers generating 429s.

A regex parses the durations, which sounds like overkill until you notice the character-loop
version read `577ms` as 577 **minutes** — a nine-hour sleep inside a worker thread. There is a
test for that specific string.

**What it taught me:** I had one retry helper in my head labelled "the right way to retry", and I
applied it to a completely different failure mode without noticing they were different. *Server
is struggling* and *you have used your quota for this minute* both look like a non-200 and need
opposite responses: back off gently versus wait for a specific clock. The provider was telling me
which one it was, in a header I was not reading. **When an API gives you a number, the retry
policy is not yours to invent.**

## 2026-08-25 — `max_tokens` is a reservation, and I was burning two thirds of the budget on tokens nobody generated

**Symptom:** Pre-planning the cohort stalled again, this time at 158 of 300. The provider's
per-minute headers said the token bucket was **full** — `x-ratelimit-remaining-tokens: 7923` of
8000 — while every call came back 429. Throttled and idle at the same time, for the second time
in one evening.

**Why:** The per-minute limit was not the one being hit. Reading the actual 429 body instead of
the headers:

```
Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 199347, Requested 2348
```

Two things in that line. There is a **daily** token budget as well as a per-minute one, and I
had spent it. And `Requested 2348` for a call whose prompt is about 1,150 tokens — the missing
1,200 were `max_tokens`, the output ceiling I had set.

**`max_tokens` is a reservation against your rate limit, not a cap on what you are charged.**
The plans actually come back at a median of 128 tokens and have never exceeded ~350. So roughly
two thirds of every request's rate-limit cost was buying output that was never generated, and my
daily budget of ~85 planning calls should have been ~140.

**Fix:** `MAX_OUTPUT_TOKENS = 600` — comfortably above the largest real plan, less than half the
old ceiling. `EST_TOKENS_PER_CALL` now reflects prompt *plus* reservation, because that is what
the limiter counts, and the client-side pacer is derived from it.

The second fix matters more. The report card now **refuses to print an ablation verdict** when
more than 5% of cases fell back to the rules, and says why. A fallback runs `plan_rules_only`, so
a case that fell back is literally the rules-only policy wearing the agent's label; at 47%
fallbacks the two columns being compared are partly the same code. It would still have printed a
confident winner.

**What it taught me:** I read the headers because they were structured and ignored the body
because it was prose — and the prose was the only place that named the limit I was actually
hitting. Twice this evening I have had a rate limiter tell me precisely what was wrong in a field
I was not reading.

And the guard is the real lesson. I had already been burned once by an ablation quietly comparing
the rules against themselves. Fixing that instance was not enough; the *class* of error needed a
check that fires on its own. The version of this that depends on me remembering is the version
that publishes the bad number at 2am on submission day.

## 2026-09-05 — The daily wall came back on submission day, and my retry loop still could not name it

**Symptom:** Warming the cache for the final run. `warm_cache` counted down 142 → 48 → 39 → 38
still to plan, and then stopped moving. The cache file went fourteen minutes without a write. The
process was alive. The per-minute headers said `x-ratelimit-remaining-requests: 994` and a token
bucket resetting in 577ms. Everything green, nothing happening.

**Why:** The same daily token ceiling I wrote up on 25 August:

```
on tokens per day (TPD): Limit 200000, Used 199324, Requested 1514
```

Rounds one to three had spent the day's entire 200,000 tokens getting from 142 down to 38. Every
call after that failed instantly and permanently, because the only thing that could clear it was
the clock. My loop retried forty times against it.

I already knew this limit existed. I had already been burned by it, already written the entry.
What I fixed in August was the *arithmetic* — pace the calls, stop over-reserving with
`max_tokens`. What I did not fix was the **reporting**, and that is the half that bit me again.

Two things were hiding it. `warm_cache` caught the exception from each round and printed nothing
about it, so "38 still to plan" for the fourth round running looked like slow progress rather
than a wall. And worse, inside `prewarm` the progress counter incremented on both paths:

```python
try:
    self.ask(signal)
except Exception as exc:
    errors.append(...)
with lock:
    done += 1                      # fires whether it worked or not
```

So a round where every single call failed still printed `290/300 planned`. I read that line twice
across two different runs and both times concluded things were fine. It is the most confident lie
the program tells.

**Fix:** The counter now tracks successes separately and prints both, so the same round reads
`0/10 of 38 planned (10 failed)` — which is unmistakable. `prewarm` already collected the reason
each call failed and `warm_cache` was throwing it away; it now prints them.

**What it taught me:** A retry is only correct if the thing you are waiting for can change inside
your retry window. Mine could not, and nothing in the output could have told me that. **A retry
loop that cannot say what it is retrying against is a spin loop with extra steps.** And a progress
counter that increments on the failure path is not instrumentation, it is decoration — if a metric
cannot go bad, it cannot tell you anything.

The general version, which I keep relearning: fixing the instance is not fixing the class. In
August I fixed the token arithmetic. What needed fixing was my ability to *see* the failure, and
because I did not, the identical wall cost me two hours on the day of the deadline.

---

## 2026-09-05 — A thinking model spent my output budget thinking, and my cache stored the leftovers

**Symptom:** Groq was out of tokens for the day, so I moved the planner to `gemini-3.6-flash` and
started re-planning all 300 cases. It ran. No exceptions. Ten cases in I checked the cached
answers instead of trusting the counter, and every one of them failed to parse:

```
gemini cases cached: 7 / 300
JSONDecodeError: Unterminated string starting at: line 1 column 20
parsed ok: 0  failed: 7
output tokens -> max 583  median 583        (my cap was 600)
```

Every single answer truncated at almost exactly the same length.

**Why:** Gemini 3.x reasons internally before it answers, and `maxOutputTokens` is **one budget
shared between the thinking and the answer** — not a ceiling on the answer alone. The usage
metadata says it plainly:

```
finishReason: MAX_TOKENS
thoughtsTokenCount:   573
candidatesTokenCount:  10
```

573 tokens of private reasoning, 27 left, 10 of them spent on JSON that stopped mid-string. This
is the August `max_tokens` bug wearing a different provider's clothes. There the number was a
reservation against a rate limit; here it is a budget the model can spend on something I never
see. Both times I read it as "the most output I want", and both times it was not.

`thinkingConfig.thinkingBudget = 0` would have been the clean answer. This model rejects it
outright with a 400, so the only lever is headroom.

**Fix:** `GEMINI_MAX_OUTPUT_TOKENS = 3000`, per provider rather than one shared constant — the
opposite direction from the Groq fix, because Gemini bills what it produces rather than what you
reserve, so unused headroom is free there and expensive on Groq. Observed thinking runs to about
1,040 tokens and a plan is about 100, so that is roughly a 2x margin.

The fix that actually matters is the second one. A reply that stops at `finishReason: MAX_TOKENS`
now **raises**, on both providers, before anything can store it:

```python
if candidates[0].get("finishReason") == "MAX_TOKENS":
    raise LLMUnavailable(f"{model} hit maxOutputTokens; the answer is truncated, not an answer")
```

I also had to go and delete the seven truncated answers that had already been written into
`data/thinker_cache.json`.

**What it taught me:** This is the worst-shaped failure I have hit on this project, and it is
worse than the one in my form answer. A truncated reply is a **success** by every check I had:
HTTP 200, non-empty text, a real model, tokens billed. It only dies at `json.loads`, which is
downstream, inside the fallback path — where it quietly becomes a rules-only plan wearing the
model's label. That is the ablation contamination bug again, arriving through a door I had not
thought to lock.

And because the cache is committed and keyed by prompt, a truncated answer stored once is a
**permanent** silent fallback for that case, in that repo, in every run afterwards, for every
reviewer who clones it. Reproducible, deterministic, and wrong.

**A fallback nobody counts is a cover-up. A cached failure is worse — it is a wrong answer with a
receipt.** The rule I am taking from it: validate a response against the thing that will
eventually consume it, at the boundary where it enters the system, and never let a value into a
cache that you have not proved you can read back.

---

## 2026-09-05 — Three planners in one evening, and none of them failed for a reason about quality

**Symptom:** Between Groq running dry and the report card needing 300 planned cases, I went
through four model names in about ninety minutes. Not one of them was rejected for reasoning
badly.

| planner | what stopped it |
|---|---|
| `openai/gpt-oss-120b` (Groq) | 262 of 300 planned, then 200,000 tokens/**day** exhausted |
| `gemini-2.5-flash` | 404 — *"no longer available to new users"* |
| `gemini-3.6-flash` | free tier allows **20 requests a day**. Cannot reach 300 |
| `gemini-3.1-flash-lite-preview` | nothing. 300/300, zero failures |

**Why, and the three separate lessons:**

The `gemini-2.5-flash` 404 is my headline bug returning almost word for word: a model name I had
written down as if it were a fixed part of the architecture, retired out from under me, failing
with a status code my fallback path would happily have swallowed. Six months apart, same lesson.
**A model name is a dependency with an expiry date, not a constant.**

The 20-requests-a-day ceiling is the one I would never have predicted. I assumed a newer, better
model meant a better option, and the newest model had the smallest free allowance by a factor of
fifteen. **On a free tier, capability and availability are unrelated — the quota is the design
constraint, not the benchmark.** The model that finished the job is the least capable of the four,
and because it does not reason before answering it was also immune to the truncation trap above.
Boring won.

The one that embarrassed me most: for about ten minutes I believed my Groq key was dead, because
a probe I wrote returned `HTTP 403, error code 1010`. That is a Cloudflare block on `urllib`'s
default user-agent. The same key, same model, same request through `httpx` — which is what the
application actually uses — returned 200 immediately. **I had tested with a different client than
the one my program runs, and drawn a conclusion about my credentials from it.** I came close to
regenerating a working key.

**Fix:** `provider_for()` already dispatched on the model name, so switching providers was a
one-constant change — the second provider was not redundancy theatre after all, it was the only
reason this run happened at all. Pacing is now derived per provider, because Groq meters tokens
per minute and Gemini meters requests per minute, and one shared constant idled at 13 seconds a
call against a limit that was never the binding one.

**What it taught me:** Every failure on the last day was an *availability* failure. I had spent
thirteen days making the system robust to the model being **wrong** and almost none making it
robust to the model being **gone**. What saved the submission was the design decision that looked
most like over-engineering at the time: caching every answer to disk, keyed by model and prompt,
and committing it. That cache is why 262 Groq answers survived three failed migration attempts
intact, and why a reviewer with no API key and no quota reproduces every number in `RESULTS.md`
exactly.

**Build the part that still works when the vendor does not.**
