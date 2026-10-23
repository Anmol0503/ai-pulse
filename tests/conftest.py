"""Shared test doubles: a fake LLM engine and a fake source, so tests never touch the network."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from pulse import fetch
from pulse.brain import CARD_FIELDS, EngineError
from pulse.fetch import item_id, utcnow
from pulse.models import Item
from pulse.sources import Source
from pulse.store import Store

ITEM = re.compile(r'<item id="([^"]+)"[^>]*>\nsource: [^\n]*\ntitle: ([^\n]*)')


class FakeEngine:
    """Sorts by a word in the title ("learn ...", "know ...", anything else = skip)."""

    name, label = "fake", "Fake engine"
    triage_batch, explain_batch, parallel = 3, 2, 2

    def __init__(self, fail_on: str | None = None, same_story: list[list[str]] | None = None):
        self.fail_on = fail_on  # "triage" or "explain": that pass always raises
        self.same_story = same_story or []  # groups of TITLES the merge pass should report as one story
        self.calls: list[str] = []

    async def status(self):
        return True, "fake"

    async def ask(self, role, system, prompt, schema, usage):
        self.calls.append(role)
        usage.record("fake-model", 100, 20, 0.01, 0.001)
        if role == self.fail_on:
            raise EngineError(f"fake {role} failure")
        if "checks" in schema["properties"]:  # the fact-check pass: flag titles that say "wrong"
            checked = re.findall(r'<item id="([^"]+)">\ncard: (.*?)\nsource text:', prompt)
            return {"checks": [{"id": iid, "problems": ["the picture is wrong"] if "wrong" in card else []}
                               for iid, card in checked]}
        if "groups" in schema["properties"]:  # the same-story merge pass
            ids = dict(re.findall(r'<item id="([^"]+)">[^:]*: Plain: ([^<]*)</item>', prompt))
            by_title = {title: iid for iid, title in ids.items()}
            return {"groups": [[by_title[t] for t in group if t in by_title] for group in self.same_story]}
        found = ITEM.findall(prompt)
        if role == "triage":
            verdicts = []
            for iid, title in found:
                word = title.split()[0].lower()
                bucket = word if word in ("learn", "know") else "skip"
                verdicts.append({"id": iid, "bucket": bucket, "hits": ["code"] if bucket == "learn" else [],
                                 "score": 8 if bucket == "learn" else 4,
                                 "headline": f"Plain: {title}", "reason": f"because {bucket}"})
            return {"verdicts": verdicts}
        return {"cards": [{"id": iid, **{f: f"{f} of {title}" for f in CARD_FIELDS}} for iid, title in found]}


def make_item(title: str, source: str = "Fake", days_old: float = 1, signal=None, url=None) -> Item:
    url = url or f"https://example.com/{title.replace(' ', '-')}"
    return Item(id=item_id(url), source=source, title=title, url=url,
                published=utcnow() - timedelta(days=days_old), summary=f"About {title}.",
                content="x" * 2_000, signal=signal)


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def fake_source(monkeypatch):
    """A Source of kind "fake" whose items the test controls through `feed.items`."""

    class Feed:
        items: list[Item] = []
        fail = False

    feed = Feed()

    async def fake_fetcher(client, src, since):
        if feed.fail:
            raise RuntimeError("source down")
        return list(feed.items)

    monkeypatch.setitem(fetch.FETCHERS, "fake", fake_fetcher)
    feed.source = Source("Fake", "fake", "https://example.com/feed", "test source", cap=50)
    return feed
