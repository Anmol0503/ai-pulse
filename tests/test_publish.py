"""The published copy: what GitHub Pages serves to your phone."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from pulse import brain, pipeline, publish
from pulse.pipeline import run_pull

from .conftest import FakeEngine, make_item


@pytest.fixture(autouse=True)
def no_network_reads(monkeypatch):
    async def from_item(client, item, limit=9_000):
        return item.content

    monkeypatch.setattr(pipeline, "full_text", from_item)


def one_pull(store, fake_source):
    fake_source.items = [make_item("learn about evals"), make_item("know about a launch"), make_item("a funding round")]
    return asyncio.run(run_pull(store, FakeEngine(), lambda e: None, [fake_source.source]))


def test_the_site_has_every_file_the_page_reads(store, fake_source, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")  # published by the daily cloud job
    pull_id = one_pull(store, fake_source)
    publish.export_site(store, tmp_path, "someone/ai-pulse")

    state = json.loads((tmp_path / "state.json").read_text())
    assert [p["id"] for p in state["pulls"]] == [pull_id] and state["running"] is False
    assert state["engines"]["claude"]["ok"] is True and len(state["sources"]) > 0

    assert state["pulls"][0]["headlines"] == ["headline of learn about evals"]  # what the list of days shows

    digest = json.loads((tmp_path / "pulls" / f"{pull_id}.json").read_text())
    assert [r["title"] for r in digest["learn"]] == ["learn about evals"]
    assert set(digest) == {"pull", "learn"} and "stats" not in digest["pull"]  # only what the page shows

    page = (tmp_path / "index.html").read_text()
    assert '"run_url": "https://github.com/someone/ai-pulse/actions/workflows/pulse.yml"' in page
    assert "window.PULSE_STATIC = null;" not in page and "<!--published-head-->" not in page
    assert '<link rel="manifest" href="manifest.webmanifest">' in page
    assert (tmp_path / ".nojekyll").exists()
    assert (tmp_path / "icon-180.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert json.loads((tmp_path / "manifest.webmanifest").read_text())["display"] == "standalone"


def test_the_profile_never_reaches_the_public_site(store, fake_source, tmp_path, monkeypatch):
    secret = "Lives in Pune, earns a secret amount"
    monkeypatch.setenv("PULSE_PROFILE", secret)
    assert brain.profile() == secret  # the cloud's profile comes from the secret...
    one_pull(store, fake_source)
    publish.export_site(store, tmp_path, "someone/ai-pulse")
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix != ".png":
            assert secret not in f.read_text(), f.name  # ...and is never written into the site


def test_a_rebuilt_site_drops_digests_it_no_longer_lists(store, fake_source, tmp_path):
    (tmp_path / "pulls").mkdir()
    (tmp_path / "pulls" / "999.json").write_text("{}")
    one_pull(store, fake_source)
    publish.export_site(store, tmp_path, "someone/ai-pulse")
    assert not (tmp_path / "pulls" / "999.json").exists()


def test_in_the_cloud_without_a_token_it_says_so_and_keeps_the_last_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("PULSE_DB", str(tmp_path / "cloud.db"))
    code = publish.main(["--out", str(tmp_path / "site"), "--repo", "someone/ai-pulse"])
    assert code == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().out
    assert (tmp_path / "site" / "index.html").exists()  # the site is still rebuilt
    assert json.loads((tmp_path / "site" / "state.json").read_text())["pulls"] == []  # and no failed pull was recorded


def test_rebuild_only(store, fake_source, tmp_path, monkeypatch):
    db = tmp_path / "cloud.db"
    monkeypatch.setenv("PULSE_DB", str(db))
    from pulse.store import Store

    one_pull(Store(db), fake_source)
    assert publish.main(["--out", str(tmp_path / "site"), "--no-pull", "--repo", "a/b"]) == 0
    assert len(json.loads((tmp_path / "site" / "state.json").read_text())["pulls"]) == 1


def test_a_copy_published_from_a_mac_promises_no_schedule_and_no_pull_link(store, fake_source, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PULSE_SCHEDULE", raising=False)
    one_pull(store, fake_source)
    publish.export_site(store, tmp_path, "someone/ai-pulse")
    page = (tmp_path / "index.html").read_text()
    assert '"run_url": ""' in page and '"schedule": ""' in page


@pytest.fixture
def india_clock():
    """The Mac's clock set to India time."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_the_morning_job_on_a_mac_promises_its_next_pull(store, fake_source, tmp_path, monkeypatch, india_clock):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("PULSE_SCHEDULE", "mac")
    one_pull(store, fake_source)
    publish.export_site(store, tmp_path, "someone/ai-pulse")
    page = (tmp_path / "index.html").read_text()
    assert '"schedule": "mac"' in page and '"next_utc": [2, 0]' in page  # 7:30 in India is 02:00 UTC
    assert '"run_url": ""' in page  # no GitHub job to open from a Mac-published copy


def test_every_day_stays_listed_newest_first(store, fake_source, tmp_path):
    one_pull(store, fake_source)
    fake_source.items = [make_item("learn about agents")]
    asyncio.run(run_pull(store, FakeEngine(), lambda e: None, [fake_source.source]))
    publish.export_site(store, tmp_path, "someone/ai-pulse")
    pulls = json.loads((tmp_path / "state.json").read_text())["pulls"]
    assert [p["headlines"] for p in pulls] == [["headline of learn about agents"], ["headline of learn about evals"]]
    assert all((tmp_path / "pulls" / f"{p['id']}.json").exists() for p in pulls)
