"""Your rule: how much you'll actually read per pull, and what wins when there's too much.

Triage (the fast first pass) can mark any number of items "learn". Each one you keep
costs a full-article fetch, a slower explanation call, and about five minutes of your
attention. This function decides which ones you get, and in what order.

Nothing you drop is thrown away: it moves to "Know" as a one-liner, labelled
"didn't fit your budget", so you can still see that it exists.
"""

from __future__ import annotations

from .models import Verdict

MAX_LEARN = 5  # full explanations per pull: the size of one sitting


def pick_learn(candidates: list[Verdict], max_learn: int = MAX_LEARN) -> tuple[list[Verdict], list[Verdict]]:
    """Split triage's "learn" items into (keep, demote). `keep` is in reading order.

    What you can rank on, for each candidate v:
      v.score      1-10: how much triage thinks it matters to you this week
      v.hits       which of your questions it passed: a list drawn from ["code", "mac", "interview"]
      v.signal     crowd score (Hacker News points, Hugging Face upvotes...), or None for blogs.
                   Careful: 300 HN points and 300 HF upvotes are not the same amount of excitement.
      v.published  when it came out (a UTC datetime), or None
      v.source     "Simon Willison", "Hacker News", ...
    """
    # TODO(you): decide the order you want to read these in (5-10 lines), then cut at max_learn.
    # Placeholder: the first max_learn in the order the sources happen to be listed. It cuts,
    # so the page stays finishable, but it doesn't rank: a 9/10 item can lose to a 4/10 one.
    return candidates[:max_learn], candidates[max_learn:]
