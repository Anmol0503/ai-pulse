"""The local web app: one page, a few JSON endpoints, and a live progress stream.

GET  /                   the page
GET  /api/state          engines, sources, past pulls
GET  /api/pulls/{id}     one pull's digest: learn / know / skip
POST /api/pull           start a pull (one at a time)
GET  /api/pull/events    the running pull's progress, as Server-Sent Events
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import brain
from . import sources as catalog
from .pipeline import run_pull
from .store import Store

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PULSE_DB", HERE.parent / "data" / "pulse.db"))

app = FastAPI(title="AI Pulse", docs_url=None, redoc_url=None)
store = Store(DB_PATH)
engines = {name: cls() for name, cls in brain.ENGINES.items()}


class Runner:
    """At most one pull at a time. Any number of browser tabs can watch it."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.events: list[dict] = []
        self.generation = 0
        self.changed = asyncio.Event()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        self.changed.set()

    def start(self, engine) -> None:
        self.generation += 1
        self.events = []
        mine = self.generation

        def emit(event: dict) -> None:  # a straggler from an earlier pull can't write into this one's stream
            if mine == self.generation:
                self.emit(event)

        self.task = asyncio.create_task(run_pull(store, engine, emit))
        self.task.add_done_callback(lambda _task: self.changed.set())

    async def stream(self):
        """Replay this pull's events so far, then follow along until it ends."""
        generation, sent = self.generation, 0
        while generation == self.generation:
            while sent < len(self.events):
                yield self.events[sent]
                sent += 1
            if not self.running:
                return
            self.changed.clear()
            if sent < len(self.events):
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield None  # heartbeat: keeps the connection visibly alive during long LLM calls


runner = Runner()

# ---------------------------------------------------------------- local-only guard

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Only this Mac's browser may drive the app.

    Host check: stops DNS-rebinding, where a website points its own domain at 127.0.0.1.
    Origin check: stops another site's page from silently POSTing "pull" and spending your plan.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in LOCAL_HOSTS:
        return JSONResponse({"detail": "AI Pulse only answers on localhost."}, status_code=403)
    origin = request.headers.get("origin")
    if request.method != "GET" and origin:
        origin_host = origin.split("://", 1)[-1].rsplit(":", 1)[0]
        if origin_host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    response = await call_next(request)
    # No other site may frame this page and trick a click on "Pull latest".
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


# ---------------------------------------------------------------- routes


@app.get("/")
async def page():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def state():
    statuses = await asyncio.gather(*(e.status() for e in engines.values()))
    return {
        "running": runner.running,
        "engines": {
            name: {"label": e.label, "ok": ok, "detail": detail}
            for (name, e), (ok, detail) in zip(engines.items(), statuses)
        },
        "sources": [{"name": s.name, "group": s.group, "why": s.why, "url": s.url} for s in catalog.SOURCES],
        "pulls": store.pulls(),
    }


@app.get("/api/pulls/{pull_id}")
async def pull_digest(pull_id: int):
    digest = store.digest(pull_id)
    if digest is None:
        raise HTTPException(404, "No such pull.")
    return digest


@app.post("/api/pull")
async def start_pull(request: Request):
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "Send JSON.")
    body = await request.json()
    engine = engines.get(str(body.get("engine", "claude")))
    if engine is None:
        raise HTTPException(400, "Unknown engine.")
    if runner.running:
        raise HTTPException(409, "A pull is already running.")
    ok, detail = await engine.status()
    if not ok:
        raise HTTPException(400, detail)
    runner.start(engine)
    return {"started": True, "engine": engine.name}


@app.get("/api/pull/events")
async def pull_events():
    async def sse():
        async for event in runner.stream():
            yield ": ping\n\n" if event is None else f"data: {json.dumps(event)}\n\n"
        yield 'event: end\ndata: {}\n\n'

    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
