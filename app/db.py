"""SQLite storage layer.

One short-lived connection per operation (WAL mode), so readers always see
the last committed version while a correction batch is being recomputed and
written. Correction batches are serialized per game with a process lock.
"""

import os
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,
    team_a_name     TEXT NOT NULL,
    team_b_name     TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS players (
    game_id    TEXT NOT NULL REFERENCES games(game_id),
    team       TEXT NOT NULL CHECK (team IN ('A', 'B')),
    player_id  TEXT NOT NULL,
    is_starter INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, team, player_id)
);
-- Every correction inserts a NEW row (revision+1); old revisions stay for audit.
CREATE TABLE IF NOT EXISTS events (
    game_id            TEXT NOT NULL REFERENCES games(game_id),
    seq                INTEGER NOT NULL,
    revision           INTEGER NOT NULL,
    version_introduced INTEGER NOT NULL,
    type               TEXT NOT NULL CHECK (type IN ('substitution', 'score')),
    payload            TEXT NOT NULL,  -- JSON
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (game_id, seq, revision)
);
CREATE TABLE IF NOT EXISTS versions (
    game_id    TEXT NOT NULL REFERENCES games(game_id),
    version    INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('import', 'correction')),
    detail     TEXT NOT NULL,  -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (game_id, version)
);
-- Stats materialized per version: a version's score/plus-minus never changes
-- once committed, so "previous version" queries are plain lookups.
CREATE TABLE IF NOT EXISTS stats (
    game_id TEXT NOT NULL REFERENCES games(game_id),
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,  -- JSON: {score, plus_minus, on_court}
    PRIMARY KEY (game_id, version)
);
"""

_locks_guard = threading.Lock()
_game_locks = {}


def db_path():
    return os.environ.get("BASKETBALL_DB", "basketball.db")


def game_lock(game_id):
    """Per-game mutex serializing correction batches (writers only)."""
    with _locks_guard:
        return _game_locks.setdefault(game_id, threading.Lock())


def connect():
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
