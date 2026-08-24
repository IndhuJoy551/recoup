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
