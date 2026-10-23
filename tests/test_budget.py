"""Checks for the rule in pulse/budget.py that picks the 5 cards.

If you change the rule, these say whether it still does the basics right:

    uv run pytest tests/test_budget.py -v
"""

from __future__ import annotations

from datetime import timedelta

from pulse.budget import MAX_LEARN, pick_learn
from pulse.fetch import utcnow
from pulse.models import Item, Verdict


def verdict(n: int, hits: list[str], signal=None, days_old: float = 1, source: str = "Blog", score: int = 5) -> Verdict:
    item = Item(id=f"id{n}", source=source, title=f"item {n}", url=f"https://example.com/{n}",
                published=utcnow() - timedelta(days=days_old), signal=signal)
    return Verdict(item=item, bucket="learn", hits=hits, score=score)


def pile() -> list[Verdict]:
    return [
        verdict(1, ["interview"], signal=40, source="Hugging Face Papers"),
        verdict(2, ["code"], signal=900, source="Hacker News"),
        verdict(3, ["mac"], days_old=5),
        verdict(4, ["code", "interview"], days_old=2),
        verdict(5, ["code"], signal=150, source="Hacker News"),
        verdict(6, ["interview"], days_old=0.5),
        verdict(7, ["mac", "code"], signal=300, source="Hacker News"),
        verdict(8, ["interview"], days_old=6),
    ]


def test_keeps_no_more_than_the_budget():
    keep, demote = pick_learn(pile())
    assert len(keep) <= MAX_LEARN, (
        f"pick_learn kept {len(keep)} of 8 with a budget of {MAX_LEARN}: "
        "the TODO in pulse/budget.py is still the placeholder"
    )


def test_every_item_lands_in_exactly_one_pile():
    candidates = pile()
    keep, demote = pick_learn(candidates)
    ids = [v.item.id for v in keep + demote]
    assert sorted(ids) == sorted(v.item.id for v in candidates), "an item went missing or was counted twice"


def test_respects_a_different_budget():
    keep, _ = pick_learn(pile(), max_learn=2)
    assert len(keep) == 2


def test_a_small_pile_is_kept_whole():
    small = pile()[:3]
    keep, demote = pick_learn(small)
    assert len(keep) == 3 and demote == []


def test_a_clearly_better_item_is_never_cut_for_a_worse_one():
    """'Clearly better' = at least as good on EVERY field you could rank by. Any sensible rule agrees."""
    weak = verdict(90, ["interview"], signal=60, days_old=6, source="Hacker News", score=3)
    strong = verdict(91, ["code", "interview"], signal=900, days_old=0.5, source="Hacker News", score=9)
    fillers = [verdict(n, ["interview"], signal=200, days_old=3, source="Hacker News", score=5) for n in range(5)]
    keep, _ = pick_learn([weak, *fillers, strong])  # the strong one arrives last
    kept = [v.item.id for v in keep]
    assert "id91" in kept, "a 9/10 item that passed two questions was cut while weaker ones stayed"
    if "id90" in kept:
        assert kept.index("id91") < kept.index("id90"), "the weaker item is read before the stronger one"


def test_the_best_item_comes_first():
    best = verdict(99, ["code", "mac", "interview"], signal=1500, days_old=0.2, source="Hacker News", score=10)
    keep, _ = pick_learn([*pile(), best])
    assert keep[0].item.id == "id99", "the item that wins on every field should be the first thing you read"



def test_one_chatty_source_cannot_take_every_slot():
    changelog = [verdict(n, ["code"], days_old=1, source="Claude API release notes", score=8) for n in range(6)]
    big_story = verdict(50, ["interview"], signal=900, days_old=1, source="Hacker News", score=7)
    keep, _ = pick_learn([*changelog, big_story])
    sources = [v.source for v in keep]
    assert "Hacker News" in sources, "the week's big story lost to a sixth changelog line"
    assert len(keep) == MAX_LEARN  # the spread doesn't leave slots empty: they're filled afterwards


def test_reading_order_is_most_important_first():
    keep, _ = pick_learn(pile())
    scores = [(v.score, len(v.hits)) for v in keep]
    assert scores == sorted(scores, reverse=True)
