"""How the 5 cards are chosen: most important first, and not all from one place.

Triage (the fast first pass) can mark a dozen items "learn". You get MAX_LEARN of them.
Each costs a full-article read, an explanation and about five minutes of you, so:

  1. Rank by triage's score (1-10), then by how many of your three questions the item
     passed, then by how new it is.
  2. First pass: at most MAX_PER_SOURCE from any one source, so five lines from one
     changelog can't crowd out the week's big story. If slots are still empty, fill
     them from what's left, still in rank order.

Nothing you drop is thrown away: it moves to "Know" as a one-liner. This file is yours:
change the rule, save, and the next pull uses it (no restart). Check it with
    uv run pytest tests/test_budget.py -v
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .models import Verdict

MAX_LEARN = 5  # cards per pull: the size of one sitting
MAX_PER_SOURCE = 2  # on the first pass
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def pick_learn(candidates: list[Verdict], max_learn: int = MAX_LEARN) -> tuple[list[Verdict], list[Verdict]]:
    """Split triage's "learn" items into (keep, demote). `keep` is in reading order."""
    ranked = sorted(candidates, key=lambda v: (v.score, len(v.hits), v.published or _OLDEST), reverse=True)
    keep: list[Verdict] = []
    kept: set[str] = set()
    per_source: Counter[str] = Counter()
    for v in ranked:  # pass 1: spread across sources
        if len(keep) < max_learn and per_source[v.source] < MAX_PER_SOURCE:
            keep.append(v)
            kept.add(v.item.id)
            per_source[v.source] += 1
    for v in ranked:  # pass 2: fill any empty slots, best first
        if len(keep) < max_learn and v.item.id not in kept:
            keep.append(v)
            kept.add(v.item.id)
    order = {v.item.id: n for n, v in enumerate(ranked)}
    keep.sort(key=lambda v: order[v.item.id])  # read the most important first
    return keep, [v for v in ranked if v.item.id not in kept]
