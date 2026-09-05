# The five-minute video — shot list and script

Target: **4:45**, hard ceiling 5:00. Unlisted YouTube.

Rule for the whole recording: **no slides after 1:15.** Razorpay said a demo that is four
minutes of PowerPoint is a fail. Everything from 1:15 is a real terminal or a real browser.

Before recording, run this so nothing is stale:

```bash
python -m scripts.generate_cohort
python -m scripts.run_report_card          # writes results/report_card.json
python -m pytest -q                        # should be green on camera later
uvicorn app.main:app --port 8000           # leave running in a second terminal
```

**Do not run `scripts.warm_cache`.** The planner cache is complete and committed -- all 300
cases, zero fallbacks -- and both free-tier quotas are spent for the day. The report card reads
the cache and makes no network calls, which is the whole point of committing it.

Re-running the report card changes exactly one number in `results/RESULTS.md`: the audit ledger
entry count, because the ledger is append-only and grows every run. Every rupee figure, the
cohort fingerprint and the verdict are byte-identical. If a reviewer ever asks why that line
moved, that is the answer, and it is a good one.

Set the terminal font to at least 16pt. Check the browser is at 100% zoom and the dashboard
is on the **Report card** tab before you start.

---

## 0:00 – 0:40 — the problem, then what this is

*On screen: a title card for the first 3 seconds, then the dashboard's four cohort cards.*

**Title card text** (static, 3 seconds, no narration over it -- this is the one slide in the
video and it is allowed because it is a title, not an explanation):

```
Recoup
AI Revenue Recovery  ·  Razorpay AI Buildathon 2026
Sandeep Togarathi  ·  RGUKT Nuzvid
```

Then cut straight to the dashboard and start talking. **Lead with the number, not with your
name** -- the reviewer is deciding in the first fifteen seconds whether to keep watching, and a
number does that work where an introduction does not. The framing comes immediately after, once
they care.

> "This merchant has five lakh forty-nine thousand rupees at risk across one month — failed
> payments, failed subscription mandates, abandoned checkouts, overdue invoices.
>
> But look at the third card. **One lakh thirty-eight thousand of that was going to arrive
> whether anyone lifted a finger.** A recovery tool that messages all three hundred customers
> will report that as a win. It caused none of it, and it spent three hundred messages doing it.
>
> That number is what this project is actually about."

*Beat. Then frame it, now that they want to know.*

> "This is **Recoup**, my submission for the **AI Revenue Recovery** track. It finds a merchant's
> at-risk revenue, decides what to do about each case, and carries it out through Razorpay's API
> behind a guardrail that is allowed to stop it — on a real account, in test mode.
>
> And the number it is built around is the one on screen: **money caused, not money collected.**"

*Do not rush any of this. It is the whole pitch.*

---

## 0:40 – 1:15 — the shape, once

*On screen: the diagram from ARCHITECTURE.md.*

> "Five parts. The **Watcher** turns a case into plain facts — no AI, deliberately. The
> **Thinker** is the one language model in the project; it proposes a plan and it can do nothing
> else. The **Guard** is ten named rules that decide whether each proposed action is allowed —
> the model cannot see it, argue with it, or change it. The **Doer** can perform exactly six
> actions. The **Ledger** records all of it, append-only.
>
> The sentence to remember is: **the AI proposes, it never disposes.**
>
> And there is a sixth box the AI cannot reach at all — a table holding what *would* have
> happened. That is what makes the false-positive number possible, and I will come back to it."

---

## 1:15 – 2:15 — one case, live

*Terminal. Run:*

```bash
python -m scripts.demo_live
```

Let it print stage by stage. Talk over it:

> "Watcher: a failed payment, eighteen hundred rupees, `error_source: customer`, reason
> `payment_cancelled`. Fourteen previous purchases — that is a good customer.
>
> Thinker: it proposes a gentle reminder tomorrow at ten, and a payment link the day after. Read the
> reason it wrote — that string goes into the audit trail.
>
> Guard: both allowed, all rules passed.
>
> Doer: and that is a **real Razorpay payment link**, created just now in test mode."

*Switch to the Razorpay dashboard tab. Show the link with the same `plink_` id.*

> "Same id. This is not a mock."

---

## 2:15 – 3:00 — breaking it on purpose

> "Razorpay asked to see one failure handled gracefully. Here are five."

```bash
python -m scripts.break_it
```

Talk over the output — do not read it aloud, land the four points:

> "Razorpay returns 503 to everything: it backs off, then the circuit breaker trips. An outage
> now costs one call a minute instead of four calls per case across three hundred cases.
>
> The same webhook delivered three times: exactly one row. At-least-once delivery is Razorpay's
> contract, not a bug, so the unique constraint *is* the idempotency check.
>
> The planner asks to issue a refund: refused at the parser. There are six actions and
> `issue_refund` is not one of them, so it cannot be partially honoured.
>
> And someone edits the audit trail: the database refuses it — and on a copy with the triggers
> removed, the hash chain still names the exact row where the tampering starts."

---

## 3:00 – 4:00 — the report card

*Dashboard, Report card tab.*

> "Three hundred cases, six policies, same seed, and every case gets the *same luck* under every
> policy — so when one wins it is the decision, not the dice.
>
> Look at `do_nothing` first. It collects one lakh thirty-eight thousand and causes **zero**.
> That is the control, and any tool that cannot beat it is worse than useless.
>
> `blast_everyone` causes the most money — and costs forty-one customers who opted out
> permanently, fifty-six people chased who were already paying, and it breaks guard rules
> five hundred and eighty-six times. That row is what most recovery tools actually are.
>
> And the row underneath is the same strategy with my Guard switched on, so you can see how much
> of the difference is targeting and how much is compliance. Those are different claims and I
> did not want to blur them."

**Then the ablation. The run is in, and it went against the model. Say it plainly.**

*The fork in this section is resolved: 300 of 300 cases planned by one model, zero fell back,
and the rules won. Do not soften it and do not rush past it -- this is the strongest forty
seconds in the video.*

> "Last thing, and this is the number I care most about: I ran the same pipeline with the model
> switched off, and published both.
>
> **The rules win.** They caused two lakh four thousand. The agent caused one lakh
> fifty-five thousand -- forty-nine thousand less, twenty-four percent worse.
>
> And the model cost zero rupees for the batch. So it isn't that the AI was too expensive to
> justify. It was free, and it still lost.
>
> On this cohort the language model is decoration, and the honest recommendation is to ship the
> rules and keep the model for the cases the rules cannot express. I am showing you this because
> it is true, and because a comparison that only gets published when it flatters the thing you
> built is not a comparison."

*If asked why you built the model layer at all: the rules-only policy exists to test that claim
rather than assert it, and this is the test doing its job. You cannot know a model is decoration
until you have built it and measured it against something honest.*

---

## 4:00 – 4:45 — weaknesses, then next, then you

Say the weaknesses **before anyone asks**. This is the part that reads as maturity.

> "Three things I would want a reviewer to know.
>
> The world is synthetic and I wrote its rules. The defences are that the rules are published in
> the repo, every policy sees identical inputs, and the ablation is allowed to go against me —
> which, as you just saw, is not hypothetical — but it is not a live A/B test, and that is
> this project's biggest limitation.
>
> Two of the six actions have no live channel on this test account. `emi` is off, so instalments
> are simulated, and messaging would need a provider. That is labelled in the code, the README
> and here.
>
> And the referee's probability model is hand-specified, not fitted to real recovery data, which
> I do not have.
>
> With three more months the first thing I would build is a five percent untouched holdout on
> real traffic. That turns the counterfactual from something I modelled into something measured,
> and it is the single change that would improve every number you just saw.
>
> I am Sandeep, final year at RGUKT Nuzvid, and this is thirteen days of work. Fourteen failures
> are written up in the bug log — including the one where my own error handling hid the fact
> that the AI was never running at all, and the one from today where a model returned an answer
> cut off mid-sentence, my code cached it, and it would have become a permanent wrong answer
> that reproduced perfectly for anyone who cloned the repo. I caught that one before it
> shipped. That is what writing the failures down as they happen actually buys you."

---

## Things to have open before you hit record

- Terminal 1: the repo, venv active
- Terminal 2: `uvicorn app.main:app --port 8000` already running
- Browser tab 1: `http://127.0.0.1:8000` on the Report card tab
- Browser tab 2: Razorpay dashboard → Payment Links, logged in, **test mode**
- Browser tab 3: the GitHub repo

## Checks

- [ ] under 5:00
- [ ] no key, secret, or `.env` visible in any frame — check the terminal scrollback too
- [ ] the Razorpay dashboard clearly shows **Test Mode**
- [ ] the `plink_` id on screen matches the one in the terminal
- [ ] audio has no room echo; test 20 seconds first
- [ ] terminal text readable at 720p
- [ ] weaknesses stated in your own voice, not read
