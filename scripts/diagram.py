"""Print the pipeline diagram from ARCHITECTURE.md.

    python -m scripts.diagram

Exists for the video. The 0:40 section of `VIDEO.md` needs the diagram on screen
while I name the five components, and the alternatives were both worse: a browser
tab means an extra window switch in the middle of the tightest part of the script,
and pasting the diagram into a second file means two copies that drift apart the
first time the architecture changes.

So it reads ARCHITECTURE.md and prints the block under "## The pipeline". One copy,
one source of truth, and the terminal is already open and already legible.
"""

from __future__ import annotations

import sys
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "ARCHITECTURE.md"
HEADING = "## The pipeline"
FENCE = "```"


def main() -> int:
    if not DOC.exists():
        print(f"not found: {DOC}", file=sys.stderr)
        return 1

    lines = DOC.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(HEADING)
    except ValueError:
        print(f"no {HEADING!r} heading in {DOC.name}", file=sys.stderr)
        return 1

    fences = [i for i, l in enumerate(lines[start:], start) if l.startswith(FENCE)]
    if len(fences) < 2:
        print(f"no fenced block under {HEADING!r}", file=sys.stderr)
        return 1

    print()
    for line in lines[fences[0] + 1:fences[1]]:
        print(line)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
