"""A whole pull, end to end, with a fake engine and a fake source."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from pulse import brain, pipeline
from pulse.fetch import utcnow
from pulse.pipeline import run_pull, select_new, window_start
from pulse.sources import Source

from .conftest import FakeEngine, make_item


@pytest.fixture(autouse=True)
def no_network_reads(monkeypatch):
    async def from_item(client, item, limit=9_000):
        return item.content or item.summary

    monkeypatch.setattr(pipeline, "full_text", from_item)


def pull(store, engine, source):
    events = []
    pull_id = asyncio.run(run_pull(store, engine, events.append, [source]))
    return pull_id, events


# ---------------------------------------------------------------- the window


def test_window_always_reaches_seven_days_behind_the_last_pull_and_never_past_thirty():
    now = utcnow()
    assert window_start(now, None) == now - timedelta(days=7)
    assert window_start(now, now - timedelta(days=2)) == now - timedelta(days=9)
    assert window_start(now, now - timedelta(days=7)) == now - timedelta(days=14)  # a weekly reader keeps a week of overlap
    assert window_start(now, now - timedelta(days=90)) == now - timedelta(days=30)


def test_select_new_filters_old_merges_duplicates_and_caps(store):
    src_a = Source("A", "fake", "u", "w", cap=2)
    src_b = Source("B", "fake", "u", "w", cap=5)
    shared = "https://lab.example.com/launch"
    items = [
        make_item("a1", "A", signal=10), make_item("a2", "A", signal=30), make_item("a3", "A", signal=20),
        make_item("too old", "A", days_old=40),
        make_item("launch via HN", "A", signal=500, url=shared),
        make_item("launch via feed", "B", url=shared),
    ]
    items[-1].discussion_url = ""
    picked, passed_over = select_new(items, store, utcnow() - timedelta(days=7), [src_a, src_b])
    titles = [i.title for i in picked]
    assert "too old" not in titles
    assert titles.count("launch via HN") + titles.count("launch via feed") == 1  # one item per URL
    from_a = [i for i in picked if i.source == "A"]
    assert len(from_a) == 2 and from_a[0].signal == 500  # cap 2, best crowd score first
    assert sorted(i.title for i in passed_over) == ["a1", "a3"]  # what the cap left out is reported, not lost


# ---------------------------------------------------------------- full pulls


def test_a_pull_sorts_explains_saves_and_never_repeats(store, fake_source):
    fake_source.items = [make_item("learn about caching"), make_item("know about a launch"), make_item("funding round")]
    engine = FakeEngine()
    pull_id, events = pull(store, engine, fake_source.source)

    assert events[-1]["type"] == "done"
    digest = store.digest(pull_id)
    assert [r["title"] for r in digest["learn"]] == ["learn about caching"]
    assert digest["learn"][0]["card"]["term"] == "term of learn about caching"
    assert [r["title"] for r in digest["know"]] == ["know about a launch"]
    assert [r["title"] for r in digest["skip"]] == ["funding round"]
    assert digest["pull"]["stats"]["usage"]["calls"] == len(engine.calls)  # every model call is counted

    _, events = pull(store, FakeEngine(), fake_source.source)  # same items again
    assert next(e for e in events if e["type"] == "new")["count"] == 0
    assert events[-1]["type"] == "done"


def test_a_failed_triage_marks_nothing_as_seen(store, fake_source):
    fake_source.items = [make_item("learn about caching")]
    pull_id, events = pull(store, FakeEngine(fail_on="triage"), fake_source.source)
    assert pull_id is None and events[-1]["type"] == "error"
    assert store.pulls()[0]["status"] == "failed"

    pull_id, _ = pull(store, FakeEngine(), fake_source.source)  # next pull gets the item after all
    assert [r["title"] for r in store.digest(pull_id)["learn"]] == ["learn about caching"]


def test_a_failed_explanation_still_shows_the_item_and_is_retried_next_pull(store, fake_source):
    fake_source.items = [make_item("learn about caching")]
    pull_id, _ = pull(store, FakeEngine(fail_on="explain"), fake_source.source)
    learn = store.digest(pull_id)["learn"]
    assert learn[0]["card"]["error"].startswith("fake explain failure")

    retry_id, _ = pull(store, FakeEngine(), fake_source.source)  # e.g. after the rate limit resets
    retried = store.digest(retry_id)["learn"]
    assert [r["title"] for r in retried] == ["learn about caching"] and "error" not in retried[0]["card"]
    assert store.digest(pull_id)["learn"] == []  # moved to the pull that explained it, not shown twice


def test_every_source_failing_is_an_error_not_an_empty_digest(store, fake_source):
    fake_source.fail = True
    pull_id, events = pull(store, FakeEngine(), fake_source.source)
    assert pull_id is None
    assert "Every source failed" in events[-1]["message"]


def test_items_over_budget_move_to_know_not_the_bin(store, fake_source, monkeypatch):
    fake_source.items = [make_item(f"learn thing {n}") for n in range(4)]
    monkeypatch.setattr(pipeline.budget, "pick_learn", lambda cands: (cands[:1], cands[1:]))
    pull_id, _ = pull(store, FakeEngine(), fake_source.source)
    digest = store.digest(pull_id)
    assert len(digest["learn"]) == 1
    assert len(digest["know"]) == 3 and all(r["demoted"] for r in digest["know"])


def test_a_crashing_budget_rule_falls_back_to_the_first_few_and_says_so(store, fake_source, monkeypatch):
    fake_source.items = [make_item("learn one"), make_item("learn two")]

    def broken(cands):
        raise ValueError("oops")

    monkeypatch.setattr(pipeline.budget, "pick_learn", broken)
    pull_id, events = pull(store, FakeEngine(), fake_source.source)
    assert len(store.digest(pull_id)["learn"]) == 2
    assert any(e["type"] == "warning" and "oops" in e["message"] for e in events)


def test_the_same_story_from_three_sources_becomes_one_item(store, fake_source):
    fake_source.items = [make_item("know Mistral Large 4 launch", signal=1900), make_item("know Mistral Large 4 review"),
                         make_item("know Mistral Large 4 pricing"), make_item("know something else")]
    same = [["know Mistral Large 4 launch", "know Mistral Large 4 review", "know Mistral Large 4 pricing"]]
    pull_id, events = pull(store, FakeEngine(same_story=same), fake_source.source)
    digest = store.digest(pull_id)
    know = {r["title"]: r for r in digest["know"]}
    assert set(know) == {"know Mistral Large 4 launch", "know something else"}  # the highest-signal copy stays
    assert {a["title"] for a in know["know Mistral Large 4 launch"]["also"]} == {
        "know Mistral Large 4 review", "know Mistral Large 4 pricing"}
    assert all(r["reason"].startswith("Same story as") for r in digest["skip"])
    assert digest["pull"]["stats"]["merged"] == 2


def test_items_over_a_cap_do_not_flood_the_next_pull_once_settled(store, fake_source, monkeypatch):
    monkeypatch.setattr(fake_source, "source", Source("Fake", "fake", "u", "w", cap=2))
    old = [make_item(f"skip old {n}", days_old=3, signal=100 - n) for n in range(4)]  # settled: 3 days old
    fresh = [make_item(f"skip fresh {n}", days_old=0.2, signal=10 - n) for n in range(3)]  # still climbing
    fake_source.items = old + fresh
    pull_id, events = pull(store, FakeEngine(), fake_source.source)
    assert store.digest(pull_id)["pull"]["stats"]["over_cap"] == {"Fake": 5}

    _, events = pull(store, FakeEngine(), fake_source.source)  # same feed again
    new = next(e for e in events if e["type"] == "new")
    assert new["count"] == 2  # only the fresh leftovers get another chance; settled ones stay passed over


def test_an_item_one_source_passed_over_can_still_arrive_from_another(store, fake_source, monkeypatch):
    repo = "https://github.com/someone/agent-tool"
    monkeypatch.setattr(fake_source, "source", Source("Fake", "fake", "u", "w", cap=1))
    fake_source.items = [make_item("skip top repo", days_old=3, signal=50), make_item("skip repo", days_old=3, signal=1, url=repo)]
    pull(store, FakeEngine(), fake_source.source)  # the repo loses to the cap and is passed over

    hn = Source("Hacker News", "fake", "u", "w", cap=5)
    fake_source.items = [make_item("know repo on HN", source="Hacker News", days_old=3, signal=800, url=repo)]
    pull_id, _ = pull(store, FakeEngine(), hn)
    assert [r["title"] for r in store.digest(pull_id)["know"]] == ["know repo on HN"]


def test_one_failed_triage_batch_cancels_the_others(store, fake_source):
    import asyncio as aio

    from .conftest import FakeEngine as Base

    class OneBadBatch(Base):
        triage_batch, parallel = 2, 4

        def __init__(self):
            super().__init__()
            self.finished = 0
            self.cancelled = 0

        async def ask(self, role, system, prompt, schema, usage):
            if role != "triage":
                return await super().ask(role, system, prompt, schema, usage)
            if "learn 0" in prompt:  # the first batch fails at once
                raise brain.EngineError("rate limited")
            try:
                await aio.sleep(5)  # the others are slow, like real claude calls
            except aio.CancelledError:
                self.cancelled += 1
                raise
            self.finished += 1
            return await super().ask(role, system, prompt, schema, usage)

    fake_source.items = [make_item(f"learn {n}") for n in range(8)]
    engine = OneBadBatch()
    pull_id, events = pull(store, engine, fake_source.source)
    assert pull_id is None and events[-1]["type"] == "error"
    assert engine.finished == 0 and engine.cancelled == 3  # nobody kept spending after the failure


def test_an_item_with_too_little_text_is_not_explained_and_the_next_one_steps_in(store, fake_source, monkeypatch):
    thin = make_item("learn talk with only a title", signal=900)
    thin.content = ""
    fake_source.items = [thin, make_item("learn a real post", signal=10), make_item("learn another post", signal=5)]
    monkeypatch.setattr(pipeline.budget, "pick_learn", lambda cands: (cands[:2], cands[2:]))

    async def text_of(client, item, limit=9_000):
        return item.content or item.title

    monkeypatch.setattr(pipeline, "full_text", text_of)
    pull_id, _ = pull(store, FakeEngine(), fake_source.source)
    digest = store.digest(pull_id)
    assert {r["title"] for r in digest["learn"]} == {"learn a real post", "learn another post"}  # the next one stepped in
    know = {r["title"]: r for r in digest["know"]}
    assert "Too little detail" in know["learn talk with only a title"]["reason"]
    assert digest["pull"]["stats"]["too_thin"] == 1


def test_cards_the_checker_flags_are_written_again_with_its_notes(store, fake_source):
    fake_source.items = [make_item("learn a wrong picture"), make_item("learn a fine card")]
    engine = FakeEngine()
    pull_id, events = pull(store, engine, fake_source.source)
    stats = store.digest(pull_id)["pull"]["stats"]
    assert stats["fixed"] == 1  # only the flagged card is rewritten
    assert engine.calls.count("check") == 1 and engine.calls.count("explain") == 2
    assert any(e.get("stage") == "check" for e in events)
