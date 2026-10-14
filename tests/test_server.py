"""The HTTP layer: the local-only guard, and a pull driven exactly the way the page drives it."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("PULSE_DB", os.path.join(tempfile.mkdtemp(), "test.db"))  # never touch the real db

from fastapi.testclient import TestClient  # noqa: E402

from pulse import pipeline, server, sources  # noqa: E402
from pulse.store import Store  # noqa: E402

from .conftest import FakeEngine, make_item  # noqa: E402

LOCAL = {"host": "127.0.0.1:8765"}


@pytest.fixture
def app(monkeypatch, fake_source):
    monkeypatch.setattr(server, "store", Store(":memory:"))
    monkeypatch.setattr(server, "engines", {"fake": FakeEngine()})
    monkeypatch.setattr(sources, "SOURCES", [fake_source.source])
    monkeypatch.setattr(pipeline, "reload_user_code", lambda emit: None)  # a reload would undo the patch above

    async def from_item(client, item, limit=9_000):
        return item.content

    monkeypatch.setattr(pipeline, "full_text", from_item)
    fake_source.items = [make_item("learn about evals"), make_item("know about a model"), make_item("a funding round")]
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        yield client


def test_state_lists_engines_sources_and_no_pulls_yet(app):
    state = app.get("/api/state").json()
    assert state["engines"]["fake"]["ok"] is True
    assert [s["name"] for s in state["sources"]] == ["Fake"]
    assert state["pulls"] == [] and state["running"] is False


def test_requests_for_another_host_are_refused(app):
    assert app.get("/api/state", headers={"host": "evil.example.com"}).status_code == 403


def test_a_cross_site_post_is_refused(app):
    r = app.post("/api/pull", json={"engine": "fake"}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_a_non_json_post_is_refused(app):
    r = app.post("/api/pull", content="engine=fake", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_a_full_pull_through_http(app):
    r = app.post("/api/pull", json={"engine": "fake"}, headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200, r.text

    events = []
    with app.stream("GET", "/api/pull/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                events.append(json.loads(line[6:]))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done", kinds
    assert "source" in kinds and "new" in kinds
    sorted_events = [e for e in events if e["type"] == "progress" and e.get("sorted")]
    assert sorted_events, "triage progress should carry per-item buckets for the strip"

    digest = app.get(f"/api/pulls/{events[-1]['pull_id']}").json()
    assert [r["title"] for r in digest["learn"]] == ["learn about evals"]
    assert app.get("/api/state").json()["pulls"][0]["learn"] == 1


def test_unknown_pull_is_a_404(app):
    assert app.get("/api/pulls/999").status_code == 404


def test_the_page_refuses_to_be_framed_by_another_site(app):
    r = app.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
