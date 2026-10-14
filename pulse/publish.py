"""One pull, published as a static site: what GitHub Actions runs every morning.

    uv run pulse-publish --out site            # pull, then write the site
    uv run pulse-publish --out site --no-pull  # just rebuild the site from what's stored

The site is the same page as the local app, reading files instead of a live server:
  site/index.html        the page, told it's a published copy (Pull opens GitHub instead)
  site/state.json        sources and the list of pulls
  site/pulls/<id>.json   each pull's digest, newest 30
  site/manifest.webmanifest + icons, so "Add to Home Screen" gives it a proper icon

It never writes profile.md into the site: in the cloud, your profile comes from the
PULSE_PROFILE secret and only shapes the explanations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import zlib
from pathlib import Path

from . import brain
from . import sources as catalog
from .pipeline import run_pull
from .store import Store

HERE = Path(__file__).resolve().parent
KEEP_PULLS = 30
SCHEDULE = "every morning at 7:30 (India time)"


# ---------------------------------------------------------------- the site


def export_site(store: Store, out: Path, repo: str, keep: int = KEEP_PULLS) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "pulls").mkdir(exist_ok=True)
    for old in (out / "pulls").glob("*.json"):  # a rebuilt site holds exactly the pulls it lists
        old.unlink()

    pulls = [p for p in store.pulls(limit=keep) if p["status"] != "running"]
    for p in pulls:
        (out / "pulls" / f"{p['id']}.json").write_text(json.dumps(store.digest(p["id"])))

    claude = brain.ClaudeCLI
    state = {
        "running": False,
        "engines": {claude.name: {"label": claude.label, "ok": True, "detail": ""}},
        "sources": [{"name": s.name, "group": s.group, "why": s.why, "url": s.url} for s in catalog.SOURCES],
        "pulls": pulls,
    }
    (out / "state.json").write_text(json.dumps(state))

    config = {
        "repo": repo,
        "run_url": f"https://github.com/{repo}/actions/workflows/pulse.yml" if repo else "",
        "schedule": SCHEDULE,
    }
    page = (HERE / "static" / "index.html").read_text()
    for placeholder, value in (
        ("<script>window.PULSE_STATIC = null;</script>", f"<script>window.PULSE_STATIC = {json.dumps(config)};</script>"),
        ("<!--published-head-->", PUBLISHED_HEAD),
    ):
        if placeholder not in page:
            raise RuntimeError(f"index.html lost its {placeholder!r} placeholder")
        page = page.replace(placeholder, value)
    (out / "index.html").write_text(page)

    (out / ".nojekyll").write_text("")  # serve files as they are; don't run GitHub's Jekyll over them
    (out / "manifest.webmanifest").write_text(json.dumps(MANIFEST, indent=1))
    for size in (180, 512):
        (out / f"icon-{size}.png").write_bytes(icon(size))


PUBLISHED_HEAD = """<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<link rel="icon" href="icon-180.png">
<meta name="apple-mobile-web-app-title" content="AI Pulse">
<meta name="theme-color" content="#F3F5F8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#131B2C" media="(prefers-color-scheme: dark)">"""

MANIFEST = {
    "name": "AI Pulse",
    "short_name": "AI Pulse",
    "description": "What's new in AI, sorted into learn / know / skip.",
    "start_url": "./",
    "display": "standalone",
    "background_color": "#131B2C",
    "theme_color": "#131B2C",
    "icons": [
        {"src": "icon-180.png", "sizes": "180x180", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


# ---------------------------------------------------------------- the home-screen icon


def icon(size: int) -> bytes:
    """The page's strip as an icon: tall yellow (learn), mid blue (know), short grey (skip)."""
    ink, colours = (19, 27, 44), {"learn": (235, 203, 69), "know": (141, 176, 227), "skip": (74, 86, 112)}
    plan = ["learn"] * 3 + ["know"] * 4 + ["skip"] * 6
    heights = {"learn": 0.52, "know": 0.32, "skip": 0.13}
    left, right, base = 0.17, 0.83, 0.78
    slot = (right - left) / len(plan)
    bars = [(left + i * slot, left + i * slot + slot * 0.72, base - heights[kind], colours[kind])
            for i, kind in enumerate(plan)]

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        u, v = (x + 0.5) / size, (y + 0.5) / size
        for x0, x1, top, colour in bars:
            if x0 <= u < x1 and top <= v < base:
                return colour
        return ink

    return png(size, size, pixel)


def png(width: int, height: int, pixel) -> bytes:
    """The smallest honest PNG encoder: 8-bit RGB, no filters. Good enough for an icon."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0: none
        for x in range(width):
            rows.extend(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 bits per channel, RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


# ---------------------------------------------------------------- the command


def say(event: dict) -> None:
    """One readable line per progress event, for the Actions log."""
    kind = event.get("type")
    if kind == "source":
        print(f"  {event['name']}: {event['error'] or str(event['count']) + ' recent'}", flush=True)
    elif kind == "new":
        print(f"{event['count']} new of {event['fetched']} fetched", flush=True)
    elif kind == "stage":
        print(f"{event['stage']}: {event['total']}", flush=True)
    elif kind == "start":
        print(f"Pull {event['pull_id']} with {event['engine']}, looking back to {event['since']}", flush=True)
    elif kind in ("done", "error", "warning"):
        print(f"{kind}: {json.dumps({k: v for k, v in event.items() if k != 'type'})[:600]}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pulse-publish", description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="site", help="where to write the site (default: site/)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name, for the Pull link")
    ap.add_argument("--engine", default=os.environ.get("PULSE_ENGINE", "claude"), choices=sorted(brain.ENGINES))
    ap.add_argument("--no-pull", action="store_true", help="rebuild the site from what's stored; don't pull")
    args = ap.parse_args(argv)

    store = Store(os.environ.get("PULSE_DB", "data/cloud.db"))
    ok = True
    if not args.no_pull:
        if os.environ.get("GITHUB_ACTIONS") and args.engine == "claude" and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            # Fail before pulling: the site keeps showing the last good digest instead of an error.
            print("::error::The CLAUDE_CODE_OAUTH_TOKEN secret isn't set. Run `claude setup-token` on your Mac, then "
                  "`gh secret set CLAUDE_CODE_OAUTH_TOKEN` (see README, 'On your phone').", flush=True)
            ok = False
        else:
            engine = brain.ENGINES[args.engine]()
            ready, detail = asyncio.run(engine.status())
            if not ready:
                print(f"::error::{detail}", flush=True)
                ok = False
            else:
                ok = asyncio.run(run_pull(store, engine, say)) is not None

    export_site(store, Path(args.out), args.repo)
    print(f"Site written to {args.out}/ ({'pull ok' if ok else 'no new pull'}).", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
