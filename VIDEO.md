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

Set the terminal font to at least 16pt. Check the browser is at 100% zoom and the dashboard
is on the **Report card** tab before you start.

---

## 0:00 – 0:30 — the problem, with a number

*On screen: the dashboard's four cohort cards.*

> "This merchant has five lakh forty-nine thousand rupees at risk across one month — failed
> payments, failed subscription mandates, abandoned checkouts, overdue invoices.
>
> But look at the third card. **One lakh thirty-eight thousand of that was going to arrive
> whether anyone lifted a finger.** A recovery tool that messages all three hundred customers
> will report that as a win. It caused none of it, and it spent three hundred messages doing it.
>
> That number is what this project is actually about."

*Do not rush this. It is the whole pitch.*

---

## 0:30 – 1:15 — the shape, once

*On screen: the diagram from ARCHITECTURE.md.*

> "Five parts. The **Watcher** turns a case into plain facts — no AI, deliberately. The
> **Thinker** is the one language model in the project; it proposes a plan and it can do nothing
> else. The **Guard** is nine named rules that decide whether each proposed action is allowed —
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
> Thinker: it proposes a payment link tomorrow morning, and one follow-up on day four. Read the
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

**Then the ablation. Read whatever it actually says.**

> "Last thing, and this is the number I care most about: I ran the same pipeline with the model
> switched off, and published both.
>
> [If the rules win] The rules win. On this cohort the model is decoration and the honest
> recommendation is to ship the rules. I am showing you that because it is true.
>
> [If the model wins] The model causes ₹X more, with fewer messages and fewer opt-outs, for
> under a rupee of inference across the whole batch."

---

## 4:00 – 4:45 — weaknesses, then next, then you

Say the weaknesses **before anyone asks**. This is the part that reads as maturity.

> "Three things I would want a reviewer to know.
>
> The world is synthetic and I wrote its rules. The defences are that the rules are published in
> the repo, every policy sees identical inputs, and the ablation is allowed to go against me —
> but it is not a live A/B test, and that is this project's biggest limitation.
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
> I am Sandeep, final year at RGUKT Nuzvid, and this is thirteen days of work. The bug log is in
> the repo — including the one where my own error handling hid the fact that the AI was never
> running at all."

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
