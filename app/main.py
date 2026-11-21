"""Basketball scorer's-table correction API.

Flow:
  1. POST /games                      import rosters, starters, ordered events  -> version 1
  2. GET  /games/{id}/events          recorder reads events + revision numbers
  3. POST /games/{id}/corrections     batch-correct events by expected revision -> version N+1
  4. GET  /games/{id}/stats           version, score, per-player plus-minus

Correction semantics:
  * every item in a batch must name the event's CURRENT revision, otherwise
    409 with the conflicting seqs; nothing changes;
  * the corrected record is validated against the LATEST effective record
    (earlier corrections to other events included); on failure 422 with the
    offending seqs; nothing changes;
  * stats for the new version are fully recomputed BEFORE the write
    transaction, so queries keep seeing the previous version during
    recalculation; events + stats become effective together at commit.
"""

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db, engine
from .schemas import CorrectionRequest, ImportRequest


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(
    title="Basketball Plus-Minus Recalculation API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers

def _event_parts(event):
    """Pydantic event -> (type, payload dict with 'in' alias applied)."""
    d = event.model_dump(by_alias=True)
    etype = d.pop("type")
    return etype, d


def _load_game_or_404(conn, game_id):
    row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"game {game_id!r} not found")
    return row


def _load_rosters(conn, game_id):
    rows = conn.execute(
        "SELECT team, player_id, is_starter FROM players WHERE game_id = ? ORDER BY team, player_id",
        (game_id,),
    ).fetchall()
    rosters, starters = {"A": [], "B": []}, {"A": [], "B": []}
    for r in rows:
        rosters[r["team"]].append(r["player_id"])
        if r["is_starter"]:
            starters[r["team"]].append(r["player_id"])
    return rosters, starters


def _load_current_events(conn, game_id):
    """Latest revision of every seq = the effective full-game record."""
    rows = conn.execute(
        """
        SELECT seq, revision, type, payload FROM events e
        WHERE game_id = ?
          AND revision = (SELECT MAX(revision) FROM events
                          WHERE game_id = e.game_id AND seq = e.seq)
        ORDER BY seq
        """,
        (game_id,),
    ).fetchall()
    return [
        {"seq": r["seq"], "revision": r["revision"], "type": r["type"], "payload": json.loads(r["payload"])}
        for r in rows
    ]


def _stats_response(game_id, version, stats):
    return {"game_id": game_id, "version": version, **stats}


# ---------------------------------------------------------------- endpoints

@app.get("/")
def root():
    return {"service": "basketball plus-minus recalculation", "docs": "/docs"}


@app.post("/games", status_code=201)
def import_game(req: ImportRequest):
    """Import rosters, starters and the ordered substitution/score records.

    The whole record is validated (5 per team on court, bench-only
    substitutions, scorer on court); an invalid record is rejected with the
    list of offending events and nothing is stored.
    """
    game_id = req.game_id or f"g-{uuid.uuid4().hex[:8]}"
    rosters = {"A": req.team_a.players, "B": req.team_b.players}
    starters = {"A": req.team_a.starters, "B": req.team_b.starters}
    events = []
    for i, ev in enumerate(req.events, start=1):
        etype, payload = _event_parts(ev)
        events.append({"seq": i, "type": etype, "payload": payload})

    stats, errors = engine.simulate(rosters, starters, events)
    if errors:
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "validation_failed",
                                "message": "imported record is invalid; nothing was stored",
                                "errors": errors}},
        )

    lock = db.game_lock(game_id)
    with lock:
        conn = db.connect()
        try:
            exists = conn.execute("SELECT 1 FROM games WHERE game_id = ?", (game_id,)).fetchone()
            if exists:
                return JSONResponse(
                    status_code=409,
                    content={"detail": {"error": "game_exists", "game_id": game_id}},
                )
            with conn:  # single transaction: game + roster + events + version 1 stats
                conn.execute(
                    "INSERT INTO games (game_id, team_a_name, team_b_name, current_version) VALUES (?, ?, ?, 1)",
                    (game_id, req.team_a.name, req.team_b.name),
                )
                for team, setup in (("A", req.team_a), ("B", req.team_b)):
                    starter_set = set(setup.starters)
                    for pid in setup.players:
                        conn.execute(
                            "INSERT INTO players (game_id, team, player_id, is_starter) VALUES (?, ?, ?, ?)",
                            (game_id, team, pid, 1 if pid in starter_set else 0),
                        )
                for ev in events:
                    conn.execute(
                        "INSERT INTO events (game_id, seq, revision, version_introduced, type, payload)"
                        " VALUES (?, ?, 0, 1, ?, ?)",
                        (game_id, ev["seq"], ev["type"], json.dumps(ev["payload"])),
                    )
                conn.execute(
                    "INSERT INTO versions (game_id, version, kind, detail) VALUES (?, 1, 'import', ?)",
                    (game_id, json.dumps({"events": len(events)})),
                )
                conn.execute(
                    "INSERT INTO stats (game_id, version, payload) VALUES (?, 1, ?)",
                    (game_id, json.dumps(stats)),
                )
        finally:
            conn.close()
    return {"game_id": game_id, "version": 1, "stats": stats}


@app.get("/games")
def list_games():
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT game_id, team_a_name, team_b_name, current_version, created_at FROM games ORDER BY created_at"
        ).fetchall()
        return {"games": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/games/{game_id}")
def get_game(game_id: str):
    conn = db.connect()
    try:
        game = _load_game_or_404(conn, game_id)
        rosters, starters = _load_rosters(conn, game_id)
        return {
            "game_id": game_id,
            "current_version": game["current_version"],
            "team_a": {"name": game["team_a_name"], "players": rosters["A"], "starters": starters["A"]},
            "team_b": {"name": game["team_b_name"], "players": rosters["B"], "starters": starters["B"]},
        }
    finally:
        conn.close()


@app.get("/games/{game_id}/events")
def get_events(game_id: str):
    """The effective event record with each event's current revision number —
    exactly what the recorder needs before submitting a correction batch."""
    conn = db.connect()
    try:
        game = _load_game_or_404(conn, game_id)
        events = _load_current_events(conn, game_id)
        return {
            "game_id": game_id,
            "version": game["current_version"],
            "events": [
                {"seq": e["seq"], "revision": e["revision"], "type": e["type"], **e["payload"]}
                for e in events
            ],
        }
    finally:
        conn.close()


@app.post("/games/{game_id}/corrections")
def apply_corrections(game_id: str, req: CorrectionRequest):
    """Apply a batch of corrections against the revisions the recorder saw.

    All-or-nothing: revision conflict -> 409 naming the seqs; failed
    re-validation against the latest effective record -> 422 naming the
    events; in both cases the stored events and stats stay as they were.
    """
    seqs = [c.seq for c in req.corrections]
    if len(set(seqs)) != len(seqs):
        dup = sorted({s for s in seqs if seqs.count(s) > 1})
        return JSONResponse(
            status_code=400,
            content={"detail": {"error": "duplicate_seq",
                                "message": "one event can only be corrected once per batch",
                                "seqs": dup}},
        )

    lock = db.game_lock(game_id)
    with lock:
        conn = db.connect()
        try:
            game = _load_game_or_404(conn, game_id)
            current = _load_current_events(conn, game_id)
            by_seq = {e["seq"]: e for e in current}

            # 1) revision check for every item in the batch
            conflicts = []
            for c in req.corrections:
                cur = by_seq.get(c.seq)
                if cur is None:
                    conflicts.append({"seq": c.seq, "expected_revision": c.expected_revision,
                                      "current_revision": None, "message": "event not found"})
                elif cur["revision"] != c.expected_revision:
                    conflicts.append({"seq": c.seq, "expected_revision": c.expected_revision,
                                      "current_revision": cur["revision"]})
            if conflicts:
                return JSONResponse(
                    status_code=409,
                    content={"detail": {"error": "revision_conflict",
                                        "message": "events were already corrected; re-read them and resubmit",
                                        "conflicts": conflicts}},
                )

            # 2) build the candidate record on top of the LATEST effective record
            candidate = [{"seq": e["seq"], "type": e["type"], "payload": e["payload"]} for e in current]
            index = {e["seq"]: i for i, e in enumerate(candidate)}
            for c in req.corrections:
                etype, payload = _event_parts(c.event)
                candidate[index[c.seq]] = {"seq": c.seq, "type": etype, "payload": payload}

            # 3) re-validate and recompute the whole game in memory
            rosters, starters = _load_rosters(conn, game_id)
            stats, errors = engine.simulate(rosters, starters, candidate)
            if errors:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"error": "validation_failed",
                                        "message": "corrected record fails validation; original events and stats unchanged",
                                        "errors": errors}},
                )

            new_version = game["current_version"] + 1

            # Recalculation finished, not yet committed: readers still see the
            # previous version. (Test hook; None in production.)
            hook = getattr(app.state, "before_commit_hook", None)
            if hook is not None:
                hook(game_id, new_version)

            # 4) one transaction: corrected events + version + stats go live together
            applied = []
            with conn:
                for c in req.corrections:
                    etype, payload = _event_parts(c.event)
                    new_revision = by_seq[c.seq]["revision"] + 1
                    conn.execute(
                        "INSERT INTO events (game_id, seq, revision, version_introduced, type, payload)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (game_id, c.seq, new_revision, new_version, etype, json.dumps(payload)),
                    )
                    applied.append({"seq": c.seq, "from_revision": new_revision - 1, "to_revision": new_revision})
                conn.execute(
                    "INSERT INTO versions (game_id, version, kind, detail) VALUES (?, ?, 'correction', ?)",
                    (game_id, new_version, json.dumps({"corrections": applied})),
                )
                conn.execute(
                    "INSERT INTO stats (game_id, version, payload) VALUES (?, ?, ?)",
                    (game_id, new_version, json.dumps(stats)),
                )
                conn.execute(
                    "UPDATE games SET current_version = ? WHERE game_id = ?",
                    (new_version, game_id),
                )
        finally:
            conn.close()

    return {"game_id": game_id, "version": new_version, "applied": applied, "stats": stats}


@app.get("/games/{game_id}/stats")
def get_stats(game_id: str, version: int | None = None):
    """Version number, score and per-player plus-minus. Defaults to the
    current version; pass ?version=N for a previous one."""
    conn = db.connect()
    try:
        game = _load_game_or_404(conn, game_id)
        v = version if version is not None else game["current_version"]
        row = conn.execute(
            "SELECT payload FROM stats WHERE game_id = ? AND version = ?", (game_id, v)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no stats for version {v}")
        return _stats_response(game_id, v, json.loads(row["payload"]))
    finally:
        conn.close()


@app.get("/games/{game_id}/versions")
def get_versions(game_id: str):
    conn = db.connect()
    try:
        _load_game_or_404(conn, game_id)
        rows = conn.execute(
            "SELECT version, kind, detail, created_at FROM versions WHERE game_id = ? ORDER BY version",
            (game_id,),
        ).fetchall()
        return {
            "game_id": game_id,
            "versions": [
                {"version": r["version"], "kind": r["kind"],
                 "detail": json.loads(r["detail"]), "created_at": r["created_at"]}
                for r in rows
            ],
        }
    finally:
        conn.close()
