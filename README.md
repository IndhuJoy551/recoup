# Recoup

An agent that recovers a merchant's at-risk revenue on Razorpay — failed payments, failed
subscription mandates, abandoned checkouts and overdue invoices — and is physically prevented
from doing anything reckless with it.

Built for the **Razorpay AI Buildathon 2026**, track: *AI Revenue Recovery*.

## The idea in one paragraph

A small merchant can have well over a lakh of rupees sitting in a "maybe" state at any moment.
Most of it is recoverable — a card declined for insufficient funds on the 28th will often clear
on the 1st — but working through every case by hand isn't realistic. Recoup reads each at-risk
case, decides what to do about it and when, and then executes that decision through Razorpay's
APIs. Every decision is explained, every action is checked against a gate the model cannot see
or modify, and everything is written to an append-only ledger.

## Architecture

```
   WATCHER  ──▶  THINKER  ──▶  GUARD  ──▶  DOER
   detect        propose       allow?      execute
                                 │
                                 ▼
                              LEDGER
                        append-only audit trail
```

| Component | What it does | Uses an LLM? |
|---|---|---|
| **Watcher** | Finds at-risk cases and classifies them | No — deterministic rules |
| **Thinker** | Proposes one action, with a written reason | Yes |
| **Guard** | Enforces quiet hours, contact caps, cooldowns, stopping rules, value ceilings, consent, idempotency | No — pure functions, unit tested |
| **Doer** | Executes a fixed set of six actions via Razorpay | No |
| **Ledger** | Records every decision, check and outcome, permanently | No |

The model proposes. It never disposes.

## Status

Day 0 — repo initialised. Under active construction until 4 September 2026.

See [`BUGLOG.md`](BUGLOG.md) for what has broken so far and what each failure changed.
