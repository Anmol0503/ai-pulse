"""One press of "Pull latest", start to finish.

  fetch every source -> keep only what's new -> triage (sort all)
  -> your budget (budget.py) -> read full articles -> explain -> save

Every step reports through emit(event), which the browser shows live.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import brain, budget
from .fetch import add_descriptions, fetch_all, full_text, new_client, parse_date, utcnow
from .models import Item, Verdict
from . import sources as catalog
from .sources import Source
from .store import Store, iso

FIRST_PULL_DAYS = 7
MAX_LOOKBACK_DAYS = 30
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def window_start(now: datetime, last_pull_started: datetime | None) -> datetime:
    """How far back to look.

    Always 7 days behind your previous pull (or behind now, the first time). Things often
    surface after the date they carry: a changelog entry stamped noon but posted at six, an
    HN story that crosses 50 points the next day, a source that was down last time. The
    seen-list in the database stops repeats, so the overlap costs nothing but a fetch.
    Never past 30 days, so a newly added source can't dump its whole archive on you.
    """
    start = now - timedelta(days=FIRST_PULL_DAYS)
    if last_pull_started:
        start = min(start, last_pull_started - timedelta(days=FIRST_PULL_DAYS))
    return max(start, now - timedelta(days=MAX_LOOKBACK_DAYS))


def _merge(keep: Item, other: Item) -> None:
    """The same page from two sources (say, a lab's feed and Hacker News): keep the best of both."""
    if keep.signal is None and other.signal is not None:
        keep.signal, keep.signal_label = other.signal, other.signal_label
    keep.discussion_url = keep.discussion_url or other.discussion_url
    if len(other.content) > len(keep.content):
        keep.content = other.content
    if len(other.summary) > len(keep.summary):
        keep.summary = other.summary


SETTLE = timedelta(days=2)  # how long a story gets to collect its points before it's judged on them


def select_new(items: list[Item], store: Store, since: datetime, sources: list[Source]) -> tuple[list[Item], list[Item]]:
    """In the window, never shown before, and at most `cap` per source (best crowd score, then newest).

    Returns (picked, passed_over). Passed-over items lost to the cap this time. The caller
    marks the settled ones (2+ days old, or undated) as seen, so they don't flood the next
    pull; fresher ones may still be climbing, so they get another chance.
    """
    fresh: dict[str, Item] = {}
    for it in items:
        if it.published and it.published < since:
            continue
        if it.id in fresh:
            _merge(fresh[it.id], it)
        else:
            fresh[it.id] = it
    seen = store.seen(list(fresh))
    passed = store.passed_over_by(list(fresh))  # {id: the source whose cap it lost to}
    by_source: dict[str, list[Item]] = defaultdict(list)
    for it in fresh.values():
        if it.id not in seen and passed.get(it.id) != it.source:  # lost to HN's cap? GitHub Trending may still show it
            by_source[it.source].append(it)
    caps = {s.name: s.cap for s in sources}
    picked: list[Item] = []
    passed_over: list[Item] = []
    for name, group in by_source.items():
        group.sort(key=lambda it: (it.signal or 0, it.published or _EPOCH), reverse=True)
        cap = caps.get(name, 8)
        picked.extend(group[:cap])
        passed_over.extend(group[cap:])
    return picked, passed_over


def settled(items: list[Item], now: datetime) -> list[Item]:
    # Undated items come from ranked lists (trending repos, trending models): one that misses the
    # cut today may top the list tomorrow, so it's never buried, just re-ranked next pull.
    return [it for it in items if it.published is not None and now - it.published >= SETTLE]


def _display_order(verdicts: list[Verdict], keep: list[Verdict]) -> list[Verdict]:
    """Learn in your budget's order; Know by crowd score then date; Skip grouped by source."""
    know = sorted(
        (v for v in verdicts if v.bucket == "know"),
        key=lambda v: (not v.demoted, v.signal or 0, v.published or _EPOCH),
        reverse=True,
    )
    skip = sorted((v for v in verdicts if v.bucket == "skip"), key=lambda v: (v.source, v.item.title))
    return keep + know + skip


def reload_user_code(emit) -> None:
    """budget.py and sources.py are yours to edit while the app runs: reload them on every pull,
    like profile.md, so pressing Pull uses what you just saved. A file that won't import keeps
    its last working version, and the page says why."""
    for module in (budget, catalog):
        try:
            importlib.reload(module)
        except Exception as exc:  # noqa: BLE001 — a typo in your file shouldn't cost you the pull
            name = module.__name__.rsplit(".", 1)[-1]
            emit({"type": "warning", "message": f"{name}.py didn't load ({type(exc).__name__}: {exc}); used the last version that did."})


async def run_pull(store: Store, engine, emit, sources: list[Source] | None = None) -> int | None:
    started = utcnow()
    clock = time.monotonic()
    try:
        if sources is None:
            reload_user_code(emit)
        sources = sources or catalog.SOURCES
        last = store.last_done()
        since = window_start(started, parse_date(last["started_at"]) if last else None)
        pull_id = store.start_pull(engine.name, since, started)
    except Exception as exc:  # noqa: BLE001 — e.g. data/pulse.db deleted while the app was running
        emit({"type": "error", "pull_id": None, "message": f"Couldn't start the pull: {exc}. If you deleted data/pulse.db, restart AI Pulse."})
        return None
    usage = brain.Usage()
    stats: dict = {"since": iso(since), "previous": last["started_at"] if last else None, "sources": {}, "errors": {}}
    emit({"type": "start", "pull_id": pull_id, "engine": engine.label, "since": iso(since),
          "sources": [s.name for s in sources]})

    def on_source(src: Source, count: int, error: str | None) -> None:
        stats["sources"][src.name] = count
        if error:
            stats["errors"][src.name] = error
        emit({"type": "source", "name": src.name, "count": count, "error": error})

    def progress(stage: str):
        def report(done: int, total: int, sorted_: dict | None = None) -> None:
            event = {"type": "progress", "stage": stage, "done": done, "total": total}
            if sorted_:
                event["sorted"] = sorted_  # {item id: bucket} — the page colours the strip as batches land
            emit(event)
        return report

    try:
        async with new_client() as client:
            items, _ = await fetch_all(client, sources, since, on_source)
            new, passed_over = select_new(items, store, since, sources)
            over_cap: dict[str, int] = defaultdict(int)
            for it in passed_over:
                over_cap[it.source] += 1
            stats.update(fetched=len(items), new=len(new), over_cap=dict(over_cap))
            emit({"type": "new", "count": len(new), "fetched": len(items), "ids": [it.id for it in new]})
            if len(stats["errors"]) == len(sources):
                raise RuntimeError("Every source failed. Are you online?")

            verdicts: list[Verdict] = []
            keep: list[Verdict] = []
            if new:
                emit({"type": "stage", "stage": "skim", "total": len(new)})
                stats["described"] = await add_descriptions(client, new)
                emit({"type": "stage", "stage": "triage", "total": len(new)})
                verdicts = await brain.triage(engine, new, usage, progress("triage"))
                if sum(v.bucket != "skip" for v in verdicts) >= 2:
                    emit({"type": "stage", "stage": "merge", "total": sum(v.bucket != "skip" for v in verdicts)})
                    stats["merged"] = await brain.merge_same_story(engine, verdicts, usage)

                learn = [v for v in verdicts if v.bucket == "learn"]
                try:
                    chosen, _ = budget.pick_learn(list(learn))
                except Exception as exc:  # noqa: BLE001 — a bug in budget.py shouldn't cost you the pull
                    limit = getattr(budget, "MAX_LEARN", 5)
                    chosen = learn[:limit]  # the placeholder's behaviour: still a finishable page
                    stats["budget_error"] = f"{type(exc).__name__}: {exc}"
                    emit({"type": "warning", "message": f"budget.py raised {stats['budget_error']}, so the first {limit} were kept."})
                learn_ids = {v.item.id for v in learn}
                keep, kept_ids = [], set()
                for v in chosen:  # keep your order; ignore duplicates and anything triage didn't pick
                    if v.item.id in learn_ids and v.item.id not in kept_ids:
                        keep.append(v)
                        kept_ids.add(v.item.id)
                for v in learn:
                    if v.item.id not in kept_ids:
                        v.bucket, v.demoted = "know", True

                if keep:
                    emit({"type": "stage", "stage": "read", "total": len(keep)})
                    texts = await asyncio.gather(*(full_text(client, v.item) for v in keep))
                    emit({"type": "stage", "stage": "explain", "total": len(keep)})
                    await brain.explain(engine, list(zip(keep, texts)), usage, progress("explain"))

        ordered = _display_order(verdicts, keep)
        counts = {b: sum(v.bucket == b for v in ordered) for b in ("learn", "know", "skip")}
        stats.update(usage=asdict(usage), seconds=round(time.monotonic() - clock, 1), counts=counts)
        store.finish_pull(pull_id, ordered, stats, utcnow(), passed_over=settled(passed_over, started))
        emit({"type": "done", "pull_id": pull_id, **counts, "usage": stats["usage"], "seconds": stats["seconds"]})
        return pull_id
    except Exception as exc:  # noqa: BLE001 — reported to the page; nothing is marked as seen
        message = str(exc) or type(exc).__name__
        stats.update(usage=asdict(usage), seconds=round(time.monotonic() - clock, 1))
        try:
            store.fail_pull(pull_id, message, stats, utcnow())
        except Exception:  # noqa: BLE001 — the database itself may be what broke; still tell the page
            pass
        emit({"type": "error", "pull_id": pull_id, "message": message})
        return None
