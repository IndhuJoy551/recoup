"""Fill the planner cache for the whole cohort, retrying around rate limits.

    python -m scripts.warm_cache

Separate from the report card on purpose. On a free-tier API key the 300
planning calls take twenty minutes of mostly waiting, and mixing that into the
scoring run makes it look like the report card is slow. Run this once; every run
afterwards is offline, instant, and byte-identical.
"""

from __future__ import annotations

import sys
import time

from app import cohort, thinker, watcher


def main() -> int:
    rows = cohort.build_cohort()
    signals = watcher.scan([case for case, _ in rows], as_of=cohort.AS_OF)
    brain = thinker.Thinker(offline=False)

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
            brain.prewarm(signals, workers=3, progress=False)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        time.sleep(4)

    brain.close()
    print("gave up with cases still unplanned", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
