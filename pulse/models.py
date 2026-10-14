"""The two shapes every other module passes around.

An Item is what a source said. A Verdict is what the LLM decided about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

BUCKETS = ("learn", "know", "skip")
HITS = ("code", "mac", "interview")


@dataclass
class Item:
    """One thing that happened, as a source reported it."""

    id: str  # hash of the canonical URL: how we know we've already shown it to you
    source: str  # "Simon Willison", "Hacker News", ...
    title: str
    url: str
    published: datetime | None  # UTC; None when the source gives no date
    summary: str = ""  # short plain text from the feed
    content: str = ""  # full article text, when the source ships it or we fetched it
    signal: int | None = None  # crowd signal: HN points, HF upvotes, ...
    signal_label: str = ""  # "points", "upvotes", ...
    discussion_url: str = ""  # e.g. the Hacker News comments page


@dataclass
class Verdict:
    """What triage decided about one Item, plus the explanation if it earned one."""

    item: Item
    bucket: str  # "learn" | "know" | "skip"
    hits: list[str] = field(default_factory=list)  # which of your 3 questions it passed
    headline: str = ""  # the title rewritten in plain English
    reason: str = ""  # why learn / why it's news / why it's safe to skip
    score: int = 0  # 1-10 from triage: how much this matters to you this week
    card: dict | None = None  # full explanation, "learn" items only
    demoted: bool = False  # triage said "learn" but it didn't fit your budget
    also: list[Item] = field(default_factory=list)  # the same story from other sources, merged into this one

    # Shortcuts, so code that ranks verdicts reads naturally: v.signal, v.published, v.source
    @property
    def signal(self) -> int | None:
        return self.item.signal

    @property
    def published(self) -> datetime | None:
        return self.item.published

    @property
    def source(self) -> str:
        return self.item.source
