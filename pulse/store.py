"""Everything AI Pulse remembers lives in one SQLite file (data/pulse.db).

pulls  one row per press of "Pull latest"
items  every item a finished pull has shown you, with its verdict and explanation.
       An id being in this table is how the next pull knows the item isn't new.

Items are written only when a pull finishes. A pull that fails part-way marks
nothing as seen, so the next pull simply picks the same items up again.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Item, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS pulls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    engine       TEXT NOT NULL,
    window_start TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    error        TEXT,
    stats        TEXT NOT NULL DEFAULT '{}'        -- JSON: per-source counts, errors, LLM usage
);
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    pull_id        INTEGER NOT NULL REFERENCES pulls(id),
    rank           INTEGER NOT NULL,               -- display order within its bucket
    source         TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    discussion_url TEXT NOT NULL DEFAULT '',
    published_at   TEXT,
    signal         INTEGER,
    signal_label   TEXT NOT NULL DEFAULT '',
    bucket         TEXT NOT NULL,                  -- learn | know | skip
    hits           TEXT NOT NULL DEFAULT '[]',
    headline       TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    score          INTEGER NOT NULL DEFAULT 0,     -- 1-10 from triage
    card           TEXT,                           -- JSON explanation, learn items only
    demoted        INTEGER NOT NULL DEFAULT 0,
    also           TEXT NOT NULL DEFAULT '[]'      -- JSON: the same story from other sources
);
CREATE INDEX IF NOT EXISTS items_by_pull ON items(pull_id);
CREATE TABLE IF NOT EXISTS passed_over (                -- lost to a source's cap: never shown, never repeated
    id      TEXT PRIMARY KEY,
    pull_id INTEGER NOT NULL REFERENCES pulls(id),
    source  TEXT NOT NULL
);
"""


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._add_missing_columns()
        # A pull still marked "running" at startup died with the previous server process.
        self.db.execute(
            "UPDATE pulls SET status='failed', error='Interrupted: the server stopped mid-pull.' WHERE status='running'"
        )
        self.db.commit()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS never alters an existing
    # table, so an older pulse.db gets them here instead of failing on its next insert.
    LATER_COLUMNS = {
        "items": {"score": "INTEGER NOT NULL DEFAULT 0", "also": "TEXT NOT NULL DEFAULT '[]'"},
    }

    def _add_missing_columns(self) -> None:
        for table, columns in self.LATER_COLUMNS.items():
            have = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------ pull lifecycle

    def start_pull(self, engine: str, window_start: datetime, started_at: datetime) -> int:
        cur = self.db.execute(
            "INSERT INTO pulls (started_at, engine, window_start) VALUES (?, ?, ?)",
            (iso(started_at), engine, iso(window_start)),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_pull(self, pull_id: int, verdicts: list[Verdict], stats: dict, finished_at: datetime,
                    passed_over: list[Item] = ()) -> None:
        """Save every verdict and close the pull — one transaction, all or nothing."""
        rank = {"learn": 0, "know": 0, "skip": 0}
        rows = []
        for v in verdicts:
            it = v.item
            also = [{"source": a.source, "title": a.title, "url": a.url} for a in v.also]
            rows.append(
                (it.id, pull_id, rank[v.bucket], it.source, it.title, it.url, it.discussion_url,
                 iso(it.published), it.signal, it.signal_label, v.bucket, json.dumps(v.hits),
                 v.headline, v.reason, v.score, json.dumps(v.card) if v.card else None, int(v.demoted),
                 json.dumps(also))
            )
            rank[v.bucket] += 1
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO items (id, pull_id, rank, source, title, url, discussion_url, published_at,"
                " signal, signal_label, bucket, hits, headline, reason, score, card, demoted, also)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO passed_over (id, pull_id, source) VALUES (?, ?, ?)",
                [(it.id, pull_id, it.source) for it in passed_over],
            )
            self.db.execute(
                "UPDATE pulls SET status='done', finished_at=?, stats=? WHERE id=?",
                (iso(finished_at), json.dumps(stats), pull_id),
            )

    def fail_pull(self, pull_id: int, error: str, stats: dict, finished_at: datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE pulls SET status='failed', error=?, finished_at=?, stats=? WHERE id=?",
                (error, iso(finished_at), json.dumps(stats), pull_id),
            )

    # ------------------------------------------------------------ queries

    def last_done(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM pulls WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()

    def seen(self, ids: list[str]) -> set[str]:
        """Which of these ids a finished pull has already shown you.

        A Learn item whose explanation failed (rate limit, timeout) doesn't count: the next
        pull picks it up again, and INSERT OR REPLACE moves it there with a real explanation.
        """
        found: set[str] = set()
        for start in range(0, len(ids), 500):  # SQLite caps the number of ? per query
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            found.update(r[0] for r in self.db.execute(
                f"SELECT id FROM items WHERE id IN ({marks}) AND (card IS NULL OR card NOT LIKE '{{\"error\"%')",
                chunk,
            ))
        return found

    def passed_over_by(self, ids: list[str]) -> dict[str, str]:
        """{id: source} for items a source's cap left out on an earlier pull."""
        out: dict[str, str] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            out.update(self.db.execute(f"SELECT id, source FROM passed_over WHERE id IN ({marks})", chunk).fetchall())
        return out

    def pulls(self, limit: int | None = None) -> list[dict]:
        """Every pull, newest first, each with its Learn headlines in reading order: the list of days."""
        rows = self.db.execute(
            """
            SELECT p.id, p.started_at, p.finished_at, p.engine, p.status, p.error,
                   COALESCE(SUM(i.bucket = 'learn'), 0) AS learn,
                   COALESCE(SUM(i.bucket = 'know'), 0)  AS know,
                   COALESCE(SUM(i.bucket = 'skip'), 0)  AS skip
            FROM pulls p LEFT JOIN items i ON i.pull_id = p.id
            GROUP BY p.id ORDER BY p.id DESC LIMIT ?
            """,
            (-1 if limit is None else limit,),  # SQLite: LIMIT -1 means no limit
        ).fetchall()
        headlines: dict[int, list[str]] = {}
        for r in self.db.execute("SELECT pull_id, card, headline, title FROM items WHERE bucket = 'learn' ORDER BY pull_id, rank"):
            card = json.loads(r["card"]) if r["card"] else {}
            headlines.setdefault(r["pull_id"], []).append(card.get("headline") or r["headline"] or r["title"])
        return [{**dict(r), "headlines": headlines.get(r["id"], [])} for r in rows]

    def digest(self, pull_id: int) -> dict | None:
        pull = self.db.execute("SELECT * FROM pulls WHERE id=?", (pull_id,)).fetchone()
        if pull is None:
            return None
        out = {"pull": {**dict(pull), "stats": json.loads(pull["stats"] or "{}")}, "learn": [], "know": [], "skip": []}
        for r in self.db.execute("SELECT * FROM items WHERE pull_id=? ORDER BY bucket, rank", (pull_id,)):
            row = dict(r)
            row["hits"] = json.loads(row["hits"])
            row["card"] = json.loads(row["card"]) if row["card"] else None
            row["demoted"] = bool(row["demoted"])
            row["also"] = json.loads(row["also"])
            out[row["bucket"]].append(row)
        return out
