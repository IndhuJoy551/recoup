# BUGLOG

Every failure hit while building Recoup, written down at the moment it happened — not
reconstructed afterwards.

Format for each entry:

- **Symptom** — what I actually saw
- **Why** — the real cause, once I found it
- **Fix** — what I changed
- **What it taught me** — the design lesson, if the fix changed how the system is built

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
