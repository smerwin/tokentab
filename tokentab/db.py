"""SQLite history at ~/.tokentab/usage.db, so spend survives Claude Code pruning old
transcripts. Upserts keep the per-field max, matching the in-memory dedupe."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import STATE
from .loaders.claude_jsonl import TOKEN_FIELDS, Load, parse_ts

DEFAULT_DB = STATE / "usage.db"
COLS = ("msg_id", "request_id", "session_id", "ts", "model", "cwd", "git_branch", "sidechain", *TOKEN_FIELDS,
        "source", "user", "reported_usd", "repo_hint")
ADDED = {"source": "'transcript'", "user": "''", "reported_usd": "NULL", "repo_hint": "''"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events ({", ".join(c for c in COLS if c not in ADDED)});
CREATE UNIQUE INDEX IF NOT EXISTS events_key ON events (msg_id, request_id);
CREATE TABLE IF NOT EXISTS pr_links (session_id, repo, number, ts, UNIQUE (session_id, repo, number, ts));
CREATE TABLE IF NOT EXISTS bridge (bridge_id PRIMARY KEY, session_id);
"""


def sync(data: Load, path: Path = DEFAULT_DB, extra: list[dict] = ()) -> Load:
    """Write everything loaded (plus `extra` events, e.g. from OTel) into the db, then replace
    `data`'s contents with the full history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    have = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    for col, default in ADDED.items():
        if col not in have:
            con.execute(f"ALTER TABLE events ADD COLUMN {col} DEFAULT {default}")
    upd = ", ".join(f"{f} = max({f}, excluded.{f})" for f in TOKEN_FIELDS)
    con.executemany(
        f"INSERT INTO events VALUES ({', '.join('?' * len(COLS))}) "
        f"ON CONFLICT (msg_id, request_id) DO UPDATE SET {upd}, ts = min(ts, excluded.ts)",
        [tuple(ev["ts"].isoformat() if c == "ts" else ev[c] for c in COLS) for ev in [*data.events.values(), *extra]],
    )
    con.executemany("INSERT OR IGNORE INTO pr_links VALUES (?, ?, ?, ?)", data.pr_links)
    con.executemany("INSERT OR REPLACE INTO bridge VALUES (?, ?)", data.bridge.items())
    con.commit()
    data.events = {}
    quoted = ", ".join('"%s"' % c for c in COLS)
    for row in con.execute(f"SELECT {quoted} FROM events"):
        ev = dict(zip(COLS, row))
        ev["ts"], ev["sidechain"] = parse_ts(ev["ts"]), bool(ev["sidechain"])
        data.events[(ev["msg_id"], ev["request_id"])] = ev
    data.pr_links = set(con.execute("SELECT * FROM pr_links"))
    data.bridge = dict(con.execute("SELECT * FROM bridge"))
    con.close()
    return data
