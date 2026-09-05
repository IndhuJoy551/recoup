"""Fill the planner cache for the whole cohort, retrying around rate limits.

    python -m scripts.warm_cache

Separate from the report card on purpose. On a free-tier API key the 300
planning calls take twenty minutes of mostly waiting, and mixing that into the
scoring run makes it look like the report card is slow. Run this once; every run
afterwards is offline, instant, and byte-identical.
"""

from __future__ import annotations

import os
import sys
import time

from app import cohort, thinker, watcher


def main() -> int:
    rows = cohort.build_cohort()
    signals = watcher.scan([case for case, _ in rows], as_of=cohort.AS_OF)
    # Honour RECOUP_MODEL the same way make_recoup_policy() does. These two
    # disagreeing is how you warm one model's cache and score another's, which
    # looks exactly like a model that never answers.
    brain = thinker.Thinker(
        model=os.environ.get("RECOUP_MODEL", thinker.DEFAULT_MODEL),
        offline=False,
    )
    print(f"planner: {brain.model}", flush=True)

    def key(signal):
        return brain.cache.key(brain.model, brain.prompt_for(signal), thinker.SYSTEM_PROMPT)

    for attempt in range(1, 41):
        missing = [s for s in signals if brain.cache.get(key(s)) is None]
        if not missing:
            print(f"cache complete: {len(signals)} cases planned", flush=True)
            brain.close()
            return 0
        print(f"round {attempt}: {len(missing)} still to plan", flush=True)
        try:
            stats = brain.prewarm(signals, workers=3, progress=True)
            # prewarm collects why each call failed and this loop used to drop it,
            # so a round that failed 38 out of 38 looked identical to a slow one.
            # A retry loop that cannot say what it is retrying against is a
            # spin loop with extra steps.
            for line in stats.get("first_errors", []):
                print(f"    ! {line}", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        time.sleep(4)

    brain.close()
    print("gave up with cases still unplanned", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
